"""Startup failure must not terminate process workers before error retrieval."""

from __future__ import annotations

from collections import deque

import pytest


@pytest.fixture(params=("plain", "pooled"))
def worker_module(request):
    from psycopg.pq import _qt_process_worker, _qt_process_worker_pool

    return _qt_process_worker if request.param == "plain" else _qt_process_worker_pool


def _run_fake_worker(monkeypatch, module, startup, *, successful):
    from psycopg import pq
    from psycopg.pq._qt_process_protocol import Operation, RemoteKind, RemoteRef
    from psycopg.pq._qt_process_protocol import Request

    requests = deque([Request(1, startup, args=(b"dbname=missing",))])
    if startup == Operation.CONNECT_START:
        requests.append(Request(2, Operation.CALL, 1, "connect_poll"))
    if successful:
        requests.append(Request(3, Operation.CALL, 1, "send_query"))
    else:
        requests.append(Request(3, Operation.GET, 1, "error_message"))
    requests.extend(
        (Request(4, Operation.CALL, 1, "finish"), Request(5, Operation.CLOSE))
    )
    responses = []

    class Conn:
        status = pq.ConnStatus.OK if successful else pq.ConnStatus.BAD
        error_message = b'FATAL: database "missing" does not exist'
        finished = False

        def finish(self):
            self.finished = True

    conn = Conn()

    worker = module.Worker(None)

    def handle(request):
        if request.operation in (Operation.CONNECT_START, Operation.CONNECT):
            worker.objects[1] = conn
            return RemoteRef(RemoteKind.PGCONN, 1)
        if request.member == "connect_poll":
            return pq.PollingStatus.OK if successful else pq.PollingStatus.FAILED
        if request.member == "send_query":
            conn.status = pq.ConnStatus.BAD
            return None
        if request.operation == Operation.GET:
            return conn.error_message
        if request.member == "finish":
            worker.objects.pop(1).finish()
            return None
        if request.operation == Operation.CLOSE:
            return None
        raise AssertionError(request)

    monkeypatch.setattr(worker, "_handle", handle)
    monkeypatch.setattr(module, "recvFrame", lambda sock: requests.popleft())
    monkeypatch.setattr(
        module, "sendFrame", lambda sock, response, *args: responses.append(response)
    )
    worker.run()
    return responses, requests, conn


@pytest.mark.parametrize("startup", ("CONNECT_START", "CONNECT"))
def test_failed_startup_keeps_error_readable(monkeypatch, worker_module, startup):
    from psycopg.pq._qt_process_protocol import Operation

    responses, remaining, conn = _run_fake_worker(
        monkeypatch, worker_module, getattr(Operation, startup), successful=False
    )
    expected = [1, 2, 3, 4, 5] if startup == "CONNECT_START" else [1, 3, 4, 5]
    assert [response.requestId for response in responses] == expected
    assert responses[-3].value == conn.error_message
    assert not remaining
    assert conn.finished


def test_established_connection_bad_still_stops_worker(monkeypatch, worker_module):
    from psycopg.pq._qt_process_protocol import Operation

    responses, remaining, conn = _run_fake_worker(
        monkeypatch, worker_module, Operation.CONNECT_START, successful=True
    )
    assert [response.requestId for response in responses] == [1, 2, 3]
    assert [request.requestId for request in remaining] == [4, 5]
    assert conn.finished
