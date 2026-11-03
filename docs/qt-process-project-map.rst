Qt process backend project map
==============================

This file is the implementation/handoff map for qpsycopg's Qt-oriented pq work.
Read :doc:`qt-process` for behaviour, :doc:`qt-process-performance` for measured
history/rejected experiments, :doc:`qt-process-optimization-log` for the current
checkpoint series, and :doc:`qt-process-coordination` for multi-chat ownership.

Repository state
----------------

* repository: ``ARMAGGIDON/qpsycopg``;
* development branch: ``feature/qt-process-pq``;
* active implementation/optimisation path: ``qt-thread``;
* retained process reference: ``PSYCOPG_IMPL=qt-process``;
* thread implementation: ``PSYCOPG_IMPL=qt-thread``;
* canonical local worker for the current paired benchmark: ``QPSYCOPG_WORKER_IMPL=c``;
* upstream comparison revision: ``99356800e90aa0e7b604f652d714ef0f33422b47``;
* canonical optimisation baseline: ``baseline-v3``;
* accepted runtime optimisation: ``optimized01`` lazy qt-thread cancellation setup;
* ``optimized01`` implementation commits: ``4637ff9`` and ``371bda8``;
* ``optimized01`` measured at ``7c781a97`` with the unchanged v3 benchmark,
  semantic smoke OK; retained for improved connection and resource lifecycle;
* next target: measure and isolate qt-thread short-query CPU/latency overhead,
  keeping cancellation/Qt responsiveness as guardrails.

The current branch has moved beyond the old qt-thread connection blocker. The
complete standalone qt-thread smoke passes. Do not restart work from the old
``64f2ee8`` handoff or from the old process short-query micro-optimisation plan.

Current direction
-----------------

The process-backed implementation already contains the major retained measured
optimisations:

* native ``PGresult -> PackedValues`` packing;
* native packed bulk decoding;
* dedicated cancellation control channel;
* ``10 ms`` worker-side WAIT drain;
* deferred send + one-request fast execute;
* compact/marshal fast frames;
* warmed ``QProcess`` reuse between PostgreSQL sessions;
* cached stable connection state where safe;
* Windows TCP loopback fast path;
* raw Windows named-pipe main transport;
* native parent-side named-pipe frame exchange.

Controlled architecture measurements showed that a dedicated worker thread is
substantially closer to upstream/direct Psycopg than the separate process for
short queries while preserving the required Qt/cancellation semantics. The real
``qt-thread`` backend is therefore the active optimisation path. ``qt-process``
remains a measured reference and an explicit alternative with stronger isolation.

The canonical ``baseline-v3`` established representative qt-thread/upstream
ratios of approximately:

==============================  ===========
Metric                          qt-thread
==============================  ===========
warm connect median             1.409x
SELECT 1 median                 1.273x
short-query CPU                 2.000x
context switches / query        3.032x
100k total                      1.027x
8-thread batch median           1.341x
process-tree RSS                1.320x
private memory                  1.377x
Qt timer p95                    0.982x
Qt timer p99                    1.008x
==============================  ===========

Bulk decode and Qt p95/p99 are not current targets. The remaining stable cost is
primarily short-query fixed overhead plus connection/resource overhead.

Architectural invariants
------------------------

Treat changes to these points as architectural changes, not local optimisations.

#. High-level Psycopg semantics remain in the application process:
   ``Connection``, ``Cursor``, transactions, adaptation, row factories,
   SQLSTATE exception mapping and user callbacks.
#. The stateful libpq ``PGconn`` is worker-owned for both Qt backends.
#. A recycled process worker does not imply a recycled PostgreSQL session.
#. ``qt-process`` and ``qt-thread`` are explicit opt-in backends and never
   implicit fallbacks.
#. The worker pq implementation is ordinary ``c``, ``binary`` or ``python``;
   never recursive ``qt-process``/``qt-thread``.
#. Result adaptation remains parent-side. Worker/native helpers may pack raw
   bytes/metadata but must not replace normal Psycopg adaptation semantics.
#. Psycopg's existing connection lock remains authoritative when a connection is
   shared by caller threads.
