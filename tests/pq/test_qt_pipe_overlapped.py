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
        (target / name).write_text(
            json.dumps(data, sort_keys=True, indent=2), encoding="utf-8"
        )
    print(f"QP09_OS_EVIDENCE={json.dumps(data, sort_keys=True)}", flush=True)


@pytest.mark.skipif(
    os.environ.get("PSYCOPG_IMPL", "").lower() != "qt-process"
    or os.environ.get("QPSYCOPG_IPC", "").lower() != "pipe",
    reason="requires explicit qt-process Named Pipe integration environment",
)
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


@pytest.mark.skipif(
    os.environ.get("PSYCOPG_IMPL", "").lower() != "qt-process"
    or os.environ.get("QPSYCOPG_IPC", "").lower() != "pipe",
    reason="requires explicit qt-process Named Pipe integration environment",
)
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


def test_win32_errors_have_explicit_mappings():
    from psycopg.pq import _qt_process_pipe as pipe

    assert isinstance(pipe._error(pipe._ERROR_BROKEN_PIPE), BrokenPipeError)
    assert isinstance(pipe._error(pipe._ERROR_PIPE_NOT_CONNECTED), BrokenPipeError)
    assert isinstance(pipe._error(pipe._ERROR_OPERATION_ABORTED), OSError)


def test_cancel_before_qprocess_creation_is_not_exit_proof():
    from psycopg.pq._qt_process_owner import newConnectAttempt

    attempt = newConnectAttempt()
    assert attempt.cancel() is None
    assert attempt.owner is None
    assert attempt.waitExit(0) is None
    with attempt:
        with pytest.raises(RuntimeError, match="cancelled"):
            attempt.check()


@pytest.mark.skipif(
    os.environ.get("PSYCOPG_IMPL", "").lower() != "qt-process"
    or os.environ.get("QPSYCOPG_IPC", "").lower() != "pipe",
    reason="requires explicit qt-process Named Pipe integration environment",
)
def test_concurrent_kill_abort_close_and_neighbor_survives():
    import psutil
    import psycopg
    from psycopg.pq._qt_process_owner import newConnectAttempt, cancelConnect

    dsn = os.environ["QPSYCOPG_OWNER_TEST_DSN"]
    attempt_a = newConnectAttempt(timeout_s=12)
    attempt_b = newConnectAttempt(timeout_s=12)
    with attempt_a:
        first = psycopg.connect(dsn)
    with attempt_b:
        second = psycopg.connect(dsn)
    owner = attempt_a.owner
    assert owner is not None
    assert first.pgconn._transport.ipcKind == "pipe"
    assert second.pgconn._transport.ipcKind == "pipe"
    assert first.pgconn._transport.owner is owner
    assert second.pgconn._transport.owner is not owner
    begun = threading.Event()

    def blocked_query():
        begun.set()
        try:
            first.execute("select pg_sleep(20)")
        except psycopg.Error:
            return
        raise AssertionError("blocked SQL unexpectedly succeeded after worker kill")

    query_thread, query_errors, query_done = _thread(blocked_query)
    try:
        assert begun.wait(2)
        time.sleep(0.2)
        closer, close_errors, close_done = _thread(first.pgconn._transport.close)
        receipt = cancelConnect(attempt_a.token)
        assert receipt is not None and receipt.owner_token == owner.ownerToken
        second_receipt = cancelConnect(attempt_a.token)
        assert second_receipt is not None
        assert second_receipt.owner_token == owner.ownerToken
        owner.requestKill(owner.ownerToken)
        assert close_done.wait(4), "close deadlocked with kill/cancel"
        assert query_done.wait(4), "blocked SQL I/O survived kill/cancel"
        closer.join(1)
        query_thread.join(1)
        assert not close_errors, f"close helper exception: {close_errors}"
        assert not query_errors, f"query helper exception: {query_errors}"
        proof = owner.waitExit(owner.ownerToken, 10)
        assert proof is not None and proof.owner_token == owner.ownerToken
        assert proof.os_pid == owner._osPid
        assert not psutil.pid_exists(proof.os_pid)
        assert second.execute("select 123").fetchone() == (123,)
    finally:
        first.close()
        second.close()
        owner.requestKill(owner.ownerToken)
        owner.waitExit(owner.ownerToken, 10)


