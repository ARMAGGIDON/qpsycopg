Qt-process owner lifecycle (QP08)
===================================

Scope
-----

This is a **fork-only** optional API. Upstream `psycopg.connect()` signatures
and other pq backends are unchanged. Only `PSYCOPG_IMPL=qt-process` supports
this lifecycle contract. The old `owner.kill()` still queues a command; it is
**not** evidence of process termination.

Managed connecting example
--------------------------

Create a `ConnectAttempt` in the controller (GUI) thread. Store it under the
controller's request ID; pass the *object* into the worker. Its context must
be entered in the thread actually calling `psycopg.connect()`::

    from psycopg.pq._qt_process_owner import newConnectAttempt, cancelConnect
    from psycopg import pq

    assert pq.__impl__ == "qt-process"  # fail closed if unsupported

    def on_owner_created(attempt_token, owner_token, owner_handle):
        # Called BEFORE the owner thread and blocking CONNECTING.
        # Publish an immutable (attempt_token, owner_token, owner_handle)
        # binding into the thread-safe SessionRegistry provisional record.
        # Do not call Qt widget methods directly from this worker thread.
        registry.publishOpeningOwner(
            request_id, attempt_token, owner_token, owner_handle
        )

    attempt = newConnectAttempt(on_owner_created, timeout_s=12.0)

    # Inside DBPT ConnectTask.run(), not in the GUI/controller thread:
    with attempt:
        conn = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        if attempt.cancelled:
            conn.close()
            raise RuntimeError("stale connection result")
        # Existing DBPT generation/epoch checks still gate final registration.

    # From DBPT DatabaseController.requestCancelConnect() / watchdog:
    receipt = cancelConnect(attempt.token)  # bounded, no worker join
    # receipt may be None if no QProcess existed yet.
    # Keep the original attempt and verify exact-owner exit off GUI:
    proof = attempt.waitExit(0.0)  # nonblocking poll
    # proof is None => EXIT_UNCONFIRMED, not CLOSED.

The `registry.publishOpeningOwner` call is pseudocode for a NEW provisional
binding operation that DBPT must implement independently. No DBPT file or
`uv.lock` is changed in this fork PR.

API and states
--------------

`psycopg.pq._qt_process_owner` exports:

* `OwnerToken` and `AttemptToken`: immutable UUIDv4-backed dataclasses.
* `newConnectAttempt(onOwnerCreated=None, *, timeout_s=None)`: creates a
  thread-scoped context manager; callback signature is
  `(attempt_token, owner_token, owner_handle)`.
* `cancelConnect(attempt_token)`: requests cancellation for precisely the
  registered attempt; returns `KillReceipt | None`. Unknown tokens raise
  `LookupError`, wrong types raise `TypeError`.
* `ConnectAttempt.owner` / `owners` / `ownerToken`, `cancel()`,
  `exitProof()`, `waitExit(timeout)`, `check()`, `remaining()`.
* `owner.requestKill(owner_token) -> KillReceipt`, `owner.receipt(owner_token)`,
  `owner.exitProof(owner_token) -> ExitProof | None`, and
  `owner.waitExit(owner_token, timeout) -> ExitProof | None`. Wrong tokens
  raise `ValueError`.
* `owner.exitConfirmed() -> bool` for the DBPT T08 conservative compatibility
  adapter. This is TRUE only if the exact QProcess reached NotRunning with
  a previously captured OS PID and an exit observation.

The receipt state is one of `EXIT_UNCONFIRMED`, `KILL_QUEUED`,
`KILL_DISPATCHED`, `OS_EXIT_CONFIRMED`. A queued command, dispatched
`QProcess.kill()`, a returned `Connection.close()`, and a dead Python
thread do **not** separately establish OS exit. `ExitProof` includes owner
token, OS PID, QProcess exit code/status, monotonic timestamp and cause.

Lifecycle and limitations
-------------------------

Explicitly managed attempts create independent QProcesses rather than reuse
the lazy worker pool. Managed startup uses loopback TCP instead of Windows
`ConnectNamedPipe`, whose synchronous accept cannot honor a Python socket
timeout. Worker startup/accept have finite wait/check points, and a supplied
attempt deadline bounds its connect IPC wait. The cancellation request does
not require the connecting worker to finish and can interrupt the TCP stream.
Late connections must still be rejected by DBPT's request-ID, generation and
profile-epoch checks.

A control-socket `cancel_safe(timeout=...)` uses its own bounded socket
roundtrip. Managed transport `close()` does not wait for an occupied SQL lock
or join the owner thread. Owner kill/wait attempts are finite inside the
owner thread. **Actual OS termination is not guaranteed by a deadline**:
if QProcess cannot confirm a stopped worker, `waitExit(timeout)` returns
`None`; callers must retain `EXIT_UNCONFIRMED` and quarantine the session.
The default legacy named-pipe fast path may still have blocking native read
operations; managed DBPT attempts deliberately avoid it.

Tests
-----

Unit and opt-in OS process tests::

    python -m pytest -q tests/pq/test_qt_owner_lifecycle.py
    # Only on isolated PostgreSQL 18, never on a production server:
    # PSYCOPG_IMPL=qt-process
    # QPSYCOPG_OWNER_TEST_DSN=postgresql://.../isolated_test_db
    python -m pytest -q tests/pq/test_qt_owner_live.py

The live test uses two exact physical owners, issues SQL `pg_sleep`, kills
only one owner, checks the neighboring query, and corroborates the OS PID
with `psutil`. A second test cancels CONNECTING against a local deliberately
stalled PostgreSQL-wire endpoint. On Windows prefer an isolated
`postgres:18` container exposed only to `127.0.0.1`. Set
`QPSYCOPG_WORKER_IMPL=python` (or validated C) and install `psutil`.

Acceptance is provisional until the fork's Windows/PG18 QProcess tests and a
separate DBPT locked-fork update re-run T08. A CI success on Linux alone does
not prove the Windows OS-exit contract.
