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

``tools/profile_qt_thread_get_real_path.py`` and
``tools/profile_qt_thread_get_real_path.ps1``
    Coordinator-approved real qt-thread no-SQL GET status diagnostic.
    One untouched control connection and one instrumented connection per
    independent subprocess; short clean-profiled-clean-profiled-clean
    blocks and timestamp-bracketed drift controls. Temporary method
    wrappers time actual caller request/perform/dispatch/response,
    worker handle/normalize/connection checks and active _run worker
    turn; no cProfile or production code changes. Windows data complete:
    all phase counts valid, but probe overhead was ~18 us per GET and
    no real semantically removable bottleneck was identified.

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

#. ``optimized01`` remains **RETAIN**. The rejected timed-vs-untimed
   response queue wait and earlier native-wait, speculative
   prefetch and serialization experiments remain rejected.
#. The Coordinator-authorised **real qt-thread GET status diagnostic
   is COMPLETE**, with archive
   ``qt-thread-real-get-profile-20261103-213627-044.zip``,
   captured at ``1c11f6525bfc75e3db27aeeae65c0498a56df4cd``.
   Four independent repeats x 12 five-block cycles, 1200
   GETs/block. All 48 cycles, 240 blocks and 12 required phase
   counts passed; 115,200 profiled and 172,800 clean-control GETs.
#. The clean GET block wall medians were 29.878-30.999 us/call.
   Instrumented-minus-clean paired wall medians were
   +18.082..+18.740 us/call, positive in 96/96 profiled blocks.
   This **instrumented-versus-clean difference** is confounded
   by the probe and separate connections, not a production saving.
#. Nested instrumented phase wall medians: caller
   ``request`` 50.906 us, ``_dispatch`` 38.412 us,
   active worker ``_run`` turn 14.137 us,
   worker pool ``_handle`` 6.264 us and base
   ``_handle`` 3.965 us. Never sum these inclusive
   times. Worker per-method CPU clocks were mostly zero
   from Windows quantization; clean repeat 3 drifted +61.18%.
#. **Decision: no actionable runtime target.** No real,
   removable, semantically safe operation was identified.
   Production runtime and benchmark v3 are unchanged.
   No ``optimized02`` or new acceptance benchmark.
#. **Independent Coordinator review COMPLETE** at
   ``5832702e``: **CREATE SCOUT -- GET/echo attribution
   plateau**. The profiling methodology is adequate for
   the negative conclusion; its strong probe overhead,
   nested phase overlap, nonstationary host timing and
   quantized Windows CPU counters cannot establish a
   semantically removable real-code operation.
#. Both read-only short-query Scouts **COMPLETED**
   at ``9777f09a``. Coordinator decision: **INVESTIGATE
   -- diagnostic-only native PGresult handoff screen**.
   No production change or ``optimized02`` is approved.
#. Scout A identified a possible hybrid Transformer
   cost; its proposed stable-vs-alternating query-object
   identity A/B is not selected because query/cache
   effects can confound Transformer attribution.
   This hypothesis is not disproven.
#. Scout B identified a thread-specific structural
   alternative: completed C-worker native PGresult
   transfer across the existing two SimpleQueues,
   rather than ``ResultSnapshot/PackedValues``
   plus synthetic-result restoration. This is a
   distinct architectural possibility, **not**
   renewed generic snapshot micro-tuning; historical
   small snapshot costs limit expected upside.
#. **Next owner:** existing ``10 Implementation — current``.
   First perform a bounded, diagnostic-only
   ownership/type/Transformer feasibility check.
   If safe, perform a same-workload Windows C-worker
   two-queue SELECT 1 comparison: current full result
   snapshot/restore/decode versus native result
   handoff/native Transformer. Preserve query work,
   adaptation context, two handoffs and output
   equivalence. Use short rotated paired blocks,
   independent repeats, process CPU/query, wall,
   context switches and drift/noise controls.
   Predeclare a material screen such as >=15 us
   CPU/query with reproducible signal beyond noise.
#. **STOP** if feasibility or reproducible material
   benefit fails. In either case record the result
   and route to Coordinator before any runtime
   variant. Preserve all public PGresult semantics:
   independent ownership, ``clear``, result lifetime
   after connection finish, error/NULL/metadata,
   multiple results, COPY, cancellation and
   callbacks. Do not transfer ``PGconn`` ownership
   or native borrowed notice results. Keep
   ``qt-process`` unchanged; unsupported worker
   implementations need correct fallback.
