Qt process backend project map
==============================

This file is the implementation/handoff map for qpsycopg's Qt-oriented pq work.
Read :doc:`qt-process` first for user-visible behaviour and
:doc:`qt-process-performance` for measurements and rejected experiments.

Repository state
----------------

* repository: ``ARMAGGIDON/qpsycopg``
* development branch: ``feature/qt-process-pq``
* draft PR: ``#1``
* primary implementation: ``PSYCOPG_IMPL=qt-process``
* architecture proof of concept: ``PSYCOPG_IMPL=qt-thread``
* canonical worker implementation: ``QPSYCOPG_WORKER_IMPL=binary``
* current code handoff before this documentation refresh: ``64f2ee8``
  (``fix: keep qt-thread worker backend consistent``)
* latest full numbered benchmark captured in the previous work: ``optimized35``;
  several later targeted validations contain newer architectural evidence and
  are more relevant than that numbered checkpoint for the current next step

The branch has moved far beyond the old ``optimized16`` handoff. Do not resume
from the old short-query profiling plan or repeat native result-packing/WAIT work.

Current direction
-----------------

The process-backed implementation has already received the major measured
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
* native parent-side named-pipe frame exchange;
* resource telemetry and reusable historical vanilla baselines.

After these changes, controlled architecture benchmarks showed that a dedicated
worker thread is much closer to direct Psycopg than the separate process design.
The current work therefore investigates whether the process boundary itself is
the wrong data-plane boundary instead of continuing small Python IPC tweaks.

The branch now contains a real ``qt-thread`` proof of concept, but it is not yet
semantically validated: its smoke suite still fails during the initial connection
path at the current handoff.

Architectural invariants
------------------------

Treat changes to these points as architectural changes, not local optimisations.

#. High-level Psycopg semantics remain in the application process:
   ``Connection``, ``Cursor``, transactions, adaptation, row factories,
   SQLSTATE exception mapping and user callbacks.
#. The canonical ``qt-process`` backend keeps the stateful libpq session in the
   worker process.
#. A recycled worker process does not imply a recycled PostgreSQL session. A
   connection close finishes the real ``PGconn``; only the warmed worker process
   may be reused.
#. ``qt-process`` and ``qt-thread`` are explicit opt-in backends and are never
   implicit fallbacks.
#. The worker pq implementation is normal ``binary``, ``c`` or ``python``;
   never recursive ``qt-process``/``qt-thread``.
#. Result adaptation remains parent-side. Worker/native helpers may pack raw
   bytes/metadata but must not replace normal Psycopg adaptation semantics.
#. Psycopg's existing connection lock remains authoritative for a connection
   shared by caller threads.
#. Synchronous Psycopg calls remain synchronous and block the caller.
#. Ordinary PostgreSQL statement errors must not kill a healthy session.
#. Fatal ``ConnStatus.BAD`` handling must still terminate/mark a broken session.
#. Parent-process file descriptors must not be passed to worker libpq.
#. Benchmark conclusions must consider the whole process tree for process-backed
   resource measurements.

Current qt-process architecture
-------------------------------

On Windows the preferred data path is::

    application process
      |
      +-- Connection/Cursor/adaptation
      |
      +-- synthetic PGconn/PGresult facade
      |
      +-- deferred send / compact fast execute
      |
      +-- raw named pipe
      |     |
      |     +-- native parent frame exchange when psycopg_c helper is available
      |
      +================ process boundary ================+
                                                        |
                                               reusable QProcess worker
                                                        |
                                               psycopg_binary.pq
                                                        |
                                                      libpq
                                                        |
                                                   PostgreSQL

Cancellation is intentionally separate::

    parent CancelControlClient
        |
    authenticated localhost TCP
        |
    worker cancel-control thread
        |
    reusable PGcancelConn

The main Windows transport defaults to ``QPSYCOPG_IPC=pipe``. TCP remains a
fallback and uses ``SIO_LOOPBACK_FAST_PATH`` when available. Non-Windows currently
uses TCP.

Source map
----------

Backend selection
~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/__init__.py``
    Selects ``qt-process`` and the experimental ``qt-thread`` only when explicitly
    requested.

``psycopg/psycopg/__init__.py``
    Routes the Qt backends through the qpsycopg connection classes.

``psycopg/psycopg/_cmodule.py``
    ``qt-process`` and ``qt-thread`` use the Python generator layer because their
    synthetic ``PGconn`` proxies are not local native ``PGconn`` objects.

