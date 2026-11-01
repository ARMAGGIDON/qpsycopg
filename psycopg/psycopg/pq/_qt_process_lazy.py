"""Lazy send coalescing for the qt-process pq transport."""

from __future__ import annotations

from typing import Any

from .. import errors as e
from ._qt_process_protocol import FastExecuteFrame, Operation, Response, SendSnapshot
from .pq_qt_process import PGresult, _SEND_MEMBERS, _Transport, _raiseRemote, _restore, _wire
from . import pq_qt_process as _pq

_FAST_EXECUTE = "qpsycopg_execute_pending"
_originalRequest = _Transport.request


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

    # Keep requestId as a local transport counter used by profiling/benchmarks.
    # The synchronous fast frame itself doesn't need an id on the wire.
    self.requestId += 1
    request = FastExecuteFrame(
        float(timeout),
        sendMember,
        _wire(sendArgs),
        tuple((name, _wire(value)) for name, value in sendKwargs),
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
            self._clearQueryCache(objectId)
            self._clearConnectionState()
            self._pendingSend = (objectId, member, args, kwargs)
            return None

        return _originalRequest(self, operation, objectId, member, args, kwargs)


_Transport.request = _request  # type: ignore[method-assign]
