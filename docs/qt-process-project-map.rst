Qt process backend project map
==============================

This is the development/handoff map for ``qpsycopg``'s experimental
``qt-process`` backend. It is intentionally more implementation-oriented than
:doc:`qt-process`. Read this file first when resuming work in a new development
session, then use :doc:`qt-process-performance` for the latest benchmark state.

Repository state
----------------

* repository: ``ARMAGGIDON/qpsycopg``
* development branch: ``feature/qt-process-pq``
* draft PR: ``#1`` (Qt/QProcess-isolated pq backend)
* public API goal: existing ``import psycopg; psycopg.connect(...)``
* opt-in selector: ``PSYCOPG_IMPL=qt-process``
* worker selector: ``QPSYCOPG_WORKER_IMPL=python|c|binary``
* latest recorded performance checkpoint: ``optimized14`` at
  ``efc8400e2505889f44d24e8c0b9822dba9964789``

Architectural invariants
------------------------

These constraints are deliberate. Treat a change which violates one of them as
an architectural change, not a local optimisation.

#. High-level Psycopg semantics stay in the application process:
   ``Connection``, ``Cursor``, transactions, adaptation, row factories,
   SQLSTATE exception mapping and user callbacks.
#. Stateful libpq objects stay in the worker process. A normal database
   connection owns one worker ``QProcess``.
#. ``qt-process`` is opt-in and is never selected as an implicit fallback.
#. The worker implementation must be a normal pq implementation
   (``python``, ``c`` or ``binary``), never ``qt-process`` recursively.
#. The normal C Psycopg generator layer is not used for qt-process because it
   assumes a local native ``PGconn``.
#. Result adaptation is still parent-side. Worker optimisations may package raw
   result bytes, but must not construct final Python rows/scalars as a semantic
   replacement for Psycopg adaptation.
#. The same connection may be shared by Python threads with separate cursors;
   existing Psycopg connection locking remains authoritative.
#. Synchronous API calls remain synchronous and block the calling thread.
   Process isolation is not a promise to make main-GUI-thread calls nonblocking.
#. Fatal/broken libpq state may terminate the worker after returning the
   originating error. Ordinary PostgreSQL statement errors must not kill a
   healthy session.
#. A parent-process file descriptor must never be passed to libpq in the worker.
   Consequently ``Connection.fileno()`` and parent-fd tracing are unsupported.

Top-level architecture
----------------------

::

    application process

      psycopg.connect()
          |
      Connection / Cursor
          |
      Python pq facade (remote PGconn / PGresult)
          |
      Transformer / row factory / SQLSTATE mapping
          |
      authenticated framed localhost IPC

    ========================= process boundary =========================

      QProcess worker
          |
      normal psycopg pq implementation (binary/c/python)
          |
      native PGconn / PGresult / libpq
          |
      PostgreSQL server

Source map
----------

Backend selection
~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/__init__.py``
    Selects pq implementation from ``PSYCOPG_IMPL``. ``qt-process`` is tried
    only when explicitly requested.

``psycopg/psycopg/__init__.py``
    Selects ``connection_qt_process.Connection`` and ``AsyncConnection`` when
    the loaded pq implementation is ``qt-process``.

``psycopg/pyproject.toml``
    Declares the ``qt`` optional dependency (``PySide6-Essentials``).

Parent-side pq transport and proxies
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/pq_qt_process.py``
    Main parent-side low-level implementation. Important responsibilities:

    * ``_ProcessOwner`` owns the ``QProcess`` in one dedicated Python thread so
      Qt object affinity is not crossed accidentally;
    * ``_Transport`` owns the authenticated TCP socket, request IDs, RLock,
      request/response framing, connection/query caches and broken-worker
      handling;
    * ``PGconn``, ``PGresult``, ``PGcancelConn`` and ``PGcancel`` implement pq
      protocol-compatible remote proxies;
    * ``waitRemote()`` converts the synthetic parent handle back into a remote
      WAIT request;
    * ``_restore()`` reconstructs remote refs and result snapshots;
    * connection state caches include hot ``status``, ``transaction_status``
      and ``client_encoding`` values;
    * query caches carry ``flush``, ``is_busy``, consumed-input, notifications
      and already-restored results.

