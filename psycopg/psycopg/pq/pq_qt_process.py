"""Qt/QProcess-backed implementation of the low-level psycopg.pq interface.

The public Connection/Cursor/adaptation layers remain in the application
process. Only the stateful libpq objects live in an isolated worker process.
"""

from __future__ import annotations

import importlib
import importlib.util
import itertools
import os
import queue
import secrets
import socket
import sys
import threading
from typing import Any
from collections.abc import Callable, Sequence

from .. import errors as e
from . import abc
from .misc import ConninfoOption, PGnotify, PGresAttDesc, _clean_error_message
from ._enums import ConnStatus, Format, Trace
from ._qt_process_protocol import Operation, RemoteError, RemoteKind, RemoteRef
from ._qt_process_protocol import Request, Response, ResultSnapshot, recvFrame, sendFrame

try:
    from PySide6.QtCore import QProcess, QProcessEnvironment
except ImportError as ex:  # pragma: no cover - depends on optional dependency
    raise ImportError(
        "the 'qt-process' pq implementation requires PySide6; "
        "install psycopg[qt] or PySide6"
    ) from ex

# Conninfo parsing doesn't own a database session and is safe to keep local.
# It also avoids spawning a worker only to parse a DSN.
from .pq_ctypes import Conninfo
from .pq_ctypes import Escaping as _LocalEscaping
from .pq_ctypes import __build_version__ as _localBuildVersion
from .pq_ctypes import version as _localVersion


__impl__ = "qt-process"
__build_version__ = _localBuildVersion

_OK = ConnStatus.OK
_handles: dict[int, tuple["_Transport", int]] = {}
_handlesLock = threading.Lock()
_nextHandle = itertools.count(1)


def version() -> int:
    # The worker is launched from the same installation/environment. This value
    # is only capability metadata; no PostgreSQL session is created locally.
    return _localVersion()


def waitRemote(handle: int, mask: int, timeout: float | None) -> int:
    with _handlesLock:
        target = _handles.get(handle)
    if target is None:
        raise e.OperationalError("remote connection handle is closed")
    transport, objectId = target
    return int(transport.request(Operation.WAIT, objectId, args=(mask, timeout)))


def _registerHandle(transport: "_Transport", objectId: int) -> int:
    handle = next(_nextHandle)
    with _handlesLock:
        _handles[handle] = (transport, objectId)
    return handle


def _unregisterHandle(handle: int) -> None:
    with _handlesLock:
        _handles.pop(handle, None)


def _wire(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, tuple):
        return tuple(_wire(item) for item in value)
    if isinstance(value, list):
        return [_wire(item) for item in value]
    return value


class _ProcessOwner:
    """Own QProcess in one Python thread so calls never cross Qt affinity."""

    def __init__(self, host: str, port: int, token: str):
        self.host = host
        self.port = port
        self.token = token
        self.commands: queue.Queue[str] = queue.Queue()
        self.started = threading.Event()
        self.startError: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="qpsycopg-QProcess", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        process: QProcess | None = None
        try:
            process = QProcess()
            process.setProgram(sys.executable)
            process.setArguments(
                ["-m", "psycopg.pq._qt_process_worker", self.host, str(self.port), self.token]
            )
            env = QProcessEnvironment.systemEnvironment()
            env.insert("PSYCOPG_IMPL", _workerImpl())
            process.setProcessEnvironment(env)
            process.setProcessChannelMode(QProcess.ProcessChannelMode.ForwardedErrorChannel)
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
            if process is not None and process.state() != QProcess.ProcessState.NotRunning:
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


def _workerImpl() -> str:
    requested = os.environ.get("QPSYCOPG_WORKER_IMPL", "").lower()
    if requested:
        if requested == "qt-process":
            raise e.InterfaceError("QPSYCOPG_WORKER_IMPL cannot be 'qt-process'")
        return requested
    if importlib.util.find_spec("psycopg_c"):
        return "c"
    if importlib.util.find_spec("psycopg_binary"):
        return "binary"
    return "python"


