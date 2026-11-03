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

``tools/preflight_qt_thread_native_pgresult.py`` and
``tools/preflight_qt_thread_native_pgresult.ps1``
    Diagnostic-only, watchdog-protected owned native C PGresult
    cross-thread SimpleQueue preflight. Compares metadata, NULLs,
    SQLSTATE and native C versus current hybrid Transformer using
    one qt-thread cursor adaptation context, plus PGconn.finish,
    PGresult.clear/destructor and borrowed notice safeguards.
    Windows pass/fail results are pending. Does not change runtime or v3.

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

#. ``optimized01`` remains **RETAIN**. Previously rejected
   native-worker socket wait, timed-vs-untimed queue
   response waiting, speculative WAIT prefetch and
   generic snapshot/serialization micro-tuning remain
   rejected. The GET/echo attribution line is complete
   and not being rerun.
#. Independent Scouts A/B were reviewed by the existing
   ``00 Coordinator`` at checkpoint
   ``98a6ad178d03ff0ad94f1b28c6d3ddac6cd164a1``.
   Decision: **INVESTIGATE**, only a diagnostic native
   PGresult handoff feasibility/CPU screen, not a
   production-authorized architecture change.
#. Current Implementation owner performed a code
   preflight: C PGresult owns its PQclear-managed
   memory, PostgreSQL 18 allows transferring
   completed read-only PGresult between threads,
   and native C Transformer requires precisely
   the C PGresult type. However the C notice
   callback PGresult is **borrowed and invalidated**,
   and worker health/cache logic depends explicitly
   on ResultSnapshot. A naive identity replacement
   is unsafe.
#. Diagnostic-only preflight scripts
   ``tools/preflight_qt_thread_native_pgresult.py``
   and ``tools/preflight_qt_thread_native_pgresult.ps1``
   were added, without changing production/v3.
   They test real C-worker result handoff through
   two SimpleQueues, native C Transformer using the
   same qt-thread cursor adaptation context,
   NULL/metadata/SQLSTATE/notice parity, result
   lifetime after PGconn.finish, and clear/destructor.
   Worker PGconn remains on worker thread only.
#. **Next user action:** sync the Windows checkout and run
   ``.\tools\preflight_qt_thread_native_pgresult.ps1``.
   Upload the resulting
   ``test-results\qt-thread-native-result-preflight-*.zip``
   to **this current Implementation chat**. The
   probe runs in a subprocess with a watchdog and
   preserves a ZIP even on failure.
#. **Gate:** do **not** begin the full native
   current-vs-candidate SELECT 1 performance A/B
   unless every required safety/Transformer/semantic
   preflight passes and is reviewed here. If a
   fundamental feasibility issue is observed:
   **STOP** and document. A successful preflight
   would allow one *diagnostic-only* A/B with
   identical SQL, adaptation, queue topology,
   outputs, independent paired runs and >=15 us
   CPU/SELECT material-benefit screen. Not optimized02.
#. After a **completed** bounded investigation
   (negative preflight or later measured A/B),
   update the optimization log, performance notes
   and this recovery snapshot and hand off to
   existing ``00 Coordinator`` for independent
   milestone review. No new Scout or second
   implementation owner is required meanwhile.

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

* **Role / ownership:** existing ``10 Implementation — current``
  alone owns the active runtime path for
  ``ARMAGGIDON/qpsycopg``, branch
  ``feature/qt-process-pq``. Existing
  ``00 Coordinator`` made the latest direction
  decision, but is **not** another runtime owner.
  Configuration PR #2 is a separate owned workstream.
* **Base / observed commits:** native result feasibility
  direction at ``98a6ad178d03ff0ad94f1b28c6d3ddac6cd164a1``.
  Preflight Python commit ``796bb9de``,
  PowerShell commit ``5a666787``,
  SQLSTATE check commit ``fbeab2ef``,
  optimization-log stage ``f183d8b1``,
  performance stage ``7dfda37f``.
  Verify live HEAD before resumption; historical
  commit IDs are not a current-branch guarantee.
* **Unfinished bounded work:** source-based owned native
  PGresult handoff/Transformer audit is complete and
  supports **conditional feasibility**, not proof.
  A watchdog-isolated Windows ownership/semantic
  preflight is staged and **not yet measured**.
  Pending: assess PGresult lifetime, clear,
  notices (borrowed pointer), metadata/NULL/error
  and C Transformer parity with the same qt-thread
  cursor AdaptContext. If it fails, STOP; if it
  passes, implement *only diagnostic* full SELECT 1
  native-vs-snapshot boundary comparison with
  two queue handoffs and >=15 us process CPU/query
  repeatable material screen.
* **Code / local-only state:** two new diagnostic tools
  are committed; production qt-thread/qt-process,
  benchmark v3 and accepted optimized01 were
  **not edited**. No new Windows ZIP has yet
  been submitted. This GitHub-only workflow
  cannot verify the user's local Windows worktree
  or assume it is clean; no uncommitted local
  implementation edits were made here.
* **Evidence / blockers:** official libpq 18 rules
  permit cross-thread transfer of completed PGresult,
  not concurrent PGconn access or borrowed notice
  result handoff. C PGresult destructor calls
  PQclear; native Transformer demands C PGresult.
  The existing worker's connection-health and
  SET/RESET parameter refresh logic assumes
  ResultSnapshot. Any future production concept
  must also preserve COPY, pipeline, multi-results,
  error/notice callbacks, cancellation, close
  and fallback for non-C workers. These remain
  open and untested beyond source audit.
* **Rejections:** optimized01 RETAIN. Do not rerun
  rejected timed/untimed response wait, worker
  native socket wait, speculative WAIT prefetch,
  generic snapshot/serialization tuning, or
  GET/echo experiments. No optimized02 candidate.
* **Next command and route:** with existing
  Windows benchmark fork venv, local root
  .env, PostgreSQL libpq DLL root and DSN,
  run ``.\tools\preflight_qt_thread_native_pgresult.ps1``.
  Share ZIP **here** for feasibility evaluation.
  Do **not** contact Coordinator or start
  full performance A/B until preflight succeeds.
  After a significant measured/STOP checkpoint,
  update docs and route to existing 00 Coordinator.
* **Context risk: CAUTION.** This is a long
  tool-heavy Implementation chat; the task,
  decisions and source remain consistent with
  GitHub. This snapshot enables repository-first
  recovery. No claimed token count; no observed
  HANDOFF NOW symptom. If reliability worsens,
  snapshot first, prepare a same-role continuation
  bootstrap and explicitly transfer ownership;
  old chat then stops editing.

When resuming in a new chat/session
-----------------------------------

#. Verify HEAD and read :doc:`qt-process`,
   this project map, :doc:`qt-process-performance`,
   :doc:`qt-process-coordination` and
   :doc:`qt-process-optimization-log`.
#. Native-result preflight tools are committed,
   but Windows feasibility remains **pending**.
   Do not run or fabricate the future SELECT A/B.
#. Maintain single implementation ownership and
   preserve all prior REJECT decisions.
   Next required evidence is the Windows
   ``qt-thread-native-result-preflight-*.zip``
   in the current Implementation chat.