``psycopg/psycopg/pq/_qt_process_lazy.py``
    Performance layer which defers ``send_query*`` and merges the common
    send+execute sequence into one remote ``qpsycopg_execute_pending`` request.
    It currently monkey-patches ``_Transport.request``. Long term this should be
    integrated directly into ``_Transport`` once the performance design is
    stable.

Protocol and result representation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/pq/_qt_process_protocol.py``
    Shared parent/worker wire types and framing:

    * ``Request`` / ``Response``;
    * ``RemoteRef`` / ``RemoteError``;
    * ``ResultSnapshot``;
    * ``SendSnapshot`` / ``WaitSnapshot`` / ``ResultReadSnapshot``;
    * ``PackedValues``;
    * operation enum (CONNECT, CALL, GET, SET, WAIT, ESCAPE, CLOSE, ...);
    * authenticated token handshake and framed pickle transport.

``PackedValues`` stores cells as one data blob plus offsets and a NULL bitmap.
Its Python sequence interface is compatibility/fallback behaviour; the hot
bulk decoder reads the underlying buffers directly.

Worker
~~~~~~

``psycopg/psycopg/pq/_qt_process_worker.py``
    Worker command loop and native pq ownership. Key functions:

    ``Worker.run()``
        Receives requests, dispatches them, serialises responses, returns remote
        exceptions and stops after fatal broken-connection state.

    ``Worker._executePending()``
        Bounded compound fast path for a normal query. It combines deferred
        send, flush, initial read/consume and early result collection inside one
        request where possible.

    ``Worker._waitConnection()``
        Handles readable WAITs. It may consume input and drain immediately
        arriving data for a bounded grace period. It must not speculatively
        consume result boundaries with ``get_result()``.

    ``_snapshotResult()``
        Converts native ``PGresult`` metadata and raw cells into a
        ``ResultSnapshot`` with ``PackedValues``.

    Current ``_WAIT_DRAIN_GRACE`` is ``0.001`` seconds.

High-level connection wait/cancel layer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/connection_qt_process.py``
    Qt-process-specific ``Connection`` and ``AsyncConnection`` subclasses plus
    wait/cancel helpers.

    ``_waitRemote()`` / ``_waitRemoteAsync()``
        Drive normal Psycopg generators using the synthetic remote handle.

    ``_cancelRemote()``
        Gives cancellation priority over the normal query wait loop. A global
        pending-handle set prevents the query loop from immediately reacquiring
        the shared transport lock while a cancellation thread is waiting.

    ``_waitRemoteConn()``
        Drives generators which yield changing connection handles, including
        connection establishment and cancel-safe machinery.

    Async waits use ``asyncio.to_thread()`` rather than event-loop fd readers,
    which keeps Windows ProactorEventLoop support.

Transformer and row decode
~~~~~~~~~~~~~~~~~~~~~~~~~~

``psycopg/psycopg/_transformer.py``
    For ``qt-process``, exports a Python Transformer facade. It owns a native
    helper Transformer when ``psycopg_c`` or ``psycopg_binary`` is available.
    It delegates ordinary context/result bookkeeping to the Python layer but
    uses the native helper for value decoding.

``psycopg_c/psycopg_c/_psycopg/qt_bulk.pyx``
    Cython hot path for remote result snapshots. It:

    * reads ``PackedValues`` data/offset/null buffers directly;
    * constructs result tuples/lists in a C loop;
    * specialises exact standard Python loaders to native equivalents for the
      hot path;
    * preserves Python fallback for custom/subclass/unsupported loaders;
    * contains a packed metadata scanner used by the profiler.

    This specialisation is important. A C loop calling a Python ``Loader.load``
    once per cell is still slow. The successful 100k-row decode path reduced
    ``load_rows`` from roughly 0.54 s to 0.13 s.

