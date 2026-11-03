Qt process backend
==================

``qpsycopg`` adds experimental Qt-oriented low-level ``pq`` backends while
preserving the normal Psycopg user API. The branch currently carries two explicit
opt-in implementations:

* ``PSYCOPG_IMPL=qt-process`` keeps the stateful libpq session in a reusable
  ``QProcess`` worker;
* ``PSYCOPG_IMPL=qt-thread`` keeps the stateful libpq session in a dedicated
  Python worker thread in the application process.

The worker implementation is selected independently with
``QPSYCOPG_WORKER_IMPL``. Supported values are ``binary``, ``c`` and ``python``;
the worker must never recurse into ``qt-process`` or ``qt-thread``.

For the current implementation handoff, measurements and active optimisation
series see :doc:`qt-process-project-map`, :doc:`qt-process-performance` and
:doc:`qt-process-optimization-log`.

Public semantics
----------------

Both Qt-oriented backends preserve the normal high-level Psycopg model:

* ``Connection`` / ``AsyncConnection`` and cursors stay in the application
  process;
* adaptation, row factories, SQLSTATE mapping and user callbacks stay in the
  application process;
* the stateful libpq ``PGconn`` belongs only to the worker;
* synchronous calls remain synchronous and block the calling thread;
* ordinary SQL errors must not kill a healthy connection;
* fatal ``ConnStatus.BAD`` handling still terminates/marks a broken session;
* the existing Psycopg connection lock remains authoritative when a connection
  is shared between caller threads.

Moving libpq to a worker does not make the synchronous API asynchronous. A Qt GUI
must still call synchronous Psycopg from a worker thread, or use the asynchronous
API.

qt-process architecture
-----------------------

The preferred Windows data path is::

    application process
      |
      +-- Connection / Cursor / adaptation
      |
      +-- synthetic PGconn / PGresult facade
      |
      +-- deferred send + compact fast execute
      |
      +-- raw Windows named pipe
      |     +-- native parent frame exchange when available
      |
      +================ process boundary ================+
                                                        |
                                               reusable QProcess worker
                                                        |
                                               psycopg c/binary/python pq
                                                        |
                                                      libpq
                                                        |
                                                   PostgreSQL

The main Windows transport defaults to ``QPSYCOPG_IPC=pipe``. TCP remains a
fallback and uses the Windows loopback fast path where available. Non-Windows
currently uses TCP.

The worker process and the PostgreSQL session have different lifetimes. Closing a
Psycopg connection closes the real ``PGconn``; a healthy worker process may be
recycled for a later, unrelated PostgreSQL session. A released parent ``PGconn``
proxy cannot operate on a later session hosted by the same worker.

The current process hot path retains the measured wins from earlier work:

* deferred ``send_query*`` + one-request fast execute;
* compact/marshal framing;
* raw Windows named-pipe transport;
* native parent-side named-pipe frame exchange;
* warmed worker-process reuse;
* cached stable connection state where safe;
* native result value/metadata packing;
* native packed bulk decoding where supported.

Worker-side native frame receive and native socket wait remain experimental only.
Controlled tests found native receive neutral and native wait slower, so the
production default remains Python worker receive plus Python ``select.select()``.

Result transfer
---------------

Result adaptation remains parent-side. Large results cross the worker boundary as
raw PostgreSQL values and metadata using ``PackedValues`` rather than one Python
``bytes`` allocation per cell.

The local Cython ``PGresult`` code can pack values/metadata natively, and the
parent transformer can decode supported packed values natively while preserving
normal Psycopg adapter semantics and Python fallbacks.

Do not move ``get_result()`` speculatively into the WAIT path. Earlier attempts
changed result-boundary timing and broke fatal/COPY semantics.

Cancellation
------------

Cancellation is deliberately isolated from the normal query transport. The real
worker ``PGconn`` creates a ``PGcancelConn`` and a separate control thread owns
that cancel object. Parent and worker communicate over an authenticated localhost
TCP control socket.

For ``qt-process`` the cancellation control channel is opened lazily before the
first normal send rather than for every connect/close pair. A cancel-before-query
fallback opens it on demand.

``qt-thread`` now applies the same lifecycle policy through
``psycopg.pq._qt_thread_lazy``: connection creation does not eagerly construct the
cancel-control infrastructure; the first normal send opens it immediately before
the query is handed to the worker thread, and ``cancel_safe()`` retains an
on-demand fallback. This is the current ``optimized01`` change and is awaiting
measurement against ``baseline-v3``.

