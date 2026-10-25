Qt process backend
==================

``qpsycopg`` adds an experimental ``qt-process`` low-level ``pq`` implementation
for PySide6 applications. The regular Psycopg ``Connection``, ``Cursor``, type
adaptation, row factories, transactions and SQLSTATE error mapping remain in the
application process. Stateful libpq objects live in a child ``QProcess``.

The backend is opt-in::

    set PSYCOPG_IMPL=qt-process

On POSIX shells::

    export PSYCOPG_IMPL=qt-process

The worker implementation can be selected independently::

    set QPSYCOPG_WORKER_IMPL=python

Valid worker implementations are the normal Psycopg low-level implementations
available in the environment: ``c``, ``binary`` and ``python``.

Architecture
------------

::

    application process
      Connection / Cursor / Transformer / rows / errors
                    |
             typed framed IPC
                    |
    ================================= process boundary
                    |
              QProcess worker
                    |
            PGconn / PGresult / libpq
                    |
               PostgreSQL

Query parameters are adapted to PostgreSQL representation before crossing the
process boundary. Result values are copied from ``PGresult`` into one contiguous
raw data block plus an offset table; the application-side ``PGresult`` exposes
``memoryview`` slices to Psycopg's normal loaders. This avoids constructing rows
and Python scalar values in the worker only to serialise them again.

Semantics
---------

The public synchronous API intentionally remains synchronous. A call such as
``cursor.execute()`` blocks the calling Python thread just like upstream Psycopg,
but libpq and the PostgreSQL session are isolated from that process.

The normal Psycopg threading rule is preserved: one connection can be shared
between threads, with a separate cursor per thread; access to a connection is
serialised by the existing Psycopg connection lock.

``AsyncConnection`` uses the same worker and performs remote socket waits outside
the asyncio event-loop thread. Consequently the backend doesn't require
``add_reader()``/``add_writer()`` and can operate with the Windows Proactor event
loop.

If libpq reports the underlying connection as ``BAD`` while handling an
operation, the worker sends the originating exception to the caller and then
terminates. Ordinary PostgreSQL errors (for example ``UndefinedTable`` or
``UniqueViolation``) don't terminate a healthy worker.

Current limitations
-------------------

* ``Connection.fileno()`` isn't available: the real PostgreSQL socket belongs to
  the worker process, so exposing its descriptor in the application process would
  be invalid. The method raises ``NotSupportedError`` instead of returning the
  backend's internal remote-wait handle.
* ``PGconn.trace()`` is not supported because a file descriptor owned by the
  application process isn't valid in the worker process.
* Result transfer currently uses a framed localhost control connection. Large
  result sets should prefer server cursors/streaming or chunked-row APIs. Shared
  memory is a possible future fast path for very large raw result blocks.
* The backend currently starts one worker process per PostgreSQL connection.
* PyInstaller/frozen executable worker bootstrap is not implemented yet; the
  development backend launches ``sys.executable -m psycopg.pq._qt_process_worker``.

Local development
-----------------

From the repository root on Windows with ``uv``::

    uv venv .venv
    uv pip install --python .venv\Scripts\python.exe -e ".\psycopg[qt]"

The ``python`` worker uses the system libpq. Make sure the PostgreSQL ``bin``
directory containing ``libpq.dll`` is available to the process if it cannot be
located automatically.

Run the smoke test::

    set PSYCOPG_IMPL=qt-process
    set QPSYCOPG_WORKER_IMPL=python
    .venv\Scripts\python.exe tools\smoke_qt_process.py "postgresql://user:password@host:5432/database"

For the normal upstream execution path use, for example::

    set PSYCOPG_IMPL=python

and run the same application code.

Benchmarking
------------

Use ``tools/bench_qt_process.py`` in separate processes because ``PSYCOPG_IMPL``
is selected when Psycopg is imported::

    set PSYCOPG_IMPL=python
    .venv\Scripts\python.exe tools\bench_qt_process.py DSN

    set PSYCOPG_IMPL=qt-process
    set QPSYCOPG_WORKER_IMPL=python
    .venv\Scripts\python.exe tools\bench_qt_process.py DSN

Compare connection startup, ``SELECT 1`` latency, bulk fetch throughput,
thread-shared connection behaviour, SQL-error survival and cancellation latency.
Run several rounds after a warm-up and compare medians rather than a single run.
