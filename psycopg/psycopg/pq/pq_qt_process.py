# mypy: disable-error-code="no-any-return, import-not-found"
"""Qt/QProcess-backed implementation of the low-level psycopg.pq interface.

The public Connection/Cursor/adaptation layers remain in the application
process. Only the stateful libpq objects live in an isolated worker process.
"""

from __future__ import annotations

import os
import sys
import queue
import socket
import secrets
import importlib
import itertools
import threading
from time import monotonic
import socket as _stdlib_socket
from ._qt_process_owner import (ConnectAttempt, ExitProof, ExitState, KillReceipt,
                                OwnerToken, currentConnectAttempt)
from typing import Any, cast
from collections.abc import Callable

from . import abc
from .. import errors as e
from .misc import ConninfoOption, PGnotify, PGresAttDesc, _clean_error_message
from ._enums import ConnStatus, Trace
from ._qt_process_protocol import Operation, RemoteError, RemoteKind, RemoteRef
from ._qt_process_protocol import Request, Response, ResultSnapshot, SendSnapshot
from ._qt_process_protocol import ResultReadSnapshot, WaitSnapshot
from ._qt_process_protocol import recvFrame, recvToken, sendFrame

try:
    from PySide6.QtCore import QProcess, QProcessEnvironment
except ImportError as ex:  # pragma: no cover - optional dependency
    raise ImportError(
        "the 'qt-process' pq implementation requires PySide6; "
        "install psycopg[qt] or PySide6"
    ) from ex


def _loadLocalPq() -> Any:
    """Load a usable local pq backend without creating a database session."""
    for name in ("psycopg_c.pq", "psycopg_binary.pq"):
        try:
            return importlib.import_module(name)
        except ImportError:
            pass

    from . import pq_ctypes

    return pq_ctypes


_localPq = _loadLocalPq()
Conninfo = _localPq.Conninfo
_LocalEscaping = _localPq.Escaping

__impl__ = "qt-process"
__build_version__ = _localPq.__build_version__

_OK = ConnStatus.OK
_handles: dict[int, tuple[_Transport, int]] = {}
_handlesLock = threading.Lock()
_nextHandle = itertools.count(1)
_SEND_MEMBERS = {
    "send_query",
    "send_query_params",
    "send_prepare",
    "send_query_prepared",
    "send_describe_prepared",
    "send_describe_portal",
    "send_close_prepared",
    "send_close_portal",
}


def version() -> int:
    return int(_localPq.version())


def waitRemote(handle: int, mask: int, timeout: float | None) -> int:
    with _handlesLock:
        target = _handles.get(handle)
    if target is None:
        raise e.OperationalError("remote connection handle is closed")
    transport, objectId = target
    return int(transport.request(Operation.WAIT, objectId, args=(mask, timeout)))


def _registerHandle(transport: _Transport, objectId: int) -> int:
    handle = next(_nextHandle)
    with _handlesLock:
        _handles[handle] = (transport, objectId)
    return handle


def _unregisterHandle(handle: int) -> None:
    with _handlesLock:
        _handles.pop(handle, None)


def _wire(value: Any) -> Any:
    if isinstance(value, (memoryview, bytearray)):
        return bytes(value)
    if isinstance(value, tuple):
        return tuple(_wire(item) for item in value)
    if isinstance(value, list):
        return [_wire(item) for item in value]
    return value


def _workerImpl() -> str:
    requested = os.environ.get("QPSYCOPG_WORKER_IMPL", "").lower()
    if requested:
        if requested == "qt-process":
            raise e.InterfaceError("QPSYCOPG_WORKER_IMPL cannot be 'qt-process'")
        if requested not in ("c", "binary", "python"):
            raise e.InterfaceError(f"unknown QPSYCOPG_WORKER_IMPL value: {requested!r}")
        return requested
    return str(_localPq.__impl__)