def test_reaper_start_failure_keeps_overlapped_until_kernel_completion(monkeypatch):
    """Fault-inject thread exhaustion after CancelIoEx, without mocking Win32 I/O."""
    from psycopg.pq import _qt_process_pipe as pipe

    listener = pipe.PipeListenerSocket()
    listener.settimeout(0.0)
    handle = listener._pipe
    assert handle is not None
    thread_start = threading.Thread.start

    def fail_reaper_start(self):
        if self.name == "qpsycopg-pipe-reaper":
            raise RuntimeError("injected reaper thread startup failure")
        return thread_start(self)

    # Force the deferred branch even if the kernel has completed the
    # cancellation before the next 25 ms poll. GetOverlappedResult remains real.
    with monkeypatch.context() as patch:
        patch.setattr(
            pipe, "_waitEvent", lambda event, milliseconds: pipe._WAIT_TIMEOUT
        )
        patch.setattr(pipe, "_CANCEL_DRAIN_SECONDS", 0.0)
        patch.setattr(threading.Thread, "start", fail_reaper_start)
        try:
            with pytest.raises(RuntimeError, match="injected reaper"):
                listener.accept()
        finally:
            listener.close()
    assert not handle._pending, "a failed reaper start orphaned pending OVERLAPPED I/O"
    assert handle.handle is None


def test_bounded_overlapped_capacity_preserves_kernel_owned_storage(monkeypatch):
    """A stalled completion holds one global slot; other Pipe calls fail fast."""
    from psycopg.pq import _qt_process_pipe as pipe

    # Use a one-slot budget for deterministic fault injection. The real
    # production limit is 128 process-wide and cannot grow at runtime.
    monkeypatch.setattr(pipe, "_inflight_io", threading.BoundedSemaphore(1))
    first, second = pipe.PipeListenerSocket(), pipe.PipeListenerSocket()
    first.settimeout(0.0)
    second.settimeout(0.02)
    entered, release, finished = (threading.Event() for _ in range(3))
    real_reap = pipe._PipeHandle._reap

    def hold_reap(self, operation):
        entered.set()
        try:
            assert release.wait(5), "injected stalled completion was not released"
            return real_reap(self, operation)  # Actual Win32 completion barrier.
        finally:
            finished.set()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(pipe._PipeHandle, "_reap", hold_reap)
            # Force the deferred branch without faking OS I/O completion.
            patch.setattr(
                pipe, "_waitEvent", lambda event, milliseconds: pipe._WAIT_TIMEOUT
            )
            patch.setattr(pipe, "_CANCEL_DRAIN_SECONDS", 0.0)
            with pytest.raises(socket.timeout):
                first.accept()
            assert entered.wait(2), "deferred completion did not enter reaper"
            assert first._pipe is not None and len(first._pipe._pending) == 1

            started = time.monotonic()
            with pytest.raises(OSError, match="capacity exhausted"):
                second.accept()
            assert time.monotonic() - started < 0.5
            assert second._pipe is not None and not second._pipe._pending
            # Neither an extra operation nor a second reaper was created.
            assert len(first._pipe._pending) == 1
            release.set()
            assert finished.wait(3), "real kernel completion barrier did not finish"
        assert first._pipe is not None and not first._pipe._pending

        # Once kernel completion releases the slot, regular Win32 I/O works.
        with pytest.raises(socket.timeout):
            second.accept()
        _evidence("qp09-final-bounded-io-capacity.json", {
            "limit_production": pipe._MAX_INFLIGHT_IO,
            "injected_limit": 1,
            "capacity_exhaustion": "fast failure before issuing Win32 I/O",
            "deferred_completion": "real kernel barrier",
            "permit_recovered": True,
        })
    finally:
        release.set()
        if entered.is_set():
            assert finished.wait(3)  # Avoid restoring the budget mid-reap.
        first.close()
        second.close()


def test_overlapped_capacity_returns_after_event_creation_failure(monkeypatch):
    """An allocation failure must not consume an I/O slot indefinitely."""
    from psycopg.pq import _qt_process_pipe as pipe

    monkeypatch.setattr(pipe, "_inflight_io", threading.BoundedSemaphore(1))
    listener = pipe.PipeListenerSocket()
    listener.settimeout(0.02)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(pipe, "_newEvent", lambda *args: None)
            with pytest.raises(OSError):
                listener.accept()
        assert listener._pipe is not None and not listener._pipe._pending
        with pytest.raises(socket.timeout):
            listener.accept()
    finally:
        listener.close()


