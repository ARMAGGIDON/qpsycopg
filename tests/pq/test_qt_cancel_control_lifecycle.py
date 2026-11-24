"""QP10: deterministic D07 regression and cancellation generation invariants.

Run this file in a dedicated PSYCOPG_IMPL=qt-process interpreter. The D07
test can also be copied unchanged to the QP09 baseline: it must fail with
two opens, not time out waiting for an artificial schedule.
"""
from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace

import pytest
import psycopg

pytestmark = pytest.mark.skipif(
    psycopg.pq.__impl__ != "qt-process", reason="requires qt-process"
)


class Control:
    def __init__(self):
        self.closed = False
        self.requests = []

    def cancel(self, timeout):
        assert not self.closed
        self.requests.append(timeout)

    def abort(self):
        was_open = not self.closed
        self.closed = True
        return was_open


def generation(transport=None):
    from psycopg.pq import _qt_process_cancel as cc

    if transport is None:
        owner = SimpleNamespace(
            _stateLock=threading.Lock(), _killRequested=False,
            _closeRequested=False, _exitEvent=threading.Event(),
        )
        transport = SimpleNamespace(
            owner=owner, closed=False, lock=threading.RLock(),
            _cancelControl=None, _pendingSend=None, _statusCache=None,
            _transactionStatusCache=None, _clearQueryCache=lambda _: None,
            _requestCancelOpen=lambda deadline: None,
        )
    pgconn = SimpleNamespace(_transport=transport, _handle=1)
    transport.connection = pgconn

    def broken():
        transport.closed = True
        if hasattr(pgconn, "_cancelLifecycle"):
            pgconn._cancelLifecycle.retire(pgconn)

    transport._markBroken = broken
    if hasattr(cc, "CancelControlLifecycle"):
        pgconn._cancelLifecycle = cc.CancelControlLifecycle()
    return pgconn


def spawn(fn):
    errors = []
    done = threading.Event()

    def run():
        try:
            fn()
        except BaseException as ex:
            errors.append(ex)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, done, errors


def join(task, *, ok=True):
    thread, done, errors = task
    assert done.wait(5), "blocked helper"
    thread.join(1)
    assert not thread.is_alive()
    if ok:
        assert not errors, errors
    return errors


def test_d07_first_send_vs_cancel(monkeypatch):
    from psycopg import connection_qt_process as conn
    from psycopg.pq import _qt_process_lazy as lazy
    from psycopg.pq import _qt_process_cancel as cc
    from psycopg.pq._qt_process_protocol import Operation

    pgconn = generation()
    transport = pgconn._transport
    entered = threading.Event()
    contender = threading.Event()
    release = threading.Event()
    opens = []
    mutex = threading.Lock()

    def open_control(cls, pgconn, **kwargs):
        with mutex:
            opens.append(Control())
            index = len(opens) - 1
            (entered if index == 0 else contender).set()
        assert release.wait(4)
        return opens[index]

    monkeypatch.setattr(cc.CancelControlClient, "open", classmethod(open_control))
    if hasattr(pgconn, "_cancelLifecycle"):
        condition = pgconn._cancelLifecycle.condition
        old_wait = condition.wait

        def wait(timeout=None):
            contender.set()
            return old_wait(timeout)

        monkeypatch.setattr(condition, "wait", wait)

    # The actual lazy send path and the original high-level fallback opener.
    first = spawn(lambda: lazy._request(
        transport, Operation.CALL, 1, "send_query", (b"select 1",)
    ))
    second = None
    try:
        assert entered.wait(3), "first opener did not reach setup"
        second = spawn(lambda: conn._openCancelControl(pgconn))
        assert contender.wait(3), "second path did not reach the setup boundary"
    finally:
        release.set()
        join(first)
        if second is not None:
            join(second)

    assert len(opens) == 1, f"D07 duplicate initialization: {len(opens)} opens"
    assert transport._cancelControl is opens[0]
    assert pgconn._cancelLifecycle.state == "READY"


def test_ready_cancel_does_not_wait_for_sql_lock(monkeypatch):
    from psycopg import connection_qt_process as conn
    from psycopg.pq import _qt_process_cancel as cc

    pgconn = generation()
    control = Control()
    monkeypatch.setattr(cc.CancelControlClient, "open", classmethod(
        lambda cls, pgconn, **kwargs: control
    ))
    connection = SimpleNamespace(pgconn=pgconn, _tpc=None)
    # Cancel before first SQL establishes the only control client.
    conn._cancelRemote(connection, 2)
    with pgconn._transport.lock:
        task = spawn(lambda: conn._cancelRemote(connection, 2))
        join(task)
    assert len(control.requests) == 2
    assert pgconn._transport._cancelControl is control


@pytest.mark.parametrize("failure", ["timeout", "closed"])
def test_uncertain_open_is_not_retried(monkeypatch, failure):
    from psycopg.pq import _qt_process_cancel as cc

    pgconn = generation()
    calls = []

    def open_control(cls, pgconn, **kwargs):
        calls.append(1)
        if failure == "timeout":
            raise TimeoutError("injected setup failure")
        control = Control()
        control.abort()
        return control

    monkeypatch.setattr(cc.CancelControlClient, "open", classmethod(open_control))
    lifecycle = pgconn._cancelLifecycle
    with pytest.raises((TimeoutError, psycopg.OperationalError)):
        lifecycle.ensure(pgconn)
    assert pgconn._transport.closed
    assert pgconn._transport._cancelControl is None
    with pytest.raises(psycopg.OperationalError):
        lifecycle.ensure(pgconn)
    assert calls == [1]