class _ProcessOwner:
    """One QProcess and an immutable identity; Qt calls stay in its owner thread."""

    def __init__(self, host: str, port: int, token: str, *,
                 attempt: ConnectAttempt | None = None, transport: Any = None) -> None:
        self.host = host
        self.port = port
        self.token = token  # IPC authentication only; never the process identity.
        self.ownerToken = OwnerToken()
        self.attemptToken = None if attempt is None else attempt.token
        self.commands: queue.Queue[str] = queue.Queue()
        self.started = threading.Event()
        self.startError: BaseException | None = None
        self._stateLock = threading.Lock()
        self._exitEvent = threading.Event()
        self._killRequested = False
        self._closeRequested = False
        self._state = ExitState.EXIT_UNCONFIRMED
        self._timestamp = monotonic()
        self._osPid: int | None = None
        self._exitProof: ExitProof | None = None
        self._diagnostic = "worker not started"
        self.thread = threading.Thread(
            target=self._run, name="qpsycopg-QProcess", daemon=True
        )
        # The callback is synchronous and precedes any potentially blocking
        # QProcess/start/handshake/connect activity.
        if attempt is not None:
            attempt._bind(self, transport)
        self.thread.start()

    def _checkToken(self, token: OwnerToken) -> None:
        if not isinstance(token, OwnerToken) or token != self.ownerToken:
            raise ValueError("wrong or stale qpsycopg owner token")

    def receipt(self, token: OwnerToken) -> KillReceipt:
        self._checkToken(token)
        with self._stateLock:
            return KillReceipt(token, self._state, self._timestamp,
                               self._osPid, self._diagnostic)

    def requestKill(self, token: OwnerToken) -> KillReceipt:
        """An idempotent request, not an OS-exit assertion."""
        self._checkToken(token)
        with self._stateLock:
            if not self._killRequested and self._exitProof is None:
                self._killRequested = True
                self._state = ExitState.KILL_QUEUED
                self._timestamp = monotonic()
                self._diagnostic = "kill queued for the owning QProcess thread"
                self.commands.put("kill")
            return KillReceipt(token, self._state, self._timestamp,
                               self._osPid, self._diagnostic)

    def kill(self) -> None:
        """Legacy exact-instance API: enqueue only; never confirm OS-exit."""
        self.requestKill(self.ownerToken)

    def close(self) -> None:
        with self._stateLock:
            if self._closeRequested or self._exitProof is not None:
                return
            self._closeRequested = True
            self.commands.put("close")

    def exitProof(self, token: OwnerToken) -> ExitProof | None:
        self._checkToken(token)
        with self._stateLock:
            return self._exitProof

    def waitExit(self, token: OwnerToken, timeout: float) -> ExitProof | None:
        self._checkToken(token)
        if timeout < 0:
            raise ValueError("timeout must be nonnegative")
        self._exitEvent.wait(timeout)
        return self.exitProof(token)

    def exitConfirmed(self) -> bool:
        """Compatibility seam for DBPT T08; thread death is not sufficient."""
        return self.exitProof(self.ownerToken) is not None

    def _dispatched(self) -> None:
        with self._stateLock:
            if self._exitProof is None:
                self._state = ExitState.KILL_DISPATCHED
                self._timestamp = monotonic()
                self._diagnostic = "QProcess.kill() dispatch in owner thread"

    def _observeExit(self, process: QProcess, reason: str) -> None:
        # QProcess.NotRunning is its OS process-status observation. Require a
        # recorded real PID; neither Python thread completion nor queue drain
        # is accepted as a substitute.
        if process.state() != QProcess.ProcessState.NotRunning:
            return
        with self._stateLock:
            if self._osPid is None or self._exitProof is not None:
                return
            status = process.exitStatus()
            self._exitProof = ExitProof(
                self.ownerToken, self._osPid, int(process.exitCode()),
                int(getattr(status, "value", status)), monotonic(), reason,
            )
            self._state = ExitState.OS_EXIT_CONFIRMED
            self._timestamp = self._exitProof.timestamp
            self._diagnostic = reason
            self._exitEvent.set()

    def _run(self) -> None:
        process: QProcess | None = None
        try:
            process = QProcess()
            process.setProgram(sys.executable)
            process.setArguments(getattr(self, "_workerArguments", [
                "-m", "psycopg.pq._qt_process_worker",
                self.host, str(self.port), self.token,
            ]))
            env = QProcessEnvironment.systemEnvironment()
            env.insert("PSYCOPG_IMPL", _workerImpl())
            process.setProcessEnvironment(env)
            process.setProcessChannelMode(
                QProcess.ProcessChannelMode.ForwardedErrorChannel
            )
            process.start()
            if not process.waitForStarted(10_000):
                raise e.OperationalError(
                    f"couldn't start qpsycopg worker: {process.errorString()}"
                )
            with self._stateLock:
                self._osPid = int(process.processId())
                self._diagnostic = "worker started"
            self.started.set()
            while True:
                command = self.commands.get()
                if command == "kill":
                    self._dispatched()
                    if process.state() != QProcess.ProcessState.NotRunning:
                        process.kill()
                        process.waitForFinished(5_000)
                    self._observeExit(process, "forced QProcess kill")
                elif command == "close":
                    if process.state() != QProcess.ProcessState.NotRunning:
                        process.waitForFinished(2_000)
                    if process.state() != QProcess.ProcessState.NotRunning:
                        self._dispatched()
                        process.kill()
                        process.waitForFinished(5_000)
                    self._observeExit(process, "owner close (graceful or escalated)")
                    return
        except BaseException as error:
            self.startError = error
            with self._stateLock:
                self._diagnostic = f"owner thread error: {error}"
            self.started.set()
        finally:
            if process is not None:
                if process.state() != QProcess.ProcessState.NotRunning:
                    self._dispatched()
                    process.kill()
                    process.waitForFinished(5_000)
                self._observeExit(process, "owner thread final cleanup")
            # If the process remains running, EXIT_UNCONFIRMED is intentional.

    def waitStarted(self) -> None:
        if not self.started.wait(10.5):
            raise e.OperationalError("timed out starting qpsycopg worker")
        if self.startError is not None:
            raise self.startError


