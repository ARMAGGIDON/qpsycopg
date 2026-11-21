"""Owner identity / bounded cancellation invariants (no DB required)."""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from psycopg.pq._qt_process_owner import (
    AttemptToken, ConnectAttempt, ExitState, OwnerToken,
    cancelConnect, newConnectAttempt,
)


def test_tokens_are_unique_immutable_and_distinct():
    from dataclasses import is_dataclass

    tokens = [OwnerToken() for _ in range(200)]
    attempts = [AttemptToken() for _ in range(200)]
    assert len(set(tokens)) == len(tokens)
    assert len(set(attempts)) == len(attempts)
    assert is_dataclass(tokens[0])
    with pytest.raises(FrozenInstanceError):
        tokens[0].value = tokens[1].value


def test_early_registration_and_wrong_owner(monkeypatch):
    pytest.importorskip("PySide6")
    from psycopg.pq import pq_qt_process as qp

    registered = []
    blocked = threading.Event()

    def fake_run(owner):
        owner.started.set()
        blocked.wait(1)

    monkeypatch.setattr(qp._ProcessOwner, "_run", fake_run)
    transport = SimpleNamespace(_abortConnect=lambda: None)
    attempt = newConnectAttempt(lambda at, ot, owner: registered.append(
        (at, ot, owner, owner.thread.is_alive())
    ))
    with attempt:
        owner = qp._ProcessOwner("127.0.0.1", 1, "secret", attempt=attempt, transport=transport)
    try:
        assert len(registered) == 1
        at, ot, instance, was_alive = registered[0]
        assert at == attempt.token and ot == owner.ownerToken
        assert instance is owner and not was_alive  # Before QProcess.start().
        with pytest.raises(ValueError):
            owner.requestKill(OwnerToken())
        with pytest.raises(ValueError):
            owner.exitProof(OwnerToken())
        assert owner.exitProof(ot) is None
    finally:
        blocked.set()
        owner.thread.join(1)


def test_queue_is_not_dispatch_or_os_exit(monkeypatch):
    pytest.importorskip("PySide6")
    from psycopg.pq import pq_qt_process as qp

    hold = threading.Event()
    monkeypatch.setattr(qp._ProcessOwner, "_run", lambda owner: hold.wait(1))
    first = qp._ProcessOwner("127.0.0.1", 1, "first")
    second = qp._ProcessOwner("127.0.0.1", 2, "second")
    try:
        receipt = first.requestKill(first.ownerToken)
        assert receipt.state == ExitState.KILL_QUEUED
        assert first.requestKill(first.ownerToken).state == ExitState.KILL_QUEUED
        assert first.commands.qsize() == 1
        assert second.receipt(second.ownerToken).state == ExitState.EXIT_UNCONFIRMED
        assert first.waitExit(first.ownerToken, 0.01) is None
        assert first.exitConfirmed() is False
        with pytest.raises(ValueError):
            first.requestKill(second.ownerToken)
        assert second.commands.empty()
    finally:
        hold.set()
        first.thread.join(1)
        second.thread.join(1)


def test_cancel_attempt_is_independent_of_blocked_owner(monkeypatch):
    pytest.importorskip("PySide6")
    from psycopg.pq import pq_qt_process as qp

    blocked = threading.Event()
    monkeypatch.setattr(qp._ProcessOwner, "_run", lambda _: blocked.wait(1))
    interrupted = []
    attempt = newConnectAttempt(timeout_s=2)
    transport = SimpleNamespace(_abortConnect=lambda: interrupted.append(True))
    with attempt:
        owner = qp._ProcessOwner("127.0.0.1", 1, "secret", attempt=attempt, transport=transport)
    try:
        start = time.monotonic()
        receipt = cancelConnect(attempt.token)
        assert time.monotonic() - start < 0.3
        assert receipt is not None and receipt.state == ExitState.KILL_QUEUED
        assert interrupted
        with pytest.raises(RuntimeError, match="cancelled"):
            attempt.check()
        assert attempt.waitExit(0.01) is None
        assert owner.requestKill(owner.ownerToken).state == ExitState.KILL_QUEUED
        with pytest.raises(LookupError):
            cancelConnect(AttemptToken())
    finally:
        blocked.set()
        owner.thread.join(1)


def test_close_does_not_wait_for_busy_transport_lock():
    pytest.importorskip("PySide6")
    from psycopg.pq import pq_qt_process as qp

    entered = threading.Event()
    release = threading.Event()
    lock = threading.RLock()
    calls = []

    def hold_lock():
        with lock:
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(1)
    transport = qp._Transport.__new__(qp._Transport)
    transport.lock = lock
    transport.closed = False
    transport._listener = None
    transport.sock = None
    transport.owner = SimpleNamespace(kill=lambda: calls.append("kill"))
    try:
        started = time.monotonic()
        transport.close()
        assert time.monotonic() - started < 0.3
        assert transport.closed and calls == ["kill"]
    finally:
        release.set()
        thread.join(1)


def test_cancel_control_recv_deadline(monkeypatch):
    from psycopg.pq import _qt_process_cancel as mod

    class StalledSocket:
        timeout = None
        closed = False
        def settimeout(self, value):
            self.timeout = value
        def close(self):
            self.closed = True

    sock = StalledSocket()
    monkeypatch.setattr(mod, "sendFrame", lambda *_: None)

    def blocked_recv(_):
        assert sock.timeout == pytest.approx(0.05)
        raise socket.timeout("simulated stalled recvFrame")

    monkeypatch.setattr(mod, "recvFrame", blocked_recv)
    control = mod.CancelControlClient(sock)
    with pytest.raises(Exception, match="disconnected"):
        control.cancel(0.05)
    assert control.closed and sock.closed
    with pytest.raises(ValueError):
        mod.CancelControlClient(StalledSocket()).cancel(0)


def test_managed_attempt_context_is_thread_local():
    assert newConnectAttempt().owner is None
    attempts = []
    def worker():
        from psycopg.pq._qt_process_owner import currentConnectAttempt
        with newConnectAttempt() as attempt:
            attempts.append(currentConnectAttempt() is attempt)
        attempts.append(currentConnectAttempt() is None)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)
    assert attempts == [True, True]
