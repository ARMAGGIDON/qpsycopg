Qt process backend
==================

``qpsycopg`` adds an experimental ``qt-process`` low-level ``pq`` implementation
for PySide6 applications. The public Psycopg API remains the normal API:
``psycopg.connect()``, ``Connection``, ``Cursor``, transactions, adaptation, row
factories and SQLSTATE error mapping stay in the application process. Stateful
libpq objects and the PostgreSQL session live in a child ``QProcess``.

The backend is opt-in and is never selected automatically::

    $env:PSYCOPG_IMPL = 'qt-process'       # PowerShell

or::

    set PSYCOPG_IMPL=qt-process            # cmd.exe

The worker implementation is selected independently::

    $env:QPSYCOPG_WORKER_IMPL = 'binary'

Valid worker implementations are ``python``, ``c`` and ``binary``. The worker
must not use ``qt-process`` recursively.

For implementation-oriented navigation and current optimisation state see
:doc:`qt-process-project-map` and :doc:`qt-process-performance`.

Architecture
------------

::

    application process

      psycopg.connect()
          |
      Connection / Cursor / transactions
          |
      remote pq facade (PGconn / PGresult proxies)
          |
      Transformer / row factory / errors
          |
      authenticated framed localhost IPC

    ========================= process boundary =========================

      QProcess worker
          |
      normal Psycopg pq implementation
          |
      native PGconn / PGresult / libpq
          |
      PostgreSQL

The design deliberately isolates the stateful libpq session while keeping
normal Psycopg semantics in the parent process. Query parameters are adapted in
the parent before they cross the process boundary. Results cross the boundary as
raw PostgreSQL data and metadata and are adapted into Python values in the
parent.

Process and transport lifecycle
-------------------------------

Each database connection currently owns one worker ``QProcess``. A small parent
listener is opened on localhost, a random 256-bit authentication token is
created, and the worker connects back to it. The token is verified before any
pickle frame is accepted.

The parent-side ``QProcess`` object is owned by a dedicated Python thread to
avoid crossing Qt object affinity. The database API does not require the caller
to create a ``QCoreApplication`` merely for the worker lifecycle.

The transport uses framed localhost TCP with ``TCP_NODELAY``. Small frames are
sent as one header+payload write and large frames are sent as header plus payload.
The protocol carries typed request/response objects, remote object references,
remote exceptions and result snapshots.

Query fast path
---------------

A normal warmed query avoids several low-level process crossings which a naive
remote pq implementation would require.

The parent caches hot low-level state such as connection status, transaction
status, client encoding, flush/busy state and already-consumed input. Normal
``send_query*`` calls are also deferred briefly so the common
``send_query*`` + execute/wait sequence can be merged into one compound worker
request.

A warmed ``SELECT 1`` therefore normally crosses the process boundary once. If
the result is not complete during the worker's bounded compound execution window,
the standard Psycopg generator continues through remote WAIT/consume cycles.

Large-query WAIT processing performs a short bounded worker-side readable-data
drain. This reduces process crossings without moving result-set ownership into
the WAIT operation. Result boundaries are still consumed by the normal Psycopg
generator path.

Result transfer
---------------

Large results use ``PackedValues`` rather than a tuple containing one Python
``bytes`` object per cell. The snapshot contains:

* one contiguous byte buffer with non-NULL cell data;
* an offset table;
* a NULL bitmap;
* column/result metadata.

This representation reduces Python object count, pickle overhead and memory use.
The application-side ``PGresult`` preserves the normal low-level interface;
``get_value()`` can still materialise an individual value when required.

Bulk row decoding
-----------------

For ``qt-process`` the public Transformer is a Python facade, because the normal
native Psycopg generator layer assumes a local native ``PGconn``. When the local
``psycopg_c`` helper is available, bulk result decoding uses a Cython helper
which reads the packed data/offset/null buffers directly and constructs rows in a
C loop.

The helper recognises selected exact standard loaders and uses native equivalents
for their hot decode path. Custom loaders and unsupported cases use the
configured Python loader, so user adaptation semantics remain parent-side and
extensible.

This distinction is important: globally replacing the qt-process adapter map with
native C adapters is unsafe because some connection-sensitive native loaders
expect a real native ``PGconn`` for settings such as ``TimeZone`` or
``DateStyle``.

Semantics and threading
-----------------------

The public synchronous API remains synchronous. A call such as
``cursor.execute()`` blocks the calling Python thread just like upstream
Psycopg. Process isolation does not turn a synchronous call made on the GUI
thread into an asynchronous one.

The intended GUI pattern is therefore the normal Qt pattern: make blocking
synchronous database calls from a worker thread if the GUI must remain
responsive.

The standard Psycopg threading rule is preserved: a connection may be shared
between threads, with a separate cursor per thread, and access to the connection
is serialised by Psycopg's existing connection lock.

AsyncConnection
---------------

