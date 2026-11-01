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
_Profile = dict[str, float | int]


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


class FastExecuteRequest(NamedTuple):
    timeout: float
    sendMember: str
    sendArgs: tuple[Any, ...]
    sendKwargs: tuple[tuple[str, Any], ...]


class FastExecuteResponse(NamedTuple):
    value: SendSnapshot | None = None
    error: RemoteError | None = None
    notices: tuple[ResultSnapshot, ...] = ()


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


def sendFrame(sock: socket.socket, value: Any, profile: _Profile | None = None, prefix: str = "frame") -> None:
    if profile is None:
        payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        header = _HEADER.pack(len(payload))
        if len(payload) <= _COALESCE_FRAME:
            sock.sendall(header + payload)
        else:
            sock.sendall(header)
            sock.sendall(payload)
        return

    start = perf_counter()
    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    _addProfile(profile, f"{prefix}_pickle_seconds", perf_counter() - start)

    header = _HEADER.pack(len(payload))
    start = perf_counter()
    if len(payload) <= _COALESCE_FRAME:
        sock.sendall(header + payload)
    else:
        sock.sendall(header)
        sock.sendall(payload)
    _addProfile(profile, f"{prefix}_socket_send_seconds", perf_counter() - start)
    _addProfile(profile, f"{prefix}_payload_bytes", len(payload))
    _addProfile(profile, f"{prefix}_frames", 1)


def recvFrame(sock: socket.socket, profile: _Profile | None = None, prefix: str = "frame") -> Any:
    if profile is None:
        size = _HEADER.unpack(_recvExact(sock, _HEADER.size))[0]
        if size > _MAX_FRAME:
            raise ValueError(f"invalid qpsycopg frame size: {size}")
        return pickle.loads(_recvExact(sock, size))

    start = perf_counter()
    size = _HEADER.unpack(_recvExact(sock, _HEADER.size))[0]
    _addProfile(profile, f"{prefix}_header_wait_seconds", perf_counter() - start)
    if size > _MAX_FRAME:
        raise ValueError(f"invalid qpsycopg frame size: {size}")

    start = perf_counter()
    payload = _recvExact(sock, size)
    _addProfile(profile, f"{prefix}_payload_recv_seconds", perf_counter() - start)

    start = perf_counter()
    value = pickle.loads(payload)
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