#. ``10 Implementation — current`` remains the sole
   active runtime owner. Scouts remain read-only;
   no second Implementation owner and no replay of
   GET/echo, queue wait, native wait, speculative
   prefetch or generic snapshot experiments.
#. Feature PR #2 (``feature/qt-backend-config``) has
   its own integration gates and owner; it is not
   part of this performance/Scout decision.

Coordinator review -- two independent short-query Scouts
--------------------------------------------------------

* **Source:** independent read-only Scouts A/B at
  ``9777f09a821ecc7a358a595e34b98063e8e70f1e``.
  Coordinator verified same current HEAD, core
  result/Transformer code, prior profiling/rejection
  evidence, and PostgreSQL 18 libpq thread rules.
* **Decision:** **INVESTIGATE**, one diagnostic-only
  feasibility/CPU screen for native-result handoff
  with C worker; not ``optimized02`` and not
  production-authorized. No new Scout.
* **Scout comparison:** A's Transformer overhead
  is plausible, but the suggested query-object
  identity experiment does not isolate it from
  query caching/preparation. B identifies a
  distinct whole-boundary alternative, yet
  historical snapshot profiling found this path
  small; possible advantage is **unmeasured**.
* **Preflight and measurement:** verify full
  ownership, destructor/``clear()``, result
  lifetime beyond ``PGconn.finish()``, exact native
  C binding/Transformer compatibility, and basic
  error/NULL/field parity. If valid, compare the
  complete result boundary after actual warmed
  ``SELECT 1`` with identical worker-thread SQL
  work and two SimpleQueues. The control uses
  snapshot/synthetic PGresult/hybrid Transformer;
  candidate uses native PGresult/native C
  Transformer. Rotate short paired blocks across
  fresh subprocess runs; calculate process
  CPU/query, wall, context switches and drift.
  Predefine a >=15 us/query CPU/noise screen before
  capture; do not attribute a small isolated
  wrapper saving to end-to-end SELECT performance.
* **STOP / ownership:** reject if native ownership
  or semantic parity is unproven or a material
  paired benefit does not survive noise/drift.
  The Implementation chat alone writes diagnostic
  tools and documents results; no production runtime
  or benchmark-v3 edits. Preserve existing rejected
  experiments, ``optimized01`` RETAIN, and
  configuration PR #2's separate integration owner.
  Return completed diagnostic for Coordinator
  review before any runtime candidate.
* **Recovery / context:** this is an in-progress
  Coordinator handoff snapshot based on committed
  repo sources and user-provided Scout reports.
  No Windows diagnostic has yet been run, no ZIP
  exists for this new screen, and no uncommitted
  runtime change is asserted.

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

Coordinator review -- real GET attribution plateau
-------------------------------------------------

* **Role / review commit:** existing ``00 Coordinator``
  independently checked ``feature/qt-process-pq``
  at observed HEAD ``5832702e12578c8fd146ac8eeee7fd9c9a7f76e7``,
  the current profiling tool, four qt-process docs,
  and the measured optimisation log. The measured
  archive was Windows-local/chat-uploaded and was
  not re-executed or independently reparsed here.
* **Decision:** **CREATE SCOUT** for the exhausted
  GET/echo attribution sequence. Four independent
  repetitions and correct phase counts support
  ``NO ACTIONABLE RUNTIME TARGET``; +18.31 us
  instrumented/clean is probe overhead, not a
  production saving; the +61.18% repeat drift and
  CPU-clock resolution rule out exclusive attribution.
  No ``optimized02``, runtime changes, or v3
  acceptance run is warranted by this checkpoint.
* **Immediate next reproducible action:** create
  two independent read-only Scout chats. Both must
  begin from the same base HEAD, read
  ``qt-process.rst``, ``qt-process-project-map.rst``,
  ``qt-process-performance.rst``,
  ``qt-process-coordination.rst`` and the
  optimisation log; independently inspect real
  SQL short-query architecture, compare with
  upstream/direct, and propose no more than one
  falsifiable safe alternative (or STOP).
  Do not give one Scout the other's hypotheses
  before both finish. No local Windows command
  is required or authorised at this handoff.
