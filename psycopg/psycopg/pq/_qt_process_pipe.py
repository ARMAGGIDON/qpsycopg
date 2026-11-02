"""Windows named-pipe byte-stream adapter for the qt-process transport."""

from __future__ import annotations

import io
import os
import secrets
import time
from typing import Any

try:
    from psycopg_c._psycopg import qpsycopg_pipe_frame_roundtrip as _nativeFrameRoundtrip
    from psycopg_c._psycopg import qpsycopg_pipe_recv_frame as _nativeRecvFrame
except (ImportError, AttributeError):
    _nativeFrameRoundtrip = _nativeRecvFrame = None
if os.environ.get("QPSYCOPG_NATIVE_IPC", "1") == "0":
    _nativeFrameRoundtrip = _nativeRecvFrame = None
elif os.environ.get("QPSYCOPG_NATIVE_RECV", "1") == "0":
    _nativeRecvFrame = None

if os.name == "nt":
    import ctypes
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _PIPE_ACCESS_DUPLEX = 0x00000003
    _PIPE_TYPE_BYTE = 0x00000000
    _PIPE_READMODE_BYTE = 0x00000000
    _PIPE_WAIT = 0x00000000
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PIPE_BUSY = 231
    _ERROR_PIPE_CONNECTED = 535
    _BUFFER_SIZE = 1 << 20
    _READ_AHEAD = 64 * 1024

    _kernel32.CreateNamedPipeW.argtypes = (
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    )
    _kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
    _kernel32.ConnectNamedPipe.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    _kernel32.ConnectNamedPipe.restype = ctypes.c_int
    _kernel32.CreateFileW.argtypes = (
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    )
    _kernel32.CreateFileW.restype = ctypes.c_void_p
    _kernel32.WaitNamedPipeW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
    _kernel32.WaitNamedPipeW.restype = ctypes.c_int
    _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    _kernel32.CloseHandle.restype = ctypes.c_int


def _winError() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _fileFromHandle(handle: int) -> io.FileIO:
    fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR)
    return io.FileIO(fd, "r+b", closefd=True)


class PipeStream:
    def __init__(self, file: io.FileIO):
        self.file = file
        self._readBuffer = bytearray(_READ_AHEAD)
        self._readStart = 0
        self._readEnd = 0

    def sendall(self, data: bytes | bytearray | memoryview) -> None:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            written = self.file.write(view[offset:])
            if not written:
                raise BrokenPipeError("qpsycopg named pipe closed")
            offset += written

    def recv_into(self, target: Any) -> int:
        view = memoryview(target)
        if not view:
            return 0

        available = self._readEnd - self._readStart
        if available:
            count = min(len(view), available)
            view[:count] = memoryview(self._readBuffer)[self._readStart:self._readStart + count]
            self._readStart += count
            if self._readStart == self._readEnd:
                self._readStart = self._readEnd = 0
            return count

        if len(view) >= _READ_AHEAD:
            received = self.file.readinto(view)
            return 0 if received is None else int(received)

        received = self.file.readinto(self._readBuffer)
        if not received:
            return 0
        count = min(len(view), received)
        view[:count] = memoryview(self._readBuffer)[:count]
        if count < received:
            self._readStart = count
            self._readEnd = int(received)
        return count

    def recv(self, size: int) -> bytes:
        data = bytearray(size)
        count = self.recv_into(data)
        return bytes(memoryview(data)[:count])

    def recv_frame(self, max_frame: int) -> tuple[int, bytearray] | None:
        if _nativeRecvFrame is None or self._readStart != self._readEnd:
            return None
        frame, payload, readStart, readEnd = _nativeRecvFrame(self.file.fileno(), self._readBuffer, max_frame)
        self._readStart = readStart
        self._readEnd = readEnd
        return frame, payload

    def roundtrip_frame(self, request: bytes, max_frame: int) -> tuple[int, bytearray] | None:
        if _nativeFrameRoundtrip is None or self._readStart != self._readEnd:
            return None
        return _nativeFrameRoundtrip(self.file.fileno(), request, self._readBuffer, max_frame)

    def settimeout(self, timeout: float | None) -> None:
        pass

    def setsockopt(self, *args: Any) -> None:
        pass

    def close(self) -> None:
        self.file.close()


class PipeListenerSocket:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows named pipes are unavailable")
        self.address = rf"\\.\pipe\qpsycopg-{os.getpid()}-{secrets.token_hex(12)}"
        handle = _kernel32.CreateNamedPipeW(
            self.address,
            _PIPE_ACCESS_DUPLEX,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            1,
            _BUFFER_SIZE,
            _BUFFER_SIZE,
            0,
            None,
        )
        if handle == _INVALID_HANDLE:
            raise _winError()
        self.handle: int | None = int(handle)

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
        handle = self.handle
        if handle is None:
            raise OSError("qpsycopg named-pipe listener is closed")
        if not _kernel32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
            raise _winError()
        self.handle = None
        return PipeStream(_fileFromHandle(handle)), None

    def close(self) -> None:
        if self.handle is not None:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def connectPipe(address: str) -> PipeStream:
    if os.name != "nt":
        raise OSError("Windows named pipes are unavailable")
    deadline = time.monotonic() + 10.0
    while True:
        handle = _kernel32.CreateFileW(
            address,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle != _INVALID_HANDLE:
            return PipeStream(_fileFromHandle(int(handle)))
        error = ctypes.get_last_error()
        if error not in (_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY) or time.monotonic() >= deadline:
            raise ctypes.WinError(error)
        _kernel32.WaitNamedPipeW(address, 100)
        time.sleep(0.001)
