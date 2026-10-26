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
from typing import Any, cast
from collections.abc import Callable

from . import abc
from .. import errors as e
from .misc import ConninfoOption, PGnotify, PGresAttDesc, _clean_error_message
from ._enums import ConnStatus, Trace
from ._qt_process_protocol import Operation, RemoteError, RemoteKind, RemoteRef
from ._qt_process_protocol import Request, Response, ResultSnapshot, recvFrame
from ._qt_process_protocol import recvToken, sendFrame

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
    """Own QProcess in one Python thread so calls never cross Qt affinity."""

    def __init__(self, host: str, port: int, token: str) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.commands: queue.Queue[str] = queue.Queue()
        self.started = threading.Event()
        self.startError: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="qpsycopg-QProcess",
            daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        process: QProcess | None = None
        try:
            process = QProcess()
            process.setProgram(sys.executable)
            process.setArguments(
                [
                    "-m",
                    "psycopg.pq._qt_process_worker",
                    self.host,
                    str(self.port),
                    self.token,
                ]
            )
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
            self.started.set()

            while True:
                command = self.commands.get()
                if command == "kill":
                    if process.state() != QProcess.ProcessState.NotRunning:
                        process.kill()
                        process.waitForFinished(5_000)
                elif command == "close":
                    if process.state() != QProcess.ProcessState.NotRunning:
                        process.waitForFinished(2_000)
                        if process.state() != QProcess.ProcessState.NotRunning:
                            process.kill()
                            process.waitForFinished(5_000)
                    return
        except BaseException as error:
            self.startError = error
            self.started.set()
        finally:
            if (
                process is not None
                and process.state() != QProcess.ProcessState.NotRunning
            ):
                process.kill()
                process.waitForFinished(5_000)

    def waitStarted(self) -> None:
        if not self.started.wait(10.0):
            raise e.OperationalError("timed out starting qpsycopg worker")
        if self.startError is not None:
            raise self.startError

    def kill(self) -> None:
        self.commands.put("kill")

    def close(self) -> None:
        self.commands.put("close")


class _Transport:
    def __init__(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10.0)
        host, port = listener.getsockname()
        tokenHex = secrets.token_hex(32)
        token = bytes.fromhex(tokenHex)

        self.owner = _ProcessOwner(host, port, tokenHex)
        self.owner.waitStarted()
        try:
            sock, _ = listener.accept()
        except BaseException:
            self.owner.kill()
            raise
        finally:
            listener.close()

        sock.settimeout(10.0)
        received = recvToken(sock)
        if not secrets.compare_digest(received, token):
            sock.close()
            self.owner.kill()
            raise e.OperationalError("qpsycopg worker authentication failed")
        sock.settimeout(None)

        self.sock = sock
        self.lock = threading.RLock()
        self.requestId = 0
        self.closed = False
        self.connection: PGconn | None = None

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

            self.requestId += 1
            request = Request(
                self.requestId,
                operation,
                objectId,
                member,
                _wire(args),
                tuple((name, _wire(value)) for name, value in kwargs),
            )
            try:
                sendFrame(self.sock, request)
                response = recvFrame(self.sock)
            except (EOFError, OSError, ValueError) as error:
                self._markBroken()
                raise e.OperationalError(
                    "qpsycopg worker process disconnected"
                ) from error

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
                _raiseRemote(response.error)
            return _restore(response.value, self)

    def _markBroken(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        finally:
            self.owner.kill()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            try:
                self.requestId += 1
                sendFrame(self.sock, Request(self.requestId, Operation.CLOSE))
                recvFrame(self.sock)
            except Exception:
                self.owner.kill()
            finally:
                self.closed = True
                self.sock.close()
                self.owner.close()


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
        try:
            return int(self._get("status"))
        except e.OperationalError:
            return int(ConnStatus.BAD)

    @property
    def transaction_status(self) -> int:
        return int(self._get("transaction_status"))

    def parameter_status(self, name: bytes) -> bytes | None:
        return self._call("parameter_status", name)

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
        return dict(self._snapshot.errorFields).get(fieldcode)

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

    def get_value(self, row_number: int, column_number: int) -> memoryview | None:
        index = row_number * self._snapshot.columnCount + column_number
        start, size = self._snapshot.cells[index]
        if start < 0:
            return None
        return memoryview(self._snapshot.data)[start : start + size]

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
            return getattr(self._local, member)(data)
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
