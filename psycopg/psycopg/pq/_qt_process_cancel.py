"""Dedicated cancellation channel for the Qt process pq backend."""

from __future__ import annotations

import importlib
import os
import sys
import secrets
import select
import socket
import threading
from time import monotonic
from typing import Any
from collections.abc import Callable

from .. import errors as e
from .. import pq
from ._qt_process_protocol import Operation, RemoteError, Request, Response
from ._qt_process_protocol import recvFrame, recvToken, sendFrame, sendToken
from ._qt_process_pipe import PipeListenerSocket, connectPipe

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
    def open(
        cls, pgconn: Any, *, deadline: float | None = None,
        request_open: Callable[[float], Any] | None = None,
    ) -> CancelControlClient:
        deadline = monotonic() + 10.0 if deadline is None else deadline
        host, port, token = (
            pgconn._call(CONTROL_OPEN) if request_open is None
            else request_open(deadline)
        )
        remaining = _remaining(deadline)
        sock = (connectPipe(host[5:], timeout=remaining) if host.startswith("pipe:")
                else socket.create_connection((host, int(port)), timeout=remaining))
        try:
            sock.settimeout(_remaining(deadline))
            sendToken(sock, bytes(token))
            # Publication requires the worker to have authenticated this peer.
            sock.settimeout(_remaining(deadline))
            if not secrets.compare_digest(recvToken(sock), bytes(token)):
                raise e.OperationalError("qpsycopg cancellation authentication failed")
            sock.settimeout(None)
            return cls(sock)
        except BaseException:
            sock.close()
            raise

    def cancel(self, timeout: float) -> None:
        if timeout <= 0:
            raise ValueError("cancel timeout must be positive")
        deadline = monotonic() + timeout
        if not self.lock.acquire(timeout=timeout):
            raise e._WaitTimeout("cancellation channel is busy")
        try:
            if self.closed:
                raise e.OperationalError("qpsycopg cancellation channel is closed")
            self.requestId += 1
            request = Request(
                self.requestId,
                Operation.CALL,
                1,
                CONTROL_CANCEL,
                (_remaining(deadline),),
            )
            try:
                # The control socket has its own deadline, independent of SQL
                # recv() and the QProcess owner command consumer.
                self.sock.settimeout(_remaining(deadline))
                sendFrame(self.sock, request)
                self.sock.settimeout(_remaining(deadline))
                response = recvFrame(self.sock)
            except (TimeoutError, e._WaitTimeout) as error:
                self._markClosed()
                raise e._WaitTimeout("cancellation response timed out") from error
            except (EOFError, OSError, ValueError) as error:
                self._markClosed()
                raise e.OperationalError("qpsycopg cancellation channel disconnected") from error
            finally:
                if not self.closed:
                    self.sock.settimeout(None)
            self._checkResponse(request, response)
        finally:
            self.lock.release()

    def abort(self) -> bool:
        """Retire without waiting for an in-flight cancel. Return idle/healthy."""
        idle = self.lock.acquire(blocking=False)
        healthy = idle and not self.closed
        try:
            self.closed = True
            try:
                if hasattr(self.sock, "abort"):
                    self.sock.abort()
                else:
                    self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            return healthy
        finally:
            if idle:
                self.lock.release()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            try:
                self.requestId += 1
                request = Request(self.requestId, Operation.CLOSE)
                self.sock.settimeout(0.5)
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



def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise e._WaitTimeout("cancellation deadline expired")
    return remaining


