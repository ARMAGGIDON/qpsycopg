"""Internal protocol shared by the Qt process pq backend and its worker."""

from __future__ import annotations

import pickle
import socket
import struct
from array import array
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

_HEADER = struct.Struct("!QI")
_BUFFER_HEADER = struct.Struct("!Q")
_TOKEN_SIZE = 32
_MAX_FRAME = 1 << 34  # 16 GiB: reject obviously corrupt aggregate frame sizes.
_MAX_BUFFERS = 4096


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


@dataclass(slots=True, frozen=True)
class Request:
    requestId: int
    operation: Operation
    objectId: int = 0
    member: str = ""
    args: tuple[Any, ...] = ()
    kwargs: tuple[tuple[str, Any], ...] = ()


@dataclass(slots=True, frozen=True)
class RemoteRef:
    kind: RemoteKind
    objectId: int


@dataclass(slots=True, frozen=True)
class RemoteError:
    module: str
    name: str
    message: str


@dataclass(slots=True, frozen=True)
class CellTable:
    values: array[int]

    def __getitem__(self, index: int) -> tuple[int, int]:
        offset = index * 2
        return self.values[offset], self.values[offset + 1]


@dataclass(slots=True, frozen=True)
class ResultSnapshot:
    status: int
    errorMessage: bytes
    errorFields: tuple[tuple[int, bytes], ...]
    columns: tuple[tuple[bytes | None, int, int, int, int, int, int], ...]
    rowCount: int
    columnCount: int
    binaryTuples: int
    data: bytearray | memoryview | pickle.PickleBuffer
    cells: CellTable
    paramTypes: tuple[int, ...]
    commandStatus: bytes | None
    commandTuples: int | None
    oidValue: int


@dataclass(slots=True, frozen=True)
class Response:
    requestId: int
    value: Any = None
    error: RemoteError | None = None
    notices: tuple[ResultSnapshot, ...] = ()


def sendToken(sock: socket.socket, token: bytes) -> None:
    if len(token) != _TOKEN_SIZE:
        raise ValueError("invalid qpsycopg worker token length")
    sock.sendall(token)


def recvToken(sock: socket.socket) -> bytes:
    return bytes(_recvExact(sock, _TOKEN_SIZE))


def sendFrame(sock: socket.socket, value: Any) -> None:
    buffers: list[pickle.PickleBuffer] = []
    payload = pickle.dumps(
        value,
        protocol=pickle.HIGHEST_PROTOCOL,
        buffer_callback=buffers.append,
    )
    if len(buffers) > _MAX_BUFFERS:
        raise ValueError(f"too many qpsycopg frame buffers: {len(buffers)}")

    views = [buffer.raw() for buffer in buffers]
    totalSize = len(payload) + sum(view.nbytes for view in views)
    if totalSize > _MAX_FRAME:
        raise ValueError(f"qpsycopg frame is too large: {totalSize}")

    sock.sendall(_HEADER.pack(len(payload), len(views)))
    sock.sendall(payload)
    for view in views:
        sock.sendall(_BUFFER_HEADER.pack(view.nbytes))
        sock.sendall(view)


def recvFrame(sock: socket.socket) -> Any:
    payloadSize, bufferCount = _HEADER.unpack(_recvExact(sock, _HEADER.size))
    if payloadSize > _MAX_FRAME:
        raise ValueError(f"invalid qpsycopg frame size: {payloadSize}")
    if bufferCount > _MAX_BUFFERS:
        raise ValueError(f"invalid qpsycopg frame buffer count: {bufferCount}")

    payload = _recvExact(sock, payloadSize)
    buffers: list[memoryview] = []
    totalSize = payloadSize
    for _ in range(bufferCount):
        size = _BUFFER_HEADER.unpack(_recvExact(sock, _BUFFER_HEADER.size))[0]
        totalSize += size
        if totalSize > _MAX_FRAME:
            raise ValueError(f"invalid qpsycopg aggregate frame size: {totalSize}")
        buffers.append(memoryview(_recvExact(sock, size)))

    return pickle.loads(payload, buffers=buffers)


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