* **Ownership / guardrails:** existing
  ``10 Implementation — current`` retains sole
  runtime ownership but pauses speculative new
  performance work until Coordinator selects
  zero or one idea. Scout roles may inspect code
  and send read-only proposals; neither writes
  to the runtime branch. Keep accepted
  ``optimized01`` RETAIN and historical
  rejections intact. Configuration PR #2 remains
  a separate integration workstream.
* **Evidence locality and recovery:** original
  ``test-results\\qt-thread-real-get-profile-20261103-213627-044.zip``
  is local/uploaded, not GitHub-tracked.
  Its SHA256 and numerical results are recorded
  in the optimisation log. This Coordinator
  review created no uncommitted code or benchmark
  artifacts. No transfer of implementation
  ownership has occurred.

Rolling recovery snapshot -- current Implementation owner
---------------------------------------------------------

* **Owner / branch:** current ``10 Implementation`` chat is the sole
  runtime implementation owner for ``ARMAGGIDON/qpsycopg``,
  ``feature/qt-process-pq``. The next bounded responsibility
  belongs to existing ``00 Coordinator`` for *independent review*.
  No competing runtime owner is authorised.
* **Base / known commits:** Coordinator approval
  ``0fc10d668ac9166f07ae3a575049da7b4b14ccd1``;
  Windows diagnostic captured at
  ``1c11f6525bfc75e3db27aeeae65c0498a56df4cd``.
  Measured-doc commits ``1064c818`` and ``83b68c8b``.
  Last HEAD observed before this snapshot update was
  ``83b68c8bf2cc67a9551a21d6f3717c03fd59be81``.
  Verify live HEAD when resuming.
* **Completed hypothesis:** real no-SQL GET worker/caller
  phase profiling after synthetic enriched echo plateau.
  The experiment proved phase call counts and recorded
  inclusive wall-time, but **no exclusive safe removable
  operation**. New hypothesis and new runtime experiment
  remain **unselected** pending Coordinator direction.
* **Code and local state:** this stage introduced only
  ``tools/profile_qt_thread_get_real_path.py`` and
  ``tools/profile_qt_thread_get_real_path.ps1`` plus
  documentation. The measured-result update changes
  **docs only**. No production runtime or canonical v3
  files changed. The user's Windows working tree was
  not inspected; do not assert it is clean.
* **Evidence / tests:** archive
  ``test-results\qt-thread-real-get-profile-20261103-213627-044.zip``
  was uploaded to this chat and is **not committed to
  GitHub**. SHA256 and all measured details are saved
  in ``docs/qt-process-optimization-log.rst``. The user
  should preserve the original ZIP. All 48 cycles
  succeeded, phase counts were exact, and 96/96 paired
  profiled blocks showed extra ~18 us wall. The probe
  itself perturbs timing, repeat 3 drifted +61.18%,
  and per-method Windows CPU is quantized. No smoke
  or v3 acceptance benchmark was rerun: no runtime
  candidate was created.
* **Rejections and guardrails:** ``optimized01`` RETAIN;
  raw untimed get REJECT for no speedup and loss of
  fatal-worker detection. Also avoid prior native
  worker wait, speculative WAIT prefetch and generic
  snapshot/serialization/bulk experiments without new
  evidence. Preserve public Psycopg semantics and
  worker exit, errors, close and cancellation.
* **Next reproducible action:** inspect current HEAD
  and all primary qt-process docs, then send the
  completed negative real-GET checkpoint to existing
  ``00 Coordinator``. No pending Windows command;
  **do not rerun this probe or start optimized02**
  until a new direction is independently approved.
* **Context risk: CAUTION.** Long, tool-heavy
  Implementation history; state remains recoverable
  from GitHub. No remaining-token/window estimates
  and no observed HANDOFF NOW symptoms. A future
  serious inconsistency requires a fresh snapshot
  and explicit same-role transfer, with the former
  owner stopping edits before the new owner resumes.

When resuming in a new chat/session
-----------------------------------

#. Read :doc:`qt-process`, this project map,
   :doc:`qt-process-performance`,
   :doc:`qt-process-coordination` and
   :doc:`qt-process-optimization-log`;
   inspect live branch HEAD.
#. The real GET diagnostic has **already finished**,
   with no actionable optimized02 candidate.
   The uploaded ZIP remains local-only evidence.
#. The next owner is **existing 00 Coordinator**
   for independent milestone review. Runtime ownership
   stays with this Implementation role unless explicitly
   transferred due to context risk; never allow
   simultaneous implementation owners.
