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

``tools/bench_qt_thread_native_result_boundary.py`` and
``tools/bench_qt_thread_native_result_boundary.ps1``
    Post-preflight diagnostic-only same-SQL warmed SELECT 1 complete
    result-boundary A/B. One native worker/PGconn and the same PQexecParams
    work and two SimpleQueues for both paths: current snapshot/synthetic/
    hybrid versus owned native PGresult/native C Transformer, with the
    same cursor AdaptContext. Interleaved five-block cycles, four
    independent repetitions, CPU/query, wall/switches and drift analysis.
    Windows performance results are pending; production/v3 unchanged.

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

Implementation Windows compiled C-worker smoke -- PASS checkpoint
-----------------------------------------------------------------

* **Repository / scope:** ``ARMAGGIDON/qpsycopg``,
  branch ``feature/qt-process-pq``; workflow
  implementation at commit
  ``d7856544f6d80f6eb06da9b7818fd40382fe6198``.
  Changes in this bounded stage: only
  ``.github/workflows/qt-process.yml``
  plus ``docs/qt-backend-config.rst``
  and recovery documentation. No
  production, C extension, pool/test
  code, benchmark v3 or performance
  algorithm changes.
* **CI PASS evidence:** GitHub Actions
  Qt process backend pull_request run
  ``41809431708``, attempt 1, job
  ``126504663426``
  (``windows-c-worker-smoke``) on
  ``windows-2025``, CPython 3.13.17
  completed **SUCCESS**. Provisioning
  with ``vcpkg libpq:x64-windows-release``,
  ``pg_config`` stub, Cython/setuptools,
  MSVC and ``delvewheel`` succeeded;
  compiled wheel
  ``psycopg_c-3.3.7.dev1-cp313-cp313-win_amd64.whl``
  installed with ``psycopg[qt]``.
* **Explicit native-worker identity:** CI ran
  ``PSYCOPG_IMPL=qt-thread``,
  ``QPSYCOPG_WORKER_IMPL=c`` and asserted
  ``psycopg.pq.__impl__ == "qt-thread"``,
  ``pq_qt_thread._workerPq is psycopg_c.pq``,
  ``_qt_process_worker.pq is psycopg_c.pq``,
  ``_qt_process_worker_pool.pq is psycopg_c.pq``,
  and imported binary suffix ``.pyd``.
  Log marker:
  ``COMPILED_C_WORKER_VALIDATED``
  from
  ``.../psycopg_c/pq.cp313-win_amd64.pyd``.
  All these assertions completed before
  the smoke; **no Python worker fallback**.
* **Full semantic smoke result:** actual
  ``tools/smoke_qt_thread.py`` passed
  ``basic adaptation``, ``NULL/empty``,
  multi-results, COPY, SQL error survival,
  shared connection/per-thread cursors,
  cancellation, caller fileno boundary,
  fatal backend termination and
  ``AsyncConnection`` against
  Windows local PostgreSQL.
  Terminal marker:
  ``qpsycopg qt-thread smoke test: OK``.
* **Artifact evidence:** GitHub Actions
  artifact ``windows-qt-thread-c-worker-smoke``,
  ID ``13310819123``, from this
  run. Downloaded and CRC-checked ZIP
  ``qt-thread-c-worker-evidence-run41809431708.zip``
  SHA256
  ``679eb5ba320eff8bb25bb6f3e54911118b632cd066361cdbc725543a7ad4bea6``;
  contains one 927-byte
  ``qt-thread-c-worker-evidence.log``
  with identity and all semantic stages
  ending in OK. Original artifact
  remains hosted on GitHub Actions,
  not newly committed to the repo.
* **Important distinction:** the *whole*
  Qt workflow run ``41809431708``
  has conclusion ``failure`` because
  ``docs-validation`` failed with
  the same nine documented **baseline**
  Sphinx warnings/errors (literal block
  quote x2, unexpected indentation x6,
  missing toctree x1).
  The C-worker job plus other
  ``smoke``, ``windows-libpq-connection``
  and ``windows-dll-bootstrap``
  jobs all succeeded. This narrowly
  **closes the Windows compiled C-worker
  smoke validation gap**; it does NOT
  certify the overall workflow, broader
  Windows parity, or remove lint/Sphinx
  exceptions.
* **Next action / owner:** existing
  ``00 Coordinator`` reviews the
  completed narrow PASS checkpoint.
  Current ``10 Implementation — current``
  retains sole ordinary runtime ownership.
  ``optimized01`` RETAIN,
  native boundary diagnostic REJECT,
  no ``optimized02``.
  No additional performance experiments
  or runtime changes are authorised.
* **Context: CAUTION.** Evidence,
  ownership and limitations recorded;
  earlier in-progress project map
  sections are historical. No same-role
  HANDOFF NOW signal.

