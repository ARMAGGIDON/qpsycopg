"""Real OS-process checks for the qt-process owner API.

Run with PSYCOPG_IMPL=qt-process and QPSYCOPG_OWNER_TEST_DSN pointing to
an isolated PostgreSQL 18 test instance. No tests kill processes by PID.
"""

from __future__ import annotations

import os
import json
import time
import socket
import threading
from pathlib import Path

import pytest

import psycopg
from psycopg import pq

# Some upstream CI jobs run installed wheels without this fork-only module.
_owner = pytest.importorskip("psycopg.pq._qt_process_owner")
ExitState = _owner.ExitState
cancelConnect = _owner.cancelConnect
newConnectAttempt = _owner.newConnectAttempt

def _emitEvidence(name: str, evidence: dict[str, object]) -> None:
    serialized = json.dumps(evidence, sort_keys=True)
    print(f"{name}={serialized}", flush=True)
    output_dir = os.environ.get("QPSYCOPG_OWNER_EVIDENCE_DIR")
    if output_dir:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name.lower()}.json").write_text(
            serialized + "\n", encoding="utf-8"
        )


_DSN = os.environ.get("QPSYCOPG_OWNER_TEST_DSN")
pytestmark = pytest.mark.skipif(
    pq.__impl__ != "qt-process" or not _DSN,
    reason="requires qt-process and isolated PostgreSQL 18 DSN",
)


def _connect(attempt):
    assert _DSN
    with attempt:
        return psycopg.connect(_DSN, connect_timeout=5, autocommit=True)


def test_real_owner_exit_and_unaffected_neighbor():
    psutil = pytest.importorskip("psutil")
    first_attempt, second_attempt = newConnectAttempt(timeout_s=15), newConnectAttempt(
        timeout_s=15
    )
    first = _connect(first_attempt)
    second = _connect(second_attempt)
    owner = first_attempt.owner
    neighbor = second_attempt.owner
    assert owner is not neighbor
    assert owner.ownerToken != neighbor.ownerToken
    pid = owner.receipt(owner.ownerToken).os_pid
    assert pid and pid != neighbor.receipt(neighbor.ownerToken).os_pid
    os_process = psutil.Process(pid)
    original_birth = os_process.create_time()
    worker_done = threading.Event()

    def busy_sql():
        try:
            first.execute("SELECT pg_sleep(20)")
        except psycopg.Error:
            pass
        finally:
            worker_done.set()

    sql_thread = threading.Thread(target=busy_sql, daemon=True)
    sql_thread.start()
    time.sleep(0.3)
    try:
        # Only the bound owner object is authorized to receive this request.
        kill_started = time.monotonic()
        queued = owner.requestKill(owner.ownerToken)
        assert queued.state in (
            ExitState.KILL_QUEUED,
            ExitState.KILL_DISPATCHED,
            ExitState.OS_EXIT_CONFIRMED,
        )
        proof = owner.waitExit(owner.ownerToken, 10.0)
        assert proof is not None, owner.receipt(owner.ownerToken)
        assert proof.owner_token == owner.ownerToken
        assert proof.os_pid == pid
        assert proof.timestamp >= queued.timestamp
        assert owner.receipt(owner.ownerToken).state == ExitState.OS_EXIT_CONFIRMED
        owner.thread.join(3)
        assert (
            not owner.thread.is_alive()
        ), "confirmed kill leaked QProcess owner thread"
        assert worker_done.wait(10)
        try:
            current = psutil.Process(pid)
        except psutil.NoSuchProcess:
            external_exited = True
        else:
            external_exited = (
                not current.is_running() or current.create_time() != original_birth
            )
        assert external_exited, "QProcess proof disagrees with external OS observer"
        assert second.execute("SELECT %s::integer", (42,)).fetchone() == (42,)
        first.close()
        assert owner.exitProof(owner.ownerToken) == proof
        assert owner.requestKill(owner.ownerToken).state == ExitState.OS_EXIT_CONFIRMED
        _emitEvidence(
            "QP08_OS_EXIT_EVIDENCE",
            {
                "attempt_token": str(first_attempt.token.value),
                "owner_token": str(proof.owner_token.value),
                "os_pid": proof.os_pid,
                "exit_code": proof.exit_code,
                "exit_status": proof.exit_status,
                "monotonic_exit_timestamp": proof.timestamp,
                "reason": proof.reason,
                "kill_receipt": {
                    "state": queued.state.value,
                    "timestamp": queued.timestamp,
                    "os_pid": queued.os_pid,
                },
                "exit_proof": {
                    "owner_token": str(proof.owner_token.value),
                    "os_pid": proof.os_pid,
                    "exit_code": proof.exit_code,
                    "exit_status": proof.exit_status,
                    "timestamp": proof.timestamp,
                },
                "duration_seconds": time.monotonic() - kill_started,
                "worker_settled": worker_done.is_set(),
                "external_process_exited": external_exited,
                "neighbor_healthy": True,
                "owner_thread_exited": not owner.thread.is_alive(),
                "timeout_or_unconfirmed": False,
            },
        )
    finally:
        try:
            first.close()
        except psycopg.Error:
            pass
        second.close()