Parent qt-process facade
~~~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/pq_qt_process.py``
    Base remote pq facade and generic transport. Important pieces:

    * ``_ProcessOwner`` owns ``QProcess`` on a dedicated Python thread;
    * ``_Transport`` owns generic request/response state and caches;
    * ``PGconn`` / ``PGresult`` expose the synthetic pq API;
    * connection/query caches mirror status needed by the parent generator.

``psycopg/psycopg/pq/_qt_process_lazy.py``
    Current production-oriented patch layer. It owns most of the later hot-path
    work:

    * deferred ``send_query*``;
    * compact fast execute frames;
    * worker-process reuse (pool size currently one);
    * lazy cancellation-control setup;
    * Windows transport selection;
    * TCP loopback fast-path patching;
    * parent native named-pipe frame exchange;
    * connection/session release rules.

Do not inspect only ``pq_qt_process.py`` when debugging current behaviour: several
important current behaviours are patched in by ``_qt_process_lazy.py``.

Windows pipe transport
~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/_qt_process_pipe.py``
    Raw byte-stream Windows named-pipe adapter using ``CreateNamedPipeW`` /
    ``CreateFileW`` and ``io.FileIO``.

    ``PipeStream`` keeps a read-ahead buffer so an over-read is preserved instead
    of corrupting the next frame.

    ``QPSYCOPG_NATIVE_IPC=0`` disables the native parent frame exchange for A/B
    testing. Native worker receive exists behind ``QPSYCOPG_NATIVE_RECV=1`` but is
    intentionally off by default after validation.

``psycopg_c/psycopg_c/_psycopg/qt_ipc.pyx``
    Cython/WinAPI helpers for:

    * native named-pipe frame round-trip with read-ahead;
    * experimental native worker frame receive;
    * experimental native Winsock socket wait.

    The parent round-trip helper is retained. Worker receive and socket wait are
    not enabled by default because controlled A/B did not justify them.

Protocol and results
~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/_qt_process_protocol.py``
    Framing, generic request/response objects, compact fast-frame encoding,
    snapshots and ``PackedValues``.

``psycopg_c/psycopg_c/pq/pgresult.pyx``
    Shared native result packers, including qpsycopg value/metadata helpers.

``psycopg_c/psycopg_c/_psycopg/qt_bulk.pyx``
    Parent-side packed bulk decoder for supported standard loaders with Python
    fallback for custom/unsupported cases.

``psycopg/psycopg/_transformer.py``
    Preserves parent-side adaptation/context semantics while using the packed
    decoder where safe.

Worker
~~~~~~

``psycopg/psycopg/pq/_qt_process_worker.py``
    Base request handling, libpq send/wait/consume/result state machine,
    ``10 ms`` WAIT drain and generic snapshot logic.

``psycopg/psycopg/pq/_qt_process_worker_pool.py``
    Reusable worker entry point and current worker-side specialisations:

    * session reset/reuse rules;
    * native result metadata capability detection;
    * cached client encoding;
    * successful-query status inference;
    * worker CPU profiling hooks;
    * optional native worker receive/wait experiment gates.

The production default keeps Python worker receive and Python ``select.select()``.
Do not enable native wait by default based only on the existence of the helper.
The 3-way validation showed it was slower.

Cancellation
~~~~~~~~~~~~

``psycopg/psycopg/pq/_qt_process_cancel.py``
    Dedicated authenticated TCP cancel channel. The worker control thread owns a
    reusable ``PGcancelConn`` after it is created by the main worker thread.

``psycopg/psycopg/connection_qt_process.py``
    Sync/async remote waits, safe cancellation routing and connection lifecycle.
    ``_qt_process_lazy`` patches cancellation setup so the control channel is
    opened lazily before the first query rather than for every connect/close pair.

Experimental qt-thread
~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/pq_qt_thread.py``
    Development proof of concept which reuses the synthetic pq facade but
    replaces process IPC with ``queue.SimpleQueue`` and a dedicated Python thread.

    Both ``_qt_process_worker`` and ``_qt_process_worker_pool`` must use the same
    real worker pq backend in this shared interpreter. Commit ``64f2ee8`` fixed a
    mixed-namespace state where one worker module saw ``binary`` while the other
    saw the ``qt-thread`` proxy module.

``tools/smoke_qt_thread.py``
    Semantic gate covering adaptation, packed NULL/empty values, multiple result
    sets, COPY, SQL error survival, caller-thread sharing, cancellation, fatal
    backend termination and ``AsyncConnection``.