#. Synchronous Psycopg calls remain synchronous and block the caller.
#. Ordinary PostgreSQL statement errors must not kill a healthy session.
#. Fatal ``ConnStatus.BAD`` handling must terminate/mark a broken session.
#. Parent-process file descriptors must not be passed to worker libpq.
#. Resource comparisons must use the whole process tree for process-backed modes.
#. Performance claims require compatible paired measurements.

Current qt-process architecture
-------------------------------

On Windows the preferred data path is::

    application process
      |
      +-- Connection / Cursor / adaptation
      |
      +-- synthetic PGconn / PGresult facade
      |
      +-- deferred send / compact fast execute
      |
      +-- raw named pipe
      |     +-- native parent frame exchange when available
      |
      +================ process boundary ================+
                                                        |
                                               reusable QProcess worker
                                                        |
                                               psycopg c/binary/python pq
                                                        |
                                                      libpq

Cancellation uses an independent authenticated localhost TCP control channel with
a worker cancel-control thread and reusable ``PGcancelConn``.

The main Windows transport defaults to ``QPSYCOPG_IPC=pipe``. TCP remains a
fallback. Native worker receive and native socket wait remain disabled by default
because controlled tests did not justify them.

Current qt-thread architecture
------------------------------

``psycopg/psycopg/pq/pq_qt_thread.py`` reuses the synthetic pq facade and worker
state machine but replaces process IPC with ``queue.SimpleQueue`` and a dedicated
worker thread::

    Connection / Cursor
        |
    synthetic PGconn / PGresult
        |
    request/response queues
        |
    qpsycopg worker thread
        |
    psycopg c/binary/python pq
        |
      libpq

The complete standalone semantic smoke passes. It covers adaptation, packed
NULL/empty semantics, multiple result sets, COPY boundaries, SQL error survival,
shared connection/per-thread cursors, cancellation, fatal backend termination and
``AsyncConnection``.

The native remote packed decoder is enabled for qt-thread; the earlier bulk
regression was caused by an accidental pure-Python transformer and is no longer a
current result.

``psycopg/psycopg/pq/_qt_thread_lazy.py`` is the current ``optimized01`` patch.
It defers cancel-control construction until immediately before the first normal
send. ``psycopg/psycopg/__init__.py`` activates that patch only for
``PSYCOPG_IMPL=qt-thread``.

Source map
----------

Backend selection
~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/__init__.py``
    Selects ``qt-process`` and ``qt-thread`` only when explicitly requested.

``psycopg/psycopg/__init__.py``
    Routes both Qt backends through the remote connection classes and activates
    the qt-thread lazy-cancellation patch only for qt-thread.

``psycopg/psycopg/_cmodule.py``
    Keeps the Qt backends on the Python generator layer because the synthetic
    ``PGconn`` is not a local native connection object.

Parent/process facade
~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/pq_qt_process.py``
    Base remote pq facade, generic transport, proxy ``PGconn``/``PGresult`` and
    query/connection caches.

``psycopg/psycopg/pq/_qt_process_lazy.py``
    Process hot-path patch layer: deferred send, compact fast execute,
    process-worker reuse, lazy cancellation, Windows transport selection and
    native parent named-pipe frame exchange.

``psycopg/psycopg/pq/_qt_process_pipe.py``
    Windows named-pipe byte-stream adapter with read-ahead preservation.

Protocol/results
~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/_qt_process_protocol.py``
    Request/response framing, fast frames, result snapshots and ``PackedValues``.

``psycopg_c/psycopg_c/pq/pgresult.pyx``
    Native result value/metadata packers.

``psycopg_c/psycopg_c/_psycopg/qt_bulk.pyx``
    Parent-side packed decoder for supported standard loaders.

``psycopg/psycopg/_transformer.py``
    Preserves normal adaptation semantics while selecting the packed decoder for
    both remote Qt backends where safe.

Worker/cancellation
~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/_qt_process_worker.py``
    Base worker request handling and libpq send/wait/consume/result state machine.

``psycopg/psycopg/pq/_qt_process_worker_pool.py``
    Reusable process worker specialisations, cached connection state, native
    packing capability detection and profiling hooks.

