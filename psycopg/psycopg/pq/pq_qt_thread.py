# mypy: disable-error-code="no-any-return, import-not-found"
"""Thread-backed qpsycopg implementation reusing the qt-process proxy API."""

from __future__ import annotations

import importlib
import os
import queue
import threading
from typing import Any, cast

from .. import errors as e
from . import abc
from . import pq_qt_process as _base
from ._enums import ConnStatus
from ._qt_process_protocol import Operation, RemoteError, Request, Response
from ._qt_process_protocol import ResultReadSnapshot, SendSnapshot, WaitSnapshot


def _loadWorkerPq() -> Any:
    requested = os.environ.get("QPSYCOPG_WORKER_IMPL", "binary").lower()
    if requested == "binary":
        return importlib.import_module("psycopg_binary.pq")
    if requested == "c":
        return importlib.import_module("psycopg_c.pq")
    if requested == "python":
        from . import pq_ctypes

        return pq_ctypes
    raise e.InterfaceError(f"unknown QPSYCOPG_WORKER_IMPL value: {requested!r}")


_workerPq = _loadWorkerPq()
from . import _qt_process_worker as _workerBase

_workerBase.pq = _workerPq
from . import _qt_process_worker_pool as _workerPool

__impl__ = "qt-thread"
__build_version__ = int(_workerPq.__build_version__)
version = _base.version
Conninfo = _base.Conninfo
Escaping = _base.Escaping
PGconn = _base.PGconn
PGresult = _base.PGresult
PGcancel = _base.PGcancel
PGcancelConn = _base.PGcancelConn
waitRemote = _base.waitRemote

_FAST_EXECUTE = "qpsycopg_execute_pending"