``tools/validate_qt_thread.ps1``
    Intended full direct/binary vs qt-thread vs qt-process validation. Do not run
    it again until the standalone qt-thread smoke passes.

Benchmark and profiling map
---------------------------

``tools/test.ps1``
    Main Windows harness. Modes: ``smoke``, ``bench``, ``transport``, ``qt``,
    ``gui`` and ``all``. Default repeated benchmark count is seven.

    The current benchmark no longer repeats vanilla seven times. It performs
    repeated qpsycopg runs plus one current vanilla sentinel; historical
    compatible vanilla runs are aggregated separately.

``tools/vanilla_baseline.py``
    Builds a reusable compatible vanilla baseline using median/MAD and sample
    counts. Compatibility is keyed by workload settings/version.

``tools/bench_qt_process.py`` and ``tools/process_resources.py``
    End-to-end SQL benchmark plus process-tree resource telemetry: CPU, context
    switches, Working Set/RSS, Private Bytes, handles, threads, processes and I/O.

``tools/profile_cpu.ps1``
    Parent/tree CPU decomposition.

``tools/profile_worker_hot_path.ps1``
    Worker CPU phase decomposition.

``tools/bench_ipc_transport.py``
    Local IPC microbenchmark which compared TCP, TCP loopback-fast-path and named
    pipes before the end-to-end transport redesign.

``tools/bench_native_ipc.ps1``
    Synthetic Python-vs-native named-pipe data-plane A/B.

``tools/validate_transport.ps1``
    Paired TCP vs named-pipe SQL/Qt/CPU validation.

``tools/validate_native_ipc.ps1``
    Same-run Python-pipe vs native parent frame-exchange validation.

``tools/validate_worker_native.ps1``
    Isolates worker native receive and native socket-wait experiments.

``tools/validate_worker_architecture.ps1``
    Direct Psycopg vs dedicated-thread vs qt-process architecture benchmark.

``tools/validate_worker_qt_cancel.ps1``
    Focused Qt/cancellation gate for those three architectures.

Execution flow: warmed short query
----------------------------------

#. Parent cursor/generator reaches synthetic ``send_query*``.
#. ``_qt_process_lazy`` stores the send locally.
#. ``qpsycopg_execute_pending`` forms one compact fast request.
#. On Windows pipe mode, the parent can exchange the complete frame through one
   Cython/WinAPI helper call with the GIL released.
#. Worker reconstructs the request and drives normal libpq.
#. If complete inside the bounded fast path, raw result/state snapshots return in
   the same response.
#. Parent restores the synthetic ``PGresult`` and normal Psycopg adaptation
   continues.

If a warmed SELECT unexpectedly uses multiple remote operations, inspect pending
send/cache invalidation before changing transport architecture.

Execution flow: large result
----------------------------

#. Same deferred/compound send starts the query.
#. If still busy, the generator enters remote WAIT cycles.
#. Worker drains readable data for up to ``10 ms`` per WAIT request.
#. Normal result-boundary handling retrieves the completed ``PGresult``.
#. Native packers produce ``PackedValues`` and metadata when available.
#. Parent packed decoder converts raw values to normal Python results.

Large ``fetchall()`` has repeatedly stayed near vanilla. Do not return to per-cell
packing unless profiling proves a regression.

Execution flow: worker reuse
----------------------------

#. ``connect_start()`` acquires an idle warmed transport if available.
#. A new real PostgreSQL ``PGconn`` is created in that worker.
#. Normal queries run.
#. ``PGconn.finish()`` closes the PostgreSQL session and cancel-control channel.
#. The transport state is reset.
#. At most one healthy idle worker is retained for the next session.

Never allow an already released parent ``PGconn`` proxy to operate on the next
session hosted by the same worker.

What the architecture benchmarks established
--------------------------------------------

The important final process-vs-thread experiment measured approximately::

    direct Psycopg       ~533 us SELECT median
    dedicated thread     ~567 us
    qt-process           ~724 us

Paired interpretation:

* dedicated thread vs direct: roughly ``+5%`` median;
* qt-process vs direct: roughly ``+38.6%`` median;
* qt-process vs dedicated thread: roughly ``+31.2%`` median;
* process CPU vs dedicated thread: roughly ``2.44x``;
* 100k-row work: thread approximately direct, process roughly ``+6.6%`` versus
  thread.