Immediate next work
-------------------

#. **Post-merge Tests ACCEPTED — 56/56 SUCCESS after
   exactly one isolated job retry.** Merge-push
   ``41801317209`` (merge commit ``9126cddf``)
   completed attempt 2 with conclusion SUCCESS;
   full paginated/latest job inventory had 56
   completed success, zero failures, zero running.
   Original attempt 1 had 55 success / one failure:
   ``126477485524``, ``linux (python, 3.14t,
   postgres:16)``, ``test_refill_on_check[asyncio]``,
   ``PoolTimeout`` after proxy restart.
   Two pytest internal retries failed;
   Coordinator allowed exactly one GitHub job rerun.
   Only that job was retried as ``126492158840``
   and passed. Root cause remains **unproven**.
   PR-triggered ``41801325261`` was
   CANCELLED (56/56); it is not the green run.
   This **supersedes earlier CI-in-progress /
   targeted-retry instructions** below.
#. **No further CI retry is authorised.**
   The accepted 56/56 matrix does not
   change documented baseline lint/Sphinx
   exceptions or close the Windows
   compiled ``worker_impl=c`` validation
   gap. No pool, tests, timeouts, runtime,
   workflows, v3 or performance changes
   were made. ``optimized01`` RETAIN;
   native result boundary REJECT;
   ``optimized02`` absent.
#. **Independent Coordinator review COMPLETE**:
   merge-push Tests ``41801317209`` is
   **ACCEPTED 56/56 success after one
   isolated job retry** (original
   ``126477485524`` FAIL; retry
   ``126492158840`` PASS). The
   prior pool/proxy timeout cause remains
   unknown; no more CI retries, waiver,
   or test changes are justified.
#. **Windows compiled C-worker validation: PASS.**
   Coordinator-authorised bounded validation
   completed on Windows 2025 / CPython 3.13
   in Qt workflow ``41809431708``,
   job ``126504663426`` (SUCCESS),
   head ``d7856544f6d80f6eb06da9b7818fd40382fe6198``.
   Real native ``psycopg_c`` wheel
   was built and installed; the explicit
   ``COMPILED_C_WORKER_VALIDATED`` assertion
   confirmed a compiled ``.pyd`` and
   ``pq_qt_thread._workerPq`` plus both
   worker-module ``pq`` globals referencing
   ``psycopg_c.pq``, with no Python fallback.
   Full existing ``tools/smoke_qt_thread.py``
   finished ``qpsycopg qt-thread smoke test: OK``
   with ``PSYCOPG_IMPL=qt-thread`` and
   ``QPSYCOPG_WORKER_IMPL=c``.
   Evidence artifact ID ``13310819123``.
   This closes the specific Windows
   compiled C-worker smoke gap, **not**
   all Windows or native-binding validation.
#. **PR #2 documentation status corrected:**
   ``docs/qt-backend-config.rst`` now reports
   PR #2 MERGED, with the accepted
   post-merge Tests evidence.
   The Qt workflow's overall conclusion
   is FAILURE solely due to separate
   ``docs-validation``: the same nine
   documented baseline Sphinx messages
   (two literal block, six indentation,
   one toctree) remain. Four other
   Qt workflow jobs are SUCCESS.
   Retain accepted lint/Sphinx exceptions
   honestly as exceptions.
#. **Coordinator independent review ACCEPTED**:
   Windows compiled C-worker smoke on
   workflow commit ``d7856544f6d80f6eb06da9b7818fd40382fe6198``
   is a **narrow PASS**. Job ``126504663426``,
   run ``41809431708``, built/installed
   ``psycopg_c`` and verified
   ``COMPILED_C_WORKER_VALIDATED`` with
   compiled ``.pyd``, ``_workerPq`` and
   both worker-module ``pq`` globals
   equal to ``psycopg_c.pq``. The
   existing full ``smoke_qt_thread.py``
   finished ``qpsycopg qt-thread smoke test: OK``.
   Evidence ZIP artifact ``13310819123``
   was independently downloaded: CRC OK,
   SHA256 ``679eb5ba320eff8bb25bb6f3e54911118b632cd066361cdbc725543a7ad4bea6``.
   This does not certify all Windows
   platforms, native variants or workloads.
#. **Next bounded stage: Sphinx CI hygiene.**
   The same Qt workflow is FAILED solely
   in ``docs-validation``; its nine known
   diagnostics are two inconsistent
   literal-block errors in
   ``qt-process-performance.rst``,
   six unexpected-indent errors in
   ``qt-process-project-map.rst``
   and one not-in-toctree warning for
   ``qt-process-coordination.rst``.
   Existing ``10 Implementation — current``
   owns one documentation-only cleanup
   without semantic rewrites, production
   runtime, benchmarks or CI masking.
   Re-run the unchanged strict Sphinx
   command ``sphinx-build -W -T -b html docs docs/_build/html``;
   accept only a warning/error-free
   build, with independent CI evidence.
   If unrelated new errors appear,
   stop and report rather than making
   broad changes.
