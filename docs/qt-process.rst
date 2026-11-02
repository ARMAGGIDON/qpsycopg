Qt process backend
==================

``qpsycopg`` adds experimental Qt-oriented low-level ``pq`` backends while
preserving the normal Psycopg user API. The primary implementation remains
``qt-process``: the application process owns ``Connection``, ``Cursor``,
adaptation, row factories and SQLSTATE mapping, while the stateful libpq session
lives in a worker ``QProcess``.

The backend is explicit opt-in and is never selected automatically::

    $env:PSYCOPG_IMPL = 'qt-process'

The worker implementation is selected independently::

    $env:QPSYCOPG_WORKER_IMPL = 'binary'

Valid worker implementations are ``python``, ``c`` and ``binary``. The canonical
performance target is ``binary`` and the worker must never recurse into
``qt-process``.

For the implementation map, current measurements and the experimental
``qt-thread`` redesign see :doc:`qt-process-project-map` and
:doc:`qt-process-performance`.

Architecture
------------

The current Windows hot path is::

    application process

      psycopg.connect() / Cursor.execute()
          |
      normal Psycopg Connection/Cursor/adaptation
          |
      synthetic qt-process PGconn/PGresult facade
          |
      deferred send + compact fast execute
          |
      raw Windows named pipe
          |
      native parent frame exchange when available

    ========================= process boundary =========================

      reusable QProcess worker
          |
      normal Psycopg pq implementation (binary/c/python)
          |
      libpq PGconn / PGresult
          |
      PostgreSQL

On Windows the main data transport defaults to a raw byte-stream named pipe.
``QPSYCOPG_IPC=tcp`` selects the TCP fallback, which enables the Windows loopback
fast path when available. Non-Windows builds currently use TCP.

Cancellation deliberately uses a separate authenticated localhost TCP control
channel. It is independent from the main data transport and from its lock.

Process and session lifecycle
-----------------------------

The worker process and the PostgreSQL session no longer have identical
lifetimes. A small worker pool currently keeps at most one idle warmed
``QProcess``. Closing a connection still closes the real PostgreSQL ``PGconn``;
only the Python/Qt worker process can be recycled for the next session.

This distinction removed most of the earlier structural connect penalty without
sharing PostgreSQL sessions between unrelated Psycopg ``Connection`` objects.
Old ``PGconn`` proxies are invalid after release and cannot control a later
session hosted by the recycled worker.

The worker ``QProcess`` is owned by a dedicated Python thread so the ``QProcess``
object is not manipulated across Qt object affinity boundaries. Frozen/PyInstaller
worker bootstrap is still not implemented.

Main IPC transport
------------------

The protocol remains framed and authenticated. A random 256-bit token is checked
before ordinary protocol frames are accepted.

Windows named-pipe mode uses ``CreateNamedPipeW`` / ``CreateFileW`` underneath a
small byte-stream adapter. The Python fallback uses read-ahead so a short response
can usually provide the frame header and payload from one kernel read while
preserving any bytes read ahead.

When the modified local ``psycopg_c`` helper is available, the common parent-side
fast request/response exchange uses a Cython helper which performs the named-pipe
``WriteFile``/``ReadFile`` loop with the GIL released. The Python framing path is
retained as a fallback. This native parent exchange produced a measurable
same-run reduction in short-query latency and CPU, so it is part of the current
preferred Windows path.

Two additional native helpers exist for experiments:

* native worker-side named-pipe frame receive;
* native Winsock ``select()`` for the worker libpq socket.

They are not production defaults. Controlled A/B measurements showed no stable
benefit for native worker receive and a regression for native socket wait. The
current default therefore keeps worker frame receive and libpq wait orchestration
in Python.

Query fast path
---------------

A warmed normal query should cross the process boundary once in the common case.
The parent defers ``send_query*`` briefly and merges it with
``qpsycopg_execute_pending``. The compact fast frame avoids the generic request
shape where possible.

The worker then performs the real libpq send/flush/wait/consume/result sequence.
If the query completes within the bounded compound path, result/state snapshots
are returned with that response. Otherwise the normal Psycopg generator resumes
through remote WAIT cycles.

