"""Internal protocol shared by the Qt process pq backend and its worker."""

from __future__ import annotations

import pickle
import socket
import struct
from enum import IntEnum
from time import perf_counter
from typing import Any, NamedTuple

_HEADER = struct.Struct("!Q")
_VALUE_OFFSET = struct.Struct("=Q")
_TOKEN_SIZE = 32
_MAX_FRAME = 1 << 34  # 16 GiB: reject obviously corrupt frame lengths.
_COALESCE_FRAME = 64 * 1024
_FAST_REQUEST_FLAG = 1 << 63
_FAST_RESPONSE_FLAG = 1 << 62
_FRAME_FLAGS = _FAST_REQUEST_FLAG | _FAST_RESPONSE_FLAG
_FRAME_SIZE_MASK = _FAST_RESPONSE_FLAG - 1
_Profile = dict[str, float | int]
_FAST_EXECUTE = "qpsycopg_execute_pending"


class Operation(IntEnum):
    CONNECT_START = 1
    CONNECT = 2
    PING = 3
    CALL = 4
    GET = 5
    SET = 6
    WAIT = 7
    ESCAPE = 8
    CLOSE = 9


class RemoteKind(IntEnum):
    PGCONN = 1
    PGCANCEL_CONN = 2
    PGCANCEL = 3


class Request(NamedTuple):
    requestId: int
    operation: Operation
    objectId: int = 0
    member: str = ""
    args: tuple[Any, ...] = ()
    kwargs: tuple[tuple[str, Any], ...] = ()


class RemoteRef(NamedTuple):
    kind: RemoteKind
    objectId: int


class RemoteError(NamedTuple):
    module: str
    name: str
    message: str


class PackedValues(NamedTuple):
    """Contiguous result cells with offsets and a NULL bitmap."""

    data: bytes
    offsets: bytes
    nulls: bytes
    count: int

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int | slice) -> bytes | None | tuple[bytes | None, ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self.count)
            return tuple(self[i] for i in range(start, stop, step))

        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        if self.nulls[index >> 3] & (1 << (index & 7)):
            return None

        start = _VALUE_OFFSET.unpack_from(self.offsets, index * _VALUE_OFFSET.size)[0]
        end = _VALUE_OFFSET.unpack_from(self.offsets, (index + 1) * _VALUE_OFFSET.size)[0]
        return self.data[start:end]


class ResultSnapshot(NamedTuple):
    status: int
    errorMessage: bytes
    errorFields: tuple[tuple[int, bytes], ...]
    columns: tuple[tuple[bytes | None, int, int, int, int, int, int], ...]
    rowCount: int
    columnCount: int
    binaryTuples: int
    values: PackedValues | tuple[bytes | None, ...]
    paramTypes: tuple[int, ...]
    commandStatus: bytes | None
    commandTuples: int | None
    oidValue: int


class SendSnapshot(NamedTuple):
    value: Any
    flush: int
    busy: int
    status: int | None = None
    transactionStatus: int | None = None
    clientEncoding: bytes | None = None


class WaitSnapshot(NamedTuple):
    ready: int
    consumed: bool = False
    busy: int | None = None
    notifies: tuple[Any, ...] = ()
    results: tuple[ResultSnapshot | None, ...] = ()


class ResultReadSnapshot(NamedTuple):
    value: ResultSnapshot
    following: tuple[ResultSnapshot | None, ...] = ()


class Response(NamedTuple):
    requestId: int
    value: Any = None
    error: RemoteError | None = None
    notices: tuple[ResultSnapshot, ...] = ()


class FastExecuteFrame(NamedTuple):
    """Parent-only marker: sendFrame serializes only these four payload fields."""

    timeout: float
    sendMember: str
    sendArgs: tuple[Any, ...]
    sendKwargs: tuple[tuple[str, Any], ...]