``psycopg_c/psycopg_c/_psycopg.pyx``
    Includes ``qt_bulk.pyx`` after the normal Transformer definitions.

C build support
~~~~~~~~~~~~~~~

``psycopg_c/build_backend/cython_backend.py``
    Custom Cython backend. It now also forwards PEP 660 editable-build hooks.

``psycopg_c/build_backend/psycopg_build_ext.py``
    Finds libpq include/library directories. On Windows it can use
    ``PSYCOPG_PG_ROOT`` as an SDK root instead of requiring ``pg_config``.

``psycopg_c/psycopg_c/__init__.py``
    On Windows, when ``PSYCOPG_PG_ROOT`` is set, adds its ``bin`` directory to
    the DLL search path so the editable extension can load ``libpq.dll`` and
    dependencies.

Execution flow: normal short query
----------------------------------

A warmed normal ``SELECT 1`` should require one process-boundary request.
Conceptually:

#. ``Cursor.execute()`` runs the normal Psycopg generator in the parent.
#. The generator calls remote ``PGconn.send_query*``.
#. ``_qt_process_lazy`` records the send locally instead of sending immediately.
#. The next ``qpsycopg_execute_pending`` CALL merges the deferred send into the
   same remote request.
#. Worker invokes the real ``send_query*``, flushes, waits/consumes input for the
   bounded fast-path window, and snapshots results if already complete.
#. ``SendSnapshot`` also piggybacks connection/query state to avoid follow-up
   status/busy/flush RPCs.
#. Parent restores ``PGresult`` and normal cursor/Transformer code continues.

If profiling a warmed short SELECT shows two or more remote requests per call,
first inspect lazy-send activation and connection-state cache invalidation.

Execution flow: large result
----------------------------

#. Parent sends the query through the same compound send path.
#. If the result is not complete inside the initial compound grace period, the
   normal generator enters WAIT/consume cycles.
#. Worker ``_waitConnection()`` consumes available input and performs bounded
   readable-data draining.
#. When libpq reports not busy, the generator obtains the result normally.
#. Worker ``_snapshotResult()`` packs all cells into ``PackedValues`` and sends
   one ``ResultSnapshot``.
#. Parent ``PGresult`` exposes snapshot metadata and the packed value sequence.
#. ``Transformer.load_rows()`` invokes the Cython bulk helper.
#. The bulk helper decodes standard hot types natively and falls back to the
   configured Python loaders where required.
#. Row factory processing remains parent-side.

Current end-to-end bulk bottleneck is mostly before ``fetchall()``: the large
``execute`` path still uses roughly 80--100 remote WAIT requests in optimized14.
See :doc:`qt-process-performance`.

Execution flow: cancellation
----------------------------

Cancellation currently shares the main ``_Transport`` and its RLock.

#. Query thread is normally inside ``_waitRemote()``.
#. Cancelling thread sets the synthetic connection handle as cancellation
   pending.
#. Query wait loop stops entering new remote WAIT calls for that handle.
#. Cancelling thread acquires the transport lock and drives Psycopg's
   ``_cancel_gen()`` over remote cancel objects.
#. Pending state is cleared afterwards.

This avoids starvation but does not eliminate contention with a WAIT already in
progress. Longer worker-side WAIT draining therefore improves large-query
throughput while worsening cancellation latency. A dedicated authenticated
cancel/control socket is the likely long-term fix.

Protocol/security model
-----------------------

The transport is a localhost TCP connection used as an internal control/data
channel.

* Parent creates a random 256-bit token.
* Worker receives the token through its process arguments and connects back to
  the parent's temporary listener.
* Token bytes are exchanged and verified with ``secrets.compare_digest`` before
  any pickle frame is accepted.