class CancelControlLifecycle:
    """Single-flight control channel for one PGconn generation.

    The condition never spans SQL RPC or IPC open. A first send must call
    ensure() before taking the SQL RPC lock. READY readers never take it.
    """

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.state = "EMPTY"
        self.client: CancelControlClient | None = None
        self.reusable = True

    @staticmethod
    def _valid(pgconn: Any) -> bool:
        transport = pgconn._transport
        owner = transport.owner
        return (
            bool(pgconn._handle) and transport.connection is pgconn
            and not transport.closed and not owner._killRequested
            and not owner._closeRequested and not owner._exitEvent.is_set()
        )

    def ensure(self, pgconn: Any, timeout: float = 10.0) -> CancelControlClient:
        if timeout <= 0:
            raise e._WaitTimeout("cancellation initialization deadline expired")
        deadline = monotonic() + timeout
        transport = pgconn._transport
        with self.condition:
            while True:
                if not self._valid(pgconn) or self.state in ("FAILED", "RETIRED"):
                    raise e.OperationalError("qpsycopg cancellation generation retired")
                if self.state == "READY":
                    assert self.client is not None
                    if not self.client.closed:
                        return self.client
                    # A disconnected channel cannot reconnect to a single-peer listener.
                    self.state = "FAILED"
                    self.reusable = False
                    break
                if self.state == "EMPTY":
                    self.state = "OPENING"
                    break
                self.condition.wait(_remaining(deadline))
            opening = self.state == "OPENING"

        if not opening:
            transport._markBroken()
            raise e.OperationalError("qpsycopg cancellation channel disconnected")

        client = None
        try:
            client = CancelControlClient.open(
                pgconn, deadline=deadline, request_open=transport._requestCancelOpen,
            )
            with self.condition:
                # Owner kill/exit is linearized with publication, never with SQL.
                with transport.owner._stateLock:
                    if self.state != "OPENING" or not self._valid(pgconn) or client.closed:
                        raise e.OperationalError("qpsycopg cancellation publication revoked")
                    _remaining(deadline)
                    self.client = client
                    transport._cancelControl = client
                    self.state = "READY"
                self.condition.notify_all()
                return client
        except BaseException:
            if client is not None:
                client.abort()
            with self.condition:
                if self.state != "RETIRED":
                    self.state = "FAILED"
                self.reusable = False
                self.condition.notify_all()
            # CONTROL_OPEN may have succeeded on the worker even if its reply
            # or authentication failed. Never try a second open on that owner.
            if transport.connection is pgconn:
                transport._markBroken()
            raise

    def retire(self, pgconn: Any) -> bool:
        """Reject late publication and invalidate client before pool reuse."""
        with self.condition:
            if self.state == "RETIRED":
                return self.reusable
            if self.state in ("OPENING", "FAILED"):
                self.reusable = False
            self.state = "RETIRED"
            client, self.client = self.client, None
            if pgconn._transport.connection is pgconn:
                pgconn._transport._cancelControl = None
            if client is not None and not client.abort():
                self.reusable = False
            self.condition.notify_all()
            return self.reusable


class CancelControlServer:
    """Worker-side owner of a reusable PGcancelConn and its control socket."""

    def __init__(self, conn: Any) -> None:
        # PQcancelCreate touches PGconn, so create the object on the worker's
        # main thread before handing only PGcancelConn to the control thread.
        self.cancelConn = conn.cancel_conn()
        defaultIpc = "pipe" if sys.platform == "win32" else "tcp"
        ipc = os.environ.get("QPSYCOPG_IPC", defaultIpc).lower()
        if ipc == "pipe":
            if sys.platform != "win32":
                raise e.NotSupportedError("QPSYCOPG_IPC=pipe is Windows-only")
            self.listener = PipeListenerSocket()
            # qt-thread hosts the cancel listener in the current process;
            # qt-process hosts it in a QProcess child. The parent explicitly
            # publishes its PID into the child's environment.
            parentPid = os.environ.get("QPSYCOPG_QPROCESS_PARENT_PID")
            self.listener.expected_pid = int(parentPid) if parentPid else os.getpid()
        elif ipc == "tcp":
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        else:
            raise e.InterfaceError(f"unknown QPSYCOPG_IPC value: {ipc!r}")
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
            sendToken(sock, self.token)
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
        if timeout <= 0:
            raise ValueError("cancel timeout must be positive")
        deadline = monotonic() + timeout
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