def _lowLatency(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


def _addProfile(profile: _Profile | None, key: str, value: float | int) -> None:
    if profile is not None:
        profile[key] = profile.get(key, 0) + value


def sendToken(sock: socket.socket, token: bytes) -> None:
    if len(token) != _TOKEN_SIZE:
        raise ValueError("invalid qpsycopg worker token length")
    _lowLatency(sock)
    sock.sendall(token)


def recvToken(sock: socket.socket) -> bytes:
    _lowLatency(sock)
    return bytes(_recvExact(sock, _TOKEN_SIZE))


def _wireValue(value: Any) -> tuple[Any, int]:
    if isinstance(value, FastExecuteFrame):
        return tuple(value), _FAST_REQUEST_FLAG
    if (
        isinstance(value, Response)
        and value.requestId == 0
        and value.error is None
        and not value.notices
        and isinstance(value.value, SendSnapshot)
    ):
        return tuple(value.value), _FAST_RESPONSE_FLAG
    return value, 0


def _sendPayload(sock: socket.socket, payload: bytes, flags: int) -> None:
    header = _HEADER.pack(flags | len(payload))
    if len(payload) <= _COALESCE_FRAME:
        sock.sendall(header + payload)
    else:
        sock.sendall(header)
        sock.sendall(payload)


def sendFrame(sock: socket.socket, value: Any, profile: _Profile | None = None, prefix: str = "frame") -> None:
    wireValue, flags = _wireValue(value)
    if profile is None:
        payload = pickle.dumps(wireValue, protocol=pickle.HIGHEST_PROTOCOL)
        _sendPayload(sock, payload, flags)
        return

    start = perf_counter()
    payload = pickle.dumps(wireValue, protocol=pickle.HIGHEST_PROTOCOL)
    _addProfile(profile, f"{prefix}_pickle_seconds", perf_counter() - start)

    start = perf_counter()
    _sendPayload(sock, payload, flags)
    _addProfile(profile, f"{prefix}_socket_send_seconds", perf_counter() - start)
    _addProfile(profile, f"{prefix}_payload_bytes", len(payload))
    _addProfile(profile, f"{prefix}_frames", 1)


def _readHeader(sock: socket.socket) -> tuple[int, int]:
    frame = _HEADER.unpack(_recvExact(sock, _HEADER.size))[0]
    flags = frame & _FRAME_FLAGS
    if flags == _FRAME_FLAGS:
        raise ValueError("invalid qpsycopg frame flags")
    size = frame & _FRAME_SIZE_MASK
    if size > _MAX_FRAME:
        raise ValueError(f"invalid qpsycopg frame size: {size}")
    return size, flags


def _restoreWireValue(value: Any, flags: int) -> Any:
    if flags == _FAST_REQUEST_FLAG:
        timeout, sendMember, sendArgs, sendKwargs = value
        return Request(0, Operation.CALL, 1, _FAST_EXECUTE, (timeout, sendMember, sendArgs, sendKwargs))
    if flags == _FAST_RESPONSE_FLAG:
        return SendSnapshot(*value)
    return value


def recvFrame(sock: socket.socket, profile: _Profile | None = None, prefix: str = "frame") -> Any:
    if profile is None:
        size, flags = _readHeader(sock)
        return _restoreWireValue(pickle.loads(_recvExact(sock, size)), flags)

    start = perf_counter()
    size, flags = _readHeader(sock)
    _addProfile(profile, f"{prefix}_header_wait_seconds", perf_counter() - start)

    start = perf_counter()
    payload = _recvExact(sock, size)
    _addProfile(profile, f"{prefix}_payload_recv_seconds", perf_counter() - start)

    start = perf_counter()
    value = _restoreWireValue(pickle.loads(payload), flags)
    _addProfile(profile, f"{prefix}_unpickle_seconds", perf_counter() - start)
    _addProfile(profile, f"{prefix}_payload_bytes", size)
    _addProfile(profile, f"{prefix}_frames", 1)
    return value


def _recvExact(sock: socket.socket, size: int) -> bytearray:
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    while offset < size:
        received = sock.recv_into(view[offset:])
        if not received:
            raise EOFError("qpsycopg worker connection closed")
        offset += received
    return data