class _Transport:
    def __init__(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10.0)
        host, port = listener.getsockname()
        token = secrets.token_hex(32)

        self.owner = _ProcessOwner(host, port, token)
        self.owner.waitStarted()
        try:
            sock, _ = listener.accept()
        except BaseException:
            self.owner.kill()
            raise
        finally:
            listener.close()

        sock.settimeout(10.0)
        received = recvFrame(sock)
        if received != token:
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
    ) -> Any:
        with self.lock:
            if self.closed:
                raise e.OperationalError("qpsycopg worker process is closed")

            self.requestId += 1
            request = Request(self.requestId, operation, objectId, member, _wire(args))
            try:
                sendFrame(self.sock, request)
                response = recvFrame(self.sock)
            except (EOFError, OSError, ValueError) as error:
                self._markBroken()
                raise e.OperationalError("qpsycopg worker process disconnected") from error

            if not isinstance(response, Response) or response.requestId != request.requestId:
                self._markBroken()
                raise e.InternalError("invalid response from qpsycopg worker")

            if self.connection is not None:
                for notice in response.notices:
                    if self.connection.notice_handler:
                        self.connection.notice_handler(PGresult(notice))

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


class PGconn:
    def __init__(self, transport: _Transport, objectId: int = 1):
        self._transport = transport
        self._objectId = objectId
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
    def connect(cls, conninfo: bytes) -> "PGconn":
        transport = _Transport()
        try:
            value = transport.request(Operation.CONNECT, args=(conninfo,))
            assert isinstance(value, PGconn)
            return value
        except BaseException:
            transport.close()
            raise

    @classmethod
    def connect_start(cls, conninfo: bytes) -> "PGconn":
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

    def _call(self, member: str, *args: Any) -> Any:
        return self._transport.request(Operation.CALL, self._objectId, member, args)

    def _get(self, member: str) -> Any:
        return self._transport.request(Operation.GET, self._objectId, member)

    def _set(self, member: str, value: Any) -> None:
        self._transport.request(Operation.SET, self._objectId, member, (value,))

    def connect_poll(self) -> int: return int(self._call("connect_poll"))
    def reset(self) -> None: self._call("reset")
    def reset_start(self) -> None: self._call("reset_start")
    def reset_poll(self) -> int: return int(self._call("reset_poll"))

    def finish(self) -> None:
        if self._handle:
            _unregisterHandle(self._handle)
            self._handle = 0
        self._transport.close()

    @property
    def info(self) -> list[ConninfoOption]: return self._get("info")
    @property
    def db(self) -> bytes: return self._get("db")
    @property
    def user(self) -> bytes: return self._get("user")
    @property
    def password(self) -> bytes: return self._get("password")
    @property
    def host(self) -> bytes: return self._get("host")
    @property
    def hostaddr(self) -> bytes: return self._get("hostaddr")
    @property
    def port(self) -> bytes: return self._get("port")
    @property
    def tty(self) -> bytes: return self._get("tty")
    @property
    def options(self) -> bytes: return self._get("options")
    @property
    def status(self) -> int:
        if self._transport.closed:
            return int(ConnStatus.BAD)
        try:
            return int(self._get("status"))
        except e.OperationalError:
            return int(ConnStatus.BAD)
    @property
    def transaction_status(self) -> int: return int(self._get("transaction_status"))
    def parameter_status(self, name: bytes) -> bytes | None: return self._call("parameter_status", name)
    @property
    def error_message(self) -> bytes: return self._get("error_message")
    def get_error_message(self, encoding: str = "") -> str:
        return _clean_error_message(self.error_message, encoding or self._encoding)
    @property
    def _encoding(self) -> str:
        if self.status == _OK:
            from .._encodings import pg2pyenc
            return pg2pyenc(self.parameter_status(b"client_encoding") or b"UTF8")
        return "utf-8"
    @property
    def protocol_version(self) -> int: return int(self._get("protocol_version"))
    @property
    def full_protocol_version(self) -> int: return int(self._get("full_protocol_version"))
    @property
    def server_version(self) -> int: return int(self._get("server_version"))
    @property
    def socket(self) -> int: return self._handle
    @property
    def backend_pid(self) -> int: return int(self._get("backend_pid"))
    @property
    def needs_password(self) -> bool: return bool(self._get("needs_password"))
    @property
    def used_password(self) -> bool: return bool(self._get("used_password"))
    @property
    def used_gssapi(self) -> bool: return bool(self._get("used_gssapi"))
    @property
    def ssl_in_use(self) -> bool: return bool(self._get("ssl_in_use"))

    def exec_(self, command: bytes) -> "PGresult": return self._call("exec_", command)
    def send_query(self, command: bytes) -> None: self._call("send_query", command)
    def exec_params(self, command: bytes, param_values: Sequence[abc.Buffer | None] | None,
                    param_types: Sequence[int] | None = None, param_formats: Sequence[int] | None = None,
                    result_format: int = Format.TEXT) -> "PGresult":
        return self._call("exec_params", command, param_values, param_types, param_formats, result_format)
    def send_query_params(self, command: bytes, param_values: Sequence[abc.Buffer | None] | None,
                          param_types: Sequence[int] | None = None, param_formats: Sequence[int] | None = None,
                          result_format: int = Format.TEXT) -> None:
        self._call("send_query_params", command, param_values, param_types, param_formats, result_format)
    def send_prepare(self, name: bytes, command: bytes, param_types: Sequence[int] | None = None) -> None:
        self._call("send_prepare", name, command, param_types)
    def send_query_prepared(self, name: bytes, param_values: Sequence[abc.Buffer | None] | None,
                            param_formats: Sequence[int] | None = None, result_format: int = Format.TEXT) -> None:
        self._call("send_query_prepared", name, param_values, param_formats, result_format)
    def prepare(self, name: bytes, command: bytes, param_types: Sequence[int] | None = None) -> "PGresult":
        return self._call("prepare", name, command, param_types)
    def exec_prepared(self, name: bytes, param_values: Sequence[abc.Buffer] | None,
                      param_formats: Sequence[int] | None = None, result_format: int = 0) -> "PGresult":
        return self._call("exec_prepared", name, param_values, param_formats, result_format)
    def describe_prepared(self, name: bytes) -> "PGresult": return self._call("describe_prepared", name)
    def send_describe_prepared(self, name: bytes) -> None: self._call("send_describe_prepared", name)
    def describe_portal(self, name: bytes) -> "PGresult": return self._call("describe_portal", name)
    def send_describe_portal(self, name: bytes) -> None: self._call("send_describe_portal", name)
    def close_prepared(self, name: bytes) -> "PGresult": return self._call("close_prepared", name)
    def send_close_prepared(self, name: bytes) -> None: self._call("send_close_prepared", name)
    def close_portal(self, name: bytes) -> "PGresult": return self._call("close_portal", name)
    def send_close_portal(self, name: bytes) -> None: self._call("send_close_portal", name)
    def get_result(self) -> "PGresult | None": return self._call("get_result")
    def consume_input(self) -> None: self._call("consume_input")
    def is_busy(self) -> int: return int(self._call("is_busy"))
    @property
    def nonblocking(self) -> int: return int(self._get("nonblocking"))
    @nonblocking.setter
    def nonblocking(self, arg: int) -> None: self._set("nonblocking", arg)
    def flush(self) -> int: return int(self._call("flush"))
    def set_single_row_mode(self) -> None: self._call("set_single_row_mode")
    def set_chunked_rows_mode(self, size: int) -> None: self._call("set_chunked_rows_mode", size)
    def cancel_conn(self) -> "PGcancelConn": return self._call("cancel_conn")
    def get_cancel(self) -> "PGcancel": return self._call("get_cancel")
    def notifies(self) -> PGnotify | None: return self._call("notifies")
    def put_copy_data(self, buffer: abc.Buffer) -> int: return int(self._call("put_copy_data", buffer))
    def put_copy_end(self, error: bytes | None = None) -> int: return int(self._call("put_copy_end", error))
    def get_copy_data(self, async_: int) -> tuple[int, memoryview]:
        size, data = self._call("get_copy_data", async_)
        return int(size), memoryview(data)
    def trace(self, fileno: int) -> None:
        raise e.NotSupportedError("trace() cannot use a parent-process file descriptor")
    def set_trace_flags(self, flags: Trace) -> None: self._call("set_trace_flags", flags)
    def untrace(self) -> None: self._call("untrace")
    def encrypt_password(self, passwd: bytes, user: bytes, algorithm: bytes | None = None) -> bytes:
        return self._call("encrypt_password", passwd, user, algorithm)
    def change_password(self, user: bytes, passwd: bytes) -> None: self._call("change_password", user, passwd)
    def make_empty_result(self, exec_status: int) -> "PGresult": return self._call("make_empty_result", exec_status)
    @property
    def pipeline_status(self) -> int: return int(self._get("pipeline_status"))
    def enter_pipeline_mode(self) -> None: self._call("enter_pipeline_mode")
    def exit_pipeline_mode(self) -> None: self._call("exit_pipeline_mode")
    def pipeline_sync(self) -> None: self._call("pipeline_sync")
    def send_flush_request(self) -> None: self._call("send_flush_request")