Context switches were approximately ``1 / 3 / 3`` per query for direct / thread /
process. Therefore the process penalty is not simply "more context switches";
the process boundary, serialization/framing, kernel IPC and second interpreter
remain material costs.

The focused Qt/cancel gate also established:

* ``caller=thread`` Qt timer behaviour is effectively similar for all three;
* cancellation succeeds and the connection remains usable afterwards;
* ``caller=main`` blocks the Qt event loop for all three architectures.

This evidence is the reason ``qt-thread`` exists. Do not resume by blindly
micro-optimising process IPC.

Danger zones / established decisions
------------------------------------

Do not reintroduce speculative WAIT result prefetch
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``get_result()`` timing is part of Psycopg's result-boundary semantics. Previous
prefetch attempts broke fatal/COPY behaviour.

Do not optimise only WAIT request count
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``10 ms`` drain already removed most avoidable WAIT crossings. Remaining WAIT
wall time is mostly real libpq/socket waiting.

Do not trust parent-only RSS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Process-backed memory must be measured as parent + worker tree. Old results which
made qpsycopg appear to use less RSS were incomplete for architecture comparison.

Do not repeatedly benchmark unchanged vanilla
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use the accumulated compatible vanilla baseline plus one sentinel unless the
workload definition or a previously unmeasured metric changes.

Do not default native worker receive or native wait
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The helpers remain useful for experiments, but the controlled 3-way validation
found receive neutral and native wait worse. Current production default is
Python worker receive + Python ``select.select()``.

Do not return to AF_UNIX or unsafe one-read TCP framing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

AF_UNIX was rejected against the retained Windows transport path. The older
``recv(64 KiB)`` TCP experiment also risked crossing frame boundaries without
preserving the over-read. The current pipe adapter explicitly preserves read-ahead
bytes.

Do not infer GUI responsiveness from worker topology
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Direct/thread/process all block when a synchronous query is called from the GUI
thread. Correct caller placement or async APIs are still required.

Current qt-thread blocker
-------------------------

The proof of concept has passed neither the standalone smoke nor full validation
yet. The sequence so far was:

#. initial implementation ``f3d45e7``;
#. validator diagnostics fixed in ``074bc7a``;
#. Python generator-layer selection fixed in ``756de46``;
#. shared-interpreter worker backend consistency fixed in ``64f2ee8``;
#. latest observed smoke still fails during ``basic adaptation`` while opening
   the initial connection, now with enough diagnostics to identify the worker
   stop reason.

Do not run full ``validate_qt_thread.ps1`` until this connection/lifecycle issue
is fixed and ``tools/smoke_qt_thread.py`` passes.

Immediate next work
-------------------

#. Keep ``qt-process`` unchanged as the measured reference implementation while
   investigating the thread architecture.
#. Fix the current ``qt-thread`` initial connection failure; use the standalone
   smoke only while iterating.
#. Once the complete qt-thread smoke passes, run ``validate_qt_thread.ps1`` to
   compare real binary/direct, qt-thread and qt-process with the full synthetic
   pq proxy/cache/snapshot layer present.
#. Verify that the real qt-thread backend preserves COPY, multiple results,
   cancellation, fatal backend termination, shared-connection locking and
   ``AsyncConnection`` semantics before treating performance numbers as useful.
#. If qt-thread remains near the earlier dedicated-thread result while passing
   semantics, decide whether it becomes the main Qt data plane, an optional
   backend, or a stepping stone toward a native worker/data plane.
#. Only if qt-thread fails structurally should the next architecture study return
   to native worker/process designs. Do not restart random process-IPC
   micro-optimisation.
#. After the architecture decision, create a fresh full numbered checkpoint and
   update :doc:`qt-process-performance` again.

When resuming in a new chat/session
-----------------------------------

#. Read :doc:`qt-process`.
#. Read this project map.
#. Read :doc:`qt-process-performance`.
#. Inspect branch HEAD before editing.
#. Treat ``optimized35`` as the last numbered full benchmark in the preserved
   work, but use the later targeted native-IPC and architecture validations for
   current design decisions.
#. Remember that Windows ``qt-process`` now defaults to raw named pipe plus native
   parent frame exchange when available.
#. Remember that native worker receive/wait are experiments, not defaults.
#. Continue from the current ``qt-thread`` smoke blocker rather than repeating
   native packer, WAIT/cancel, TCP/AF_UNIX or worker-native experiments.
