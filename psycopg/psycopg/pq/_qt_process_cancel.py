"""Dedicated cancellation channel for the Qt process pq backend."""

from __future__ import annotations

import importlib
import secrets
import select
import socket
import threading
from time import monotonic
from typing import Any

from .. import errors as e
from .. import pq
from ._qt_process_protocol import Operation, RemoteError, Request, Response
from ._qt_process_protocol import recvFrame, recvToken, sendFrame, sendToken

CONTROL_OPEN = "qpsycopg_cancel_control_open"
CONTROL_CANCEL = "qpsycopg_cancel_control"


class CancelControlClient:
    """Parent-side client using a socket independent from normal pq traffic."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lock = threading.Lock()
        self.requestId = 0
        self.closed = False

    @classmethod
    def open(cls, pgconn: Any) -> CancelControlClient:
        host, port, token = pgconn._call(CONTROL_OPEN)
        sock = socket.create_connection((host, int(port)), timeout=10.0)
        try:
            sock.settimeout(10.0)
            sendToken(sock, bytes(token))
            sock.settimeout(None)
            return cls(sock)
        except BaseException:
            sock.close()
            raise

    def cancel(self, timeout: float) -> None:
        with self.lock:
            if self.closed:
                raise e.OperationalError("qpsycopg cancellation channel is closed")
            self.requestId += 1
            request = Request(
                self.requestId,
                Operation.CALL,
                1,
                CONTROL_CANCEL,
                (timeout,),
            )
            try:
                sendFrame(self.sock, request)
                response = recvFrame(self.sock)
            except (EOFError, OSError, ValueError) as error:
                self._markClosed()
                raise e.OperationalError("qpsycopg cancellation channel disconnected") from error
            self._checkResponse(request, response)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            try:
                self.requestId += 1
                request = Request(self.requestId, Operation.CLOSE)
                sendFrame(self.sock, request)
                response = recvFrame(self.sock)
                self._checkResponse(request, response)
            except Exception:
                pass
            finally:
                self._markClosed()

    def _checkResponse(self, request: Request, response: Any) -> None:
        if not isinstance(response, Response) or response.requestId != request.requestId:
            self._markClosed()
            raise e.InternalError("invalid qpsycopg cancellation response")
        if response.error is not None:
            _raiseRemote(response.error)

    def _markClosed(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class CancelControlServer:
    """Worker-side owner of a reusable PGcancelConn and its control socket."""

    def __init__(self, conn: Any) -> None:
        # PQcancelCreate touches PGconn, so create the object on the worker's
        # main thread before handing only PGcancelConn to the control thread.
        self.cancelConn = conn.cancel_conn()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(10.0)
        host, port = self.listener.getsockname()
        self.token = secrets.token_bytes(32)
        self.info = (host, port, self.token)
        self.thread = threading.Thread(
            target=self._run,
            name="qpsycopg-cancel-control",
            daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        sock: socket.socket | None = None
        try:
            sock, _ = self.listener.accept()
            sock.settimeout(10.0)
            if not secrets.compare_digest(recvToken(sock), self.token):
                raise e.OperationalError("qpsycopg cancellation authentication failed")
            sock.settimeout(None)

            used = False
            while True:
                request = recvFrame(sock)
                if not isinstance(request, Request):
                    raise TypeError(f"unexpected cancellation request: {type(request)!r}")
                if request.operation == Operation.CLOSE:
                    sendFrame(sock, Response(request.requestId))
                    return

                try:
                    if (
                        request.operation != Operation.CALL
                        or request.objectId != 1
                        or request.member != CONTROL_CANCEL
                    ):
                        raise ValueError("unsupported cancellation control request")
                    if used:
                        self.cancelConn.reset()
                    used = True
                    self._cancel(float(request.args[0]))
                    response = Response(request.requestId)
                except BaseException as error:
                    response = Response(
                        requestId=request.requestId,
                        error=RemoteError(
                            module=type(error).__module__,
                            name=type(error).__name__,
                            message=str(error),
                        ),
                    )
                sendFrame(sock, response)
        except (EOFError, OSError):
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.listener.close()
            self.cancelConn.finish()

    def _cancel(self, timeout: float) -> None:
        deadline = monotonic() + timeout if timeout else None
        self.cancelConn.start()
        while True:
            status = self.cancelConn.poll()
            if status == pq.PollingStatus.OK:
                return
            if status == pq.PollingStatus.FAILED:
                raise e.OperationalError(
                    f"cancellation failed: {self.cancelConn.get_error_message()}"
                )
            if status not in (pq.PollingStatus.READING, pq.PollingStatus.WRITING):
                raise e.InternalError(f"unexpected poll status: {status}")

            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise e._WaitTimeout("wait timeout expired")
            fd = self.cancelConn.socket
            readers = [fd] if status == pq.PollingStatus.READING else []
            writers = [fd] if status == pq.PollingStatus.WRITING else []
            readable, writable, _ = select.select(readers, writers, [], remaining)
            if not readable and not writable:
                raise e._WaitTimeout("wait timeout expired")


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
