"""Lazy send coalescing for the qt-process pq transport."""

from __future__ import annotations

from typing import Any

from ._qt_process_protocol import Operation
from .pq_qt_process import _SEND_MEMBERS, _Transport

_FAST_EXECUTE = "qpsycopg_execute_pending"
_originalRequest = _Transport.request


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

            if (
                operation == Operation.CALL
                and objectId == sendObjectId
                and member == _FAST_EXECUTE
                and args
            ):
                self._clearQueryCache(objectId)
                return _originalRequest(
                    self,
                    operation,
                    objectId,
                    member,
                    (args[0], sendMember, sendArgs, sendKwargs),
                    kwargs,
                )

            _originalRequest(
                self,
                Operation.CALL,
                sendObjectId,
                sendMember,
                sendArgs,
                sendKwargs,
            )

        if operation == Operation.CALL and objectId == 1 and member in _SEND_MEMBERS:
            self._clearQueryCache(objectId)
            self._pendingSend = (objectId, member, args, kwargs)
            return None

        return _originalRequest(self, operation, objectId, member, args, kwargs)


_Transport.request = _request  # type: ignore[method-assign]
