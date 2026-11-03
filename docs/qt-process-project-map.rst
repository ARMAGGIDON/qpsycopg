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

#. The bounded qt-thread short-query CPU profiling investigation is **complete**.
   The corrected diagnostic archive
   ``qt-thread-cpu-profile-20261103-192809-646.zip`` contains two valid
   upstream/direct/qt-thread rounds at ``9b0f7108``. Production runtime
   remains unchanged, and ``optimized01`` remains RETAIN.
#. Clean qt-thread CPU is 250-255 us/SELECT, compared with fork direct
   125-146 us. The no-SQL worker status GET costs 40-45 us wall and 36-47 us
   process CPU per request, with about two extra context switches; its
   ``_dispatch`` wait occupies approximately 35-39 us inclusive wall.
   This is a measured **fixed thread handoff** cost, not a complete
   decomposition of the 104-130 us SELECT CPU gap.
#. ``_wait`` includes actual PostgreSQL socket waiting and nested timings
   overlap. Upstream Windows itself uses Python ``select.select()``
   rather than native ``wait_c``. Do not restart the rejected native
   wait, speculative result prefetch, snapshot or serialization work.
#. Next action belongs to the existing ``00 Coordinator`` chat:
   independently review the completed diagnostic and decide whether the
   following *single local hypothesis* merits a diagnostic-only A/B:
   ``_ThreadTransport._dispatch`` uses
   ``SimpleQueue.get(timeout=0.1)``, and its timed-response wait might
   contribute >=10 us to the observed 35-39 us fixed handoff wall.
   Compare timed vs untimed waits in a controlled **non-production**
   no-SQL worker request, preserving a clear liveness/error criterion.
#. If the hypothesis is not measurably supported, reject it without
   creating ``optimized02``. A production untimed wait must never
   silently lose fatal worker/close detection. If the Coordinator approves
   a safe variant after local evidence, the implementation owner shall
   run semantic smoke and unchanged paired benchmark v3 with CPU/query,
   cancellation, Qt p95/p99, bulk, connect and resources as guardrails.
#. A new Critic/Scout is **not required** by current evidence. Only one
   implementation chat owns any subsequent runtime experiment.

When resuming in a new chat/session
-----------------------------------

#. Read :doc:`qt-process`, this project map, :doc:`qt-process-performance`
   and :doc:`qt-process-coordination`; inspect current HEAD and
   :doc:`qt-process-optimization-log`.
#. ``optimized01`` is retained, not pending measurement; semantic smoke
   passed at that checkpoint. Do not repeat rejected experiments.
#. The corrected CPU profiling archive was analysed, with the result
   recorded in the optimisation log and performance notes. The invalid
   Python 3.13 cProfile function rankings are rejected.
#. This is a completed investigation checkpoint requiring independent
   review by the **existing 00 Coordinator** before new optimisation
   direction. The next action is coordinator review of the bounded
   timed-vs-untimed thread-handoff diagnostic hypothesis, not an
   implementation change and not another Windows profile rerun.
