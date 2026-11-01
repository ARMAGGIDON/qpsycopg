Qt process backend project map
==============================

This is the implementation/handoff map for qpsycopg's experimental
``qt-process`` backend. Read this file first when resuming work, then read
:doc:`qt-process-performance` for current measurements.

Repository state
----------------

* repository: ``ARMAGGIDON/qpsycopg``
* development branch: ``feature/qt-process-pq``
* draft PR: ``#1``
* public API goal: normal ``psycopg.connect(...)`` semantics
* opt-in selector: ``PSYCOPG_IMPL=qt-process``
* worker selector: ``QPSYCOPG_WORKER_IMPL=python|c|binary``
* canonical performance worker: ``binary``
* latest full performance checkpoint: ``optimized16`` measured at
  ``e24e4169b000ef052f07b90924c45cd625f96791``
* latest post-checkpoint cleanup: ``a186786`` removes obsolete cancellation
  gating and the old main-channel fast-cancel path

Current performance state
-------------------------

The two dominant costs identified by the earlier phase decomposition have been
addressed.

Native snapshot packing
~~~~~~~~~~~~~~~~~~~~~~~

The worker no longer builds a large ``PackedValues`` payload by calling Python
``PGresult.get_value()`` once per cell on the hot binary-worker path.

The native packer is implemented in the shared Cython ``PGresult`` source and is
therefore generated into ``psycopg_binary`` as well as ``psycopg_c``. The worker
capability-detects it and retains a Python fallback.

Representative 100k x 3 packing time changed from roughly ``109 ms`` to
``5 ms``. Preserve the current data/offset/NULL-bitmap wire layout.

WAIT batching and cancellation isolation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Cancellation now has a separate authenticated localhost control channel. It no
longer waits for the main query transport lock.

The worker main thread creates the ``PGcancelConn`` from the real ``PGconn``;
a dedicated control thread then owns and drives only that cancel object. This
allows the main query WAIT drain to be increased to ``10 ms`` without restoring
the old cancellation-latency regression.

A representative 100k x 3 profile with ``10 ms`` drain reported ``11`` remote
WAIT requests instead of roughly ``67`` with ``1 ms`` drain. Full-checkpoint
cancellation is now approximately vanilla latency.

The remaining important performance gaps are therefore:

* warmed one-RPC short-query latency (about ``2x`` summary median in
  ``optimized16``);
* shared-connection threaded operations (about ``1.7x``);
* connection startup (structurally much slower, but not currently a hot-path
  target).

Architectural invariants
------------------------

Treat changes to these points as architectural changes rather than local
optimisations.

#. High-level Psycopg semantics stay in the application process:
   ``Connection``, ``Cursor``, transactions, adaptation, row factories,
   SQLSTATE exception mapping and user callbacks.
#. Stateful libpq/session objects stay in the worker process.
#. One normal PostgreSQL connection owns one worker ``QProcess``.
#. ``qt-process`` is explicit opt-in and never an implicit fallback.
#. The worker pq implementation is normal ``python``, ``c`` or ``binary``;
   never recursive ``qt-process``.
#. Result adaptation remains parent-side. The worker may package raw result
   bytes but must not replace normal Psycopg adaptation semantics.
#. Psycopg's existing connection lock remains authoritative for a connection
   shared by Python threads.
#. Synchronous calls remain synchronous and block the calling thread.
#. Ordinary PostgreSQL statement errors must not kill a healthy worker/session;
   fatal BAD-state handling may terminate it after returning the originating
   error.
#. Parent-process file descriptors must not be passed to worker libpq.

Top-level architecture
----------------------

::

    application process

      Connection / Cursor / adaptation
          |
      qt-process PGconn/PGresult facade
          |
          +-- main authenticated TCP channel -------------------+
          |                                                     |
          +-- dedicated authenticated cancel control channel ---|---+
                                                                |   |
    ================= process boundary =========================|===|===
                                                                |   |
      QProcess worker                                           |   |
          |                                                     |   |
          +-- main worker thread -> native PGconn/PGresult <----+   |
          |                                                         |
          +-- cancel control thread -> reusable PGcancelConn <------+
          |
      normal pq implementation (binary/c/python)
          |
      libpq -> PostgreSQL

