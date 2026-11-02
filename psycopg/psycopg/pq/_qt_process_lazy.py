"""Lazy send coalescing and reusable workers for the qt-process transport."""

from __future__ import annotations

import atexit
import sys
import threading
from typing import Any

from .. import errors as e
from ._qt_process_cancel import CancelControlClient
from ._qt_process_protocol import FastExecuteFrame, Operation, Response, SendSnapshot
from .pq_qt_process import PGresult, _SEND_MEMBERS, _Transport, _raiseRemote, _restore, _wire
from . import pq_qt_process as _pq

_FAST_EXECUTE = "qpsycopg_execute_pending"
_WORKER_POOL_SIZE = 1
_workerPool: list[_Transport] = []
_workerPoolLock = threading.Lock()
_originalRequest = _Transport.request
_originalTransportClose = _Transport.close
_originalPgconnCall = _pq.PGconn._call
_originalPgconnGet = _pq.PGconn._get
_originalPgconnSet = _pq.PGconn._set
_originalPgconnStatus = _pq.PGconn.status.fget


def _ownerRun(self: Any) -> None:
    process = None
    try:
        process = _pq.QProcess()
        process.setProgram(sys.executable)
        process.setArguments([
            "-m", "psycopg.pq._qt_process_worker_pool", self.host, str(self.port), self.token
        ])
        env = _pq.QProcessEnvironment.systemEnvironment()
        env.insert("PSYCOPG_IMPL", _pq._workerImpl())
        process.setProcessEnvironment(env)
        process.setProcessChannelMode(_pq.QProcess.ProcessChannelMode.ForwardedErrorChannel)
        process.start()
        if not process.waitForStarted(10_000):
            raise e.OperationalError(f"couldn't start qpsycopg worker: {process.errorString()}")
        self.started.set()

        while True:
            command = self.commands.get()
            if command == "kill":
                if process.state() != _pq.QProcess.ProcessState.NotRunning:
                    process.kill()
                    process.waitForFinished(5_000)
            elif command == "close":
                if process.state() != _pq.QProcess.ProcessState.NotRunning:
                    process.waitForFinished(2_000)
                    if process.state() != _pq.QProcess.ProcessState.NotRunning:
                        process.kill()
                        process.waitForFinished(5_000)
                return
    except BaseException as error:
        self.startError = error
        self.started.set()
    finally:
        if process is not None and process.state() != _pq.QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished(5_000)


def _patchConnectionHooks() -> None:
    module = sys.modules.get("psycopg.connection_qt_process")
    if module is None or getattr(module, "_qpsycopgWorkerPoolPatched", False):
        return

    originalWait = module._waitProcessOwner
    originalOpenCancel = module._openCancelControl

    def waitProcessOwner(pgconn: object) -> None:
        if getattr(pgconn, "_qpsycopgWorkerReleased", False):
            return
        originalWait(pgconn)

    def cancelRemote(connection: Any, timeout: float) -> None:
        pgconn = connection.pgconn
        originalOpenCancel(pgconn)
        control = getattr(pgconn._transport, "_cancelControl", None)
        if control is None:
            raise e.OperationalError("qpsycopg cancellation channel is not available")
        control.cancel(timeout)

    # Don't create/destroy a cancellation thread for connect()/close() pairs
    # which never run a query. _request() opens it before the first send, while
    # cancelRemote() keeps a correctness fallback for cancel-before-query.
    module._openCancelControl = lambda pgconn: None
    module._cancelRemote = cancelRemote
    module._waitProcessOwner = waitProcessOwner
    module._qpsycopgWorkerPoolPatched = True


def _ensureCancelControl(transport: _Transport) -> None:
    if getattr(transport, "_cancelControl", None) is not None:
        return
    pgconn = transport.connection
    if pgconn is None:
        return
    transport._cancelControl = CancelControlClient.open(pgconn)


def _resetTransport(transport: _Transport) -> None:
    transport.connection = None
    transport._clearCaches()
    transport._statusCache = None
    transport._transactionStatusCache = None
    transport._clientEncodingCache = None
    transport._pendingSend = None


def _discardTransport(transport: _Transport) -> None:
    try:
        _originalTransportClose(transport)
    except Exception:
        pass


def _acquireTransport() -> _Transport:
    _patchConnectionHooks()
    with _workerPoolLock:
        while _workerPool:
            transport = _workerPool.pop()
            if not transport.closed:
                return transport
    return _Transport()


def _recycleTransport(transport: _Transport) -> bool:
    if transport.closed:
        return False
    _resetTransport(transport)
    with _workerPoolLock:
        if len(_workerPool) < _WORKER_POOL_SIZE:
            _workerPool.append(transport)
            return True
    _discardTransport(transport)
    return False


def _closeWorkerPool() -> None:
    with _workerPoolLock:
        transports = _workerPool[:]
        _workerPool.clear()
    for transport in transports:
        _discardTransport(transport)
        transport.owner.thread.join(2.0)


def _connectStart(cls: type[_pq.PGconn], conninfo: bytes) -> _pq.PGconn:
    transport = _acquireTransport()
    try:
        value = _originalRequest(transport, Operation.CONNECT_START, args=(conninfo,))
        assert isinstance(value, cls)
        return value
    except BaseException:
        _discardTransport(transport)
        raise