def test_actual_pipe_dacl_and_handle_inheritance():
    """Read back the kernel object's actual DACL and inherit flag."""
    from psycopg.pq import _qt_process_pipe as pipe

    listener = pipe.PipeListenerSocket()
    try:
        handle = listener._pipe
        assert handle is not None and handle.handle is not None
        flags = pipe.wt.DWORD()
        get_flags = pipe._api(
            pipe._kernel, "GetHandleInformation",
            (pipe.wt.HANDLE, pipe.ctypes.POINTER(pipe.wt.DWORD)), pipe.wt.BOOL,
        )
        assert get_flags(handle.handle, pipe.ctypes.byref(flags))
        assert flags.value & 1 == 0, "Named Pipe HANDLE must not be inherited"

        get_security = pipe._api(
            pipe._advapi, "GetSecurityInfo",
            (pipe.wt.HANDLE, pipe.wt.DWORD, pipe.wt.DWORD,
             pipe.ctypes.c_void_p, pipe.ctypes.c_void_p, pipe.ctypes.c_void_p,
             pipe.ctypes.c_void_p, pipe.ctypes.POINTER(pipe.ctypes.c_void_p)),
            pipe.wt.DWORD,
        )
        descriptor = pipe.ctypes.c_void_p()
        # SE_FILE_OBJECT=1; DACL_SECURITY_INFORMATION=4.
        code = get_security(handle.handle, 1, 4, None, None, None, None,
                            pipe.ctypes.byref(descriptor))
        assert code == 0, f"GetSecurityInfo failed: {code}"
        try:
            to_sddl = pipe._api(
                pipe._advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW",
                (pipe.ctypes.c_void_p, pipe.wt.DWORD, pipe.wt.DWORD,
                 pipe.ctypes.POINTER(pipe.wt.LPWSTR),
                 pipe.ctypes.POINTER(pipe.wt.DWORD)),
                pipe.wt.BOOL,
            )
            sddl_ptr = pipe.wt.LPWSTR()
            assert to_sddl(descriptor, 1, 4, pipe.ctypes.byref(sddl_ptr), None)
            try:
                sddl = sddl_ptr.value
                assert "D:P" in sddl, f"Pipe DACL is not protected: {sddl}"
                assert "SY" in sddl, f"Local SYSTEM missing: {sddl}"
                assert "WD" not in sddl and "AN" not in sddl, (
                    f"Everyone or Anonymous unexpectedly allowed: {sddl}"
                )
            finally:
                pipe._localFree(pipe.ctypes.cast(sddl_ptr, pipe.ctypes.c_void_p))
        finally:
            pipe._localFree(descriptor)
    finally:
        listener.close()


@pytest.mark.skipif(
    os.environ.get("QPSYCOPG_QP09_AUDIT_STRESS") != "1",
    reason="run explicitly with QPSYCOPG_QP09_AUDIT_STRESS=1",
)
def test_qp09_audit_repeated_pipe_io_and_resource_stability():
    """Real Win32 stress covering accept, read cancellation and write timeout."""
    import gc
    import psutil
    from psycopg.pq._qt_process_pipe import PipeListenerSocket, connectPipe

    process = psutil.Process()
    gc.collect()
    before = {
        "handles": process.num_handles(), "threads": process.num_threads(),
        "rss_bytes": process.memory_info().rss,
        "children": len(process.children(recursive=True)),
    }
    payload = b"x" * (8 << 20)
    duration = float(os.environ.get("QPSYCOPG_QP09_STRESS_SECONDS", "30"))
    start = time.monotonic()
    iterations = 0
    peak_handles = before["handles"]
    peak_threads = before["threads"]

    while iterations < 200 or time.monotonic() - start < duration:
        mode = iterations % 4
        listener = PipeListenerSocket()
        client = server = None
        try:
            if mode == 0:
                client = connectPipe(listener.address)
                server, _ = listener.accept()
                client.sendall(b"ok")
                assert server.recv(2) == b"ok"
            elif mode == 1:
                listener.settimeout(0.005)
                with pytest.raises(socket.timeout):
                    listener.accept()
            elif mode == 2:
                client = connectPipe(listener.address)
                server, _ = listener.accept()
                begun = threading.Event()

                def blocked_read():
                    begun.set()
                    with pytest.raises(OSError):
                        server.recv(8)

                reader, errors, done = _thread(blocked_read)
                assert begun.wait(2)
                time.sleep(0.005)
                server.close()
                assert done.wait(3), "blocked read failed to cancel"
                reader.join(1)
                assert not errors
            else:
                client = connectPipe(listener.address)
                server, _ = listener.accept()
                server.settimeout(0.05)
                with pytest.raises(socket.timeout):
                    server.sendall(payload)
        finally:
            if server is not None:
                server.close()
            if client is not None:
                client.close()
            listener.close()
        iterations += 1
        if iterations % 25 == 0:
            peak_handles = max(peak_handles, process.num_handles())
            peak_threads = max(peak_threads, process.num_threads())

    gc.collect()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        reapers = sum(t.name == "qpsycopg-pipe-reaper" for t in threading.enumerate())
        if reapers == 0 and process.num_handles() <= before["handles"] + 16:
            break
        time.sleep(0.1)
    after = {
        "handles": process.num_handles(), "threads": process.num_threads(),
        "rss_bytes": process.memory_info().rss,
        "children": len(process.children(recursive=True)),
    }
    reapers = sum(t.name == "qpsycopg-pipe-reaper" for t in threading.enumerate())
    _evidence("qp09-final-pipe-resource-stress.json", {
        "iterations": iterations,
        "duration_seconds": round(time.monotonic() - start, 3),
        "before": before, "after": after, "peak_handles": peak_handles,
        "peak_threads": peak_threads, "pending_reaper_threads": reapers,
    })
    assert reapers == 0, "overlapped I/O completion reapers did not terminate"
    assert after["handles"] <= before["handles"] + 16, "HANDLE count failed to recover"
    assert after["threads"] <= before["threads"] + 3, "thread count failed to recover"
    assert after["children"] == before["children"], "unexpected child processes"


