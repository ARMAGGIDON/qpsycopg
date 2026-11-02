"""Worker process for the experimental Qt-isolated pq implementation."""

from __future__ import annotations

import os
import select
import socket
import struct
import sys
from time import monotonic, perf_counter
from typing import Any

from psycopg import errors as e
from psycopg import pq

from ._qt_process_cancel import CONTROL_OPEN, CancelControlServer
from ._qt_process_protocol import Operation, PackedValues, RemoteError, RemoteKind
from ._qt_process_protocol import RemoteRef, Request, Response, ResultSnapshot
from ._qt_process_protocol import ResultReadSnapshot, SendSnapshot, WaitSnapshot
from ._qt_process_protocol import recvFrame, sendFrame, sendToken

READ = 1
WRITE = 2
_VALUE_OFFSET = struct.Struct("=Q")
_SEND_MEMBERS = {
    "send_query",
    "send_query_params",
    "send_prepare",
    "send_query_prepared",
    "send_describe_prepared",
    "send_describe_portal",
    "send_close_prepared",
    "send_close_portal",
}
_SUCCESS_RESULTS = {
    pq.ExecStatus.EMPTY_QUERY,
    pq.ExecStatus.COMMAND_OK,
    pq.ExecStatus.TUPLES_OK,
}
_COPY_RESULTS = {
    pq.ExecStatus.COPY_IN,
    pq.ExecStatus.COPY_OUT,
    pq.ExecStatus.COPY_BOTH,
}
_ERROR_RESULTS = {
    pq.ExecStatus.BAD_RESPONSE,
    pq.ExecStatus.NONFATAL_ERROR,
    pq.ExecStatus.FATAL_ERROR,
}
_FAST_EXECUTE = "qpsycopg_execute_pending"
_FAST_CANCEL = "qpsycopg_cancel"
_PROFILE_TAKE = "qpsycopg_profile_take"
_WAIT_DRAIN_GRACE = 0.010