class PGresult:
    def __init__(self, snapshot: ResultSnapshot):
        self._snapshot = snapshot
        self._columns = list(snapshot.columns)

    def clear(self) -> None:
        return
    @property
    def status(self) -> int: return self._snapshot.status
    @property
    def error_message(self) -> bytes: return self._snapshot.errorMessage
    def get_error_message(self, encoding: str = "utf-8") -> str:
        return _clean_error_message(self.error_message, encoding)
    def error_field(self, fieldcode: int) -> bytes | None:
        return dict(self._snapshot.errorFields).get(fieldcode)
    @property
    def ntuples(self) -> int: return self._snapshot.rowCount
    @property
    def nfields(self) -> int: return len(self._columns)
    def fname(self, column_number: int) -> bytes | None: return self._columns[column_number][0]
    def ftable(self, column_number: int) -> int: return self._columns[column_number][1]
    def ftablecol(self, column_number: int) -> int: return self._columns[column_number][2]
    def fformat(self, column_number: int) -> int: return self._columns[column_number][3]
    def ftype(self, column_number: int) -> int: return self._columns[column_number][4]
    def fmod(self, column_number: int) -> int: return self._columns[column_number][5]
    def fsize(self, column_number: int) -> int: return self._columns[column_number][6]
    @property
    def binary_tuples(self) -> int: return self._snapshot.binaryTuples
    def get_value(self, row_number: int, column_number: int) -> memoryview | None:
        index = row_number * self._snapshot.columnCount + column_number
        start, size = self._snapshot.cells[index]
        if start < 0:
            return None
        return memoryview(self._snapshot.data)[start:start + size]
    @property
    def nparams(self) -> int: return len(self._snapshot.paramTypes)
    def param_type(self, param_number: int) -> int: return self._snapshot.paramTypes[param_number]
    @property
    def command_status(self) -> bytes | None: return self._snapshot.commandStatus
    @property
    def command_tuples(self) -> int | None: return self._snapshot.commandTuples
    @property
    def oid_value(self) -> int: return self._snapshot.oidValue
    def set_attributes(self, descriptions: list[PGresAttDesc]) -> None:
        self._columns = [
            (d.name, d.tableid, d.columnid, d.format, d.typid, d.atttypmod, d.typlen)
            for d in descriptions
        ]