``AsyncConnection`` uses the same isolated worker. Remote waits run through
``asyncio.to_thread()`` instead of depending on ``add_reader()`` or
``add_writer()`` for the PostgreSQL socket. This allows the backend to work with
the Windows Proactor event loop even though the real libpq socket belongs to the
worker process.

Cancellation
------------

``cancel_safe()`` uses Psycopg's normal cancellation generator with remote cancel
objects. Cancellation currently shares the main transport lock with the query
path. To prevent starvation, the query wait loop notices when cancellation is
pending and stops starting new WAIT requests until the cancelling thread can
acquire the transport.

This mechanism preserves cancellation, but worker-side WAIT batching and
cancellation latency are currently coupled: holding a query WAIT longer reduces
WAIT RPC count while delaying the cancelling thread. A dedicated authenticated
control/cancel transport is a possible future architectural improvement.

Errors and worker failure
-------------------------

Remote exceptions preserve their module/class/message and are reconstructed in
the application process where possible.

Ordinary PostgreSQL statement errors, such as ``UndefinedTable`` or constraint
violations, do not terminate a healthy worker/session. If the underlying libpq
connection becomes ``BAD`` during an operation, the worker returns the
originating response/error and then terminates. The parent marks its transport
broken and subsequent operations fail as a disconnected connection.

Current limitations
-------------------

* One worker process is currently created per PostgreSQL connection.
* Connection startup is substantially more expensive than vanilla Psycopg
  because of worker/QProcess/bootstrap setup. Current optimisation work focuses
  on steady-state operations rather than hiding that structural cost.
* ``Connection.fileno()`` is unavailable because the real PostgreSQL socket
  belongs to another process.
* ``PGconn.trace()`` cannot accept a parent-process file descriptor.
* Cancellation still contends with the main transport lock and is slower than
  vanilla in the current performance checkpoint.
* Large query completion still performs tens of remote WAIT operations before a
  large result is ready, although ``fetchall()`` itself is now close to native
  performance.
* PyInstaller/frozen executable worker bootstrap is not implemented. Development
  currently launches ``sys.executable -m psycopg.pq._qt_process_worker``.
* The high-performance packed bulk decoder currently depends on building the
  local modified ``psycopg_c`` extension; the published ``psycopg-binary`` wheel
  does not contain qpsycopg's extra Cython bulk helper.

Local development on Windows
----------------------------

Create/activate the project venv and install the editable Python package plus Qt
and a released binary worker::

    uv pip install -e ".\psycopg[qt]" "psycopg-binary==3.3.6"

The development ``psycopg`` version is currently ``3.3.7.dev1``. Do not use
``-e ".\psycopg[qt,binary]"`` unless a matching exact development binary package
is actually available.

To reproduce the current C bulk decoder, build the local ``psycopg_c`` extension.
A PostgreSQL Windows binary/SDK tree can be used without installing a server
service. Point ``PSYCOPG_PG_ROOT`` to the directory containing ``bin``,
``include`` and ``lib``::

    $env:PSYCOPG_PG_ROOT = 'D:\Tools\PostgreSQL\18\pgsql'
    uv pip install -e .\psycopg_c

The build needs PostgreSQL headers and ``libpq.lib``. At runtime Windows must also
be able to load ``libpq.dll`` and its dependencies; the local ``psycopg_c``
package adds ``PSYCOPG_PG_ROOT\bin`` to the DLL search path when configured.

A convenient local PostgreSQL test server is::

    docker run --name qpsycopg-test `
        -e POSTGRES_PASSWORD=postgres `
        -e POSTGRES_DB=postgres `
        -p 5432:5432 `
        -d postgres:18

The development tools default to::

    postgresql://postgres:postgres@127.0.0.1:5432/postgres

Testing
-------

Run the semantic smoke suite::

    .\tools\test.ps1 smoke

The smoke suite covers basic adaptation, multiple result sets, COPY result
boundaries, SQL-error survival, shared connection/per-thread cursors,
cancellation, unsupported parent-process fd behaviour, fatal backend termination
and ``AsyncConnection``.

Run a full repeated benchmark checkpoint::

    .\tools\test.ps1 all -Tag optimizedNN

The runner alternates vanilla and qt-process order, repeats the benchmark and Qt
responsiveness tests, writes raw JSON plus ``summary.json`` and ``manifest.json``,
and creates a timestamped ZIP under ``test-results``.

For lower-level investigation use::

    python tools/profile_qt_process.py select
    python tools/profile_qt_process.py fetch
    python tools/profile_qt_process.py decode

The profiler reports which low-level calls actually cross the process boundary,
PackedValues layout sizes and decode phases.

Current status
--------------

The ``optimized14`` checkpoint moved bulk result decoding from the earlier
multi-times-slower behaviour to roughly native performance and reduced result
RSS through packed storage. The remaining major steady-state work is concentrated
in query completion/``execute`` and cancellation rather than ``fetchall()``.

See :doc:`qt-process-performance` for exact current benchmark numbers and
experiments already rejected, and :doc:`qt-process-project-map` for the source
map and debugging routes.