class _Transport:
    def __init__(self) -> None:
        self._attempt = currentConnectAttempt()
        # A managed attempt requires bounded accept, not an uninterruptible
        # Windows ConnectNamedPipe. Existing unmanaged IPC stays unchanged.
        factory = _stdlib_socket.socket if self._attempt is not None else socket.socket
        listener = factory(_stdlib_socket.AF_INET, _stdlib_socket.SOCK_STREAM)
        self._listener = listener
        self.sock: Any = None
        listener.setsockopt(_stdlib_socket.SOL_SOCKET, _stdlib_socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.2 if self._attempt is not None else 10.0)
        host, port = listener.getsockname()
        tokenHex = secrets.token_hex(32)
        token = bytes.fromhex(tokenHex)

        try:
            self.owner = _ProcessOwner(host, port, tokenHex,
                                       attempt=self._attempt, transport=self)
            self.owner.waitStarted()
            while True:
                if self._attempt is not None:
                    self._attempt.check()
                try:
                    sock, _ = listener.accept()
                    break
                except _stdlib_socket.timeout:
                    if self._attempt is None:
                        raise
            self.sock = sock
        except BaseException:
            if hasattr(self, "owner"):
                self.owner.kill()
            raise
        finally:
            self._listener = None
            listener.close()

        try:
            sock.settimeout(10.0)
            received = recvToken(sock)
            if not secrets.compare_digest(received, token):
                raise e.OperationalError("qpsycopg worker authentication failed")
            if self._attempt is not None:
                self._attempt.check()
            sock.settimeout(None)
        except BaseException:
            sock.close()
            self.owner.kill()
            raise

        self.lock = threading.RLock()
        self.requestId = 0
        self.closed = False
        self.connection: PGconn | None = None
        self._flushCache: dict[int, int] = {}
        self._busyCache: dict[int, int] = {}
        self._consumeCache: set[int] = set()
        self._notifyCache: dict[int, list[PGnotify | None]] = {}
        self._resultCache: dict[int, list[PGresult | None]] = {}
        self._statusCache: int | None = None
        self._transactionStatusCache: int | None = None
        self._clientEncodingCache: bytes | None = None

    def _abortConnect(self) -> None:
        """Best-effort wakeup without taking a lock owned by blocked recv()."""
        listener = getattr(self, "_listener", None)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        sock = getattr(self, "sock", None)
        if sock is not None and hasattr(sock, "shutdown"):
            try:
                sock.shutdown(_stdlib_socket.SHUT_RDWR)
            except OSError:
                pass

    def _clearQueryCache(self, objectId: int) -> None:
        self._flushCache.pop(objectId, None)
        self._busyCache.pop(objectId, None)
        self._consumeCache.discard(objectId)
        self._notifyCache.pop(objectId, None)
        self._resultCache.pop(objectId, None)

    def _clearConnectionState(self) -> None:
        self._statusCache = None
        self._transactionStatusCache = None
        self._clientEncodingCache = None

    def _updateConnectionState(self, snapshot: SendSnapshot) -> None:
        if snapshot.status is not None:
            self._statusCache = snapshot.status
        if snapshot.transactionStatus is not None:
            self._transactionStatusCache = snapshot.transactionStatus
        if snapshot.clientEncoding is not None:
            self._clientEncodingCache = snapshot.clientEncoding

    def _clearCaches(self) -> None:
        self._flushCache.clear()
        self._busyCache.clear()
        self._consumeCache.clear()
        self._notifyCache.clear()
        self._resultCache.clear()

    def _cachedCall(self, objectId: int, member: str) -> tuple[bool, Any]:
        if member == "flush" and objectId in self._flushCache:
            return True, self._flushCache.pop(objectId)
        if member == "consume_input" and objectId in self._consumeCache:
            self._consumeCache.discard(objectId)
            return True, None
        if member == "is_busy":
            if self._resultCache.get(objectId):
                return True, 0
            if objectId in self._busyCache:
                return True, self._busyCache.pop(objectId)
        if member == "notifies":
            cache = self._notifyCache.get(objectId)
            if cache:
                value = cache.pop(0)
                if not cache:
                    self._notifyCache.pop(objectId, None)
                return True, value
            if self._resultCache.get(objectId):
                return True, None
        if member == "get_result":
            cache = self._resultCache.get(objectId)
            if cache:
                value = cache.pop(0)
                if not cache:
                    self._resultCache.pop(objectId, None)
                return True, value
        return False, None

    def request(
        self,
        operation: Operation,
        objectId: int = 0,
        member: str = "",
        args: tuple[Any, ...] = (),
        kwargs: tuple[tuple[str, Any], ...] = (),
    ) -> Any:
        with self.lock:
            if self.closed:
                raise e.OperationalError("qpsycopg worker process is closed")

            if operation == Operation.CALL:
                cached, value = self._cachedCall(objectId, member)
                if cached:
                    return value
                if member in _SEND_MEMBERS:
                    self._clearQueryCache(objectId)

            self.requestId += 1
            request = Request(
                self.requestId,
                operation,
                objectId,
                member,
                _wire(args),
                tuple((name, _wire(value)) for name, value in kwargs),
            )
            attempt = self._attempt if operation in (Operation.CONNECT, Operation.CONNECT_START) else None
            if attempt is not None:
                attempt.check()
                remaining = attempt.remaining()
                if remaining is not None:
                    self.sock.settimeout(max(0.001, remaining))
            try:
                sendFrame(self.sock, request)
                response = recvFrame(self.sock)
                if attempt is not None:
                    attempt.check()
            except (EOFError, OSError, ValueError) as error:
                self._markBroken()
                raise e.OperationalError(
                    "qpsycopg worker process disconnected"
                ) from error
            finally:
                if attempt is not None:
                    try:
                        self.sock.settimeout(None)
                    except OSError:
                        pass

            if (
                not isinstance(response, Response)
                or response.requestId != request.requestId
            ):
                self._markBroken()
                raise e.InternalError("invalid response from qpsycopg worker")

            if self.connection is not None:
                for notice in response.notices:
                    if self.connection.notice_handler:
                        self.connection.notice_handler(
                            cast(abc.PGresult, PGresult(notice))
                        )

            if response.error is not None:
                self._clearQueryCache(objectId)
                self._clearConnectionState()
                _raiseRemote(response.error)

            value = response.value
            if isinstance(value, SendSnapshot):
                self._updateConnectionState(value)
                self._flushCache[objectId] = value.flush
                self._busyCache[objectId] = value.busy
                return _restore(value.value, self)

            if isinstance(value, ResultReadSnapshot):
                self._clearConnectionState()
                if value.following:
                    self._resultCache[objectId] = [
                        cast(PGresult | None, _restore(item, self))
                        for item in value.following
                    ]
                return cast(PGresult, _restore(value.value, self))

            if isinstance(value, WaitSnapshot):
                if value.consumed:
                    self._clearConnectionState()
                    self._consumeCache.add(objectId)
                    if value.busy is not None:
                        self._busyCache[objectId] = value.busy
                    self._notifyCache[objectId] = [*value.notifies, None]
                    if value.results:
                        self._resultCache[objectId] = [
                            cast(PGresult | None, _restore(item, self))
                            for item in value.results
                        ]
                return value.ready

            return _restore(value, self)

    def _markBroken(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._statusCache = int(ConnStatus.BAD)
        self._transactionStatusCache = None
        self._clientEncodingCache = None
        self._clearCaches()
        try:
            self.sock.close()
        finally:
            self.owner.kill()

    def close(self) -> None:
        # Never wait on the request lock: a SQL recv may be blocked forever.
        if self.closed:
            return
        if not self.lock.acquire(blocking=False):
            self.closed = True
            self._abortConnect()
            self.owner.kill()
            return
        try:
            if self.closed:
                return
            self.closed = True
            self._statusCache = int(ConnStatus.BAD)
            self._transactionStatusCache = None
            self._clientEncodingCache = None
            self._clearCaches()
            # A Windows named-pipe read has no usable socket timeout. Do not
            # issue an unbounded CLOSE RPC on it; QProcess close owns cleanup.
            if isinstance(self.sock, _stdlib_socket.socket):
                try:
                    self.sock.settimeout(0.5)
                    self.requestId += 1
                    sendFrame(self.sock, Request(self.requestId, Operation.CLOSE))
                    recvFrame(self.sock)
                except (EOFError, OSError, ValueError):
                    self.owner.kill()
            self._abortConnect()
            try:
                self.sock.close()
            except OSError:
                pass
            self.owner.close()
        finally:
            self.lock.release()


def _raiseRemote(error: RemoteError) -> None:
    cls: type[BaseException] = e.OperationalError
    try:
        module = importlib.import_module(error.module)
        candidate = getattr(module, error.name)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            cls = candidate
    except Exception:
        candidate = getattr(e, error.name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            cls = candidate
    raise cls(error.message)


def _restore(value: Any, transport: _Transport) -> Any:
    if isinstance(value, ResultSnapshot):
        return PGresult(value)
    if isinstance(value, RemoteRef):
        if value.kind == RemoteKind.PGCONN:
            return PGconn(transport, value.objectId)
        if value.kind == RemoteKind.PGCANCEL_CONN:
            return PGcancelConn(transport, value.objectId)
        if value.kind == RemoteKind.PGCANCEL:
            return PGcancel(transport, value.objectId)
    if isinstance(value, tuple):
        return tuple(_restore(item, transport) for item in value)
    if isinstance(value, list):
        return [_restore(item, transport) for item in value]
    return value


class _RemoteObject:
    def __init__(self, transport: _Transport, objectId: int) -> None:
        self._transport = transport
        self._objectId = objectId

    def _call(self, member: str, *args: Any, **kwargs: Any) -> Any:
        return self._transport.request(
            Operation.CALL,
            self._objectId,
            member,
            args,
            tuple(kwargs.items()),
        )

    def _get(self, member: str) -> Any:
        return self._transport.request(Operation.GET, self._objectId, member)

    def _set(self, member: str, value: Any) -> None:
        self._transport.request(
            Operation.SET,
            self._objectId,
            member,
            (value,),
        )

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, *args, **kwargs)

        return call


class PGconn(_RemoteObject):
    def __init__(self, transport: _Transport, objectId: int = 1) -> None:
        super().__init__(transport, objectId)
        self._handle = _registerHandle(transport, objectId)
        self.notice_handler: Callable[[abc.PGresult], None] | None = None
        self.notify_handler: Callable[[PGnotify], None] | None = None
        transport.connection = self

    def __del__(self) -> None:
        try:
            self.finish()
        except Exception:
            pass

    @classmethod
    def connect(cls, conninfo: bytes) -> PGconn:
        transport = _Transport()
        try:
            value = transport.request(Operation.CONNECT, args=(conninfo,))
            assert isinstance(value, PGconn)
            if transport._attempt is not None:
                transport._attempt.check()
            return value
        except BaseException:
            transport.close()
            raise

    @classmethod
    def connect_start(cls, conninfo: bytes) -> PGconn:
        transport = _Transport()
        try:
            value = transport.request(Operation.CONNECT_START, args=(conninfo,))
            assert isinstance(value, PGconn)
            if transport._attempt is not None:
                transport._attempt.check()
            return value
        except BaseException:
            transport.close()
            raise

    @classmethod
    def ping(cls, conninfo: bytes) -> int:
        transport = _Transport()
        try:
            return int(transport.request(Operation.PING, args=(conninfo,)))
        finally:
            transport.close()

    def finish(self) -> None:
        if self._handle:
            _unregisterHandle(self._handle)
            self._handle = 0
        self._transport.close()

    def connect_poll(self) -> int:
        value = int(self._call("connect_poll"))
        self._transport._statusCache = None
        return value

    @property
    def info(self) -> list[ConninfoOption]:
        return self._get("info")

    @property
    def db(self) -> bytes:
        return self._get("db")

    @property
    def user(self) -> bytes:
        return self._get("user")

    @property
    def password(self) -> bytes:
        return self._get("password")

    @property
    def host(self) -> bytes:
        return self._get("host")

    @property
    def hostaddr(self) -> bytes:
        return self._get("hostaddr")

    @property
    def port(self) -> bytes:
        return self._get("port")

    @property
    def tty(self) -> bytes:
        return self._get("tty")

    @property
    def options(self) -> bytes:
        return self._get("options")

    @property
    def status(self) -> int:
        if self._transport.closed:
            return int(ConnStatus.BAD)
        if self._transport._statusCache is not None:
            return self._transport._statusCache
        try:
            value = int(self._get("status"))
            self._transport._statusCache = value
            return value
        except e.OperationalError:
            return int(ConnStatus.BAD)

    @property
    def transaction_status(self) -> int:
        if self._transport._transactionStatusCache is not None:
            return self._transport._transactionStatusCache
        value = int(self._get("transaction_status"))
        self._transport._transactionStatusCache = value
        return value

    def parameter_status(self, name: bytes) -> bytes | None:
        if name == b"client_encoding" and self._transport._clientEncodingCache is not None:
            return self._transport._clientEncodingCache
        value = self._call("parameter_status", name)
        if name == b"client_encoding" and value is not None:
            self._transport._clientEncodingCache = bytes(value)
        return value

    @property
    def error_message(self) -> bytes:
        return self._get("error_message")

    def get_error_message(self, encoding: str = "") -> str:
        return _clean_error_message(
            self.error_message,
            encoding or self._encoding,
        )

    @property
    def _encoding(self) -> str:
        if self.status == _OK:
            from .._encodings import pg2pyenc

            return pg2pyenc(self.parameter_status(b"client_encoding") or b"UTF8")
        return "utf-8"

    @property
    def protocol_version(self) -> int:
        return int(self._get("protocol_version"))

    @property
    def full_protocol_version(self) -> int:
        return int(self._get("full_protocol_version"))

    @property
    def server_version(self) -> int:
        return int(self._get("server_version"))

    @property
    def socket(self) -> int:
        return self._handle

    @property
    def backend_pid(self) -> int:
        return int(self._get("backend_pid"))

    @property
    def needs_password(self) -> bool:
        return bool(self._get("needs_password"))

    @property
    def used_password(self) -> bool:
        return bool(self._get("used_password"))

    @property
    def used_gssapi(self) -> bool:
        return bool(self._get("used_gssapi"))

    @property
    def ssl_in_use(self) -> bool:
        return bool(self._get("ssl_in_use"))

    @property
    def nonblocking(self) -> int:
        return int(self._get("nonblocking"))

    @nonblocking.setter
    def nonblocking(self, value: int) -> None:
        self._set("nonblocking", value)

    @property
    def pipeline_status(self) -> int:
        return int(self._get("pipeline_status"))

    def get_copy_data(self, async_: int) -> tuple[int, memoryview]:
        size, data = self._call("get_copy_data", async_)
        return int(size), memoryview(data)

    def trace(self, fileno: int) -> None:
        raise e.NotSupportedError("trace() cannot use a parent-process file descriptor")

    def set_trace_flags(self, flags: Trace) -> None:
        self._call("set_trace_flags", flags)


class PGresult:
    def __init__(self, snapshot: ResultSnapshot) -> None:
        self._snapshot = snapshot
        self._columns = list(snapshot.columns)
        self._values = snapshot.values
        self._columnCount = snapshot.columnCount
        self._errorFields = dict(snapshot.errorFields)

    def clear(self) -> None:
        pass

    @property
    def status(self) -> int:
        return self._snapshot.status

    @property
    def error_message(self) -> bytes:
        return self._snapshot.errorMessage

    def get_error_message(self, encoding: str = "utf-8") -> str:
        return _clean_error_message(self.error_message, encoding)

    def error_field(self, fieldcode: int) -> bytes | None:
        return self._errorFields.get(fieldcode)

    @property
    def ntuples(self) -> int:
        return self._snapshot.rowCount

    @property
    def nfields(self) -> int:
        return len(self._columns)

    def fname(self, column_number: int) -> bytes | None:
        return self._columns[column_number][0]

    def ftable(self, column_number: int) -> int:
        return self._columns[column_number][1]

    def ftablecol(self, column_number: int) -> int:
        return self._columns[column_number][2]

    def fformat(self, column_number: int) -> int:
        return self._columns[column_number][3]

    def ftype(self, column_number: int) -> int:
        return self._columns[column_number][4]

    def fmod(self, column_number: int) -> int:
        return self._columns[column_number][5]

    def fsize(self, column_number: int) -> int:
        return self._columns[column_number][6]

    @property
    def binary_tuples(self) -> int:
        return self._snapshot.binaryTuples

    def get_value(self, row_number: int, column_number: int) -> bytes | None:
        return self._values[row_number * self._columnCount + column_number]

    @property
    def nparams(self) -> int:
        return len(self._snapshot.paramTypes)

    def param_type(self, param_number: int) -> int:
        return self._snapshot.paramTypes[param_number]

    @property
    def command_status(self) -> bytes | None:
        return self._snapshot.commandStatus

    @property
    def command_tuples(self) -> int | None:
        return self._snapshot.commandTuples

    @property
    def oid_value(self) -> int:
        return self._snapshot.oidValue

    def set_attributes(self, descriptions: list[PGresAttDesc]) -> None:
        self._columns = [
            (
                desc.name,
                desc.tableid,
                desc.columnid,
                desc.format,
                desc.typid,
                desc.atttypmod,
                desc.typlen,
            )
            for desc in descriptions
        ]


class PGcancelConn(_RemoteObject):
    def __init__(self, transport: _Transport, objectId: int) -> None:
        super().__init__(transport, objectId)
        self._handle = _registerHandle(transport, objectId)

    @property
    def status(self) -> int:
        return int(self._get("status"))

    @property
    def socket(self) -> int:
        return self._handle

    @property
    def error_message(self) -> bytes:
        return self._get("error_message")

    def get_error_message(self, encoding: str = "utf-8") -> str:
        return _clean_error_message(self.error_message, encoding)

    def finish(self) -> None:
        if self._handle:
            _unregisterHandle(self._handle)
            self._handle = 0
        try:
            self._call("finish")
        except e.OperationalError:
            pass


class PGcancel(_RemoteObject):
    def __init__(self, transport: _Transport, objectId: int) -> None:
        super().__init__(transport, objectId)
        self._freed = False

    def cancel(self) -> None:
        if self._freed:
            raise e.OperationalError("cancel object is closed")
        self._call("cancel")

    def free(self) -> None:
        if self._freed:
            return
        self._freed = True
        try:
            self._call("free")
        except e.OperationalError:
            pass


class Escaping:
    def __init__(self, conn: PGconn | None = None) -> None:
        self.conn = conn
        self._local = _LocalEscaping(None) if conn is None else None

    def _escape(self, member: str, data: abc.Buffer) -> bytes:
        if self.conn is None:
            assert self._local is not None
            return getattr(self._local, member)(_wire(data))
        return self.conn._transport.request(
            Operation.ESCAPE,
            self.conn._objectId,
            member,
            (_wire(data),),
        )

    def escape_literal(self, data: abc.Buffer) -> bytes:
        return self._escape("escape_literal", data)

    def escape_identifier(self, data: abc.Buffer) -> bytes:
        return self._escape("escape_identifier", data)

    def escape_string(self, data: abc.Buffer) -> bytes:
        return self._escape("escape_string", data)

    def escape_bytea(self, data: abc.Buffer) -> bytes:
        return self._escape("escape_bytea", data)

    def unescape_bytea(self, data: abc.Buffer) -> bytes:
        return self._escape("unescape_bytea", data)
