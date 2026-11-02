"""Worker entry point supporting safe reuse between PostgreSQL sessions."""

from __future__ import annotations

import socket
import sys
from time import perf_counter
from typing import Any

from psycopg import pq

from .. import errors as e
from . import _qt_process_worker as _worker
from ._qt_process_protocol import Operation, Request, ResultSnapshot, SendSnapshot, sendToken


def _snapshotResult(result: Any, profile: dict[str, float | int] | None = None) -> ResultSnapshot:
    """Snapshot PGresult using the native metadata packer when available."""
    totalStart = perf_counter() if profile is not None else 0.0
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
    values = _worker._packResultValues(result, ntuples, nfields, profile)
    if profile is not None:
        profile["snapshot_values_seconds"] = profile.get("snapshot_values_seconds", 0) + perf_counter() - packStart
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


class Worker(_worker.Worker):
    def __init__(self, sock: socket.socket):
        super().__init__(sock)
        self._clientEncoding: bytes | None = None
        self._snapshotStatus: int | None = None

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
        status = int(obj.status)
        self._snapshotStatus = status
        transactionStatus = int(obj.transaction_status)
        clientEncoding: bytes | None = None
        if status == int(pq.ConnStatus.OK) and (self._clientEncoding is None or _refreshesParameters(value)):
            encoding = obj.parameter_status(b"client_encoding")
            self._clientEncoding = bytes(encoding) if encoding is not None else b""
            clientEncoding = self._clientEncoding or None
        return SendSnapshot(value, flush, busy, status, transactionStatus, clientEncoding)

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


def main() -> int:
    if len(sys.argv) != 4:
        return 2

    host, port, tokenHex = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    sock = socket.create_connection((host, port), timeout=10.0)
    sock.settimeout(None)
    sendToken(sock, bytes.fromhex(tokenHex))
    try:
        Worker(sock).run()
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
