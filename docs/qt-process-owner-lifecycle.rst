:orphan:

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
the lazy worker pool. QP09 selects physical IPC independently of this attempt:
``QPSYCOPG_IPC=pipe`` means a real Windows named pipe; ``tcp`` keeps loopback
TCP. Both managed and unmanaged connections must use the requested transport.
A Pipe listener now uses overlapped ``ConnectNamedPipe`` and cancelable
``ReadFile``/``WriteFile``; the parent can abort a blocked accept or token
handshake without joining the connecting thread. The accepted client PID is
checked against the exact QProcess PID prior to authentication. Failure to
initialize Pipe is fatal and never converted into a TCP connection.
Late connections must still be rejected by DBPT's request-ID, generation and
profile-epoch checks. See :doc:`qt-process-ipc-pipe` for Win32, ACL, timeout,
error and benchmark details.

A control-socket `cancel_safe(timeout=...)` uses its own bounded socket
roundtrip. Managed transport `close()` does not wait for an occupied SQL lock
or join the owner thread. Owner kill/wait attempts are finite inside the
owner thread. **Actual OS termination is not guaranteed by a deadline**:
if QProcess cannot confirm a stopped worker, `waitExit(timeout)` returns
`None`; callers must retain `EXIT_UNCONFIRMED` and quarantine the session.
All Windows named-pipe instances created by QP09 use overlapped handles.
The earlier synchronous CRT/C native named-pipe fast paths are intentionally
disabled for these handles; no implicit blocking read is permitted.
CancelIoEx cancellation completion and OS-exit observation are separate events.

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


QP08-REV: DBPT T04/T07/T08 integration contract (not applied in DBPT)
---------------------------------------------------------------------

DBPT's current `SessionRegistry.bindTransport()` requires an established
connection and therefore cannot publish owner identity during CONNECTING.
The DBPT integration must add a thread-safe provisional owner record tied
to the full SessionHandle (session_id, generation, profile_epoch), request ID,
AttemptToken and OwnerToken. Pseudocode (not runnable on unmodified DBPT)::

    # In DatabaseController.openSession(), before ConnectTask.start():
    handle = registry.registerOpening(
        request_id, fingerprint, purpose, profile_epoch,
        owner_id=f"ConnectAttempt:{request_id}",
    )

    def on_owner_created(attempt_token, owner_token, owner_handle):
        # This callback is in ConnectTask's worker thread, not the GUI.
        registry.publishOpeningOwner(
            handle, request_id, attempt_token, owner_token, owner_handle
        )

    qp_attempt = newConnectAttempt(on_owner_created, timeout_s=12.0)
    task = ConnectTask(profile, cancel_event, request_id=request_id,
                       qp_attempt=qp_attempt)

    # In ConnectTask.run(), on the same connecting worker thread:
    with qp_attempt:
        conn = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        # The controller (not ConnectTask) decides whether to accept it.

    # In DatabaseController.requestCancelConnect() / watchdog:
    cancel_event.set()
    receipt = cancelConnect(qp_attempt.token)  # no worker join
    # A receipt alone never means the QProcess has exited.

The new registry operation must atomically validate request_id, generation,
profile_epoch, token types and uniqueness, and the session state. It must
never overwrite a different owner or grant a stale successful connection.
In `_onConnectResult`, cross-check the provisional binding against the
Connection's actual transport/owner, then use `bindTransport` only for an
admissible live generation. Reject cancelled/late results, retire their
physical owners and prohibit scheduler admission or TEMP cache reuse.

DBPT state mapping:

* `KILL_QUEUED`: existing `KILL_ENQUEUED` notification, no exit assertion.
* `KILL_DISPATCHED`: QProcess.kill was dispatched in the owning thread;
  still no confirmed termination.
* `OS_EXIT_CONFIRMED`: exact ExitProof with OS PID, exit code/status and
  monotonic timestamp; CLOSED is allowed only after owner validation and
  SQL worker/scheduler lease settlement.
* `EXIT_UNCONFIRMED`: remain revoked and quarantined; preserve owner
  evidence and do not emit shutdown-completed.

The independent watchdog may call `cancelConnect()` and inspect the
nonblocking receipt, but it must not block the GUI on `waitExit()`.
An off-GUI observer may use `owner.waitExit(owner_token, timeout)` and relay
the result to DatabaseController on its owning Qt thread. Preserve the
T07/T08 settlement order: SQL worker completed, recovery decision, lease
release, then final retirement. No DBPT changes are made by QP08-REV.

Windows PostgreSQL 18 — isolated manual acceptance (not yet executed)
---------------------------------------------------------------------

Run in PowerShell from the qpsycopg root on a Windows machine with Docker
Desktop Linux containers enabled and Windows libpq available to Python.
Set `PSYCOPG_PG_ROOT` to the installed PostgreSQL client root if needed::

    docker run --rm -d --name qp08-pg18 -e POSTGRES_PASSWORD=qp08test -e POSTGRES_DB=qp08test -p 127.0.0.1:55432:5432 postgres:18
    docker exec qp08-pg18 pg_isready -U postgres -d qp08test
    python -m pip install -e './psycopg[qt]' pytest psutil
    $env:PYTHONPATH = Join-Path (Get-Location).Path 'psycopg'
    $env:PSYCOPG_IMPL = 'qt-process'
    $env:QPSYCOPG_WORKER_IMPL = 'python'
    $env:QPSYCOPG_IPC = 'tcp'
    $env:QPSYCOPG_OWNER_TEST_DSN = 'host=127.0.0.1 port=55432 dbname=qp08test user=postgres password=qp08test'
    $env:QPSYCOPG_OWNER_EVIDENCE_DIR = Join-Path (Get-Location).Path 'qp08-evidence'
    python -m pytest -q --capture=tee-sys tests/pq/test_qt_owner_lifecycle.py tests/pq/test_qt_owner_live.py

    # Collect qp08-evidence/*.json, then stop the disposable database:
    docker rm -f qp08-pg18

QP08 live tests emit structured JSON lines and write JSON files when
`QPSYCOPG_OWNER_EVIDENCE_DIR` is set, recording owner/attempt tokens,
OS PID, kill receipt, ExitProof, monotonic timestamps, duration,
worker settlement and neighbor health. The Qt workflow uploads them as
artifacts from Linux PG18, Windows Python-worker and Windows C-worker
jobs. A successful Windows hosted runner against PG17 does not prove
Windows PG18. For C-worker, first install a compiled psycopg_c wheel,
validate the worker is a native .pyd and set `QPSYCOPG_WORKER_IMPL=c`
before repeating the test command.
