"""Worker process for the experimental Qt-isolated pq implementation."""

from __future__ import annotations

import sys
import select
import socket
from typing import Any

from psycopg import pq

from ._qt_process_protocol import Operation, RemoteError, RemoteKind, RemoteRef
from ._qt_process_protocol import Request, Response, ResultSnapshot, recvFrame
from ._qt_process_protocol import sendFrame

READ = 1
WRITE = 2


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
            return self._normalize(
                getattr(obj, request.member)(
                    *request.args,
                    **dict(request.kwargs),
                )
            )

        if op == Operation.GET:
            return self._normalize(getattr(obj, request.member))

        if op == Operation.SET:
            setattr(obj, request.member, request.args[0])
            return None

        if op == Operation.WAIT:
            return self._wait(obj, int(request.args[0]), request.args[1])

        if op == Operation.ESCAPE:
            escapingConn: Any = obj if request.objectId else None
            escaping = pq.Escaping(escapingConn)
            return self._normalize(
                getattr(escaping, request.member)(*request.args)
            )

        raise ValueError(f"unsupported operation: {op!r}")

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
    columns = tuple(
        (
            result.fname(i),
            result.ftable(i),
            result.ftablecol(i),
            result.fformat(i),
            result.ftype(i),
            result.fmod(i),
            result.fsize(i),
        )
        for i in range(result.nfields)
    )

    data = bytearray()
    cells: list[tuple[int, int]] = []
    for row in range(result.ntuples):
        for column in range(result.nfields):
            value = result.get_value(row, column)
            if value is None:
                cells.append((-1, 0))
            else:
                start = len(data)
                data.extend(value)
                cells.append((start, len(value)))

    errorFields = tuple(
        (int(field), result.error_field(int(field))) for field in pq.DiagnosticField
    )

    return ResultSnapshot(
        status=int(result.status),
        errorMessage=result.error_message,
        errorFields=errorFields,
        columns=columns,
        rowCount=result.ntuples,
        columnCount=result.nfields,
        binaryTuples=result.binary_tuples,
        data=bytes(data),
        cells=tuple(cells),
        paramTypes=tuple(result.param_type(i) for i in range(result.nparams)),
        commandStatus=result.command_status,
        commandTuples=result.command_tuples,
        oidValue=result.oid_value,
    )


def main() -> int:
    if len(sys.argv) != 4:
        return 2

    host, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    sock = socket.create_connection((host, port), timeout=10.0)
    sock.settimeout(None)
    sendFrame(sock, token)
    try:
        Worker(sock).run()
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