def _connect(cls: type[_pq.PGconn], conninfo: bytes) -> _pq.PGconn:
    transport = _acquireTransport()
    try:
        value = _originalRequest(transport, Operation.CONNECT, args=(conninfo,))
        assert isinstance(value, cls)
        return value
    except BaseException:
        _discardTransport(transport)
        raise


def _ping(cls: type[_pq.PGconn], conninfo: bytes) -> int:
    transport = _acquireTransport()
    try:
        return int(_originalRequest(transport, Operation.PING, args=(conninfo,)))
    finally:
        _recycleTransport(transport)


def _finish(self: _pq.PGconn) -> None:
    if not self._handle:
        return

    _pq._unregisterHandle(self._handle)
    self._handle = 0
    self._qpsycopgWorkerReleased = False
    transport = self._transport
    control = getattr(transport, "_cancelControl", None)
    if control is not None:
        try:
            control.close()
        finally:
            transport._cancelControl = None
    transport._pendingSend = None

    try:
        _originalRequest(transport, Operation.CALL, 1, "finish")
    except Exception:
        _discardTransport(transport)
        return

    self._qpsycopgWorkerReleased = _recycleTransport(transport)


def _ensurePgconnOpen(self: _pq.PGconn) -> None:
    if not self._handle:
        raise e.OperationalError("qpsycopg connection is closed")


def _pgconnCall(self: _pq.PGconn, member: str, *args: Any, **kwargs: Any) -> Any:
    _ensurePgconnOpen(self)
    return _originalPgconnCall(self, member, *args, **kwargs)


def _pgconnGet(self: _pq.PGconn, member: str) -> Any:
    _ensurePgconnOpen(self)
    return _originalPgconnGet(self, member)


def _pgconnSet(self: _pq.PGconn, member: str, value: Any) -> None:
    _ensurePgconnOpen(self)
    _originalPgconnSet(self, member, value)


def _pgconnStatus(self: _pq.PGconn) -> int:
    if not self._handle:
        return int(_pq.ConnStatus.BAD)
    assert _originalPgconnStatus is not None
    return int(_originalPgconnStatus(self))


def _fastExecute(
    self: _Transport,
    timeout: Any,
    sendMember: str,
    sendArgs: tuple[Any, ...],
    sendKwargs: tuple[tuple[str, Any], ...],
) -> Any:
    """Run the coalesced send/execute hot path with a compact wire message."""
    if self.closed:
        raise e.OperationalError("qpsycopg worker process is closed")

    self.requestId += 1
    request = FastExecuteFrame(
        float(timeout), sendMember, _wire(sendArgs), tuple((name, _wire(value)) for name, value in sendKwargs)
    )
    try:
        _pq.sendFrame(self.sock, request)
        response = _pq.recvFrame(self.sock)
    except (EOFError, OSError, ValueError) as error:
        self._markBroken()
        raise e.OperationalError("qpsycopg worker process disconnected") from error

    notices = ()
    if isinstance(response, SendSnapshot):
        value = response
    elif isinstance(response, Response) and response.requestId == 0:
        notices = response.notices
        if response.error is not None:
            self._clearQueryCache(1)
            self._clearConnectionState()
            _raiseRemote(response.error)
        value = response.value
        if not isinstance(value, SendSnapshot):
            self._markBroken()
            raise e.InternalError("missing fast execute response value")
    else:
        self._markBroken()
        raise e.InternalError("invalid fast execute response from qpsycopg worker")

    if self.connection is not None and self.connection.notice_handler:
        for notice in notices:
            self.connection.notice_handler(PGresult(notice))

    self._updateConnectionState(value)
    state = value.value[0] if isinstance(value.value, tuple) and value.value else None
    if state != "done":
        self._flushCache[1] = value.flush
        self._busyCache[1] = value.busy
    return _restore(value.value, self)


def _request(
    self: _Transport,
    operation: Operation,
    objectId: int = 0,
    member: str = "",
    args: tuple[Any, ...] = (),
    kwargs: tuple[tuple[str, Any], ...] = (),
) -> Any:
    with self.lock:
        pending = getattr(self, "_pendingSend", None)
        if pending is not None:
            sendObjectId, sendMember, sendArgs, sendKwargs = pending
            self._pendingSend = None

            if operation == Operation.CALL and objectId == sendObjectId == 1 and member == _FAST_EXECUTE and args:
                self._clearQueryCache(objectId)
                return _fastExecute(self, args[0], sendMember, sendArgs, sendKwargs)

            _originalRequest(self, Operation.CALL, sendObjectId, sendMember, sendArgs, sendKwargs)

        if operation == Operation.CALL and objectId == 1 and member in _SEND_MEMBERS:
            _ensureCancelControl(self)
            self._clearQueryCache(objectId)
            self._clearConnectionState()
            self._pendingSend = (objectId, member, args, kwargs)
            return None

        return _originalRequest(self, operation, objectId, member, args, kwargs)


_pq._ProcessOwner._run = _ownerRun
_pq.PGconn.connect_start = classmethod(_connectStart)
_pq.PGconn.connect = classmethod(_connect)
_pq.PGconn.ping = classmethod(_ping)
_pq.PGconn.finish = _finish
_pq.PGconn._call = _pgconnCall
_pq.PGconn._get = _pgconnGet
_pq.PGconn._set = _pgconnSet
_pq.PGconn.status = property(_pgconnStatus)
_Transport.request = _request  # type: ignore[method-assign]
atexit.register(_closeWorkerPool)