Source map
----------

Backend selection
~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/__init__.py``
    Selects the low-level pq implementation. ``qt-process`` is used only when
    explicitly requested.

``psycopg/psycopg/__init__.py``
    Selects qt-process-specific ``Connection`` / ``AsyncConnection`` classes
    when the active pq implementation is ``qt-process``.

Parent transport and proxies
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/pq_qt_process.py``
    Parent low-level facade and main worker transport.

    Important pieces:

    * ``_ProcessOwner`` owns ``QProcess`` in one dedicated Python thread;
    * ``_Transport`` owns the main authenticated socket, RLock, request IDs,
      request/response framing, caches and worker-break handling;
    * ``PGconn`` exposes a synthetic handle used by remote wait helpers;
    * ``PGresult`` exposes snapshot-backed result metadata and packed values;
    * connection caches cover status, transaction status and client encoding;
    * query caches cover flush/busy/consume/notify/result state.

``psycopg/psycopg/pq/_qt_process_lazy.py``
    Defers ``send_query*`` and merges the common send/execute path into
    ``qpsycopg_execute_pending``. A warmed short SELECT should normally use one
    remote process-boundary request.

Cancellation control
~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/_qt_process_cancel.py``
    Dedicated parent/worker cancellation channel.

    Parent ``CancelControlClient`` owns an independent socket and serialises
    cancel requests on its own lock.

    Worker ``CancelControlServer`` owns a reusable ``PGcancelConn`` and a
    dedicated control thread. It performs ``start()`` / ``poll()`` / local
    ``select()`` and ``reset()`` without acquiring the main query transport.

``psycopg/psycopg/connection_qt_process.py``
    Opens the control channel after a successful database connection, closes it
    before worker teardown, and routes ``cancel_safe()`` exclusively over the
    control channel.

    ``_waitRemote()`` / ``_waitRemoteAsync()`` now continue normal query WAITs
    independently of cancellation. The obsolete pending-handle gate was removed
    in ``a186786``.

Protocol and result representation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/_qt_process_protocol.py``
    Shared framed request/response and snapshot types.

``PackedValues`` stores result cells as:

* one data blob;
* an offset table;
* a NULL bitmap;
* cell count.

The Python sequence interface is fallback/compatibility behaviour; the hot bulk
decoder reads the underlying buffers directly.

Worker
~~~~~~

``psycopg/psycopg/pq/_qt_process_worker.py``
    Owns the real pq ``PGconn`` and normal request loop.

    ``Worker._executePending()``
        Compound fast path combining deferred send, flush, initial waiting and
        result collection where possible.

    ``Worker._waitConnection()``
        Handles readable WAITs, consumes input and drains immediately arriving
        data for a bounded grace period. Current ``_WAIT_DRAIN_GRACE`` is
        ``0.010`` seconds.

        Do not speculatively call ``get_result()`` from this path; result
        boundaries belong to the normal Psycopg generator/result lifecycle.

    ``_snapshotResult()``
        Captures metadata and produces ``PackedValues``. The hot binary-worker
        path uses the native packer when available.

Native pack/decode
~~~~~~~~~~~~~~~~~~

``psycopg_c/psycopg_c/pq/pgresult.pyx``
    Contains ``_qpsycopg_pack_values()``. It reads native ``PGresult`` cells and
    writes the existing packed layout without per-cell Python ``bytes`` calls.

``psycopg_c/psycopg_c/_psycopg/qt_bulk.pyx``
    Parent-side bulk row decoder. It reads packed buffers directly and uses
    native specialisation for supported standard loaders while preserving
    Python fallback for custom/unsupported loaders.

``psycopg/psycopg/_transformer.py``
    Keeps normal parent-side adaptation/context semantics and delegates the
    supported packed hot path to the native helper.