@pytest.mark.skipif(
    os.environ.get("QPSYCOPG_QP09_AUDIT_STRESS") != "1"
    or os.environ.get("PSYCOPG_IMPL", "").lower() != "qt-process"
    or os.environ.get("QPSYCOPG_IPC", "").lower() != "pipe",
    reason="requires explicit managed Qt Pipe stress environment",
)
def test_qp09_audit_repeated_real_workers_and_exact_exit_proofs():
    """Repeated QProcess creation, normal close, forced kill and reconnect."""
    import ctypes
    import gc
    from ctypes import wintypes as wt

    import psutil
    import psycopg
    from psycopg.pq import _qt_process_pipe as pipe
    from psycopg.pq._qt_process_owner import newConnectAttempt

    # Retain the exact process object before termination, not a numeric PID.
    # ExitProof can be published before the QProcess owner thread releases
    # its process handle. PID presence alone cannot distinguish this from
    # a running process or reuse of the same PID.
    open_process = pipe._api(
        pipe._kernel, "OpenProcess",
        (wt.DWORD, wt.BOOL, wt.DWORD), wt.HANDLE,
    )
    get_exit_code = pipe._api(
        pipe._kernel, "GetExitCodeProcess",
        (wt.HANDLE, ctypes.POINTER(wt.DWORD)), wt.BOOL,
    )

    proc = psutil.Process()
    baseline_children = {p.pid for p in proc.children(recursive=True)}
    before = {"handles": proc.num_handles(), "threads": proc.num_threads(),
              "rss_bytes": proc.memory_info().rss}
    iterations = int(os.environ.get("QPSYCOPG_QP09_WORKER_STRESS_LOOPS", "60"))
    proofs = 0
    owner_threads_joined = 0
    for i in range(iterations):
        attempt = newConnectAttempt(timeout_s=12)
        with attempt:
            connection = psycopg.connect(os.environ["QPSYCOPG_OWNER_TEST_DSN"])
        owner = attempt.owner
        assert owner is not None and owner._osPid is not None
        assert connection.pgconn._transport.ipcKind == "pipe"
        witness = open_process(0x00101000, False, owner._osPid)
        if not witness:
            error = ctypes.WinError(ctypes.get_last_error())
            connection.close()
            raise error
        try:
            assert connection.execute("select 1").fetchone() == (1,)
        finally:
            try:
                if i % 3 == 0:
                    owner.requestKill(owner.ownerToken)
                connection.close()
                owner.requestKill(owner.ownerToken)
                proof = owner.waitExit(owner.ownerToken, 10)
                assert proof is not None
                assert proof.owner_token == owner.ownerToken
                assert proof.os_pid == owner._osPid
                assert pipe._waitEvent(witness, 10_000) == pipe._WAIT_OBJECT_0, (
                    f"iteration {i}: OS process not signalled: {proof!r}"
                )
                exit_code = wt.DWORD()
                assert get_exit_code(witness, ctypes.byref(exit_code))
                assert exit_code.value == proof.exit_code, (
                    f"iteration {i}: Win32 exit code {exit_code.value} "
                    f"!= ExitProof {proof.exit_code}"
                )
                proofs += 1
                owner.thread.join(10)
                assert not owner.thread.is_alive(), (
                    f"iteration {i}: owner thread did not settle"
                )
                owner_threads_joined += 1
            finally:
                pipe._closeHandle(witness)

    gc.collect()
    after_children = {p.pid for p in proc.children(recursive=True)}
    after = {"handles": proc.num_handles(), "threads": proc.num_threads(),
             "rss_bytes": proc.memory_info().rss}
    _evidence("qp09-final-worker-resource-stress.json", {
        "iterations": iterations, "exact_exit_proofs": proofs,
        "owner_threads_joined": owner_threads_joined,
        "method": "OpenProcess + WaitForSingleObject + GetExitCodeProcess",
        "baseline_child_pids": sorted(baseline_children),
        "remaining_child_pids": sorted(after_children),
        "before": before, "after": after,
    })
    assert proofs == owner_threads_joined == iterations
    assert after_children == baseline_children