class Worker:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.objects: dict[int, Any] = {}
        self.nextObjectId = 2
        self.notices: list[ResultSnapshot] = []
        self.cancelControl: CancelControlServer | None = None
        self.profile: dict[str, float | int] | None = {} if os.environ.get("QPSYCOPG_PROFILE_PHASES") else None

    def _profile(self, key: str, value: float | int) -> None:
        if self.profile is not None:
            self.profile[key] = self.profile.get(key, 0) + value

    def _takeProfile(self) -> dict[str, float | int]:
        if self.profile is None:
            return {}
        value = self.profile
        self.profile = {}
        return value

    def run(self) -> None:
        while True:
            request = recvFrame(self.sock)
            if not isinstance(request, Request):
                raise TypeError(f"unexpected worker request: {type(request)!r}")

            stop = request.operation == Operation.CLOSE
            try:
                value = self._handle(request)
                stop |= self._connectionBroken()
                response = Response(
                    requestId=request.requestId,
                    value=value,
                    notices=tuple(self.notices),
                )
            except BaseException as error:
                stop |= self._connectionBroken()
                response = Response(
                    requestId=request.requestId,
                    error=RemoteError(
                        module=type(error).__module__,
                        name=type(error).__name__,
                        message=str(error),
                    ),
                    notices=tuple(self.notices),
                )
            finally:
                self.notices.clear()

            sendFrame(self.sock, response, self.profile, "worker_response")
            if stop:
                self._closeObjects()
                return

    def _connectionBroken(self) -> bool:
        conn = self.objects.get(1)
        if conn is None:
            return False
        try:
            return bool(conn.status == pq.ConnStatus.BAD)
        except Exception:
            return True

    def _handle(self, request: Request) -> Any:
        op = request.operation
        if op == Operation.CONNECT_START:
            conn = pq.PGconn.connect_start(request.args[0])
            self._setConnection(conn)
            return RemoteRef(RemoteKind.PGCONN, 1)

        if op == Operation.CONNECT:
            conn = pq.PGconn.connect(request.args[0])
            self._setConnection(conn)
            return RemoteRef(RemoteKind.PGCONN, 1)

        if op == Operation.PING:
            return pq.PGconn.ping(request.args[0])

        if op == Operation.CLOSE:
            return None

        obj = self.objects[request.objectId]

        if op == Operation.CALL:
            if request.objectId == 1 and request.member == _PROFILE_TAKE:
                return self._takeProfile()

            if request.objectId == 1 and request.member == CONTROL_OPEN:
                if self.cancelControl is None:
                    self.cancelControl = CancelControlServer(obj)
                return self.cancelControl.info

            if request.objectId == 1 and request.member == _FAST_CANCEL:
                return self._cancelPending(obj, float(request.args[0]))

            if request.objectId == 1 and request.member == _FAST_EXECUTE:
                timeout = float(request.args[0])
                if len(request.args) > 1:
                    sendMember = request.args[1]
                    if sendMember not in _SEND_MEMBERS:
                        raise ValueError(f"unsupported deferred send: {sendMember!r}")
                    getattr(obj, sendMember)(*request.args[2], **dict(request.args[3]))
                return self._executePending(obj, timeout)

            value = getattr(obj, request.member)(*request.args, **dict(request.kwargs))
            if request.objectId == 1 and request.member == "get_result" and value is not None:
                snapshot = _snapshotResult(value, self.profile)
                following: list[ResultSnapshot | None] = []
                if (
                    value.status in _SUCCESS_RESULTS
                    and obj.pipeline_status == pq.PipelineStatus.OFF
                    and obj.status == pq.ConnStatus.OK
                    and not obj.is_busy()
                ):
                    nextResult = obj.get_result()
                    following.append(
                        None if nextResult is None else _snapshotResult(nextResult, self.profile)
                    )
                return ResultReadSnapshot(snapshot, tuple(following))

            if (
                request.objectId == 1
                and request.member in _SEND_MEMBERS
                and obj.pipeline_status == pq.PipelineStatus.OFF
            ):
                return self._sendSnapshot(
                    obj,
                    self._normalize(value),
                    int(obj.flush()),
                    int(obj.is_busy()),
                )
            return self._normalize(value)

        if op == Operation.GET:
            return self._normalize(getattr(obj, request.member))

        if op == Operation.SET:
            setattr(obj, request.member, request.args[0])
            return None

        if op == Operation.WAIT:
            mask = int(request.args[0])
            timeout = request.args[1]
            if request.objectId == 1 and mask & READ:
                return self._waitConnection(obj, mask, timeout)
            return WaitSnapshot(ready=self._wait(obj, mask, timeout))

        if op == Operation.ESCAPE:
            escapingConn: Any = obj if request.objectId else None
            escaping = pq.Escaping(escapingConn)
            return self._normalize(getattr(escaping, request.member)(*request.args))

        raise ValueError(f"unsupported operation: {op!r}")

    def _cancelPending(self, obj: Any, timeout: float) -> None:
        cancelConn = obj.cancel_conn()
        deadline = monotonic() + timeout if timeout else None
        try:
            cancelConn.start()
            while True:
                status = cancelConn.poll()
                if status == pq.PollingStatus.OK:
                    return
                if status == pq.PollingStatus.FAILED:
                    raise e.OperationalError(
                        f"cancellation failed: {cancelConn.get_error_message()}"
                    )
                if status not in (pq.PollingStatus.READING, pq.PollingStatus.WRITING):
                    raise e.InternalError(f"unexpected poll status: {status}")

                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise e._WaitTimeout("wait timeout expired")
                fd = cancelConn.socket
                readers = [fd] if status == pq.PollingStatus.READING else []
                writers = [fd] if status == pq.PollingStatus.WRITING else []
                readable, writable, _ = select.select(readers, writers, [], remaining)
                if not readable and not writable:
                    raise e._WaitTimeout("wait timeout expired")
        finally:
            cancelConn.finish()

    def _waitConnection(self, obj: Any, mask: int, timeout: float | None) -> WaitSnapshot:
        ready = self._wait(obj, mask, timeout)
        if not ready & READ:
            return WaitSnapshot(ready=ready)

        try:
            if obj.status != pq.ConnStatus.OK:
                return WaitSnapshot(ready=ready)

            drainDeadline = monotonic() + _WAIT_DRAIN_GRACE
            consumed = False
            busy: int | None = None
            notifies = []
            while ready & READ:
                start = perf_counter() if self.profile is not None else 0.0
                obj.consume_input()
                if self.profile is not None:
                    self._profile("consume_input_seconds", perf_counter() - start)
                    self._profile("consume_input_calls", 1)
                consumed = True

                start = perf_counter() if self.profile is not None else 0.0
                busy = int(obj.is_busy())
                if self.profile is not None:
                    self._profile("is_busy_seconds", perf_counter() - start)
                    self._profile("is_busy_calls", 1)
                while notify := obj.notifies():
                    notifies.append(notify)
                if not busy:
                    break

                remaining = drainDeadline - monotonic()
                if remaining <= 0:
                    break
                nextReady = self._wait(obj, READ, remaining)
                if not nextReady & READ:
                    break
                ready |= nextReady

            return WaitSnapshot(
                ready=ready,
                consumed=consumed,
                busy=busy,
                notifies=tuple(notifies),
            )
        except Exception:
            return WaitSnapshot(ready=ready)

    def _sendSnapshot(self, obj: Any, value: Any, flush: int, busy: int) -> SendSnapshot:
        status = int(obj.status)
        transactionStatus = int(obj.transaction_status)
        clientEncoding: bytes | None = None
        if status == int(pq.ConnStatus.OK):
            encoding = obj.parameter_status(b"client_encoding")
            if encoding is not None:
                clientEncoding = bytes(encoding)
        return SendSnapshot(value, flush, busy, status, transactionStatus, clientEncoding)

    def _executePending(self, obj: Any, timeout: float) -> SendSnapshot:
        """Try to finish a normal query in one bounded worker round-trip."""
        if obj.pipeline_status != pq.PipelineStatus.OFF:
            return self._sendSnapshot(obj, ("pending", (), ()), int(obj.flush()), int(obj.is_busy()))

        deadline = monotonic() + max(0.0, timeout)
        while obj.flush() != 0:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return self._sendSnapshot(obj, ("pending", (), ()), 1, int(obj.is_busy()))
            ready = self._wait(obj, READ | WRITE, remaining)
            if not ready:
                return self._sendSnapshot(obj, ("pending", (), ()), 1, int(obj.is_busy()))
            if ready & READ:
                start = perf_counter() if self.profile is not None else 0.0
                obj.consume_input()
                if self.profile is not None:
                    self._profile("consume_input_seconds", perf_counter() - start)
                    self._profile("consume_input_calls", 1)

        results: list[ResultSnapshot] = []
        notifies: list[Any] = []
        while True:
            while True:
                start = perf_counter() if self.profile is not None else 0.0
                busy = obj.is_busy()
                if self.profile is not None:
                    self._profile("is_busy_seconds", perf_counter() - start)
                    self._profile("is_busy_calls", 1)
                if not busy:
                    break

                remaining = deadline - monotonic()
                if remaining <= 0:
                    state = "partial" if results else "pending"
                    return self._sendSnapshot(obj, (state, tuple(results), tuple(notifies)), 0, 1)
                ready = self._wait(obj, READ, remaining)
                if not ready:
                    state = "partial" if results else "pending"
                    return self._sendSnapshot(obj, (state, tuple(results), tuple(notifies)), 0, 1)
                start = perf_counter() if self.profile is not None else 0.0
                obj.consume_input()
                if self.profile is not None:
                    self._profile("consume_input_seconds", perf_counter() - start)
                    self._profile("consume_input_calls", 1)

            while notify := obj.notifies():
                notifies.append(notify)

            try:
                result = obj.get_result()
            except e.DatabaseError:
                if any(item.status == int(pq.ExecStatus.FATAL_ERROR) for item in results):
                    return self._sendSnapshot(obj, ("done", tuple(results), tuple(notifies)), 0, 0)
                raise

            if result is None:
                return self._sendSnapshot(obj, ("done", tuple(results), tuple(notifies)), 0, 0)

            snapshot = _snapshotResult(result, self.profile)
            results.append(snapshot)
            status = snapshot.status
            if status in _COPY_RESULTS:
                return self._sendSnapshot(obj, ("done", tuple(results), tuple(notifies)), 0, 0)
            if status == pq.ExecStatus.PIPELINE_SYNC:
                if len(results) != 1:
                    raise AssertionError(results)
                return self._sendSnapshot(obj, ("done", tuple(results), tuple(notifies)), 0, 0)
            if status == pq.ExecStatus.FATAL_ERROR and obj.status == pq.ConnStatus.BAD:
                return self._sendSnapshot(obj, ("done", tuple(results), tuple(notifies)), 0, 0)

    def _setConnection(self, conn: Any) -> None:
        old = self.objects.get(1)
        if old is not None:
            try:
                old.finish()
            except Exception:
                pass
        self.objects[1] = conn
        conn.notice_handler = self._notice

    def _notice(self, result: Any) -> None:
        try:
            self.notices.append(_snapshotResult(result, self.profile))
        except Exception:
            pass

    def _register(self, value: Any, kind: RemoteKind) -> RemoteRef:
        objectId = self.nextObjectId
        self.nextObjectId += 1
        self.objects[objectId] = value
        return RemoteRef(kind, objectId)

    def _normalize(self, value: Any) -> Any:
        if isinstance(value, pq.PGresult):
            return _snapshotResult(value, self.profile)
        if isinstance(value, pq.PGcancelConn):
            return self._register(value, RemoteKind.PGCANCEL_CONN)
        if isinstance(value, pq.PGcancel):
            return self._register(value, RemoteKind.PGCANCEL)
        if isinstance(value, memoryview):
            return bytes(value)
        if isinstance(value, tuple):
            return tuple(self._normalize(item) for item in value)
        if isinstance(value, list):
            return [self._normalize(item) for item in value]
        return value

    def _wait(self, obj: Any, mask: int, timeout: float | None) -> int:
        start = perf_counter() if self.profile is not None else 0.0
        fd = obj.socket
        readers = [fd] if mask & READ else []
        writers = [fd] if mask & WRITE else []
        readable, writable, _ = select.select(readers, writers, [], timeout)
        if self.profile is not None:
            self._profile("libpq_wait_seconds", perf_counter() - start)
            self._profile("libpq_wait_calls", 1)
        ready = 0
        if readable:
            ready |= READ
        if writable:
            ready |= WRITE
        return ready

    def _closeObjects(self) -> None:
        for objectId in sorted(self.objects, reverse=True):
            obj = self.objects.pop(objectId)
            try:
                if hasattr(obj, "finish"):
                    obj.finish()
                elif hasattr(obj, "free"):
                    obj.free()
            except Exception:
                pass


