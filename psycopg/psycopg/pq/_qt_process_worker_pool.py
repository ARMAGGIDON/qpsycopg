"""Worker entry point supporting safe reuse between PostgreSQL sessions."""

from __future__ import annotations

import socket
import sys

from .. import errors as e
from ._qt_process_protocol import Operation, Request, sendToken
from ._qt_process_worker import Worker as _Worker


class Worker(_Worker):
    def _closeCancelControl(self) -> None:
        control = self.cancelControl
        if control is None:
            return
        control.thread.join(5.0)
        if control.thread.is_alive():
            raise e.OperationalError("qpsycopg cancellation control did not stop")
        self.cancelControl = None

    def _handle(self, request: Request):
        if request.operation in (Operation.CONNECT, Operation.CONNECT_START):
            self._closeCancelControl()
        elif request.operation == Operation.CALL and request.objectId == 1 and request.member == "finish":
            self._closeCancelControl()
        return super()._handle(request)


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
