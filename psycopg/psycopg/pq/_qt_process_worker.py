"""Worker process for the experimental Qt-isolated pq implementation."""

from __future__ import annotations

import select
import socket
import sys
from time import monotonic
from typing import Any

from psycopg import errors as e
from psycopg import pq

from ._qt_process_protocol import Operation, RemoteError, RemoteKind
from ._qt_process_protocol import RemoteRef, Request, Response, ResultSnapshot
from ._qt_process_protocol import ResultReadSnapshot, SendSnapshot, WaitSnapshot
from ._qt_process_protocol import recvFrame, sendFrame, sendToken

READ = 1
WRITE = 2
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
_FAST_EXECUTE = "qpsycopg_execute_pending"
_WAIT_DRAIN_GRACE = 0.001


class Worker:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.objects: dict[int, Any] = {}
        self.nextObjectId = 2
        self.notices: list[ResultSnapshot] = []

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

            sendFrame(self.sock, response)
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
                snapshot = _snapshotResult(value)
                following: list[ResultSnapshot | None] = []
                if (
                    value.status in _SUCCESS_RESULTS
                    and obj.pipeline_status == pq.PipelineStatus.OFF
                    and obj.status == pq.ConnStatus.OK
                    and not obj.is_busy()
                ):
                    nextResult = obj.get_result()
                    following.append(
                        None if nextResult is None else _snapshotResult(nextResult)
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
                obj.consume_input()
                consumed = True
                busy = int(obj.is_busy())
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
                obj.consume_input()

        results: list[ResultSnapshot] = []
        notifies: list[Any] = []
        while True:
            while obj.is_busy():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    state = "partial" if results else "pending"
                    return self._sendSnapshot(obj, (state, tuple(results), tuple(notifies)), 0, 1)
                ready = self._wait(obj, READ, remaining)
                if not ready:
                    state = "partial" if results else "pending"
                    return self._sendSnapshot(obj, (state, tuple(results), tuple(notifies)), 0, 1)
                obj.consume_input()

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

            snapshot = _snapshotResult(result)
            results.append(snapshot)
            status = result.status
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
            self.notices.append(_snapshotResult(result))
        except Exception:
            pass

    def _register(self, value: Any, kind: RemoteKind) -> RemoteRef:
        objectId = self.nextObjectId
        self.nextObjectId += 1
        self.objects[objectId] = value
        return RemoteRef(kind, objectId)

    def _normalize(self, value: Any) -> Any:
        if isinstance(value, pq.PGresult):
            return _snapshotResult(value)
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

    @staticmethod
    def _wait(obj: Any, mask: int, timeout: float | None) -> int:
        fd = obj.socket
        readers = [fd] if mask & READ else []
        writers = [fd] if mask & WRITE else []
        readable, writable, _ = select.select(readers, writers, [], timeout)
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


def _snapshotResult(result: Any) -> ResultSnapshot:
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

    getValue = result.get_value
    values: list[bytes | None] = [None] * (ntuples * nfields)
    cellIndex = 0
    for row in range(ntuples):
        for column in range(nfields):
            value = getValue(row, column)
            if value is not None:
                values[cellIndex] = bytes(value)
            cellIndex += 1

    errorFields: list[tuple[int, bytes]] = []
    errorField = result.error_field
    for field in pq.DiagnosticField:
        code = int(field)
        value = errorField(code)
        if value is not None:
            errorFields.append((code, value))

    return ResultSnapshot(
        status=int(result.status),
        errorMessage=result.error_message,
        errorFields=tuple(errorFields),
        columns=columns,
        rowCount=ntuples,
        columnCount=nfields,
        binaryTuples=result.binary_tuples,
        values=tuple(values),
        paramTypes=tuple(result.param_type(i) for i in range(result.nparams)),
        commandStatus=result.command_status,
        commandTuples=result.command_tuples,
        oidValue=result.oid_value,
    )


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
