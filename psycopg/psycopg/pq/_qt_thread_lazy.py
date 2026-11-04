"""Lazy cancellation setup for the qt-thread backend."""

from __future__ import annotations

from typing import Any

from .. import connection_qt_process as _connection
from .. import errors as e
from . import pq_qt_process as _base
from . import pq_qt_thread as _thread
from ._qt_process_cancel import CancelControlClient
from ._qt_process_protocol import Operation

_Transport = _thread._ThreadTransport
_originalRequest = _Transport.request


def _ensureCancelControl(transport: _Transport) -> None:
    if getattr(transport, "_cancelControl", None) is not None:
        return
    pgconn = transport.connection
    if pgconn is None:
        return
    transport._cancelControl = CancelControlClient.open(pgconn)


def _request(
    self: _Transport,
    operation: Operation,
    objectId: int = 0,
    member: str = "",
    args: tuple[Any, ...] = (),
    kwargs: tuple[tuple[str, Any], ...] = (),
) -> Any:
    if operation == Operation.CALL and objectId == 1 and member in _base._SEND_MEMBERS:
        _ensureCancelControl(self)
    return _originalRequest(self, operation, objectId, member, args, kwargs)


def _cancelRemote(connection: Any, timeout: float) -> None:
    pgconn = connection.pgconn
    _ensureCancelControl(pgconn._transport)
    control = getattr(pgconn._transport, "_cancelControl", None)
    if control is None:
        raise e.OperationalError("qpsycopg cancellation channel is not available")
    control.cancel(timeout)


# Connection.connect() normally opens cancellation eagerly. qt-thread can defer
# it because the transport can prepare the independent cancel channel immediately
# before the first query is handed to the worker thread.
_connection._openCancelControl = lambda pgconn: None
_connection._cancelRemote = _cancelRemote
_Transport.request = _request  # type: ignore[method-assign]