@pytest.mark.parametrize("reason", ["close", "kill", "exit"])
def test_late_publication_is_revoked(monkeypatch, reason):
    from psycopg.pq import _qt_process_cancel as cc

    pgconn = generation()
    transport = pgconn._transport
    lifecycle = pgconn._cancelLifecycle
    entered, release = threading.Event(), threading.Event()
    control = Control()

    def open_control(cls, pgconn, **kwargs):
        entered.set()
        assert release.wait(4)
        return control

    monkeypatch.setattr(cc.CancelControlClient, "open", classmethod(open_control))
    task = spawn(lambda: lifecycle.ensure(pgconn))
    try:
        assert entered.wait(3)
        if reason == "close":
            assert not lifecycle.retire(pgconn)
        elif reason == "kill":
            with transport.owner._stateLock:
                transport.owner._killRequested = True
        else:
            with transport.owner._stateLock:
                transport.owner._exitEvent.set()
    finally:
        release.set()
    assert isinstance(join(task, ok=False)[0], psycopg.OperationalError)
    assert control.closed
    assert transport._cancelControl is None
    assert lifecycle.state in ("FAILED", "RETIRED")


def test_waiter_timeout_does_not_start_second_open(monkeypatch):
    from psycopg.pq import _qt_process_cancel as cc

    pgconn = generation()
    lifecycle = pgconn._cancelLifecycle
    entered, release = threading.Event(), threading.Event()
    calls = []

    def open_control(cls, pgconn, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(4)
        return Control()

    monkeypatch.setattr(cc.CancelControlClient, "open", classmethod(open_control))
    task = spawn(lambda: lifecycle.ensure(pgconn))
    try:
        assert entered.wait(3)
        with pytest.raises(psycopg.errors._WaitTimeout):
            lifecycle.ensure(pgconn, 0.05)
        assert lifecycle.state == "OPENING"
        assert pgconn._transport._cancelControl is None
    finally:
        release.set()
        join(task)
    assert calls == [1]
    assert lifecycle.state == "READY"


def test_pooled_generation_does_not_reuse_retired_client(monkeypatch):
    from psycopg.pq import _qt_process_cancel as cc

    old = generation()
    opens = []

    def open_control(cls, pgconn, **kwargs):
        control = Control()
        opens.append(control)
        return control

    monkeypatch.setattr(cc.CancelControlClient, "open", classmethod(open_control))
    first = old._cancelLifecycle.ensure(old)
    assert old._cancelLifecycle.retire(old)
    old._handle = 0
    new = generation(old._transport)
    second = new._cancelLifecycle.ensure(new)
    assert first is not second and first.closed and not second.closed
    with pytest.raises(psycopg.OperationalError):
        old._cancelLifecycle.ensure(old)
    old._cancelLifecycle.fail_if_closed(old, first)
    assert new._transport._cancelControl is second
    assert not new._transport.closed
    neighbor = generation()
    assert neighbor._cancelLifecycle.ensure(neighbor) is not second


def test_sync_async_share_control(monkeypatch):
    from psycopg import connection_qt_process as conn
    from psycopg.pq import _qt_process_cancel as cc

    pgconn = generation()
    control = Control()
    monkeypatch.setattr(cc.CancelControlClient, "open", classmethod(
        lambda cls, pgconn, **kwargs: control
    ))
    monkeypatch.setattr(conn, "capabilities", SimpleNamespace(
        has_cancel_safe=lambda: True
    ))
    fake = SimpleNamespace(pgconn=pgconn, _tpc=None)
    conn.Connection.cancel_safe(fake, timeout=2)
    asyncio.run(conn.AsyncConnection.cancel_safe(fake, timeout=2))
    assert len(control.requests) == 2
    assert pgconn._transport._cancelControl is control


def test_concurrent_control_requests_and_retirement():
    from psycopg.pq._qt_process_cancel import CancelControlClient
    from psycopg.pq._qt_process_protocol import recvFrame, sendFrame, Response

    left, right = socket.socketpair()
    client = CancelControlClient(left)
    requests = []
    got_third = threading.Event()

    def server():
        for _ in range(2):
            req = recvFrame(right)
            requests.append(req.requestId)
            sendFrame(right, Response(req.requestId))
        req = recvFrame(right)
        requests.append(req.requestId)
        got_third.set()

    server_task = spawn(server)
    try:
        first = spawn(lambda: client.cancel(2))
        second = spawn(lambda: client.cancel(2))
        join(first)
        join(second)
        third = spawn(lambda: client.cancel(4))
        assert got_third.wait(3)
        assert not client.abort()  # active request: no safe reuse
        assert join(third, ok=False)
        assert requests == [1, 2, 3]
        assert client.closed
    finally:
        client.abort()
        right.close()
        join(server_task)