Large-query WAIT processing uses a ``10 ms`` bounded worker-side drain. The old
``1 ms`` drain produced many more parent/worker round trips. The larger drain is
safe because cancellation no longer waits for the main query transport lock.

Do not move ``get_result()`` into the WAIT path speculatively. Earlier attempts
changed result-boundary timing and broke fatal-connection semantics.

Result transfer
---------------

Results cross the process boundary as raw PostgreSQL values and metadata.
Application-side adaptation remains in the parent process.

Large results use ``PackedValues`` instead of one Python ``bytes`` object per
cell. The representation contains:

* one contiguous data buffer;
* an offset table;
* a NULL bitmap;
* the cell count;
* result/column metadata in the surrounding snapshot.

The shared Cython ``PGresult`` source contains the native value packer, so a
locally generated ``psycopg_binary`` package can pack values directly from
``PGresult`` without a Python per-cell ``get_value()`` loop. A Python fallback is
kept for workers without that capability.

The worker pool also capability-detects native result metadata packing. Parent
bulk decoding can use the modified local ``psycopg_c`` helper to read packed
buffers directly while keeping Python fallback for unsupported or custom
loaders.

Do not globally replace the qt-process adapter map with the native C adapter map.
Connection-sensitive native loaders may assume that the application process owns
a real native ``PGconn``.

Cancellation
------------

``cancel_safe()`` is isolated from the main query transport.

The worker main thread creates ``PGcancelConn`` from the real ``PGconn``. A
separate control thread then owns that cancel object and serves authenticated
cancel requests on its own localhost TCP socket. Repeated cancellation reuses the
cancel object through ``reset()``.

The control channel is opened lazily before the first normal send instead of for
every connect/close pair. A cancel-before-first-query fallback opens it on demand.
This keeps warmed worker reuse cheap while preserving cancellation semantics.

Qt and threading semantics
--------------------------

The public synchronous API remains synchronous. Process or thread isolation does
not make ``cursor.execute()`` asynchronous: the thread which calls a synchronous
Psycopg method remains blocked until that call completes.

Measured Qt architecture gates confirm the intended usage:

* when synchronous DB work is called from a worker thread, direct Psycopg,
  dedicated-thread execution and ``qt-process`` show similar Qt timer
  median/p95 behaviour;
* when the same synchronous work is called from the Qt main thread, all three
  designs block the event loop for the duration of the query.

Therefore a responsive GUI must still use a worker thread or the asynchronous
Psycopg API. Moving libpq into a process is not a replacement for correct GUI
threading.

``AsyncConnection`` continues to use the same isolated worker. Remote waits are
bridged with ``asyncio.to_thread()`` so Windows Proactor loops do not need direct
access to the worker-owned libpq socket.

Experimental qt-thread backend
------------------------------

The branch also contains a development proof of concept selected with::

    $env:PSYCOPG_IMPL = 'qt-thread'

It reuses the qt-process proxy/cache/snapshot model but replaces the process
boundary with ``SimpleQueue`` request/response exchange to a dedicated Python
thread::

    Psycopg Connection/Cursor
            |
    synthetic PGconn/PGresult facade
            |
    SimpleQueue<Request/Response>
            |
    dedicated qpsycopg worker thread
            |
    psycopg_binary.pq / libpq

This experiment was motivated by an architecture benchmark where a dedicated
thread was much closer to direct Psycopg than the process-backed design for short
queries. It is not yet a validated replacement for ``qt-process``.

At the current branch HEAD the full ``qt-thread`` smoke suite has not passed yet.
Import/generator-layer and mixed worker-backend namespace problems were fixed,
but the latest run still stops during the initial connection path. Treat
``qt-thread`` as development-only until its semantic smoke suite passes.

Resource cost
-------------

Do not compare memory using only the application-process RSS. ``qt-process``
intentionally moves work and memory into another process.