* ``TCP_NODELAY`` is enabled.
* Frames have an explicit 64-bit length and reject obviously corrupt enormous
  lengths.
* Small frames (up to 64 KiB) coalesce header+payload; larger frames send header
  then payload.
* Pickle must remain strictly behind the authentication boundary.

State/caching model
-------------------

The parent deliberately mirrors a small amount of low-level state to avoid RPCs.
Whenever modifying query lifecycle code, check invalidation rules for:

* ``_flushCache``;
* ``_busyCache``;
* ``_consumeCache``;
* ``_notifyCache``;
* ``_resultCache``;
* ``_statusCache``;
* ``_transactionStatusCache``;
* ``_clientEncodingCache``;
* deferred ``_pendingSend`` state.

A previous COPY hang was caused by the worker consuming query state while the
parent retained stale busy/flush caches. Compound execute responses therefore
carry ``SendSnapshot`` state back to the parent. Cache coherency is semantic,
not merely a performance optimisation.

Tests and benchmark map
-----------------------

``tools/smoke_qt_process.py``
    Fast semantic regression test. It currently covers basic adaptation,
    multiple result sets, COPY result boundaries, normal SQL error survival,
    thread sharing, cancellation, unsupported parent fd, fatal backend
    termination and ``AsyncConnection``.

``tools/bench_qt_process.py``
    End-to-end performance test for connect, SELECT, 100k fetch phases, RSS,
    threads, SQL error survival and cancellation.

``tools/bench_qt_responsiveness.py``
    Qt timer responsiveness. Thread-caller mode is the meaningful GUI pattern;
    main-caller mode is a blocking control.

``tools/profile_qt_process.py``
    RPC and phase profiler. Modes include small SELECT, large fetch and decoder
    analysis. It reports decoder implementation, ``PackedValues`` sizes,
    loader types, packed metadata scan and direct ``load_rows`` timings.

``tools/test.ps1``
    Main Windows test harness. It runs smoke/bench/Qt/main-thread control,
    alternates vanilla and qpsycopg order, repeats benchmarks, builds
    ``summary.json``/``manifest.json`` and archives a timestamped ZIP.

Recommended validation after a semantic/runtime change::

    .\tools\test.ps1 smoke

Recommended full performance checkpoint after a validated optimisation::

    .\tools\test.ps1 all -Tag optimizedNN

Windows development setup
-------------------------

Typical Python package setup in the active venv::

    uv pip install -e ".\psycopg[qt]" "psycopg-binary==3.3.6"

Do not use ``-e ".\psycopg[qt,binary]"`` in the current development checkout:
``psycopg`` is version ``3.3.7.dev1`` and the matching exact binary development
wheel is not available from normal PyPI.

To reproduce the current high-performance packed C decoder, build the local
``psycopg_c`` extension. On Windows an unpacked PostgreSQL SDK/binary tree can
be used without installing the server as a service::

    $env:PSYCOPG_PG_ROOT = 'D:\Tools\PostgreSQL\18\pgsql'
    uv pip install -e .\psycopg_c

The root must contain ``include``, ``lib`` and ``bin``; the build needs headers
and ``libpq.lib``, while runtime loading needs ``libpq.dll`` plus dependencies.

The standard local Docker test database is typically::

    docker run --name qpsycopg-test `
        -e POSTGRES_PASSWORD=postgres `
        -e POSTGRES_DB=postgres `
        -p 5432:5432 `
        -d postgres:18

Default benchmark DSN::

    postgresql://postgres:postgres@127.0.0.1:5432/postgres

Danger zones / lessons already learned
--------------------------------------

Do not prefetch ``get_result()`` from WAIT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Worker WAIT may consume input, inspect busy state and collect notifications.
Aggressively calling ``get_result()`` there changed expected libpq/Psycopg timing
and caused incorrect fatal-termination behaviour. Keep result boundaries in the
normal generator path.