#. Repository-wide lint baseline failures
   remain separately accepted exceptions.
   Do not start performance probes or
   ``optimized02``. ``optimized01``
   remains RETAIN; the native
   PGresult-boundary screen remains
   drift-gated REJECT. After Sphinx
   cleanup, return a significant
   checkpoint for Coordinator review.

#. **No new performance series** or
   ``optimized02`` is authorised.
   ``optimized01`` remains RETAIN;
   native result-boundary A/B remains
   drift-gated REJECT. Return a
   completed C-worker validation
   checkpoint to Coordinator for review.

#. **PR #2 merged and verified.** Import-time backend
   configuration via application ``.env`` and the
   Windows DLL bootstrap entered
   ``feature/qt-process-pq`` at two-parent merge
   ``9126cddfb9db45b787ea7ea626c3e152f1d1ac24``
   (feature ``b82ca223``, target ``5c1c4c35``,
   Coordinator review ``5765518048``). Exactly
   eight configuration-owned files were integrated;
   earlier performance runtime and evidence remain.
   See merged GitHub PR #2 and
   :doc:`qt-backend-config` for import semantics.
#. **Integration write pause ENDED** following
   independent Coordinator post-merge verification.
   Existing ``10 Implementation — current``
   remains sole ordinary runtime writer; no
   competing implementation owner was created.
   That chat should sync its checkout and verify
   live target HEAD before writing. This docs
   checkpoint may be newer than the merge SHA.
#. **Post-merge Tests triaged; final matrix incomplete.**
   PR-triggered ``41801325261`` finished
   CANCELLED (all 56 matrix jobs cancelled).
   Merge-push ``41801317209`` has 56 jobs:
   at inspection 24 SUCCESS, 28 running, 3 queued,
   **one failed** (``linux (python, 3.14t, postgres:16)``).
   ``test_refill_on_check[asyncio]`` timed out
   refilling the async pool after proxy restart;
   its two automatic retries failed as well.
   This **same test** needed an isolated retry
   before merge (successful matrix
   ``41720955777``, attempt 2, 56/56).
   Cause and post-merge regression status
   remain unproven; notify existing Coordinator.
   A fresh docs-only-HEAD Tests run
   ``41802212474`` was also incomplete.
   Accepted lint/Sphinx baseline failures
   (including identical nine Sphinx messages
   on merge and docs-only heads) and the
   Windows compiled ``worker_impl=c``
   validation gap remain unchanged.
#. **Performance direction: STOP / plateau.**
   The bounded native PGresult handoff
   ownership/Transformer preflight PASS
   stands, but the complete SELECT 1
   result-boundary A/B failed its
   predeclared material-benefit screen
   because 3/4 independent repeats
   exceeded the 15% wall drift ceiling.
   Paired process CPU difference
   +34.921 us/SELECT (snapshot minus
   native) was directional only; no
   stable end-to-end gain is established.
   Do not repeat this A/B just to obtain
   a favourable sample or modify its
   predeclared gate after capture.
#. ``optimized01`` remains RETAIN,
   ``optimized02`` was not created.
   No new runtime performance experiment
   is authorised until an independently
   supported, falsifiable next target is
   proposed and reviewed. Existing
   GET/echo, queue-wait, native-wait,
   speculative prefetch and generic
   snapshot micro-tuning rejections
   remain in effect.
#. **Next action:** existing Implementation
   owner verifies live HEAD and post-merge
   Tests outcomes, then performs a bounded
   repository/state and semantic validation
   checkpoint. Do not start a new
   optimisation series by default. Return
   a significant CI/correctness blocker
   or new architectural decision to
   ``00 Coordinator`` for review.

Coordinator C-worker acceptance and documentation CI direction
--------------------------------------------------------------

* **Role / observed head:** existing ``00 Coordinator``,
  ``feature/qt-process-pq`` at
  ``3b5477c88794746201801ff766e7965ac593d433``
  after two documentation-only commits
  beyond measured workflow SHA
  ``d7856544f6d80f6eb06da9b7818fd40382fe6198``.
* **Accepted evidence:** Windows Actions run
  ``41809431708``, job ``126504663426``
  SUCCESS. Native ``psycopg_c`` wheel built,
  installed and resolved as compiled CPython
  3.13 ``.pyd``; actual qt-thread
  ``_workerPq`` and both worker module
  ``pq`` globals matched ``psycopg_c.pq``.
  Full existing qt-thread smoke finished
  OK on real Windows PostgreSQL.
  Independent ZIP check: artifact
  ``13310819123``, CRC OK, SHA256
  ``679eb5ba320eff8bb25bb6f3e54911118b632cd066361cdbc725543a7ad4bea6``.
  The ZIP contains the C identity marker
  and stage-by-stage smoke output.