The current benchmark harness therefore measures the whole process tree where
possible: CPU, context switches, Working Set/RSS, Private Bytes, handles, thread
count, process count and I/O. These measurements show that process isolation has
a real resource cost even when the application-process RSS alone looks small.

The architecture benchmark also showed that the process design and a dedicated
thread can have a similar number of context switches per short query while the
process design still costs substantially more CPU and latency. The remaining
fixed cost is therefore not explained simply by the count of context switches;
serialization/framing, kernel IPC and a second Python runtime are part of the
boundary cost.

Current limitations
-------------------

* ``qt-process`` remains experimental and explicit opt-in.
* Windows named pipe is the preferred main transport; other platforms use TCP.
* The best Windows hot path currently benefits from the modified local
  ``psycopg_c`` native IPC helper. A Python fallback exists but is slower.
* The worker is still a Python interpreter; the architecture work has shown that
  this process boundary is now the dominant remaining short-query penalty.
* ``Connection.fileno()`` is unavailable because the real PostgreSQL socket
  belongs to the worker process.
* ``PGconn.trace()`` cannot safely consume a parent-process file descriptor.
* Synchronous calls still block the caller, including the Qt GUI thread if used
  incorrectly.
* Frozen executable worker bootstrap is not implemented.
* ``qt-thread`` exists only as a proof of concept and is not smoke-clean yet.

Local development on Windows
----------------------------

Install the editable Python package, Qt and a binary worker::

    uv pip install -e ".\psycopg[qt]" "psycopg-binary==3.3.6"

The modified local ``psycopg_c`` extension is required for the current native
packed decoder and native named-pipe helpers. Point ``PSYCOPG_PG_ROOT`` to a
PostgreSQL SDK/binary tree containing ``bin``, ``include`` and ``lib`` and build::

    $env:PSYCOPG_PG_ROOT = 'D:\Tools\PostgreSQL\18\pgsql'
    uv pip install -e .\psycopg_c

The C extension build needs PostgreSQL headers and ``libpq.lib``. At runtime
``libpq.dll`` and its dependencies must also be loadable.

Testing
-------

The normal semantic gate is::

    .\tools\test.ps1 smoke

The standard repeated benchmark uses the current platform default IPC and reads
``QPSYCOPG_DSN`` when no explicit DSN is supplied::

    .\tools\test.ps1 bench

A complete qt-process package of smoke, SQL benchmark, Qt responsiveness and GUI
blocking control is::

    .\tools\test.ps1 all

The harness runs repeated qpsycopg measurements but only one current vanilla
sentinel. ``tools/vanilla_baseline.py`` aggregates compatible historical vanilla
runs using robust median/MAD statistics, avoiding repeated full vanilla runs when
the workload has not changed.

Targeted tools used by the current architecture work include::

    .\tools\validate_transport.ps1
    .\tools\validate_native_ipc.ps1
    .\tools\validate_worker_native.ps1
    .\tools\validate_worker_architecture.ps1
    .\tools\validate_worker_qt_cancel.ps1
    .\tools\validate_qt_thread.ps1

and lower-level profilers/benchmarks such as ``profile_cpu.ps1``,
``profile_worker_hot_path.ps1``, ``bench_ipc_transport.py`` and
``bench_native_ipc.ps1``.

Current status
--------------

The earlier major bottlenecks are solved or substantially reduced:

* native ``PGresult -> PackedValues`` packing removed the old ~109 ms Python
  per-cell snapshot bottleneck;
* a dedicated cancellation channel allowed the WAIT drain to grow to ``10 ms``
  without restoring the old cancel latency regression;
* worker reuse removed most of the original connection-startup penalty;
* compact/marshal fast frames, raw Windows named pipes and native parent frame
  exchange reduced short-query fixed cost further.

The remaining architectural question is no longer "which Python micro-cache is
missing?". Controlled benchmarks show that a dedicated thread can stay much
closer to direct Psycopg than a separate process while preserving the relevant Qt
and cancellation behaviour. The current next step is therefore to validate the
real ``qt-thread`` backend semantically before deciding whether it should replace
or complement the process-backed data plane.