Do not optimise only WAIT count
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A 10 ms worker drain reduced a 100k query to roughly 14--15 WAIT RPCs but made
cancellation materially worse. A 0 ms drain retained too many RPCs. Current
1 ms is a compromise, not a proven final value.

Do not globally turn on C adapters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The parent needs remote-aware Python connection semantics. Some native C loaders
assume a native ``PGconn`` for connection parameters. Native specialisation must
be local and controlled, with fallback for custom loaders.

Do not judge PackedValues using slice materialisation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``PackedValues.__getitem__(slice)`` materialises Python ``bytes`` objects for
compatibility. Profiling that path measures reconstruction of the old layout,
not the intended bulk C path.

Do not reintroduce pickle protocol-5 OOB without evidence
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Earlier OOB experiments were slower and were rolled back.

Cache state is correctness-sensitive
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Any optimisation which consumes worker state without synchronising the parent
can hang COPY, misreport busy state, or alter result-set boundaries. State
piggybacking and invalidation must be reviewed together with the operation.

Known limitations / cleanup
---------------------------

* one QProcess per PostgreSQL connection;
* connection startup is intentionally much slower than vanilla and is not the
  current hot-path optimisation target;
* no usable parent ``fileno()`` and no parent-fd ``PGconn.trace()``;
* PyInstaller/frozen worker bootstrap is not implemented;
* lazy-send remains a monkey-patch module and should later be folded into
  ``_Transport``;
* cancellation still contends with the main transport and remains about 2x
  vanilla in the optimized14 checkpoint;
* large execute still spends too much time in repeated query completion WAITs;
* ``_waitProcessOwner`` reaches into the private transport owner and lifecycle
  handling can be cleaned after performance stabilises.

Where to look by symptom
------------------------

========================================  ======================================
Symptom                                   Start with
========================================  ======================================
SELECT suddenly uses >1 remote RPC        ``_qt_process_lazy.py`` and transport
                                          state invalidation
Large execute is slow                     worker ``_executePending()``,
                                          ``_waitConnection()``, WAIT profiler
Cancellation regresses                    ``connection_qt_process._cancelRemote``
                                          and WAIT drain duration
COPY hangs after query optimisation       parent flush/busy caches and
                                          ``SendSnapshot`` synchronisation
Wrong/missing second result set           worker/result-boundary handling; never
                                          speculative WAIT ``get_result()``
``fetchall`` becomes slow                 ``_transformer.py`` and
                                          ``psycopg_c/.../qt_bulk.pyx``
Custom type adaptation breaks             native-loader specialisation fallback
Worker exits on ordinary SQL error        worker broken-connection decision
Worker survives fatal BAD state           worker ``_connectionBroken()``/run loop
Qt objects warn about wrong thread        ``_ProcessOwner`` lifecycle/affinity
Asyncio/Windows stalls                    ``_waitRemoteAsync`` / ``to_thread``
Import of local ``psycopg_c`` fails       PostgreSQL SDK root / DLL search path
========================================  ======================================

Immediate next work
-------------------

#. Instrument the large ``execute`` path into server/libpq wait, consume/busy,
   snapshot pack, pickle, transfer, unpickle/restore phases.
#. Determine the share of the remaining overhead caused by 80--100 WAIT RPCs.
#. If WAIT contention remains dominant, design a dedicated authenticated
   cancel/control channel before increasing drain duration aggressively.
#. Preserve the now-successful ``PackedValues`` + specialised C decoder path.
#. Re-run smoke first, then a full tagged benchmark for every architectural
   performance change.

When resuming in a new chat/session
-----------------------------------

A minimal context recovery sequence is:

#. read :doc:`qt-process` for supported behaviour and setup;
#. read this project map;
#. read :doc:`qt-process-performance` for the latest numbers and rejected
   experiments;
#. inspect the current branch diff/HEAD before editing because documentation
   checkpoints may lag newer code;
#. run ``.\tools\test.ps1 smoke`` before and after semantic changes;
#. use ``profile_qt_process.py`` before proposing another performance fix.