def test_cancel_hung_connect_before_connection_publication():
    # PostgreSQL wire endpoint deliberately never sends startup response.
    # This is a local test listener, NOT a production PostgreSQL server.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    ready = threading.Event()
    done = threading.Event()
    attempt = newConnectAttempt(
        lambda _attempt_token, _owner_token, _owner: ready.set(), timeout_s=12
    )
    result = []

    def hang_server():
        try:
            listener.settimeout(8)
            peer, _ = listener.accept()
            with peer:
                done.wait(6)
        except OSError:
            pass

    def connect_worker():
        try:
            with attempt:
                conn = psycopg.connect(
                    f"host={host} port={port} dbname=postgres user=postgres connect_timeout=25"
                )
                result.append("UNEXPECTED_SUCCESS")
                conn.close()
        except Exception as error:
            result.append(type(error).__name__)

    server_thread = threading.Thread(target=hang_server, daemon=True)
    client_thread = threading.Thread(target=connect_worker, daemon=True)
    server_thread.start()
    client_thread.start()
    try:
        assert ready.wait(5), "owner not published before CONNECTING"
        start = time.monotonic()
        receipt = cancelConnect(attempt.token)
        cancel_elapsed = time.monotonic() - start
        assert cancel_elapsed < 0.3
        assert receipt is not None
        client_thread.join(10)
        assert not client_thread.is_alive(), "CONNECTING worker did not settle"
        assert result and result != ["UNEXPECTED_SUCCESS"], result
        proof = attempt.waitExit(10)
        assert proof is not None, attempt.owner.receipt(attempt.ownerToken)
        assert attempt.exitProof() == proof
        assert cancelConnect(attempt.token).state == ExitState.OS_EXIT_CONFIRMED
        _emitEvidence(
            "QP08_CONNECT_CANCEL_EVIDENCE",
            {
                "attempt_token": str(attempt.token.value),
                "owner_token": str(proof.owner_token.value),
                "os_pid": proof.os_pid,
                "exit_code": proof.exit_code,
                "exit_status": proof.exit_status,
                "reason": proof.reason,
                "monotonic_exit_timestamp": proof.timestamp,
                "kill_receipt": {
                    "state": receipt.state.value,
                    "timestamp": receipt.timestamp,
                    "os_pid": receipt.os_pid,
                },
                "exit_proof": {
                    "owner_token": str(proof.owner_token.value),
                    "os_pid": proof.os_pid,
                    "exit_code": proof.exit_code,
                    "exit_status": proof.exit_status,
                    "timestamp": proof.timestamp,
                },
                "cancel_elapsed_seconds": cancel_elapsed,
                "duration_seconds": time.monotonic() - start,
                "worker_settled": not client_thread.is_alive(),
                "late_success_rejected": result != ["UNEXPECTED_SUCCESS"],
                "timeout_or_unconfirmed": False,
            },
        )
    finally:
        done.set()
        listener.close()
        server_thread.join(2)
