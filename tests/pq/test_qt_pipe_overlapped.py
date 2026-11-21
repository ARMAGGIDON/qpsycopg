"""QP09 Windows integration tests; no mock can substitute for QProcess evidence."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Named Pipes only"
)


def _thread(target):
    errors = []
    finished = threading.Event()

    def run():
        try:
            target()
        except BaseException as ex:
            errors.append(ex)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors, finished


def test_real_connect_timeout_and_exactly_once_close():
    from psycopg.pq._qt_process_pipe import PipeListenerSocket

    listener = PipeListenerSocket()
    listener.settimeout(0.08)
    try:
        with pytest.raises(socket.timeout):
            listener.accept()
    finally:
        listener.close()
        listener.close()
    assert listener._pipe is None


def test_close_interrupts_pending_connect():
    from psycopg.pq._qt_process_pipe import PipeListenerSocket

    listener = PipeListenerSocket()
    listener.settimeout(10)
    started = threading.Event()

    def accept():
        started.set()
        with pytest.raises(OSError):
            listener.accept()

    thread, errors, finished = _thread(accept)
    assert started.wait(2)
    time.sleep(0.1)
    start = time.monotonic()
    listener.close()
    assert finished.wait(3), "ConnectNamedPipe did not terminate after close"
    assert time.monotonic() - start < 3
    thread.join(1)
    assert not errors, f"uncaught helper thread exception: {errors}"


def test_pid_verification_and_stream_timeout():
    from psycopg.pq._qt_process_pipe import PipeListenerSocket, connectPipe

    listener = PipeListenerSocket()
    listener.expected_pid = os.getpid()
    client = connectPipe(listener.address)
    server, _ = listener.accept()
    try:
        client.sendall(b"hello")
        assert server.recv(5) == b"hello"
        server.sendall(b"world")
        assert client.recv(5) == b"world"
        server.settimeout(0.1)
        with pytest.raises(socket.timeout):
            server.recv(1)
        server.settimeout(None)
        client.sendall(b"a" * 120000)
        payload = bytearray()
        while len(payload) != 120000:
            chunk = server.recv(120000 - len(payload))
            assert chunk
            payload.extend(chunk)
        assert payload == b"a" * 120000
    finally:
        server.close()
        server.close()
        client.close()
        listener.close()


def test_unexpected_client_pid_rejected():
    from psycopg.pq._qt_process_pipe import PipeListenerSocket, connectPipe

    listener = PipeListenerSocket()
    listener.expected_pid = 0
    client = connectPipe(listener.address)
    try:
        with pytest.raises(PermissionError, match="client PID"):
            listener.accept()
    finally:
        client.close()
        listener.close()


def test_write_timeout_with_nonreading_peer():
    from psycopg.pq._qt_process_pipe import PipeListenerSocket, connectPipe

    listener = PipeListenerSocket()
    client = connectPipe(listener.address)
    server, _ = listener.accept()
    try:
        server.settimeout(0.25)
        started = time.monotonic()
        with pytest.raises(socket.timeout):
            server.sendall(b"x" * (32 << 20))
        assert time.monotonic() - started < 4
    finally:
        server.close()
        client.close()
        listener.close()


def test_explicit_security_descriptor():
    from psycopg.pq import _qt_process_pipe as pipe

    attrs, sd = pipe._current_user_security()
    try:
        assert attrs.lpSecurityDescriptor == sd.value
        assert attrs.bInheritHandle == 0
        assert attrs.nLength == pipe.ctypes.sizeof(pipe._SECURITY_ATTRIBUTES)
    finally:
        pipe._localFree(sd)


def _evidence(name: str, data: dict) -> None:
    folder = os.environ.get("QPSYCOPG_OWNER_EVIDENCE_DIR")
    if folder:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(json.dumps(data, sort_keys=True, indent=2), encoding="utf-8")
    print(f"QP09_OS_EVIDENCE={json.dumps(data, sort_keys=True)}", flush=True)


def test_managed_pipe_two_real_qprocess_workers():
    import psutil
    import psycopg
    from psycopg.pq._qt_process_owner import newConnectAttempt

    assert psycopg.pq.__impl__ == "qt-process"
    assert os.environ.get("QPSYCOPG_IPC") == "pipe"
    dsn = os.environ["QPSYCOPG_OWNER_TEST_DSN"]
    owners = []
    attempts = []
    connections = []
    try:
        for index in range(2):
            created = []
            attempt = newConnectAttempt(
                lambda at, ot, owner: created.append((at, ot, owner)), timeout_s=12
            )
            with attempt:
                connection = psycopg.connect(dsn)
            assert len(created) == 1
            assert created[0][0] == attempt.token
            assert created[0][1] == created[0][2].ownerToken
            assert connection.pgconn._transport.ipcKind == "pipe"
            assert connection.pgconn._transport.sock.ipc_kind == "pipe"
            assert connection.execute("select 1").fetchone() == (1,)
            owners.append(created[0][2])
            attempts.append(attempt)
            connections.append(connection)
        assert owners[0]._osPid != owners[1]._osPid
        assert all(psutil.pid_exists(owner._osPid) for owner in owners)
        version = connections[0].execute("show server_version_num").fetchone()[0]
        if os.environ.get("QPSYCOPG_REQUIRE_PG18") == "1":
            assert int(version) >= 180000, f"PostgreSQL 18 is required: {version}"
        # Kill only owner A: its independent sibling must keep servicing SQL.
        receipt = owners[0].requestKill(owners[0].ownerToken)
        assert receipt.owner_token == owners[0].ownerToken
        proof = owners[0].waitExit(owners[0].ownerToken, 10)
        assert proof is not None and proof.owner_token == owners[0].ownerToken
        assert proof.os_pid == owners[0]._osPid
        assert not psutil.pid_exists(proof.os_pid)
        assert connections[1].execute("select 42").fetchone() == (42,)
        _evidence("qp09-windows-pipe-pg18.json", {
            "result": "PASS", "ipc": "pipe", "pg_server_version_num": int(version),
            "attempt_tokens_distinct": attempts[0].token != attempts[1].token,
            "owner_tokens_distinct": owners[0].ownerToken != owners[1].ownerToken,
            "worker_a_os_pid": proof.os_pid, "worker_b_os_pid": owners[1]._osPid,
            "exit_proof": repr(proof), "external_pid_absent": True,
            "worker_b_survived": True,
        })
    finally:
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass
        for owner in owners:
            owner.requestKill(owner.ownerToken)
            owner.waitExit(owner.ownerToken, 10)


def test_cancel_managed_connect_with_peer_accepted_but_silent(monkeypatch):
    import psutil
    import psycopg
    from psycopg.pq._qt_process_owner import newConnectAttempt, cancelConnect
    from psycopg.pq._qt_process_pipe import PipeStream

    assert os.environ["QPSYCOPG_IPC"] == "pipe"
    monkeypatch.setenv("QPSYCOPG_QP09_TEST_STALL_HANDSHAKE", "1")
    connected = threading.Event()
    original = PipeStream.recv_into

    def observe_handshake(self, target):
        if len(target) == 32:
            connected.set()  # accept() and the QProcess PID check completed.
        return original(self, target)

    monkeypatch.setattr(PipeStream, "recv_into", observe_handshake)
    attempt = newConnectAttempt(timeout_s=15)
    failures = []

    def do_connect():
        try:
            with attempt:
                psycopg.connect(os.environ["QPSYCOPG_OWNER_TEST_DSN"])
        except BaseException as ex:
            failures.append(ex)

    thread, errors, done = _thread(do_connect)
    try:
        assert connected.wait(12), (
            "peer never connected or parent never entered handshake read"
        )
        owner = attempt.owner
        assert owner is not None and psutil.pid_exists(owner._osPid)
        receipt = cancelConnect(attempt.token)
        assert receipt is not None and receipt.owner_token == owner.ownerToken
        assert done.wait(4), "blocked handshake read ignored cancellation"
        thread.join(1)
        assert not errors, f"uncaught helper thread exception: {errors}"
        assert failures, "cancelled connect unexpectedly succeeded"
        proof = owner.waitExit(owner.ownerToken, 10)
        assert proof is not None
        assert proof.owner_token == owner.ownerToken and proof.os_pid == owner._osPid
        assert not psutil.pid_exists(owner._osPid)
        _evidence("qp09-cancel-handshake.json", {
            "result": "PASS", "peer_accepted": True,
            "handshake_read_started": True, "attempt_cancelled": True,
            "owner_token_matches": True, "exit_proof": repr(proof),
            "external_pid_absent": True,
        })
    finally:
        cancelConnect(attempt.token)
        thread.join(2)


def test_close_interrupts_real_blocked_recv():
    from psycopg.pq._qt_process_pipe import PipeListenerSocket, connectPipe

    listener = PipeListenerSocket()
    client = connectPipe(listener.address)
    server, _ = listener.accept()
    started = threading.Event()

    def receive():
        started.set()
        with pytest.raises(OSError):
            server.recv(10)

    thread, errors, done = _thread(receive)
    try:
        assert started.wait(2)
        time.sleep(0.05)
        server.close()
        server.close()
        assert done.wait(3), "blocked ReadFile survived PipeStream.close()"
        thread.join(1)
        assert not errors, f"uncaught helper thread exception: {errors}"
    finally:
        server.close()
        client.close()
        listener.close()


def test_reconnect_after_timeout_and_multiple_independent_pipes():
    from psycopg.pq._qt_process_pipe import PipeListenerSocket, connectPipe

    timed_out = PipeListenerSocket()
    timed_out.settimeout(0.02)
    try:
        with pytest.raises(socket.timeout):
            timed_out.accept()
    finally:
        timed_out.close()

    a, b = PipeListenerSocket(), PipeListenerSocket()
    client_a = connectPipe(a.address)
    client_b = connectPipe(b.address)
    stream_a, _ = a.accept()
    stream_b, _ = b.accept()
    try:
        stream_a.sendall(b"first")
        stream_b.sendall(b"second")
        assert client_a.recv(5) == b"first"
        assert client_b.recv(6) == b"second"
        stream_a.close()
        client_b.sendall(b"still alive")
        assert stream_b.recv(11) == b"still alive"
    finally:
        stream_a.close()
        stream_b.close()
        client_a.close()
        client_b.close()
        a.close()
        b.close()


def test_real_truncated_token_handshake_rejected():
    from psycopg.pq._qt_process_pipe import PipeListenerSocket, connectPipe
    from psycopg.pq._qt_process_protocol import recvToken

    listener = PipeListenerSocket()
    client = connectPipe(listener.address)
    server, _ = listener.accept()
    server.settimeout(0.5)
    try:
        client.sendall(b"short-token")
        client.close()
        with pytest.raises(EOFError):
            recvToken(server)
    finally:
        client.close()
        server.close()
        listener.close()


def test_partial_write_is_retried_without_dropping_bytes():
    from psycopg.pq._qt_process_pipe import PipeStream

    class ShortWrite:
        def __init__(self):
            self.data = bytearray()

        def perform(self, kind, buffer, size, deadline):
            assert kind == "write"
            n = min(size, 3)
            self.data.extend(buffer.raw[:n])
            return n

    stream = PipeStream.__new__(PipeStream)
    stream._pipe = ShortWrite()
    stream._writeLock = threading.Lock()
    stream._timeout = 0.5
    stream.sendall(b"abcdefghijk")
    assert stream._pipe.data == b"abcdefghijk"
