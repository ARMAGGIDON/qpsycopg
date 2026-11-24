QP10: cancellation control lifecycle
====================================

Status: candidate, not independently accepted. No Windows results are claimed
until the runner and regression suites have been executed.

Baseline: 1f07f105902905df474888ab12aa0e94749496f9 (QP09). DBPT T17 handoff
at 549a442823f646bbb774b9730fa65e386abc7e0c documents 15/89 mixed failures,
3/17 focused failures and 3/14 IPC trace failures. The normal median is
25.912 ms; duplicate Pipe opens stalled about 10 seconds before any actual
cancel request. These are consumer baseline measurements, not QP10 results.

Root cause
----------

The lazy first-send path held the SQL RPC lock while testing and creating
the control client. The high-level fallback independently checked/opened
the same single-peer listener. The SQL lock did not serialize that second
check and publication. Acquiring that lock for every cancel is not a safe
fix: active SQL can hold it indefinitely. In addition, an uncached
connection status read can issue a SQL RPC before cancellation starts.

Model
-----

Each PGconn generation owns a condition and a single client:

* EMPTY -> OPENING: one caller becomes initializer. Waiters do not open.
* OPENING -> READY: authenticated peer acknowledgement received, client
  live, current PGconn and owner still valid, publication under the
  condition and owner state lock.
* OPENING/READY -> FAILED: uncertain initialization or disconnected
  channel. No retry against that single-peer listener. Exact physical
  owner is retired, not silently replaced.
* Any -> RETIRED: close/revoke/owner exit clears publication and aborts
  the socket without waiting on a blocked SQL RPC or a cancel request.
  Late clients are aborted, never published.

Lazy send rendezvous is before SQL RPC lock acquisition. Cold CONTROL_OPEN
takes the RPC lock only during setup, after releasing the condition.
READY cancellation does not acquire SQL RPC lock or query status.
Owner state changes release the owner lock before invoking retirement.
Control request serialization is independent. Managed owners never enter
the worker pool. An idle healthy unmanaged generation can be recycled only
after acknowledged worker finish; a new PGconn has a new condition/client.
Opening, failed or active-control retirement is not eligible for recycling.

Deadlines
---------

High-level cancel_safe(timeout) now budgets waiting for initialization,
CONTROL_OPEN lock/IPC, Pipe/TCP connection, authentication acknowledgement,
serialized cancellation and response. Cold send uses a separate default
10-second setup budget; a concurrent cancel waiter keeps its own deadline
and does not start a second open if its wait expires. The independent Pipe
connect timeout is no longer hardcoded for control setup. Timeout is
reported as CancellationTimeout on the high-level path where applicable.
Uncertain transport failure retires the owner. Socket timeouts apply to
individual I/O operations; this is not a hard real-time bound under arbitrary
partial-frame progress or OS scheduling. Close and verified OS exit remain
separate phases. The dedicated control authentication handshake now includes
worker acknowledgement, shared with qt-thread; mixed-version parent/worker
protocol is not supported. No new TCP fallback, watchdog change or DBPT change.

Cancellation before an SQL statement is sent does not guarantee cancellation
of that future statement. The runtime runner separately checks active SQL
cancellation, query settlement, subsequent healthy reuse and neighbor isolation.
Legacy libpq without cancel_safe retains its pre-existing fallback; the
acceptance target is PostgreSQL/libpq 18.

Acceptance
----------

Run the deterministic test on baseline (expected duplicate-open failure) and
feature (expected pass). The Windows runner forces both paths to reach the
old boundary, records opens, request/settlement timing, physical IPC, exact
owner and Win32 process-object exit proof, and process resources. It explicitly
refuses any database other than dbpt_regression. The runner uses only SELECT
queries, including pg_sleep and pg_stat_activity. Baseline runner failures
are evidence, not acceptance. No credentials or IPC tokens are logged.
Run at least 60 completed Pipe iterations, a TCP comparator and the QP08/QP09
regression files, including opt-in stress and applicable worker implementations.
The deterministic suite covers simulated pooled generation, failure and
retirement; real pooled/async, fault scenarios and shared qt-thread compatibility
still require separate verification.

No merge and no DBPT uv.lock change before independent acceptance.