* **Scope of PASS:** closes the specific
  Windows compiled-C-worker validation gap;
  not general Windows native binding
  certification or benchmark-v3 parity.
  Whole Qt workflow remains FAILED due to
  a separate strict Sphinx job; four other
  Qt jobs including this one succeeded.
* **Next owner / exact bounded action:**
  existing ``10 Implementation — current``
  remains the sole implementation/runtime
  writer. Correct only the existing nine
  Sphinx errors/warnings (2 literal blocks,
  6 project-map indentations, 1 missing
  toctree inclusion) without changing
  history, API claims or benchmark data.
  Run strict ``sphinx-build -W -T -b html
  docs docs/_build/html`` and confirm
  ``docs-validation`` independently
  in GitHub CI. No new runtime experiment
  or alternative implementation owner.
* **Outstanding / recovery:** repository
  lint still baseline red, pool/proxy
  timeout cause remains unknown despite
  accepted post-merge Tests 56/56 after
  exactly one retry. ``optimized01``
  RETAIN, native result-boundary A/B
  REJECT, no ``optimized02``.
  Coordinator made no uncommitted runtime
  changes; the GitHub job and downloaded
  ZIP are the evidence sources.
  Current context risk CAUTION;
  read live HEAD and five core docs
  before further work.

Coordinator acceptance -- post-merge Tests and next stabilization gate
----------------------------------------------------------------------

