"""Windows named-pipe stream adapter for the qt-process transport."""

from __future__ import annotations

import os
import secrets
from multiprocessing.connection import Client, Connection, Listener
from typing import Any


class PipeStream:
    def __init__(self, connection: Connection):
        self.connection = connection
        self.buffer = bytearray()

    def sendall(self, data: bytes | bytearray | memoryview) -> None:
        self.connection.send_bytes(data)

    def recv_into(self, target: Any) -> int:
        if not self.buffer:
            try:
                self.buffer.extend(self.connection.recv_bytes())
            except EOFError:
                return 0
        count = min(len(target), len(self.buffer))
        target[:count] = self.buffer[:count]
        del self.buffer[:count]
        return count

    def recv(self, size: int) -> bytes:
        data = bytearray(size)
        count = self.recv_into(data)
        return bytes(memoryview(data)[:count])

    def settimeout(self, timeout: float | None) -> None:
        pass

    def setsockopt(self, *args: Any) -> None:
        pass

    def close(self) -> None:
        self.connection.close()


class PipeListenerSocket:
    def __init__(self):
        self.address = rf"\\.\pipe\qpsycopg-{os.getpid()}-{secrets.token_hex(12)}"
        self.listener = Listener(self.address, family="AF_PIPE", backlog=1)

    def setsockopt(self, *args: Any) -> None:
        pass

    def bind(self, address: Any) -> None:
        pass

    def listen(self, backlog: int) -> None:
        pass

    def settimeout(self, timeout: float | None) -> None:
        pass

    def getsockname(self) -> tuple[str, int]:
        return f"pipe:{self.address}", 0

    def accept(self) -> tuple[PipeStream, None]:
        return PipeStream(self.listener.accept()), None

    def close(self) -> None:
        self.listener.close()


def connectPipe(address: str) -> PipeStream:
    return PipeStream(Client(address, family="AF_PIPE"))