``psycopg/psycopg/pq/_qt_process_cancel.py``
    Independent authenticated cancellation control channel.

``psycopg/psycopg/connection_qt_process.py``
    Shared sync/async remote connection lifecycle and wait/cancel routing used by
    both Qt backends.

``psycopg/psycopg/pq/pq_qt_thread.py``
    Dedicated-thread transport and shared-interpreter worker integration.

``psycopg/psycopg/pq/_qt_thread_lazy.py``
    Current lazy-cancellation lifecycle optimisation for qt-thread.

Validation and benchmark map
----------------------------

``tools/smoke_qt_thread.py``
    Narrow semantic gate for the thread backend.

``tools/validate_qt_thread.ps1``
    Broader direct/thread/process validation retained for structural checks.

``tools/benchmark_qpsycopg.ps1``
    Canonical current paired benchmark runner. It builds isolated upstream/fork
    environments against the same local libpq and runs adjacent upstream pairs
    for ``direct``, ``qt-thread`` and ``qt-process``.

``tools/bench_qt_process.py``
    SQL/connection/concurrency/cancellation/resource workload.

``tools/bench_qt_responsiveness.py``
    Qt p95/p99 responsiveness workload; maximum stall is diagnostic only.

``tools/summarize_qpsycopg_benchmark.py``
    Produces paired ratios, MAD/range, host drift, architecture deltas and process
    inventory.

``tools/profile_qt_thread_short_query.py`` and
``tools/profile_qt_thread_short_query.ps1``
    Diagnostic CPU attribution: upstream/fork direct/qt-thread warm SELECT
    controls; separate process/caller CPU; worker dispatch/wait phase timers;
    synthetic no-SQL status request as a control for handoff cost. The earlier
    Python 3.13 cProfile attribution was rejected after producing negative
    per-function CPU and cross-thread stack contamination. The corrected
    phase-only Windows archive is now analysed and identifies the synchronous
    thread request/response handoff as the largest fixed non-SQL phase.
    Production runtime and canonical paired v3 workload are unchanged.

``tools/bench_qt_thread_echo_baseline.py`` and
``tools/bench_qt_thread_echo_baseline.ps1``
    Diagnostic-only paired comparison of a minimal two-SimpleQueue echo
    against the existing no-SQL qt-thread PGconn status GET path. Uses timed
    response waits in both cases, interleaved blocks, wall/CPU/context-switch
    measurements, a child-process watchdog and a ZIP result archive.
    The Windows comparison is complete: both modes have about two context
    switches/call; qpsycopg has a positive extra ~19 us median paired
    wall delay but strong temporal drift. Neither the residual nor any
    specific allocation is proven removable. Runtime and benchmark v3
    are unchanged.

``tools/bench_qt_thread_enriched_echo.py`` and
``tools/bench_qt_thread_enriched_echo.ps1``
    Coordinator-approved diagnostic-only progressively enriched queue echo:
    bare token, real Request/Response NamedTuple, validation, synthetic worker
    bookkeeping, and real qt-thread no-SQL GET. Independent subprocess runs,
    short balanced blocks, bracketing bare controls and timestamp-based
    linear drift adjustment. Four independent Windows repetitions are complete:
    representation and synthetic bookkeeping stages show positive effects,
    but one repeat has severe drift and no removable real production
    bottleneck was isolated. Runtime and benchmark v3 remain unchanged.

The canonical current baseline is ``baseline-v3``. Older ``initial`` and
``baseline-v2`` checkpoints used different benchmark definitions and must not be
used as the direct before-state for numbered optimisations.

Established decisions / do not repeat blindly
---------------------------------------------

* Do not reintroduce speculative WAIT result prefetch; it broke result-boundary
  and fatal/COPY semantics.
* Do not optimise only WAIT request count; the ``10 ms`` drain already removed
  most avoidable crossings.
* Do not use parent-only RSS for process comparisons.
* Do not default native worker receive; controlled benefit was neutral.
* Do not default native worker socket wait; it was slower.
* Do not return to Windows AF_UNIX without new evidence.
* Do not reintroduce unsafe one-read TCP framing without preserving over-read.
* Do not resume protocol-5 out-of-band pickle experiments; they were slower.
* Do not spend another iteration on generic serialization/snapshot micro-tuning
  unless fresh profiling shows it dominates.
