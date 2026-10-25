"""Internal protocol shared by the Qt process pq backend and its worker."""

from __future__ import annotations

import pickle
import socket
import struct
from enum import IntEnum
from array import array
from typing import Any
from dataclasses import dataclass

_HEADER = struct.Struct("!Q")
_MAX_FRAME = 1 << 34  # 16 GiB: reject obviously corrupt frame lengths.


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
class ResultSnapshot:
    status: int
    errorMessage: bytes
    errorFields: tuple[tuple[int, bytes], ...]
    columns: tuple[tuple[bytes | None, int, int, int, int, int, int], ...]
    rowCount: int
    columnCount: int
    binaryTuples: int
    data: bytearray
    cells: array
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


def sendFrame(sock: socket.socket, value: Any) -> None:
    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    sock.sendall(_HEADER.pack(len(payload)))
    sock.sendall(payload)


def recvFrame(sock: socket.socket) -> Any:
    size = _HEADER.unpack(_recvExact(sock, _HEADER.size))[0]
    if size > _MAX_FRAME:
        raise ValueError(f"invalid qpsycopg frame size: {size}")
    return pickle.loads(_recvExact(sock, size))


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
