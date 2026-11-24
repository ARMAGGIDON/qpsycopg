QP10: cancellation control lifecycle
====================================

Status: accepted with restrictions for Windows qt-process / PostgreSQL 18
and the DBPT T17 integration retest. Local Windows evidence supplied by the
user and reviewed in PR #6; not a claim of independently reproduced CI.

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
retirement; the separate live suite checks real pooled generation, failure
after authenticated setup, sync/async cancellation and qt-thread close.

No DBPT uv.lock change in this task. The DBPT workload must be rerun against
an accepted target-branch revision before T17 can be closed.

Local Windows evidence
----------------------

Runtime candidate: 54b3506141d356370478987e16b946691a8671c5.
Final tested feature code: aadefc51cd16e6233e235e487a561971de372ed0.
The latter changes only the sequential Win32 exit test, the qt-thread
legacy retirement adapter, and the additional live tests relative to the
120-cycle candidate. QProcess production code is unchanged.

* Deterministic baseline test: duplicate initialization, 2 opens (expected
  failure); fixed unit suite: 19 passed (11 QP10, 8 QP08).
* Windows PostgreSQL 18.6 Python-worker: 3/3 Pipe smoke; 60/60 Pipe and
  60/60 TCP controlled races. Each full run had 60 opens and 60 publications,
  zero duplicate opens, initialization timeouts, failed iterations,
  unexpected owner kills or remaining children. Healthy reuse and neighbor
  isolation verified each cycle. 122 exact Win32 exit proofs across both
  full runs, including the two neighbor processes.
* QP08/QP09: 23 of 24 initial Pipe tests passed; the sequential worker test
  failed on immediate numeric PID presence after graceful ExitProof.
  Commit 2f491de7b10dfc81fe20eca43634efcacf210d84 replaces that ambiguous
  assertion with a process HANDLE acquired before termination, matching
  exit code and a bounded owner-thread join. Retest: 60/60 exact process
  signals and joins, no remaining children. TCP owner regression: 2/2.
  Initial stress: 6282 overlapped I/O iterations in 180.092 s, handles
  208 -> 207, peak 209, zero pending reapers or children; concurrent
  independent workers: 40/40 exact OS exit witnesses.
* Live Python-worker matrix: qt-process Pipe/TCP 4/4 each, qt-thread
  Pipe/TCP 2/2 each, 4 expected QProcess-only skips. The qt-thread legacy
  close adapter is restored in 0770d313f2e185d8a47cb6d15e2f41910fd75f76;
  the QProcess single-flight guarantee does not extend to qt-thread.
* Local C extension build succeeded after resolving the missing pg_config
  environment dependency. C-worker live/owner regression: Pipe 6/6 in
  10.28 s, TCP 6/6 in 7.51 s. These are the requested C-worker runs,
  not separate child-provenance traces.

Full runtime latency (milliseconds, min / median / p95 / max):

.. list-table::
   :header-rows: 1

   * - Metric
     - Pipe
     - TCP
   * - Setup
     - 3.269 / 4.100 / 6.155 / 7.463
     - 4.192 / 21.217 / 27.766 / 34.153
   * - Cold cancel
     - 11.002 / 13.912 / 34.714 / 39.078
     - 13.032 / 30.624 / 39.957 / 72.526
   * - Warm cancel
     - 6.210 / 9.644 / 27.794 / 33.753
     - 6.359 / 17.057 / 31.650 / 48.097
   * - Control request
     - 6.180 / 9.747 / 30.483 / 34.372
     - 6.048 / 9.873 / 29.877 / 48.074
   * - Warm settlement
     - 4.723 / 11.009 / 29.168 / 34.342
     - 5.690 / 19.215 / 33.428 / 49.945

Baseline DBPT D07: normal median 25.912 ms, failure about 10024-10034 ms;
candidate focused tests show no ten-second stall. The baseline consumer
workload and the isolated fork runner are different fixtures. Do not
interpret these measurements as a controlled end-to-end speedup ratio.
The old revision was exercised with the deterministic test, not a full
60-cycle physical baseline run.

Full runtime parent resources before -> after GC: Pipe handles 194 -> 199,
threads 5 -> 4, RSS 54,636,544 -> 56,946,688 bytes; TCP handles 194 -> 202,
threads 5 -> 5, RSS 54,661,120 -> 57,356,288 bytes. No remaining children.
Residual parent handles and RSS are not individually classified. These
finite samples do not prove universal absence of leaks; DBPT D08 remains
outside scope.

Compatibility and restrictions
------------------------------

Private cancellation setup APIs now route through CancelControlLifecycle
and CancelControlClient.open accepts an optional deadline and request_open
callback. connectPipe accepts a timeout keyword. No new public Connection
API, mixed-version IPC compatibility, SQL scheduler or watchdog changes.
The original qpsycopg public cancel_safe signature is unchanged.

Acceptance is scoped to the tested local Windows configurations. Full
upstream CI, lint/type matrix and independently reproduced reviewer runs
are not available in this evidence set; no green result is asserted for
them. The later pytest logs do not embed a git SHA or a child backend
identity, so provenance relies on the requested checkout/environment.
Unmanaged connection reuse was tested sequentially; arbitrary concurrent
use/close of a stale high-level connection is not a supported guarantee.
The existing qt-thread single-flight limitations are not solved here.
Recheck the DBPT mixed/focused cancellation workload and resource behavior
after pinning the accepted fork revision. Do not treat QP10 as closure of
D08 or T17.