Execution flow: warmed short query
----------------------------------

#. Parent cursor/generator reaches remote ``send_query*``.
#. Lazy-send records the send locally.
#. ``qpsycopg_execute_pending`` sends one compound request.
#. Worker invokes real libpq send/flush/wait/consume.
#. If the result completes in the bounded fast path, result snapshots/state are
   piggybacked back to the parent.
#. Parent restores ``PGresult`` and normal adaptation continues.

If a warmed short SELECT starts using more than one remote request per call,
inspect lazy-send activation and cache invalidation before redesigning transport.

Execution flow: large result
----------------------------

#. Parent uses the same compound send path.
#. If the query is still busy, the generator enters remote WAIT cycles.
#. Worker drains readable input for up to ``10 ms`` per WAIT request.
#. Normal generator/result logic retrieves the completed ``PGresult``.
#. Worker native-packs cells into ``PackedValues``.
#. Parent bulk decoder converts packed values into normal Python rows/scalars.

Current large-result ``fetchall()`` is near vanilla. A representative profile
uses about ``11`` WAIT requests and about ``5 ms`` snapshot packing for
300,000 cells.

Execution flow: cancellation
----------------------------

#. ``cancel_safe()`` obtains the existing ``CancelControlClient``.
#. Parent sends a control request over the independent cancel socket.
#. Worker control thread drives its reusable ``PGcancelConn`` locally.
#. Main query worker remains free to continue/complete its current WAIT request.
#. Backend cancellation makes the main libpq socket readable; normal query
   processing observes the cancellation result.

No pending-handle gate or main transport lock acquisition is required.

Protocol/security model
-----------------------

The IPC transports are localhost-only internal channels with authentication.

Main transport:

* parent creates a random 256-bit token;
* worker receives it in process arguments and connects back to the temporary
  listener;
* token is verified before pickle frames are accepted.

Cancel transport:

* worker main thread creates a separate random token/listener when the control
  channel is opened;
* parent connects and authenticates before cancel frames are accepted.

Pickle must remain behind these authentication boundaries.

State/caching model
-------------------

Parent-side mirrored state is a performance optimisation with correctness
consequences. Review invalidation whenever query lifecycle code changes:

* ``_flushCache``;
* ``_busyCache``;
* ``_consumeCache``;
* ``_notifyCache``;
* ``_resultCache``;
* ``_statusCache``;
* ``_transactionStatusCache``;
* ``_clientEncodingCache``;
* deferred ``_pendingSend`` state.

A previous COPY hang came from worker-side state consumption combined with stale
parent busy/flush state. Preserve snapshot/state piggybacking semantics.

Tests and benchmark map
-----------------------

``tools/smoke_qt_process.py``
    Semantic regression suite: adaptation, packed NULL/empty values, multiple
    result sets, COPY boundaries, SQL error survival, thread sharing,
    cancellation, unsupported parent fd, fatal backend termination and async
    connection behaviour.

``tools/bench_qt_process.py``
    Connect, warmed SELECT, bulk execute/fetchall, RSS, shared-connection
    threads, SQL-error survival and cancellation.

``tools/bench_qt_responsiveness.py``
    Qt timer behaviour. Thread-caller mode is the intended synchronous GUI
    pattern; main-caller mode deliberately demonstrates blocking.

``tools/profile_qt_process.py``
    Remote-request and phase profiler. Parent and worker timings overlap; never
    sum them as independent pipeline stages.

``tools/test.ps1``
    Windows smoke/bench/Qt/gui/all harness with repeated runs, summary/manifest
    JSON and timestamped ZIP archives.

Current validation commands::

    .\tools\test.ps1 smoke
    .\tools\test.ps1 all -Tag optimizedNN -Worker binary

Windows development notes
-------------------------

Typical Python package setup in the active venv::

    uv pip install -e ".\psycopg[qt]" "psycopg-binary==3.3.6"

