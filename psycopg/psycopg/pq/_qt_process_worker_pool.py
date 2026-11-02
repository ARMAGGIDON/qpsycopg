"""Worker entry point supporting safe reuse between PostgreSQL sessions."""

from __future__ import annotations

import os
import socket
import sys
from time import perf_counter, thread_time_ns
from typing import Any

from psycopg import pq

from .. import errors as e
from . import _qt_process_worker as _worker
from ._qt_process_pipe import connectPipe
from ._qt_process_protocol import Operation, RemoteError, Request, Response, ResultSnapshot, SendSnapshot
from ._qt_process_protocol import recvFrame, sendFrame

try:
    from psycopg_c._psycopg import qpsycopg_wait_socket as _nativeWaitSocket
except (ImportError, AttributeError):
    _nativeWaitSocket = None
if sys.platform != "win32" or os.environ.get("QPSYCOPG_NATIVE_WAIT", "0") == "0":
    _nativeWaitSocket = None


def _addCpu(profile: dict[str, float | int] | None, key: str, started: int) -> None:
    if profile is not None:
        profile[f"{key}_cpu_ns"] = profile.get(f"{key}_cpu_ns", 0) + thread_time_ns() - started
        profile[f"{key}_calls"] = profile.get(f"{key}_calls", 0) + 1


def _snapshotResult(result: Any, profile: dict[str, float | int] | None = None) -> ResultSnapshot:
    """Snapshot PGresult using the native metadata packer when available."""
    totalStart = perf_counter() if profile is not None else 0.0
    totalCpuStart = thread_time_ns() if profile is not None else 0
    snapshotMeta = getattr(result, "_qpsycopg_snapshot_meta", None)
    if snapshotMeta is not None:
        status, ntuples, nfields, columns, binaryTuples, paramTypes, commandStatus, commandTuples, oidValue = snapshotMeta()
        if profile is not None:
            profile["snapshot_native_meta_calls"] = profile.get("snapshot_native_meta_calls", 0) + 1
    else:
        status = result.status
        nfields = result.nfields
        ntuples = result.ntuples
        fname, ftable, ftablecol = result.fname, result.ftable, result.ftablecol
        fformat, ftype, fmod, fsize = result.fformat, result.ftype, result.fmod, result.fsize
        columns = tuple(
            (fname(i), ftable(i), ftablecol(i), fformat(i), ftype(i), fmod(i), fsize(i)) for i in range(nfields)
        )
        binaryTuples = result.binary_tuples
        paramTypes = tuple(result.param_type(i) for i in range(result.nparams))
        commandStatus = result.command_status
        commandTuples = result.command_tuples
        oidValue = result.oid_value

    packStart = perf_counter() if profile is not None else 0.0
    packCpuStart = thread_time_ns() if profile is not None else 0
    values = _worker._packResultValues(result, ntuples, nfields, profile)
    if profile is not None:
        profile["snapshot_values_seconds"] = profile.get("snapshot_values_seconds", 0) + perf_counter() - packStart
        profile["snapshot_values_cpu_ns"] = profile.get("snapshot_values_cpu_ns", 0) + thread_time_ns() - packCpuStart
        profile["snapshot_cells"] = profile.get("snapshot_cells", 0) + ntuples * nfields

    if status in _worker._SUCCESS_RESULTS:
        errorMessage = b""
        errorFields: tuple[tuple[int, bytes], ...] = ()
    else:
        errorMessage = result.error_message
        errorField = result.error_field
        errorFields = tuple(
            (code, value)
            for field in pq.DiagnosticField
            if (value := errorField(code := int(field))) is not None
        )

    snapshot = ResultSnapshot(
        status=int(status),
        errorMessage=errorMessage,
        errorFields=errorFields,
        columns=columns,
        rowCount=ntuples,
        columnCount=nfields,
        binaryTuples=binaryTuples,
        values=values,
        paramTypes=paramTypes,
        commandStatus=commandStatus,
        commandTuples=commandTuples,
        oidValue=oidValue,
    )
    if profile is not None:
        profile["snapshot_total_seconds"] = profile.get("snapshot_total_seconds", 0) + perf_counter() - totalStart
        profile["snapshot_total_cpu_ns"] = profile.get("snapshot_total_cpu_ns", 0) + thread_time_ns() - totalCpuStart
        profile["snapshot_results"] = profile.get("snapshot_results", 0) + 1
    return snapshot


_worker._snapshotResult = _snapshotResult


def _refreshesParameters(value: Any) -> bool:
    if not isinstance(value, tuple) or len(value) < 2 or not isinstance(value[1], tuple):
        return False
    return any(
        isinstance(result, ResultSnapshot) and result.commandStatus in (b"SET", b"RESET", b"DISCARD")
        for result in value[1]
    )


def _completedHealthy(value: Any) -> bool:
    if not isinstance(value, tuple) or len(value) < 2 or value[0] != "done" or not isinstance(value[1], tuple):
        return False
    return all(
        not isinstance(result, ResultSnapshot) or result.status != int(pq.ExecStatus.FATAL_ERROR)
        for result in value[1]
    )