def _packResultValues(
    result: Any,
    ntuples: int,
    nfields: int,
    profile: dict[str, float | int] | None,
) -> PackedValues:
    cellCount = ntuples * nfields
    packValues = getattr(result, "_qpsycopg_pack_values", None)
    if packValues is not None:
        valueData, valueOffsets, nulls, packedCount = packValues()
        if packedCount != cellCount:
            raise AssertionError(f"native PGresult pack count mismatch: {packedCount} != {cellCount}")
        if profile is not None:
            profile["snapshot_native_pack_calls"] = profile.get("snapshot_native_pack_calls", 0) + 1
        return PackedValues(valueData, valueOffsets, nulls, cellCount)

    valueData = bytearray()
    valueOffsets = bytearray((cellCount + 1) * _VALUE_OFFSET.size)
    nulls = bytearray((cellCount + 7) // 8)
    getValue = result.get_value
    cellIndex = 0
    for row in range(ntuples):
        for column in range(nfields):
            _VALUE_OFFSET.pack_into(valueOffsets, cellIndex * _VALUE_OFFSET.size, len(valueData))
            value = getValue(row, column)
            if value is None:
                nulls[cellIndex >> 3] |= 1 << (cellIndex & 7)
            else:
                valueData.extend(value)
            cellIndex += 1
    _VALUE_OFFSET.pack_into(valueOffsets, cellIndex * _VALUE_OFFSET.size, len(valueData))
    if profile is not None:
        profile["snapshot_python_pack_calls"] = profile.get("snapshot_python_pack_calls", 0) + 1
    return PackedValues(bytes(valueData), bytes(valueOffsets), bytes(nulls), cellCount)


def _snapshotResult(result: Any, profile: dict[str, float | int] | None = None) -> ResultSnapshot:
    totalStart = perf_counter() if profile is not None else 0.0
    status = result.status
    nfields = result.nfields
    ntuples = result.ntuples
    fname = result.fname
    ftable = result.ftable
    ftablecol = result.ftablecol
    fformat = result.fformat
    ftype = result.ftype
    fmod = result.fmod
    fsize = result.fsize
    columns = tuple(
        (
            fname(i),
            ftable(i),
            ftablecol(i),
            fformat(i),
            ftype(i),
            fmod(i),
            fsize(i),
        )
        for i in range(nfields)
    )

    packStart = perf_counter() if profile is not None else 0.0
    values = _packResultValues(result, ntuples, nfields, profile)
    cellCount = ntuples * nfields
    if profile is not None:
        profile["snapshot_values_seconds"] = profile.get("snapshot_values_seconds", 0) + perf_counter() - packStart
        profile["snapshot_cells"] = profile.get("snapshot_cells", 0) + cellCount

    errorFields: list[tuple[int, bytes]] = []
    if status in _ERROR_RESULTS:
        errorField = result.error_field
        for field in pq.DiagnosticField:
            code = int(field)
            value = errorField(code)
            if value is not None:
                errorFields.append((code, value))

    snapshot = ResultSnapshot(
        status=int(status),
        errorMessage=result.error_message,
        errorFields=tuple(errorFields),
        columns=columns,
        rowCount=ntuples,
        columnCount=nfields,
        binaryTuples=result.binary_tuples,
        values=values,
        paramTypes=tuple(result.param_type(i) for i in range(result.nparams)),
        commandStatus=result.command_status,
        commandTuples=result.command_tuples,
        oidValue=result.oid_value,
    )
    if profile is not None:
        profile["snapshot_total_seconds"] = profile.get("snapshot_total_seconds", 0) + perf_counter() - totalStart
        profile["snapshot_results"] = profile.get("snapshot_results", 0) + 1
    return snapshot


def main() -> int:
    if len(sys.argv) != 4:
        return 2

    host, port, tokenHex = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    token = bytes.fromhex(tokenHex)
    sock = socket.create_connection((host, port), timeout=10.0)
    sock.settimeout(None)
    sendToken(sock, token)
    try:
        Worker(sock).run()
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