The native local ``psycopg_c`` extension is needed when developing/rebuilding
shared Cython helpers. On Windows ``PSYCOPG_PG_ROOT`` may point to an unpacked
PostgreSQL SDK/binary tree containing ``include``, ``lib`` and ``bin``.

The canonical performance worker remains ``binary``. Rebuild the generated
binary package only when shared Cython/native code changes; Python-only worker,
connection or control-channel edits do not require a binary rebuild.

Danger zones / established decisions
------------------------------------

Do not prefetch ``get_result()`` from WAIT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

It changes result-boundary timing and previously broke fatal-termination
behaviour.

Do not optimise only WAIT count
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The current ``10 ms`` drain is justified because cancellation is independent and
a representative profile already reduced WAIT RPCs to about ``11``. The
remaining WAIT wall time is mostly real worker libpq waiting. Longer drain is
not automatically better.

Do not globally substitute native adapters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Connection-sensitive native loaders may assume a local native ``PGconn``.
Native packed decoding must keep controlled specialisation and Python fallback.

Do not judge PackedValues through slice materialisation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Slicing deliberately rebuilds Python ``bytes`` objects for compatibility and is
not the intended bulk path.

Do not reintroduce pickle protocol-5 OOB without new evidence
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Earlier OOB experiments were slower.

Do not switch canonical worker to ``c`` as a generic optimisation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The native packer already reaches the binary worker through shared generated
Cython code. Worker switching is not a substitute for profiling a measured cost.

Where to look by symptom
------------------------

========================================  ======================================
Symptom                                   Start with
========================================  ======================================
SELECT uses >1 remote RPC                 ``_qt_process_lazy.py`` and transport
                                          cache invalidation
One-RPC SELECT still slow                 short-query phase profile; fixed IPC /
                                          worker dispatch / libpq round trip
Large execute slow                        phase profiler: WAIT vs pack/serialize
Snapshot pack >~10 ms for 100k x 3        native packer activation/fallback
Cancellation slow                         ``_qt_process_cancel.py``; verify it is
                                          not falling back to main transport
COPY hangs                                busy/flush/result cache coherency
Wrong second result set                   result-boundary handling; never WAIT
                                          ``get_result()`` prefetch
``fetchall`` slow                          packed bulk decoder / loader fallback
Custom adaptation breaks                 native-loader fallback semantics
Worker exits on ordinary SQL error        broken-connection decision
Worker survives fatal BAD state           worker run/broken-state lifecycle
Qt wrong-thread warning                   ``_ProcessOwner`` lifecycle/affinity
Async Windows stall                       ``_waitRemoteAsync`` / ``to_thread``
========================================  ======================================

Immediate next work
-------------------

#. Smoke-test the post-optimized16 cleanup commit ``a186786``.
#. Profile the warmed one-RPC short SELECT path with ``worker=binary``. Separate
   parent framing/serialization, worker dispatch, libpq wait and response costs.
#. Use that result to decide whether the next optimisation is transport/framing,
   worker scheduling/dispatch, or something inside the compound execute path.
#. Compare the shared-connection thread benchmark with the short-query fixed
   cost before adding thread-specific mechanisms.
#. Keep native packing and ``10 ms`` WAIT drain unchanged unless a profile gives
   evidence to revisit them.
#. Treat connection startup as a separate structural optimisation problem, not
   the next hot-path target.
#. Run smoke before and after semantic/runtime changes; after a validated
   performance improvement, create a new full tagged checkpoint.

When resuming in a new chat/session
-----------------------------------

#. Read :doc:`qt-process` for user-visible behaviour/setup.
#. Read this project map.
#. Read :doc:`qt-process-performance` for the latest measurements.
#. Inspect current branch HEAD before editing; the latest full benchmark may be
   intentionally older than a cleanup/docs commit.
#. Treat ``optimized16 / e24e416`` as the latest measured full checkpoint until
   a newer tagged full run exists.
#. Follow ``Immediate next work`` instead of repeating native packer or WAIT/
   cancellation investigations.