qt-thread architecture and status
---------------------------------

``qt-thread`` reuses the synthetic pq facade/cache/snapshot model but replaces the
process data-plane boundary with ``queue.SimpleQueue`` and a dedicated worker
thread::

    Psycopg Connection / Cursor
            |
    synthetic PGconn / PGresult facade
            |
    SimpleQueue request / response handoff
            |
    dedicated qpsycopg worker thread
            |
    psycopg c/binary/python pq + libpq

The complete standalone semantic smoke now passes. The gate covers basic
adaptation, packed NULL/empty values, multiple result sets, COPY boundaries, SQL
error survival, shared connection/per-thread cursors, cancellation, unsupported
caller file descriptors, fatal backend termination and ``AsyncConnection``.

The native remote packed decoder is enabled for ``qt-thread`` as well as
``qt-process``. This removed the earlier false bulk regression caused by falling
back to the pure-Python transformer.

The canonical ``baseline-v3`` benchmark is now the before-state for numbered
optimisations. Representative ``qt-thread / upstream`` ratios at that checkpoint
were approximately:

* warm connect median ``1.409x``;
* ``SELECT 1`` median ``1.273x``;
* short-query CPU ``2.000x``;
* context switches/query ``3.032x``;
* 100k-row total ``1.027x``;
* process-tree RSS ``1.320x``;
* private memory ``1.377x``;
* Qt timer p95 ``0.982x`` and p99 ``1.008x``.

Qt p95/p99 are therefore not a current optimisation target. Bulk decoding also
remains deprioritised while it stays near upstream.

Benchmark methodology
---------------------

The current optimisation series uses ``tools/benchmark_qpsycopg.ps1`` rather
than treating the historical vanilla-baseline harness as the primary acceptance
test.

The comparison revision is the merge-base with upstream Psycopg. Upstream and
fork are built into isolated non-editable benchmark environments from the same
CPython and the same local PostgreSQL/libpq installation.

Every fork mode has an immediately adjacent upstream run and pair orientation is
rotated. The primary ratio is the median of per-pair ``fork / upstream`` ratios.
The report also records MAD/range, host-state telemetry and process-tree resource
usage. Historical upstream results are used only to show drift, not as the sole
ratio denominator.

The canonical before-state is ``baseline-v3``. Do not compare numbered runtime
optimisations against the older ``initial`` or ``baseline-v2`` checkpoints; those
used earlier benchmark definitions.

Local development on Windows
----------------------------

Development configuration is normally stored in a root ``.env`` (ignored by
Git) following ``.env.example``. Typical values are::

    PSYCOPG_IMPL=qt-thread
    QPSYCOPG_WORKER_IMPL=c
    PSYCOPG_PG_ROOT=C:\path\to\PostgreSQL\18\pgsql
    QPSYCOPG_DSN=postgresql://user:password@host:5432/database

The tooling loader prepares the Windows PostgreSQL DLL directory from
``PSYCOPG_PG_ROOT``. The current local development packages are branch-local dev
packages; do not pin the old ``psycopg-binary==3.3.6`` snippet from earlier
handoffs.

Useful current validation commands are::

    python tools\smoke_qt_thread.py
    .\tools\validate_qt_thread.ps1
    .\tools\benchmark_qpsycopg.ps1 -Checkpoint <name>

The benchmark runner creates isolated upstream/fork venvs, builds both against
the same local libpq, reruns upstream beside each fork mode and emits
``comparison.json``, ``comparison.md`` and a ZIP archive.

Current limitations
-------------------

* Both Qt-oriented implementations remain experimental and explicit opt-in.
* ``qt-process`` has materially higher process-tree memory, handles, threads and
  process count because process isolation is structural.
* ``Connection.fileno()`` is unavailable for the remote-worker connection model.
* Parent-process file descriptors cannot be handed to worker libpq APIs such as
  tracing.
* Frozen/PyInstaller worker bootstrap is not implemented.
* Synchronous calls still block whichever thread invokes them.

Current status
--------------

The process implementation is the retained measured reference while the branch
optimises ``qt-thread``. The thread backend is smoke-clean and its bulk path is
near upstream. The current accepted optimisation sequence is recorded in
:doc:`qt-process-optimization-log`.

At current HEAD ``optimized01`` (lazy qt-thread cancellation setup) is implemented
but not yet accepted: it must be measured with the unchanged v3 benchmark against
``baseline-v3`` before another runtime optimisation is stacked on top of it.
