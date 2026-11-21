# mypy: disable-error-code="attr-defined, no-any-return"
"""Cancelable Windows named-pipe byte stream for the qt-process backend.

All overlapped storage, events, and data buffers remain owned until the kernel
reports completion. Native CRT read/write fast paths are intentionally disabled:
an overlapped HANDLE must never be passed to synchronous file-descriptor I/O.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import socket
import threading
import time
from typing import Any

_READ_AHEAD = 64 * 1024
_IO_CHUNK = 256 * 1024
_POLL_MS = 25
_CANCEL_DRAIN_SECONDS = 1.0

if os.name == "nt":
    from ctypes import wintypes as wt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _PIPE_ACCESS_DUPLEX = 3
    _PIPE_TYPE_BYTE = 0
    _PIPE_READMODE_BYTE = 0
    _PIPE_WAIT = 0
    _PIPE_REJECT_REMOTE_CLIENTS = 8
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _ERROR_IO_PENDING = 997
    _ERROR_IO_INCOMPLETE = 996
    _ERROR_OPERATION_ABORTED = 995
    _ERROR_NOT_FOUND = 1168
    _ERROR_PIPE_CONNECTED = 535
    _ERROR_BROKEN_PIPE = 109
    _ERROR_PIPE_NOT_CONNECTED = 233
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PIPE_BUSY = 231
    _ERROR_INSUFFICIENT_BUFFER = 122
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258
    _BUFFER_SIZE = 1 << 20

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE),
        ]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wt.BOOL),
        ]

    def _api(dll: Any, name: str, args: tuple[Any, ...], result: Any) -> Any:
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
        return fn

    _createPipe = _api(
        _kernel, "CreateNamedPipeW",
        (wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
         wt.DWORD, ctypes.POINTER(_SECURITY_ATTRIBUTES)), wt.HANDLE)
    _connect = _api(
        _kernel, "ConnectNamedPipe",
        (wt.HANDLE, ctypes.POINTER(_OVERLAPPED)), wt.BOOL)
    _createFile = _api(
        _kernel, "CreateFileW",
        (wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
         wt.DWORD, wt.HANDLE), wt.HANDLE)
    _waitPipe = _api(_kernel, "WaitNamedPipeW", (wt.LPCWSTR, wt.DWORD), wt.BOOL)
    _read = _api(
        _kernel, "ReadFile",
        (wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
         ctypes.POINTER(_OVERLAPPED)), wt.BOOL)
    _write = _api(
        _kernel, "WriteFile",
        (wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
         ctypes.POINTER(_OVERLAPPED)), wt.BOOL)
    _newEvent = _api(
        _kernel, "CreateEventW",
        (ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR), wt.HANDLE)
    _waitEvent = _api(_kernel, "WaitForSingleObject", (wt.HANDLE, wt.DWORD), wt.DWORD)
    _overlappedResult = _api(
        _kernel, "GetOverlappedResult",
        (wt.HANDLE, ctypes.POINTER(_OVERLAPPED),
         ctypes.POINTER(wt.DWORD), wt.BOOL), wt.BOOL)
    _cancelIo = _api(
        _kernel, "CancelIoEx",
        (wt.HANDLE, ctypes.POINTER(_OVERLAPPED)), wt.BOOL)
    _closeHandle = _api(_kernel, "CloseHandle", (wt.HANDLE,), wt.BOOL)
    _clientPid = _api(
        _kernel, "GetNamedPipeClientProcessId",
        (wt.HANDLE, ctypes.POINTER(wt.ULONG)), wt.BOOL)
    _getProcess = _api(_kernel, "GetCurrentProcess", (), wt.HANDLE)
    _openToken = _api(
        _advapi, "OpenProcessToken",
        (wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)), wt.BOOL)
    _tokenInfo = _api(
        _advapi, "GetTokenInformation",
        (wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
         ctypes.POINTER(wt.DWORD)), wt.BOOL)
    _sidString = _api(
        _advapi, "ConvertSidToStringSidW",
        (ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)), wt.BOOL)
    _parseSddl = _api(
        _advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        (wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
         ctypes.POINTER(wt.DWORD)), wt.BOOL)
    _localFree = _api(_kernel, "LocalFree", (ctypes.c_void_p,), ctypes.c_void_p)


def _error(code: int) -> OSError:
    if code in (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED):
        return BrokenPipeError(code, "qpsycopg named-pipe peer disconnected")
    if code == _ERROR_OPERATION_ABORTED:
        return OSError(code, "qpsycopg named-pipe I/O cancelled")
    return ctypes.WinError(code)


def _current_user_security() -> tuple[Any, Any]:
    """DACL grants full pipe access only to the launching user and SYSTEM."""
    token = wt.HANDLE()
    if not _openToken(_getProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wt.DWORD()
        _tokenInfo(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(ctypes.get_last_error())
        storage = ctypes.create_string_buffer(size.value)
        if not _tokenInfo(token, 1, storage, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(storage, ctypes.POINTER(ctypes.c_void_p))[0]
        sidText = wt.LPWSTR()
        if not _sidString(sid, ctypes.byref(sidText)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{sidText.value})"
        finally:
            _localFree(ctypes.cast(sidText, ctypes.c_void_p))
    finally:
        _closeHandle(token)
    descriptor = ctypes.c_void_p()
    if not _parseSddl(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    attrs = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False)
    return attrs, descriptor


class _Operation:
    def __init__(self, buffer: Any = None) -> None:
        self.buffer = buffer  # Pin storage until actual I/O completion.
        self.event = _newEvent(None, True, False, None)
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())
        self.overlapped = _OVERLAPPED()
        self.overlapped.hEvent = self.event
        self.deferred = False


class _PipeHandle:
    """Exactly-once HANDLE closure coordinated with all outstanding I/O."""

    def __init__(self, handle: int) -> None:
        self.handle: int | None = handle
        self._lock = threading.Lock()
        self._pending: set[_Operation] = set()
        self._closed = False

    def _cancel(self, operation: _Operation) -> None:
        handle = self.handle
        if handle is not None and not _cancelIo(
            handle, ctypes.byref(operation.overlapped)
        ):
            error = ctypes.get_last_error()
            if error != _ERROR_NOT_FOUND:
                # Completion still has to be observed; never free on this error.
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for operation in tuple(self._pending):
                self._cancel(operation)
            self._close_if_idle()

    def _close_if_idle(self) -> None:
        # Called under _lock, never while a pending OVERLAPPED exists.
        if self._closed and not self._pending and self.handle is not None:
            handle, self.handle = self.handle, None
            _closeHandle(handle)

    def _finish(self, operation: _Operation) -> None:
        with self._lock:
            self._pending.remove(operation)
            _closeHandle(operation.event)
            self._close_if_idle()

    def _reap(self, operation: _Operation) -> None:
        # GetOverlappedResult(..., TRUE) is the completion barrier, including
        # cancellation. Event wait alone is not sufficient if the wait failed.
        count = wt.DWORD()
        handle = self.handle
        if handle is None:
            raise RuntimeError("pending I/O lost its HANDLE")
        _overlappedResult(
            handle, ctypes.byref(operation.overlapped),
            ctypes.byref(count), True,
        )
        self._finish(operation)

    def perform(self, kind: str, buffer: Any = None, size: int = 0,
                deadline: float | None = None) -> int:
        operation = _Operation(buffer)
        pending = False
        with self._lock:
            if self._closed or self.handle is None:
                _closeHandle(operation.event)
                raise OSError("qpsycopg named pipe closed")
            handle = self.handle
            self._pending.add(operation)
            if kind == "connect":
                ok = _connect(handle, ctypes.byref(operation.overlapped))
            elif kind == "read":
                ok = _read(
                    handle, buffer, size, None, ctypes.byref(operation.overlapped)
                )
            elif kind == "write":
                ok = _write(
                    handle, buffer, size, None, ctypes.byref(operation.overlapped)
                )
            else:
                raise ValueError(f"unsupported pipe operation: {kind}")
            error = 0 if ok else ctypes.get_last_error()
            pending = not ok and error == _ERROR_IO_PENDING

        try:
            if not ok and kind == "connect" and error == _ERROR_PIPE_CONNECTED:
                return 0
            if not ok and not pending:
                raise _error(error)
            cancelled_at: float | None = None
            timed_out = False
            while pending:
                wait = _waitEvent(operation.event, _POLL_MS)
                if wait == _WAIT_OBJECT_0:
                    break
                if wait != _WAIT_TIMEOUT:
                    # The wait failed while kernel I/O may still be live.
                    # Retain all state until GetOverlappedResult completes.
                    operation.deferred = True
                    threading.Thread(
                        target=self._reap, args=(operation,),
                        name="qpsycopg-pipe-reaper", daemon=True,
                    ).start()
                    raise ctypes.WinError(ctypes.get_last_error())
                now = time.monotonic()
                if cancelled_at is None and (
                    self._closed or (deadline is not None and now >= deadline)
                ):
                    timed_out = (
                        deadline is not None and now >= deadline and not self._closed
                    )
                    self._cancel(operation)
                    cancelled_at = now
                if (
                    cancelled_at is not None
                    and now - cancelled_at >= _CANCEL_DRAIN_SECONDS
                ):
                    operation.deferred = True
                    threading.Thread(
                        target=self._reap, args=(operation,),
                        name="qpsycopg-pipe-reaper", daemon=True,
                    ).start()
                    if timed_out:
                        raise socket.timeout("qpsycopg named-pipe I/O timed out")
                    raise OSError(
                        _ERROR_OPERATION_ABORTED, "qpsycopg named-pipe I/O cancelled"
                    )
            count = wt.DWORD()
            if not _overlappedResult(handle, ctypes.byref(operation.overlapped),
                                     ctypes.byref(count), False):
                error = ctypes.get_last_error()
                if timed_out:
                    raise socket.timeout("qpsycopg named-pipe I/O timed out")
                raise _error(error)
            if self._closed:
                raise OSError(
                    _ERROR_OPERATION_ABORTED, "qpsycopg named-pipe I/O cancelled"
                )
            if deadline is not None and time.monotonic() >= deadline and timed_out:
                raise socket.timeout("qpsycopg named-pipe I/O timed out")
            return count.value
        finally:
            if not operation.deferred:
                self._finish(operation)


class PipeStream:
    """socket-like PipeStream with real deadlines for reads and writes."""

    ipc_kind = "pipe"

    def __init__(self, handle: int) -> None:
        self._pipe = _PipeHandle(handle)
        self._timeout: float | None = None
        self._readLock = threading.Lock()
        self._writeLock = threading.Lock()
        self._readBuffer = b""
        self._readOffset = 0

    def settimeout(self, timeout: float | None) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be nonnegative")
        self._timeout = timeout

    def setsockopt(self, *args: Any) -> None:
        # TCP_NODELAY and SO_REUSEADDR do not apply to a byte-mode named pipe.
        pass

    def _deadline(self) -> float | None:
        return None if self._timeout is None else time.monotonic() + self._timeout

    def recv_into(self, target: Any) -> int:
        view = memoryview(target).cast("B")
        if view.readonly:
            raise TypeError("recv_into requires a writable buffer")
        if not view:
            return 0
        with self._readLock:
            if self._readOffset < len(self._readBuffer):
                count = min(len(view), len(self._readBuffer) - self._readOffset)
                end = self._readOffset + count
                view[:count] = self._readBuffer[self._readOffset:end]
                self._readOffset += count
                return count
            requested = min(max(len(view), _READ_AHEAD), _IO_CHUNK)
            buffer = ctypes.create_string_buffer(requested)
            try:
                count = self._pipe.perform("read", buffer, requested, self._deadline())
            except BrokenPipeError:
                return 0
            n = min(len(view), count)
            view[:n] = memoryview(buffer).cast("B")[:n]
            self._readBuffer = buffer.raw[n:count] if n < count else b""
            self._readOffset = 0
            return n

    def recv(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("size must be nonnegative")
        data = bytearray(size)
        count = self.recv_into(data)
        return bytes(memoryview(data)[:count])

    def sendall(self, data: bytes | bytearray | memoryview) -> None:
        view = memoryview(data).cast("B")
        with self._writeLock:
            deadline = self._deadline()
            for offset in range(0, len(view), _IO_CHUNK):
                part = view[offset:offset + _IO_CHUNK]
                sent = 0
                while sent < len(part):
                    remaining = part[sent:]
                    buffer = ctypes.create_string_buffer(remaining.tobytes())
                    written = self._pipe.perform(
                        "write", buffer, len(remaining), deadline
                    )
                    if not written:
                        raise BrokenPipeError("qpsycopg named-pipe zero-byte write")
                    sent += written

    def abort(self) -> None:
        self._pipe.close()

    def close(self) -> None:
        self._pipe.close()


class PipeListenerSocket:
    ipc_kind = "pipe"

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows named pipes are unavailable")
        self.address = rf"\\.\pipe\qpsycopg-{os.getpid()}-{secrets.token_hex(16)}"
        attributes, descriptor = _current_user_security()
        try:
            handle = _createPipe(
                self.address,
                _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
                | _FILE_FLAG_FIRST_PIPE_INSTANCE,
                _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT
                | _PIPE_REJECT_REMOTE_CLIENTS,
                1, _BUFFER_SIZE, _BUFFER_SIZE, 0, ctypes.byref(attributes),
            )
            if handle in (None, _INVALID_HANDLE):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _localFree(descriptor)
        self._pipe: _PipeHandle | None = _PipeHandle(int(handle))
        self._timeout: float | None = 10.0
        self.expected_pid: int | None = None

    def setsockopt(self, *args: Any) -> None:
        pass

    def bind(self, address: Any) -> None:
        pass

    def listen(self, backlog: int) -> None:
        pass

    def settimeout(self, timeout: float | None) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be nonnegative")
        self._timeout = timeout

    def getsockname(self) -> tuple[str, int]:
        return f"pipe:{self.address}", 0

    def accept(self) -> tuple[PipeStream, None]:
        pipe = self._pipe
        if pipe is None:
            raise OSError("qpsycopg named-pipe listener closed")
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        pipe.perform("connect", deadline=deadline)
        handle = pipe.handle
        if handle is None:
            raise OSError("qpsycopg named-pipe listener closed")
        if self.expected_pid is not None:
            pid = wt.ULONG()
            if not _clientPid(handle, ctypes.byref(pid)):
                pipe.close()
                raise ctypes.WinError(ctypes.get_last_error())
            if int(pid.value) != self.expected_pid:
                pipe.close()
                raise PermissionError(
                    f"unexpected qpsycopg pipe client PID: {pid.value}"
                )
        # Ownership transfer occurs only after the operation has completed.
        self._pipe = None
        return PipeStreamHandle(pipe), None

    def close(self) -> None:
        pipe, self._pipe = self._pipe, None
        if pipe is not None:
            pipe.close()


def PipeStreamHandle(pipe: _PipeHandle) -> PipeStream:
    """Transfer a connected listener instance without closing its HANDLE."""
    stream = PipeStream.__new__(PipeStream)
    stream._pipe = pipe
    stream._timeout = None
    stream._readLock = threading.Lock()
    stream._writeLock = threading.Lock()
    stream._readBuffer = b""
    stream._readOffset = 0
    return stream


def connectPipe(address: str) -> PipeStream:
    if os.name != "nt":
        raise OSError("Windows named pipes are unavailable")
    deadline = time.monotonic() + 10.0
    while True:
        handle = _createFile(address, _GENERIC_READ | _GENERIC_WRITE, 0, None,
                             _OPEN_EXISTING, _FILE_FLAG_OVERLAPPED, None)
        if handle not in (None, _INVALID_HANDLE):
            return PipeStream(int(handle))
        code = ctypes.get_last_error()
        if code not in (_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY):
            raise _error(code)
        if time.monotonic() >= deadline:
            raise socket.timeout("timed out connecting to qpsycopg named pipe")
        if code == _ERROR_PIPE_BUSY:
            _waitPipe(address, _POLL_MS)
        else:
            time.sleep(_POLL_MS / 1000)