@pytest.mark.skipif(
    os.environ.get("QPSYCOPG_QP09_AUDIT_STRESS") != "1"
    or os.environ.get("PSYCOPG_IMPL", "").lower() != "qt-process"
    or os.environ.get("QPSYCOPG_IPC", "").lower() != "pipe",
    reason="requires explicit managed Qt Pipe stress environment",
)
def test_qp09_audit_concurrent_independent_qprocess_sessions():
    """Prove exact OS process exit without relying on PID disappearance."""
    import ctypes
    from ctypes import wintypes as wt
    from concurrent.futures import ThreadPoolExecutor

    import psycopg
    from psycopg.pq import _qt_process_pipe as pipe
    from psycopg.pq._qt_process_owner import newConnectAttempt

    # Keep an independent Win32 process HANDLE open from before termination.
    # WaitForSingleObject on this exact object is not affected by PID reuse,
    # and remains valid while Qt still holds its own process HANDLE.
    open_process = pipe._api(
        pipe._kernel, "OpenProcess",
        (wt.DWORD, wt.BOOL, wt.DWORD), wt.HANDLE,
    )
    get_exit_code = pipe._api(
        pipe._kernel, "GetExitCodeProcess",
        (wt.HANDLE, ctypes.POINTER(wt.DWORD)), wt.BOOL,
    )
    dsn = os.environ["QPSYCOPG_OWNER_TEST_DSN"]

    def worker(index):
        confirmed = 0
        for iteration in range(10):
            attempt = newConnectAttempt(timeout_s=12)
            with attempt:
                connection = psycopg.connect(dsn)
            owner = attempt.owner
            assert owner is not None and owner._osPid is not None
            # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION.
            witness = open_process(0x00101000, False, owner._osPid)
            if not witness:
                connection.close()
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                assert connection.execute("select %s", (index,)).fetchone() == (index,)
                if (index + iteration) % 3 == 0:
                    owner.requestKill(owner.ownerToken)
            finally:
                try:
                    connection.close()
                    owner.requestKill(owner.ownerToken)
                    proof = owner.waitExit(owner.ownerToken, 10)
                    assert proof is not None and proof.owner_token == owner.ownerToken
                    assert proof.os_pid == owner._osPid
                    assert pipe._waitEvent(witness, 10_000) == pipe._WAIT_OBJECT_0, (
                        f"QProcess exited without OS process signalling: {proof!r}"
                    )
                    exit_code = wt.DWORD()
                    assert get_exit_code(witness, ctypes.byref(exit_code)), (
                        f"GetExitCodeProcess failed for {proof.os_pid}"
                    )
                    assert exit_code.value == proof.exit_code, (
                        f"Win32 exit code {exit_code.value} "
                        f"!= ExitProof {proof.exit_code}"
                    )
                    confirmed += 1
                finally:
                    pipe._closeHandle(witness)
        return confirmed

    with ThreadPoolExecutor(max_workers=4) as executor:
        counts = list(executor.map(worker, range(4)))
    _evidence("qp09-final-concurrent-worker-stress.json", {
        "workers": 4, "cycles_per_worker": 10,
        "exact_os_exit_witnesses_per_worker": counts,
        "method": "OpenProcess + WaitForSingleObject + GetExitCodeProcess",
    })
    assert counts == [10] * 4