* **Role and observed state:** existing ``00 Coordinator``
  independently checked branch ``feature/qt-process-pq``
  at HEAD ``d9257a43741e05c3a1022c42dd0d353270de8b16``
  (a documentation-only descendant of
  PR #2 merge ``9126cddfb9db45b787ea7ea626c3e152f1d1ac24``).
  No local/uncommitted Windows state was inspected.
* **Acceptance evidence:** GitHub Tests push run
  ``41801317209``, attempt 2, ``completed/success``,
  verified all 56 latest jobs successful.
  Attempt 1 failed only job ``126477485524``
  on ``test_refill_on_check[asyncio]``, with
  ``PoolTimeout`` after proxy restart.
  Exactly one isolated retry, job
  ``126492158840``, completed successfully
  including its Run tests step.
  This is an accepted **56/56 latest state
  after isolated retry**, not 56 fresh
  reruns or proof that the transient
  pool/proxy problem is fixed.
  PR-triggered matrix ``41801325261``
  and docs-only matrix ``41802212474``
  were cancelled, not accepted green.
* **Next bounded action:** existing Implementation
  owner (sole runtime writer) assesses
  Windows runner compilation/install of
  ``psycopg_c``, then runs the current
  ``tools/smoke_qt_thread.py`` against
  real Windows PostgreSQL with
  ``PSYCOPG_IMPL=qt-thread`` and
  ``QPSYCOPG_WORKER_IMPL=c``.
  Verify genuine C worker selection.
  Scope may touch narrow CI/validation
  tooling and the outdated status text
  in ``docs/qt-backend-config.rst``,
  not runtime code or benchmark v3.
  If CI provisioning is nontrivial,
  stop and report evidence/blocker
  before broadening the work.
* **Guardrails:** accepted repository lint
  and strict Sphinx baseline exceptions
  remain red; Windows C-worker CI gap
  remains open until demonstrated.
  No new performance optimization,
  native handoff reruns, or additional
  pool CI retries. ``optimized01``
  RETAIN; native boundary A/B REJECT;
  no ``optimized02``.
* **Recovery:** no new local ZIP or
  uncommitted source is claimed. This
  documentation snapshot plus GitHub
  Actions run/job IDs above is the
  recovery source. Return after the
  next bounded validation checkpoint
  for independent Coordinator review.

Implementation post-merge Tests closure -- 56/56 after isolated retry
---------------------------------------------------------------------

* **Completed on 2026-11-04:** merge-push GitHub Actions
  Tests run ``41801317209``, attempt 2,
  ``completed / success``, SHA
  ``9126cddfb9db45b787ea7ea626c3e152f1d1ac24``.
  Full latest-attempt 56-job inventory checked:
  **56 success / 0 failure / 0 cancelled / 0 running**.
* **Original negative evidence:** attempt 1
  completed failure (55 success / 1 failure),
  job ``126477485524`` on Linux CPython
  3.14t/PostgreSQL 16; async pool test
  ``test_refill_on_check[asyncio]`` raised
  ``psycopg_pool.PoolTimeout`` after proxy
  stop/start at second ``p.wait(timeout=2)``.
  Two internal pytest last-failure retries
  failed. Same test failed in a pre-merge
  matrix (different job/environment) and
  later passed its isolated retry.
  **Pool/proxy lifetime/timing root cause
  remains unknown**; green CI is not
  proof that its intermittency is fixed.
* **Exactly one isolated GitHub Actions
  rerun authorised and performed:** original
  job ``126477485524`` was retried
  only once, resulting in new attempt-2
  job ``126492158840``, now PASS.
  No additional retry or whole-workflow
  rerun is permitted or necessary.
  Original PR-triggered Tests
  ``41801325261`` finished CANCELLED
  (all 56), not a success source.
* **Repository and constraints:** this
  closure is docs-only. No changes
  to CI workflow, pool/test code,
  timeouts, qt-thread runtime, C
  extension, benchmark v3 or
  optimized01. Accepted baseline
  lint/Sphinx exceptions and Windows
  compiled C-worker validation gap
  persist, not relabelled as passing.
  Native PGresult boundary REJECT
  under predeclared drift gate;
  optimized01 RETAIN, no optimized02.
* **Ownership / next stage:** send the
  accepted post-merge Tests checkpoint
  to existing ``00 Coordinator``
  for final independent acknowledgment.
  Current ``10 Implementation — current``
  remains sole normal runtime owner.
  No new performance experiment or
  architecture target starts by default.
  The previously configured condition
  watch for this CI run should be
  disabled after this checkpoint.
* **Context risk: CAUTION.** This is the
  authoritative latest CI status and
  supersedes earlier below-the-line
  in-progress/retry instructions.
  No HANDOFF NOW signal or unverified
  remaining-token estimate.

Implementation isolated job retry -- triggered
----------------------------------------------

* **Verified original-run final state (2026-11-04):**
  merge-push Tests ``41801317209`` completed
  ``failure`` with **55 success / one failure**
  among its full **56-job** matrix;
  no queued, in-progress or cancelled jobs.
  The only failed job was
  ``126477485524``,
  ``linux (python, 3.14t, postgres:16)``,
  ``test_refill_on_check[asyncio]`` after proxy
  restart, ``PoolTimeout``; both automatic
  pytest last-failure retries had failed.
* **Action already executed:** the Coordinator's
  gate was met, and the exact GitHub Actions
  **single-job rerun endpoint** was called **ONCE**
  for job ``126477485524``. GitHub confirmed
  ``success: true``. **Do NOT re-trigger that job
  or retry any others**; the one authorised
  action has already been consumed. A subsequent
  fresh GitHub status read timed out, so the
  **isolated retry result is still unknown**
  at this checkpoint. The original
  ``55/56`` is not acceptance.
* **Next recovery action:** inspect the latest
  run attempt, its 56-job latest-state
  matrix and the one rerun job result.
  If all 56 are ultimately SUCCESS, record
  ``56/56 after isolated retry`` with the
  original proxy/timing failure and unknown
  root cause. If the retry fails or another
  failure emerges, STOP retries and send
  narrow pool/proxy lifecycle diagnostics
  to existing ``00 Coordinator``.
  An automated **read-only** condition watch
  is configured for the retry outcome;
  it is forbidden to issue further retry calls
  and should be disabled when the final
  checkpoint is documented.
* **Scope:** docs-only state record.
  No production code, pool/tests/timeouts,
  workflows, benchmark v3 or performance
  experiments changed. Baseline lint/Sphinx
  exceptions and Windows compiled C-worker
  gap persist. ``optimized01`` RETAIN;
  native result-boundary REJECT;
  no ``optimized02``.
* **Context risk: CAUTION.** Sole
  Implementation ownership remains here.
  This section supersedes the earlier
  in-progress snapshot below for retry
  action/status, and prevents accidental
  duplicate GitHub Actions execution.

Implementation targeted CI retry -- in-progress checkpoint
----------------------------------------------------------

* **Owner / scope:** existing ``10 Implementation — current``,
  ``feature/qt-process-pq``, observed HEAD
  ``030367248027d27d8ee2a75c1885ca1371529c81``.
  Coordinator approved **one isolated GitHub Actions job retry**
  only after the merge-push Tests matrix is fully completed
  and all 56 latest jobs have been inspected.
* **Observed CI state:** merge-push Tests
  ``41801317209`` (merge SHA ``9126cddf``)
  was **still in progress** on 2026-11-04.
  Latest complete 56-job inventory:
  **43 success, 12 in progress, one failure**,
  no other completed failures or cancelled jobs.
  The failed job is exactly ``126477485524``,
  ``linux (python, 3.14t, postgres:16)``,
  ``test_refill_on_check[asyncio]``, known
  proxy-restart ``PoolTimeout`` (two pytest
  last-failure retries already failed).
  **No isolated GitHub Actions retry has been
  triggered at this checkpoint**, because the
  required completion gate is not met.
* **Conditional execution gate:** once the
  original run is terminal, fetch **all 56 jobs**
  including pagination and confirm that the sole
  unsuccessful one remains job ``126477485524``.
  Only then invoke the *single-job* rerun endpoint
  once, never rerun all failures or the workflow.
  On a second failure or any new failed job,
  STOP further retries and provide focused
  pool/proxy lifecycle logs and environment
  evidence to existing ``00 Coordinator``.
  On 56/56 latest-job success after the isolated
  retry, preserve the original failure and unknown
  cause in this map. Do not declare acceptance
  until those checks are complete.
* **Unchanged decisions:** PR #2 merged;
  accepted baseline lint/Sphinx exceptions
  and Windows compiled ``worker_impl=c``
  validation gap are preserved.
  ``optimized01`` RETAIN; native result
  boundary diagnostic REJECT; no ``optimized02``.
  No production, tests, timeout, CI workflow,
  benchmark-v3 or performance change.
* **Recovery / context:** this is an in-progress
  snapshot; the original run has **not yet
  reached the retry gate**. Recheck live GitHub
  HEAD and jobs before any action, and enforce
  at-most-one retry. Context risk CAUTION;
  no independent second runtime owner or
  speculative implementation is authorised.

Coordinator post-merge CI decision -- targeted retry
----------------------------------------------------

* **Owner/review:** existing ``00 Coordinator``, observed ``feature/qt-process-pq``
  at ``38e71e2ccadd40c8181056d59a489a7a291c3c73``.
  GitHub merge verification ``9126cddf`` stands; follow-up
  changes are project-map docs only. No local Windows
  tree, uncommitted changes or new test artifacts
  were inspected by Coordinator.
* **Evidence:** ``41801317209`` merge-push matrix
  partially executed with job ``126477485524``
  failed on ``test_refill_on_check[asyncio]``
  with two internal retries also failing.
  Pool proxy refused connections after restart,
  with pending ``pproxy`` asyncio tasks;
  the exact cause remains unproven.
  ``41801325261`` and ``41802212474``
  were cancelled. Earlier synchronized
  matrix ``41720955777`` succeeded
  56/56 after one isolated job retry
  of the *same test* in a different
  C/postgres matrix configuration.
* **Decision/unfinished:** no automatic
  waiver for this failed CI. After
  ``41801317209`` completes,
  allow **one fresh isolated retry**
  of only the failed job if other
  jobs pass; inspect 56 latest-job results.
  If 56 successes result, accept with
  a documented transient-failure
  limitation, not a proved flake.
  If failure recurs or new failures
  appear, STOP retries and investigate
  the proxy restart/refill lifecycle
  with a bounded evidence-based plan.
* **Ownership/next action:** existing
  ``10 Implementation — current``
  remains sole runtime writer and
  owns CI monitoring / single retry.
  No performance experiment, ``optimized02``,
  or pool/test source changes are
  approved. Return a completed CI
  acceptance/failure checkpoint
  to Coordinator, with exact run,
  attempt and job IDs.
* **Context:** Coordinator CAUTION,
  with this GitHub recovery snapshot.
  This is an operational heuristic,
  not a context-window measurement.

Coordinator post-merge closeout -- PR #2 and performance plateau
----------------------------------------------------------------

* **Coordinator role and observed base:** existing
  ``00 Coordinator``; target ``feature/qt-process-pq``
  at verified two-parent merge SHA ``9126cddfb9db45b787ea7ea626c3e152f1d1ac24``.
  This snapshot is a docs-only follow-up commit;
  read the live HEAD when resuming.
* **Completed bounded configuration milestone:**
  PR #2 merged after exact SHA/CI/diff
  verification, with eight configuration-owned
  files, no SQL hot-path changes, and unchanged
  performance runtime/docs.
  The PR description was updated post-merge;
  accepted lint/Sphinx and Windows C-worker
  validation exceptions are recorded there.
* **Completed independent performance decision:**
  native PGresult result-boundary A/B is
  drift-gated REJECT; no real production
  result handoff was implemented. Native
  standalone feasibility PASS is narrower
  than correctness of a new runtime path.
  ``optimized01`` RETAIN, no ``optimized02``.
* **Unfinished validation:** post-merge Tests
  runs ``41801325261`` and ``41801317209``
  were queued at inspection; outcome unknown.
  Review their final results on GitHub.
  No new ZIP, uncommitted runtime changes,
  or local Windows worktree state was inspected
  by Coordinator. Older local-only benchmark
  ZIP hashes remain in the optimisation log.
* **Ownership and exact next action:** lift
  integration write pause; existing
  ``10 Implementation — current``
  resumes sole runtime ownership, verifies
  branch HEAD/CI, and records a bounded
  verification checkpoint. Configuration
  feature owner is closed and should not
  continue editing the active runtime.
  No Scout or second Implementation owner.
* **Context:** Coordinator context risk
  ``CAUTION``; this snapshot plus PR #2
  merge/evidence and optimisation log are
  the authoritative recovery package.
  No measured token count is asserted.

Implementation post-merge CI triage -- 2026-11-04
-------------------------------------------------

* **Repository:** ``ARMAGGIDON/qpsycopg``,
  ``feature/qt-process-pq``, observed pre-checkpoint
  HEAD ``c9aefa28da3ebbd448afb9721aafd32bd8f1dbab``.
  It is the verified two-parent PR #2 merge
  ``9126cddfb9db45b787ea7ea626c3e152f1d1ac24``
  plus one Coordinator docs-only closeout commit.
  GitHub compare shows only ``docs/qt-process-project-map.rst``
  changed after merge; all eight merged config files,
  production performance code and documented decisions
  are intact. This chat could not inspect or synchronize
  the user's Windows/local checkout: repository state
  was verified using live GitHub branch and diff.
* **Requested Tests runs at merge SHA:**
  ``41801325261`` (``pull_request``) completed
  ``cancelled``; 56/56 matrix jobs cancelled.
  ``41801317209`` (``push``) was still executing
  at inspection, with 56 matrix jobs: 24 SUCCESS,
  28 in progress, 3 queued and **one FAILURE**.
  These are **observed in-progress statuses**, not
  a claim about the eventual run conclusion.
* **Failure triage:** job
  ``126477485524``,
  ``linux (python, 3.14t, postgres:16)``,
  ``tests/pool/test_pool_async.py::test_refill_on_check[asyncio]``.
  Full suite 5700 passed/1 failed/610 skipped/
  39 xfailed; failure at the second
  ``await p.wait(timeout=2)`` after
  ``proxy.stop(); proxy.start(); await p.check()``.
  Error ``psycopg_pool.PoolTimeout``;
  proxy reported closed connections/refused TCP
  and ``pproxy`` pending asyncio tasks. Both
  automatic last-failure reruns failed identically.
  The pre-merge matrix ``41720955777``,
  attempt 2, had passed 56/56 following an
  isolated retry of **the same test**. Thus this
  is a recurring, previously documented
  pool/proxy timing failure, not a newly
  identified Qt failure. Actual root cause
  remains unproven and post-merge Tests are
  **not green**. The merge diff's eight files
  exclude pool/proxy code and this test;
  the failing job used Python libpq wrapper,
  not qt-thread. Do not declare it a proven
  runtime regression or silently waive the
  failing CI check.
* **Other post-merge signals:** docs-only
  ``c9aefa28`` created fresh PR Tests
  ``41802212474``, also unfinished at inspection.
  Qt-process smoke/DLL jobs passed on
  merge-triggered ``41801325345``, while
  its docs-validation job failed with the same
  **nine baseline Sphinx messages** observed
  again in docs-only ``41802212499``
  (two literal-block quoting, six unexpected
  indentations, one missing-toctree warning).
  Repository Lint still reports the
  documented baseline mypy/cython-lint issues.
  Do not change or mislabel accepted exceptions
  as passing validation. Windows
  ``worker_impl=c`` remains an unclosed
  validation gap, not verified success.
* **Ownership / next action:** write pause has
  ended and current ``10 Implementation — current``
  remains sole ordinary runtime owner.
  No runtime, C extension, test, workflow,
  benchmark-v3 or optimization change was
  made for this checkpoint. ``optimized01``
  RETAIN, native result-boundary screen
  REJECT (predeclared drift gate),
  no ``optimized02``.
  Report the recurring failed post-merge
  matrix job and incomplete status to the
  **existing 00 Coordinator** for triage
  decision. Avoid any new performance series
  or speculative test changes; check
  fresh CI state before further acceptance.
* **Context risk: CAUTION.** Current evidence,
  branch ownership and pending CI status are
  recoverable from this checkpoint; do not
  infer remaining tokens. No observed
  HANDOFF NOW signal or ownership transfer.

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
--------------------------------------------------

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

Native PGresult boundary -- measured implementation recovery snapshot
---------------------------------------------------------------------

* **Current role and ownership:** existing Implementation/
  Optimisation chat is the sole ordinary runtime owner
  for ``ARMAGGIDON/qpsycopg``,
  ``feature/qt-process-pq``. The completed bounded
  diagnostic goes to existing ``00 Coordinator``
  for independent milestone review, not parallel
  implementation. Configuration PR #2 has
  separate integration ownership.
* **Base / branch / commits:** Coordinator hypothesis
  selected at ``98a6ad178d03ff0ad94f1b28c6d3ddac6cd164a1``.
  Native feasibility preflight PASS captured
  at ``c303749b6b1551901eb5fc7bf4c2bcaeb2f2ae56``.
  Complete-result-boundary Python/PowerShell
  diagnostic files committed before the run;
  A/B manifest HEAD
  ``f19aafcee7d07441e704d22567f0e4abae780c74``.
  Measured optimization log commit
  ``c64ef448``, performance notes
  ``8c36ab10``. Reconfirm *live*
  branch HEAD when resuming; these
  are historical checkpoint hashes.
* **Task and result:** investigate whether replacing
  the complete snapshot/``PackedValues`` →
  synthetic PGresult → hybrid Transformer
  boundary with an owned cross-thread
  native C PGresult → C Transformer offers
  >=15 us/SELECT process CPU gain under
  controlled current-vs-native A/B.
  Standalone preflight PASS, but
  **measured A/B FAILS predeclared drift
  gate; decision REJECT**. The
  raw CPU direction is positive,
  not validated production speedup.
  No new optimisation hypothesis selected.
* **Tests, metrics and drift:** four independent
  child processes x 12 five-block cycles,
  1000 SELECT 1 requests/block, 96 paired
  comparisons. CPU repeat medians
  +31.276/+54.671/+29.817/+38.567 us;
  across-repeat median +34.921 us,
  MAD 4.375 us; 71/96 positive.
  Wall repeat-median +43.460 us,
  72/96 positive. Mode wall drift
  over 15% occurred in **three
  of four** repeats and invalidated
  the original acceptance screen.
  Windows process CPU quantization
  and within-repeat CPU MAD
  30-44 us also limit attribution.
  No acceptance smoke/v3 benchmark
  was warranted because no runtime
  patch was created.
* **Evidence / locality:** original ZIP
  ``test-results\qt-thread-native-boundary-ab-20261103-230648-799.zip``
  was supplied in this chat and
  remains Windows-local; **not
  committed to GitHub**. SHA256
  ``997eb072dbaf3a148ef886fe3a353e1ddd16c6ca0a028605d7eb04855109f6c3``.
  Prior successful preflight ZIP
  also remains local, with its SHA
  documented in optimization log.
  Preserve these artifacts for
  independent review.
* **Code and local state:** only standalone
  diagnostic tools and documentation
  were added/updated in this bounded
  investigation. **No production
  runtime, C extension, optimized01
  or benchmark v3 edits**.
  Windows local worktree state is
  uninspected from GitHub and
  must not be asserted clean.
* **Constraints / rejected approaches:**
  ``optimized01`` RETAIN. Historical
  response-wait removal, worker
  native wait, speculative prefetch,
  GET/echo attribution and generic
  snapshot tuning remain rejected
  or completed. Native result
  screen now **REJECT** due to
  prespecified drift criterion.
  Do not rerun just to favor
  positive medians. Any new
  production design must preserve
  Psycopg public behavior, worker
  health/cache/errors, notices,
  close, cancellation, COPY,
  pipeline, multi-result and
  non-C fallback. No ``optimized02``.
* **Exact next action / review:** no pending
  Windows command or local experiment.
  Read latest branch HEAD, this map,
  optimization/performance evidence;
  send the **completed negative native
  boundary result** to existing
  ``00 Coordinator`` for independent
  decision. Return here after review
  if Coordinator selects work for
  current Implementation owner.
* **Context risk: CAUTION.** Long,
  tool-heavy Implementation chat,
  but key task, evidence and ownership
  are recoverable from GitHub.
  No invented token balance, and
  no observed HANDOFF NOW condition.
  If inconsistency appears, preserve
  a fresh rolling snapshot, provide
  an explicit same-role bootstrap,
  transfer sole runtime ownership
  and stop old-chat edits.

When resuming in a new chat/session
-----------------------------------

#. Verify live HEAD and read
   :doc:`qt-process`,
   :doc:`qt-process-project-map`,
   :doc:`qt-process-performance`,
   :doc:`qt-process-coordination`
   and :doc:`qt-process-optimization-log`.
#. Standalone PGresult feasibility PASS;
   subsequent native-result boundary
   CPU screen **REJECT** on prespecified
   drift gate. No runtime candidate
   or further Windows A/B is pending.
#. Coordinator review of the native-boundary
   REJECT and PR #2 post-merge verification
   are complete. Integration write pause
   has ended; existing ``10 Implementation — current``
   retains sole runtime ownership. Verify
   the post-merge Tests CI before choosing
   any further work. No new optimisation
   direction is authorised by this closeout.