* Do not optimise bulk decoding while the canonical benchmark keeps it near
  upstream.
* Do not infer GUI responsiveness from worker topology; synchronous calls block
  the invoking thread in every architecture.

Immediate next work
-------------------

#. ``optimized01`` remains **RETAIN**. The timed-vs-untimed
   response wait stays **REJECT** (no speed benefit and
   dead-worker liveness regression). Avoid all other
   documented rejected wait/prefetch/snapshot experiments.
#. The Coordinator-approved enriched echo investigation
   is **COMPLETE**. The Windows diagnostic ZIP
   ``qt-thread-enriched-echo-20261103-205855-365.zip``
   was captured at ``d1b6a5a``: four fresh subprocess
   repeats, 16 bracketed short-block cycles/repeat,
   600 calls/block, and original timed queue get.
   All 64 cycles finished without reported errors.
#. Across four independent-repeat medians, overhead
   above interpolated bare echo: envelope +4.031 us,
   envelope plus validation +5.174 us, synthetic
   bookkeeping +9.420 us, actual GET +14.172 us.
   Incremental validation +0.855 us (44/64 cycles
   positive), bookkeeping +4.184 us (61/64), and
   actual GET beyond proxy +5.016 us (59/64).
#. **Drift warning:** bare echo rose +131.8% across
   the first repetition, compared with +3.5-6.7% in
   the remaining three. Timestamp bracket correction
   handles only approximate linear local drift.
   Windows CPU clock quantization prevents reliable
   microsecond-level per-function attribution.
#. **Decision: diagnostic COMPLETE, no runtime
   candidate.** The synthetic stage costs do not
   identify a dominant removable actual qpsycopg
   operation or predict SQL improvements. Production
   code, accepted ``optimized01`` and benchmark v3
   were unchanged. Do not start ``optimized02``.
#. **Independent Coordinator review COMPLETE** at
   ``d226f8e6``: **CONTINUE -- real-path profiling**.
   The enriched synthetic measurements are useful
   for scale but cannot identify an avoidable
   real operation. Do not add more synthetic
   echo stages or start ``optimized02``.
#. **Next owner: existing ``10 Implementation — current``**.
   Perform one diagnostic-only investigation of the
   actual qt-thread no-SQL GET status path, focusing
   on request construction/transport entry, worker
   request handling and reply, and caller response
   restore. Keep all production code and canonical
   v3 unchanged. Capture uninstrumented controls
   and narrowly bracketed real-path inclusive
   wall/thread-CPU phases with fresh independent
   runs, short paired blocks and explicit drift
   analysis; do not sum overlapping phases.
#. The deliverable is one reproducible, falsifiable
   **real-code** cost hypothesis (or an explicit
   finding that no such target is supported).
   In particular, do not interpret synthetic
   4-5 us stages or coarse Windows CPU counters
   as a removable production saving. Preserve
   connection, worker-exit/close/error semantics,
   and compare any eventual safe runtime candidate
   using smoke plus the unchanged paired v3.
#. No new Critic/Scout is needed yet. If real-path
   profiling fails to isolate an actionable cost,
   stop the local sequence and return to Coordinator
   for a fresh direction/Scout decision.

Coordinator review -- enriched echo decision
--------------------------------------------

* **Role and outcome:** existing ``00 Coordinator`` independently
  reviewed the enriched SimpleQueue echo checkpoint on branch
  ``feature/qt-process-pq`` at observed HEAD ``d226f8e6``.
  Decision: **CONTINUE with real-path, diagnostic-only
  attribution**; no new runtime optimisation or Scout.
* **Evidence:** four independent repeats and 64 cycles
  support positive synthetic representation/bookkeeping
  overhead but cannot identify a dominant removable
  qpsycopg function. Repeat-1 bare baseline drifted
  +131.8%; Windows microsecond CPU attribution is
  unreliable. The ZIP remains local-only, identified
  by SHA256 in the optimisation log.