class _ThreadTransport(_base._Transport):
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.requestId = 0
        self.closed = False
        self.connection: PGconn | None = None
        self._flushCache: dict[int, int] = {}
        self._busyCache: dict[int, int] = {}
        self._consumeCache: set[int] = set()
        self._notifyCache: dict[int, list[Any]] = {}
        self._resultCache: dict[int, list[PGresult | None]] = {}
        self._statusCache: int | None = None
        self._transactionStatusCache: int | None = None
        self._clientEncodingCache: bytes | None = None
        self._pendingSend: tuple[int, str, tuple[Any, ...], tuple[tuple[str, Any], ...]] | None = None
        self._requests: queue.SimpleQueue[Request] = queue.SimpleQueue()
        self._responses: queue.SimpleQueue[Response] = queue.SimpleQueue()
        self.owner = self
        self.thread = threading.Thread(target=self._run, name="qpsycopg-worker", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        worker = _workerPool.Worker(None)
        while True:
            request = self._requests.get()
            stop = request.operation == Operation.CLOSE
            try:
                value = worker._handle(request)
                stop |= worker._connectionBroken()
                response = Response(request.requestId, value=value, notices=tuple(worker.notices))
            except BaseException as error:
                stop |= worker._connectionBroken()
                response = Response(
                    requestId=request.requestId,
                    error=RemoteError(type(error).__module__, type(error).__name__, str(error)),
                    notices=tuple(worker.notices),
                )
            finally:
                worker.notices.clear()

            self._responses.put(response)
            if stop:
                try:
                    worker._closeCancelControl()
                except Exception:
                    pass
                worker._closeObjects()
                return

    def _dispatch(self, request: Request) -> Response:
        if not self.thread.is_alive():
            self._markBroken()
            raise e.OperationalError("qpsycopg worker thread stopped")
        self._requests.put(request)
        return self._responses.get()

    def _responseValue(self, request: Request, objectId: int, response: Response) -> Any:
        if response.requestId != request.requestId:
            self._markBroken()
            raise e.InternalError("invalid response from qpsycopg worker thread")

        if self.connection is not None and self.connection.notice_handler:
            for notice in response.notices:
                self.connection.notice_handler(cast(abc.PGresult, PGresult(notice)))

        if response.error is not None:
            self._clearQueryCache(objectId)
            self._clearConnectionState()
            _base._raiseRemote(response.error)

        value = response.value
        if isinstance(value, SendSnapshot):
            self._updateConnectionState(value)
            self._flushCache[objectId] = value.flush
            self._busyCache[objectId] = value.busy
            return _base._restore(value.value, self)

        if isinstance(value, ResultReadSnapshot):
            self._clearConnectionState()
            if value.following:
                self._resultCache[objectId] = [cast(PGresult | None, _base._restore(item, self)) for item in value.following]
            return cast(PGresult, _base._restore(value.value, self))

        if isinstance(value, WaitSnapshot):
            if value.consumed:
                self._clearConnectionState()
                self._consumeCache.add(objectId)
                if value.busy is not None:
                    self._busyCache[objectId] = value.busy
                self._notifyCache[objectId] = [*value.notifies, None]
                if value.results:
                    self._resultCache[objectId] = [cast(PGresult | None, _base._restore(item, self)) for item in value.results]
            return value.ready

        return _base._restore(value, self)

    def _perform(
        self,
        operation: Operation,
        objectId: int = 0,
        member: str = "",
        args: tuple[Any, ...] = (),
        kwargs: tuple[tuple[str, Any], ...] = (),
        requestId: int | None = None,
    ) -> Any:
        if requestId is None:
            self.requestId += 1
            requestId = self.requestId
        request = Request(
            requestId,
            operation,
            objectId,
            member,
            _base._wire(args),
            tuple((name, _base._wire(value)) for name, value in kwargs),
        )
        return self._responseValue(request, objectId, self._dispatch(request))

    def _flushPending(self) -> None:
        pending = self._pendingSend
        if pending is None:
            return
        self._pendingSend = None
        objectId, member, args, kwargs = pending
        self._perform(Operation.CALL, objectId, member, args, kwargs)

    def _fastExecute(
        self,
        objectId: int,
        timeout: Any,
        sendMember: str,
        sendArgs: tuple[Any, ...],
        sendKwargs: tuple[tuple[str, Any], ...],
    ) -> Any:
        self.requestId += 1
        request = Request(
            0,
            Operation.CALL,
            objectId,
            _FAST_EXECUTE,
            (float(timeout), sendMember, _base._wire(sendArgs), tuple((name, _base._wire(value)) for name, value in sendKwargs)),
        )
        return self._responseValue(request, objectId, self._dispatch(request))

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
                raise e.OperationalError("qpsycopg worker thread is closed")

            pending = self._pendingSend
            if pending is not None:
                if operation == Operation.CALL and objectId == pending[0] == 1 and member == _FAST_EXECUTE and args:
                    self._pendingSend = None
                    self._clearQueryCache(objectId)
                    return self._fastExecute(objectId, args[0], pending[1], pending[2], pending[3])
                self._flushPending()

            if operation == Operation.CALL:
                cached, value = self._cachedCall(objectId, member)
                if cached:
                    return value
                if member in _base._SEND_MEMBERS:
                    self._clearQueryCache(objectId)
                    self._statusCache = None
                    self._transactionStatusCache = None
                    self._pendingSend = (objectId, member, args, kwargs)
                    return None

            return self._perform(operation, objectId, member, args, kwargs)

    def _markBroken(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._statusCache = int(ConnStatus.BAD)
        self._transactionStatusCache = None
        self._clientEncodingCache = None
        self._clearCaches()

    def kill(self) -> None:
        self._markBroken()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self._pendingSend = None
            try:
                self.requestId += 1
                request = Request(self.requestId, Operation.CLOSE)
                response = self._dispatch(request)
                if response.requestId != request.requestId:
                    raise e.InternalError("invalid close response from qpsycopg worker thread")
            finally:
                self.closed = True
                self._statusCache = int(ConnStatus.BAD)
                self._transactionStatusCache = None
                self._clientEncodingCache = None
                self._clearCaches()
        self.thread.join(5.0)


_base._Transport = _ThreadTransport