class PGcancelConn:
    def __init__(self, transport: _Transport, objectId: int):
        self._transport = transport
        self._objectId = objectId
        self._handle = _registerHandle(transport, objectId)
    def _call(self, member: str, *args: Any) -> Any:
        return self._transport.request(Operation.CALL, self._objectId, member, args)
    def _get(self, member: str) -> Any:
        return self._transport.request(Operation.GET, self._objectId, member)
    def start(self) -> None: self._call("start")
    def blocking(self) -> None: self._call("blocking")
    def poll(self) -> int: return int(self._call("poll"))
    @property
    def status(self) -> int: return int(self._get("status"))
    @property
    def socket(self) -> int: return self._handle
    @property
    def error_message(self) -> bytes: return self._get("error_message")
    def get_error_message(self, encoding: str = "utf-8") -> str:
        return _clean_error_message(self.error_message, encoding)
    def reset(self) -> None: self._call("reset")
    def finish(self) -> None:
        if self._handle:
            _unregisterHandle(self._handle)
            self._handle = 0
        try:
            self._call("finish")
        except e.OperationalError:
            pass


class PGcancel:
    def __init__(self, transport: _Transport, objectId: int):
        self._transport = transport
        self._objectId = objectId
        self._freed = False
    def cancel(self) -> None:
        if self._freed:
            raise e.OperationalError("cancel object is closed")
        self._transport.request(Operation.CALL, self._objectId, "cancel")
    def free(self) -> None:
        if not self._freed:
            self._freed = True
            try:
                self._transport.request(Operation.CALL, self._objectId, "free")
            except e.OperationalError:
                pass


class Escaping:
    def __init__(self, conn: PGconn | None = None):
        self.conn = conn
        self._local = _LocalEscaping(None) if conn is None else None

    def _escape(self, member: str, data: abc.Buffer) -> bytes:
        if self.conn is None:
            assert self._local is not None
            return getattr(self._local, member)(data)
        return self.conn._transport.request(
            Operation.ESCAPE, self.conn._objectId, member, (_wire(data),)
        )

    def escape_literal(self, data: abc.Buffer) -> bytes: return self._escape("escape_literal", data)
    def escape_identifier(self, data: abc.Buffer) -> bytes: return self._escape("escape_identifier", data)
    def escape_string(self, data: abc.Buffer) -> bytes: return self._escape("escape_string", data)
    def escape_bytea(self, data: abc.Buffer) -> bytes: return self._escape("escape_bytea", data)
    def unescape_bytea(self, data: abc.Buffer) -> bytes: return self._escape("unescape_bytea", data)