class Worker(_worker.Worker):
    def __init__(self, sock: Any):
        super().__init__(sock)
        self._clientEncoding: bytes | None = None
        self._snapshotStatus: int | None = None

    def run(self) -> None:
        while True:
            started = thread_time_ns() if self.profile is not None else 0
            request = recvFrame(self.sock)
            _addCpu(self.profile, "worker_recv_frame", started)
            if not isinstance(request, Request):
                raise TypeError(f"unexpected worker request: {type(request)!r}")

            stop = request.operation == Operation.CLOSE
            try:
                started = thread_time_ns() if self.profile is not None else 0
                try:
                    value = self._handle(request)
                finally:
                    _addCpu(self.profile, "worker_handle", started)

                started = thread_time_ns() if self.profile is not None else 0
                try:
                    stop |= self._connectionBroken()
                finally:
                    _addCpu(self.profile, "worker_connection_check", started)

                response = Response(requestId=request.requestId, value=value, notices=tuple(self.notices))
            except BaseException as error:
                started = thread_time_ns() if self.profile is not None else 0
                try:
                    stop |= self._connectionBroken()
                finally:
                    _addCpu(self.profile, "worker_connection_check", started)
                response = Response(
                    requestId=request.requestId,
                    error=RemoteError(module=type(error).__module__, name=type(error).__name__, message=str(error)),
                    notices=tuple(self.notices),
                )
            finally:
                self.notices.clear()

            started = thread_time_ns() if self.profile is not None else 0
            sendFrame(self.sock, response, self.profile, "worker_response")
            _addCpu(self.profile, "worker_send_frame", started)
            if stop:
                self._closeObjects()
                return

    def _closeCancelControl(self) -> None:
        control = self.cancelControl
        if control is None:
            return
        control.thread.join(5.0)
        if control.thread.is_alive():
            raise e.OperationalError("qpsycopg cancellation control did not stop")
        self.cancelControl = None

    def _setConnection(self, conn: Any) -> None:
        self._clientEncoding = None
        super()._setConnection(conn)

    def _sendSnapshot(self, obj: Any, value: Any, flush: int, busy: int) -> SendSnapshot:
        started = thread_time_ns() if self.profile is not None else 0
        try:
            status = int(pq.ConnStatus.OK) if _completedHealthy(value) else int(obj.status)
            self._snapshotStatus = status
            transactionStatus = int(obj.transaction_status)
            clientEncoding: bytes | None = None
            if status == int(pq.ConnStatus.OK) and (self._clientEncoding is None or _refreshesParameters(value)):
                encoding = obj.parameter_status(b"client_encoding")
                self._clientEncoding = bytes(encoding) if encoding is not None else b""
                clientEncoding = self._clientEncoding or None
            return SendSnapshot(value, flush, busy, status, transactionStatus, clientEncoding)
        finally:
            _addCpu(self.profile, "worker_send_snapshot", started)

    def _executePending(self, obj: Any, timeout: float) -> SendSnapshot:
        started = thread_time_ns() if self.profile is not None else 0
        try:
            return super()._executePending(obj, timeout)
        finally:
            _addCpu(self.profile, "worker_execute_pending", started)

    def _wait(self, obj: Any, mask: int, timeout: float | None) -> int:
        started = thread_time_ns() if self.profile is not None else 0
        try:
            if _nativeWaitSocket is None:
                return super()._wait(obj, mask, timeout)

            wallStart = perf_counter() if self.profile is not None else 0.0
            ready = int(_nativeWaitSocket(int(obj.socket), mask, timeout))
            if self.profile is not None:
                self._profile("libpq_wait_seconds", perf_counter() - wallStart)
                self._profile("libpq_wait_calls", 1)
            return ready
        finally:
            _addCpu(self.profile, "worker_wait", started)

    def _connectionBroken(self) -> bool:
        if self._snapshotStatus is not None:
            return self._snapshotStatus == int(pq.ConnStatus.BAD)
        return super()._connectionBroken()

    def _handle(self, request: Request) -> Any:
        self._snapshotStatus = None
        if request.operation in (Operation.CONNECT, Operation.CONNECT_START):
            self._closeCancelControl()
        elif request.operation == Operation.CALL and request.objectId == 1 and request.member == "finish":
            self._closeCancelControl()
            conn = self.objects.pop(1, None)
            if conn is not None:
                conn.finish()
            self._clientEncoding = None
            return None

        value = super()._handle(request)
        if request.operation == Operation.CALL and request.objectId == 1 and request.member == "set_client_encoding":
            self._clientEncoding = None
        return value


def _connectWorker(host: str, port: int) -> Any:
    if host.startswith("pipe:"):
        return connectPipe(host[5:])
    if sys.platform == "win32" and hasattr(socket, "SIO_LOOPBACK_FAST_PATH"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.ioctl(socket.SIO_LOOPBACK_FAST_PATH, 1)
            sock.settimeout(10.0)
            sock.connect((host, port))
            return sock
        except OSError:
            sock.close()
    return socket.create_connection((host, port), timeout=10.0)


def _sendToken(sock: Any, token: bytes) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    sock.sendall(token)


def main() -> int:
    if len(sys.argv) != 4:
        return 2

    host, port, tokenHex = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    sock = _connectWorker(host, port)
    sock.settimeout(None)
    _sendToken(sock, bytes.fromhex(tokenHex))
    try:
        Worker(sock).run()
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