* **Ownership and next action:** ``10 Implementation — current``
  continues as sole runtime owner. No code or
  benchmark-v3 change was approved. Inspect the real
  GET/request/worker/response path using bounded
  instrumented phases and uninstrumented paired
  controls; preserve original liveness semantics.
  Report a falsifiable actual-code hypothesis or
  stop without ``optimized02``. Then update docs
  and return any significant completed checkpoint
  for independent review.
* **Recovery and context:** this Coordinator stage has
  no uncommitted runtime work or local measurement.
  The Implementation rolling snapshot below remains
  historical evidence of its own last handoff.

Rolling recovery snapshot -- current Implementation owner
---------------------------------------------------------

* **Owner / branch:** this current Implementation/Optimisation
  chat is the sole ordinary runtime owner for
  ``ARMAGGIDON/qpsycopg``, branch
  ``feature/qt-process-pq``. The next action belongs
  to the **existing 00 Coordinator** for *review*,
  not runtime editing. Do not activate concurrent
  implementation chats.
* **Base / observed HEAD:** the enriched diagnostic
  stage began at ``492ab4317eba0e0d3fb48b37963e37404551ed26``;
  the Windows capture was at
  ``d1b6a5a1899ac5690961a2ff335b96debd24cb6e``.
  HEAD observed before this snapshot edit:
  ``f955a0d2f2a29ad193daa3f80802a6fcf8eb9f18``.
  Verify current HEAD when resuming.
* **Hypothesis and evidence:** progressive enriched
  SimpleQueue echo tried to partition Request/Response,
  validation and synthetic worker bookkeeping from the
  real no-SQL GET. All four repetitions succeeded and
  the synthetic envelope/bookkeeping stages show
  measurable wall effects. One repeat has extreme
  baseline drift, and the synthetic proxy differs
  materially from real worker code. **Open question:**
  which, if any, real-path function is costly enough
  and semantically safe to optimize? No such target
  is established by the current artifact.
* **Changes / rejected approaches:** the stage
  analysis changed **docs only** (optimization log,
  performance and this project map). The prior
  diagnostic Python/PowerShell files already exist.
  No production or v3 source was edited, and there
  is no new candidate. ``optimized01`` stays RETAIN.
  Timed-vs-untimed queue wait remains REJECT;
  rejected native worker wait, speculative WAIT
  prefetch and generic snapshot tuning should not
  be repeated without new evidence.
* **Local-only evidence:** the original
  ``test-results\qt-thread-enriched-echo-20261103-205855-365.zip``
  is a Windows-local artifact attached to the chat,
  **not committed to GitHub**. The optimization log
  records the archive SHA256 and detailed medians.
  Preserve the ZIP for independent review. Windows
  worktree state was not independently inspected;
  no uncommitted local changes are known from
  this GitHub-only workflow.
* **Tests / next action:** no semantic smoke or
  canonical v3 acceptance benchmark was rerun
  because there is no runtime change. Next:
  check branch HEAD and four qt-process docs;
  send this completed checkpoint to existing
  ``00 Coordinator``. **Do not run another
  Windows profile or start optimized02 until
  Coordinator selects the next bounded target.**
  There is no pending architectural or runtime
  ownership transfer.
* **Context-risk assessment: CAUTION.** This is
  a long, tool-heavy chat, but critical branch/
  decision/evidence state remains recoverable.
  No guessed context percentage/token count;
  no HANDOFF NOW signs currently observed.
  If material inconsistency appears, first
  commit a fresh recovery snapshot, then
  prepare an explicit same-role continuation
  prompt. The previous owner must stop editing
  when a new owner takes over.

When resuming in a new chat/session
-----------------------------------

#. Read :doc:`qt-process`, this map,
   :doc:`qt-process-performance` and
   :doc:`qt-process-coordination`; inspect
   HEAD and :doc:`qt-process-optimization-log`.
#. Enriched echo is **measured and documented**,
   not awaiting another ZIP. The result is a
   synthetic attribution plateau, not permission
   for a production optimization.
#. Next owner is existing ``00 Coordinator``
   for independent review. Runtime ownership
   stays with the current implementation role;
   a same-role context handoff, if required,
   must explicitly transfer ownership and
   never create two simultaneous owners.
