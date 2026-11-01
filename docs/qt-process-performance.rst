Qt process performance notes
============================

This document is a development checkpoint for the experimental ``qt-process``
backend. It records the acceptance target, the latest reproducible benchmark,
important optimisation decisions, and experiments which should not be repeated
without a new reason.

Performance target
------------------

For frequent database operations the target is approximately ``1x`` the normal
Psycopg binary implementation. Process startup/connection creation and final
worker shutdown are structural exceptions: the backend intentionally creates a
``QProcess`` per PostgreSQL connection.

The performance requirement is not satisfied by a backend which is merely
"fast enough" for a GUI. In particular, stable ``2x`` or ``3x`` regressions on
normal query/fetch paths are considered unresolved.

Current full checkpoint: optimized14
------------------------------------

The latest full end-to-end benchmark checkpoint is ``optimized14``:

* branch: ``feature/qt-process-pq``
* runtime code commit: ``efc8400e2505889f44d24e8c0b9822dba9964789``
* timestamp: ``2026-11-01T22:15:55+02:00``
* worker implementation: ``binary``
* repetitions: ``3``
* SELECT iterations per run: ``500``
* bulk result: ``100000`` rows, three columns
* threading test: ``8`` threads sharing one connection with separate cursors
* smoke suite: PASS

The summary medians were:

===========================  ============  ============  =============
Metric                       vanilla       qt-process    summary ratio
===========================  ============  ============  =============
connect                       34.84 ms      636.54 ms     18.27x
SELECT 1 median               0.967 ms      1.727 ms      1.79x
SELECT 1 p95                  1.383 ms      2.082 ms      1.51x
100k execute                  0.170 s       0.431 s       2.53x
100k fetchall                 0.187 s       0.207 s       1.11x
100k total                    0.344 s       0.638 s       1.85x
8 shared-connection threads   14.57 ms      24.04 ms      1.65x
cancel call                   6.36 ms       13.55 ms      2.13x
RSS delta for 100k result     39.37 MiB     34.75 MiB     0.88x
Qt timer median               9.934 ms      9.940 ms      ~=1x
Qt timer p95                  10.459 ms     10.481 ms     ~=1x
===========================  ============  ============  =============

The runs were noisy, especially for bulk query execution. Paired ratios are
therefore useful in addition to ratios of the summary medians. The median paired
ratios were approximately:

* SELECT median: ``1.76x``
* SELECT p95: ``1.57x``
* execute: ``1.95x``
* fetchall: ``0.83x``
* total bulk operation: ``1.34x``
* threads: ``1.70x``
* cancel: ``2.18x``
* RSS delta: ``0.88x``

The three qt-process bulk executions used ``100``, ``91`` and ``82`` remote
requests respectively. ``fetchall()`` used zero remote requests.

Latest phase-profile checkpoint
-------------------------------

After ``optimized14``, commit
``4cef1bf702e4fc4f4c51f2c4a7e8388700a558d8`` added phase instrumentation to
``tools/profile_qt_process.py`` and the qt-process transport/worker profiling
hooks. This is a diagnostic checkpoint, not a replacement for the full
``optimized14`` benchmark baseline.

A representative 100k x 3 ``fetch`` profile with the canonical ``binary`` worker
reported::

    elapsed_seconds                         0.26264
    remote_requests                              75
    WAIT remote requests                         71
    WAIT remote seconds                     0.11438

    worker libpq_wait_seconds                0.06582
    worker is_busy_seconds                   0.01541
    worker consume_input_seconds             0.00580
    worker snapshot_values_seconds           0.10893
    worker snapshot_total_seconds            0.11042
    worker response_pickle_seconds           0.00561
    worker response_socket_send_seconds      0.00693

    parent response_payload_recv_seconds     0.00614
    parent response_unpickle_seconds         0.00628
    parent restore_seconds                   0.00003

The dominant directly measured worker CPU phase is therefore currently
``_snapshotResult()`` value packing: roughly ``0.109 s`` for 300,000 cells. It
is larger than the measured libpq wait time in that representative run and is
orders of magnitude larger than pickle, socket transfer, unpickle or restore.

``parent_response_header_wait_seconds`` was about ``0.224 s`` in the same run.
Do **not** add that value to the worker phase timings: it is parent wall-clock
waiting which overlaps worker libpq wait, snapshot construction, serialization
and other worker activity. The profiler contains overlapping views of the same
execution interval, not an additive critical-path accounting table.

This phase profile changes the immediate optimisation priority. The first next
runtime experiment should be a native worker-side ``PGresult -> PackedValues``
packer which removes the Python per-cell loop in ``_snapshotResult()``. Only
after measuring that change should WAIT/cancel architecture become the next
performance target.

Interpretation
--------------

``fetchall()`` is no longer the primary bottleneck. The combination of packed
result storage and the qt-process C bulk decoder reduced the previous stable
~``3.4x`` fetch/decode regression to practical parity with the native binary
backend. A direct decoder microbenchmark measured:

* native binary ``Transformer.load_rows()``: about ``0.1417 s`` for 100k rows;
* qt-process packed bulk ``load_rows()``: about ``0.1287 s`` in the successful
  specialised-loader run;
* packed metadata traversal alone: about ``0.00052 s`` for 300k cells.

The remaining large-result bottleneck is before ``fetchall()``. Phase profiling
has now separated two substantial components:

* worker snapshot construction: about ``0.109 s`` in the representative run;
* repeated WAIT activity: about ``0.114 s`` parent-side remote WAIT wall time,
  with about ``0.066 s`` measured inside worker libpq waits.

Because snapshot packing is a concrete CPU cost with no cancellation trade-off,
it is the next target. Do not skip directly to more aggressive WAIT batching
merely because the request count is visually large.

PackedValues
------------

``ResultSnapshot`` no longer serialises hundreds of thousands of independent
``bytes`` objects. ``PackedValues`` carries:

* one contiguous data buffer;
* an offset table;
* a NULL bitmap;
* the cell count.

For the 100k x 3 benchmark the typical payload is approximately:

* data: ``5.47 MiB``;
* offsets: ``2.40 MiB``;
* NULL bitmap: ``37.5 KiB``.

This representation improved worker snapshot/pickle/transfer time and reduced
combined parent+worker RSS. Do not revert to ``tuple[bytes | None, ...]`` merely
to simplify the decoder.

The current remaining snapshot cost is not evidence that ``PackedValues`` is
wrong. The expensive part is constructing it in Python by visiting every native
``PGresult`` cell. The intended next optimisation is to preserve the same wire
layout while moving that packing loop into native code.

Native row decoding
-------------------

The qt-process ``Transformer`` intentionally remains a Python facade because
normal C Psycopg generator code expects a local native ``PGconn``. Result rows,
however, have a separate Cython bulk helper in ``psycopg_c``.

A critical detail is that the normal qt-process adapter map contains Python
loader instances. Calling those loaders from a C loop still costs a Python call
per cell and measured roughly ``0.54 s`` for the 100k-row benchmark. The current
bulk helper therefore recognises exact standard loaders and uses detached native
C equivalents for the hot decode path. Custom loaders and unsupported cases must
fall back to the configured Python loader to preserve adaptation semantics.

Do not globally enable the normal ``psycopg_c`` adapter optimiser for
``qt-process``. Some native datetime loaders expect a native C ``PGconn`` for
``DateStyle``/``TimeZone`` state, whereas the application process owns a remote
``PGconn`` proxy.

Small-query RPC reduction
-------------------------

The normal warmed ``SELECT 1`` path has been reduced to one process-boundary
request by two mechanisms:

* parent-side connection/query state caching avoids repeated ``status``,
  ``transaction_status`` and ``parameter_status`` RPCs;
* lazy send coalescing defers ``send_query*`` and merges it with
  ``qpsycopg_execute_pending``.

The lazy-send implementation currently lives in ``pq/_qt_process_lazy.py`` and
monkey-patches ``_Transport.request``. This should eventually be integrated into
``_Transport`` after the performance architecture stabilises.

WAIT batching and cancellation
------------------------------

Large query completion originally generated roughly 450 ``WAIT`` RPCs. Worker
side readable-data draining reduced this dramatically. The current drain budget
is ``1 ms``.

Observed trade-off during development:

* no extra drain: about 274 WAITs in a representative profiler run;
* 1 ms drain: about 70--90 WAITs in current runs;
* 10 ms drain: about 14--15 WAITs, but cancellation latency regressed
  substantially because the shared transport lock stayed occupied longer.

Do not tune ``_WAIT_DRAIN_GRACE`` based only on execute time. Every change must
be benchmarked together with cancellation and Qt responsiveness.

More importantly, do not tune WAIT at all as the immediate next experiment while
snapshot packing still costs roughly ``0.1 s``. First remove or substantially
reduce the Python snapshot loop, re-profile, and then decide whether WAIT is the
new dominant remaining cost.

The likely architectural solution for strict low cancellation latency is a
separate authenticated control/cancel channel so cancellation does not contend
with the main query-response transport lock.

Binary versus C worker
----------------------

The canonical performance baseline remains ``QPSYCOPG_WORKER_IMPL=binary``.
Changing the worker to ``c`` is **not** the next performance optimisation.

A diagnostic run with the local C worker, after making PostgreSQL DLLs visible
to the child process, produced approximately::

    binary worker: elapsed 0.26264 s, snapshot_values 0.10893 s, WAIT 0.11438 s
    c worker:      elapsed 0.26758 s, snapshot_values 0.10630 s, WAIT 0.12271 s

The difference is noise-level and did not address the snapshot bottleneck. Both
worker implementations still execute qpsycopg's Python ``_snapshotResult()``
loop.

The C worker is still useful as a **feasibility/A-B harness** when a local
``psycopg_c`` object exposes native ``PGresult`` state that is convenient for a
prototype. A C-only prototype may estimate the best-case speed of direct
``PQgetisnull``/``PQgetlength``/``PQgetvalue`` packing. However, that result is
not by itself a production optimisation for qt-process.

Before investing in a C-only fast path, answer how the same optimisation reaches
the canonical ``binary`` worker. ``psycopg_c.pq.PGresult`` and
``psycopg_binary.pq.PGresult`` are different extension types; do not assume that
a helper which relies on ``psycopg_c`` private/native fields can consume a
``psycopg_binary`` result or that their internal layouts are interchangeable.
A production proposal must therefore identify one of the following:

* a supported/native-accessible path from the canonical binary ``PGresult``;
* a worker-agnostic native interface/capsule/buffer contract usable by both
  implementations;
* or an explicit architectural decision to change the canonical worker, backed
  by end-to-end benchmarks and dependency/deployment trade-offs.

If none of those exists yet, keep a ``worker=c`` packer clearly labelled as a
proof-of-concept and use it only to estimate the performance ceiling. Do not
build production-grade C-worker bootstrap/DLL infrastructure solely to support
that experiment before its transfer path to ``binary`` is understood.

On Windows, a worker import failure for ``psycopg_c.pq`` can surface in the
parent as a later ``listener.accept()`` timeout because the child dies before
connecting back. Treat the worker's original import/DLL error as the root cause,
not the parent timeout as a separate transport regression.

Also note that ``psycopg_c`` is designed to be imported after ``psycopg``. A
standalone ``from psycopg_c import pq`` diagnostic can create misleading import
or circular-import behaviour in this source checkout. Test the pq implementation
through ``PSYCOPG_IMPL=c`` followed by ``import psycopg.pq`` when such a test is
actually required.

Experiments already rejected or deprioritised
---------------------------------------------

Protocol 5 out-of-band pickle buffers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Protocol-5/OOB experiments increased overhead and were rolled back. The current
framing uses one pickle payload; small frames are coalesced with their header and
large frames are sent as header + payload.

Aggressive result prefetch during WAIT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Do not call ``get_result()`` speculatively from the worker WAIT path. Earlier
experiments changed libpq/Psycopg timing semantics and produced incorrect
behaviour around fatal termination, including duplicate transaction activity.
WAIT may consume input, inspect busy state and notifications, but result
boundaries belong to the normal generator/result path.

Python row pre-slicing
~~~~~~~~~~~~~~~~~~~~~~

Pre-slicing the flat values into Python row tuples did not help. It added Python
allocations and was slower than the direct row path. With ``PackedValues`` it is
especially misleading because slicing intentionally materialises the packed
cells back into individual ``bytes`` objects.

Global native adapter substitution
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Do not replace the qt-process global adapter map with the native C adapter map.
The pq generator layer must remain Python/remote-aware, and connection-sensitive
native loaders may assume a native ``PGconn``.

Worker implementation switching
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Do not switch ``binary -> c`` as a generic attempt to improve the current bulk
execute time. The measured C-worker run did not improve the target path and can
introduce Windows DLL/bootstrap noise which distracts from the measured qpsycopg
costs.

A deliberate ``c`` run is acceptable when it answers a narrow feasibility
question, such as the speed ceiling of a direct native packer. In that case,
keep the experiment isolated and temporary. Do not promote a ``c``-only result
to the production plan until a binary-compatible integration route is known.

Benchmark tools
---------------

``tools/test.ps1``
    Main reproducible runner. Modes: smoke, bench, qt, gui, all. It alternates
    vanilla/qt-process order, runs repeated benchmarks, writes JSON plus a
    ``summary.json`` and ``manifest.json``, and archives the run as a ZIP.

``tools/bench_qt_process.py``
    Measures connection startup, SELECT latency, bulk execute/fetchall, RSS,
    shared-connection threads, SQL-error survival, and cancellation.

``tools/bench_qt_responsiveness.py``
    Measures Qt timer behaviour. The intended synchronous usage is a worker
    thread; ``--caller main`` is a control showing that a synchronous call blocks
    whichever thread invokes it.

``tools/profile_qt_process.py``
    Low-level profiler for process-boundary requests, large execute behaviour,
    parent/worker phase timings, result layout, and decoder phases. Use it to
    identify where time is spent; do not treat overlapping parent/worker timing
    fields as additive, and do not treat its deliberately repeated decode phases
    as end-to-end timings.

Next performance work -- follow in this order
---------------------------------------------

The large-query phase decomposition is now complete enough to choose the next
experiment. Do **not** repeat that investigation or switch worker implementation
before acting on its result.

#. Before implementing a production native packer, inspect the canonical
   ``binary`` worker path and identify how native ``PGresult`` data can be
   accessed without depending on ``psycopg_c``-private object layout. Record the
   intended binary-compatible integration route.
#. If useful, implement a small ``worker=c`` Cython packer only as a controlled
   feasibility experiment to measure the attainable packing speed. Do not spend
   time productising C-worker bootstrap/DLL handling for this purpose, and do
   not treat the experiment as complete until the transfer path to ``binary`` is
   clear.
#. Implement/prototype the production worker-side packer which converts native
   ``PGresult`` cell storage directly into the existing ``PackedValues`` layout
   (data buffer + offsets + NULL bitmap), avoiding 300,000 Python
   ``result.get_value()``/``bytearray`` iterations for the 100k x 3 benchmark.
#. Preserve result metadata, NULL semantics, formats and current
   ``ResultSnapshot``/``PackedValues`` wire compatibility. This is a packing
   optimisation, not a semantic redesign.
#. Run smoke before benchmarking. Pay particular attention to multiple result
   sets, COPY result boundaries, SQL errors and fatal BAD-state handling because
   snapshot/result ownership is correctness-sensitive.
#. Re-run ``python tools/profile_qt_process.py fetch`` with the canonical
   ``binary`` worker and compare ``snapshot_values_seconds``, total elapsed,
   remote WAIT count/time, pickle/send/recv/unpickle and payload sizes against
   the ``4cef1bf`` phase-profile checkpoint above.
#. Only if snapshot packing is no longer a dominant cost should the next work
   move to WAIT/cancel architecture. If WAIT is then dominant, prefer designing
   a dedicated authenticated cancel/control path before increasing drain
   duration aggressively.
#. After a validated runtime improvement, run a full tagged benchmark
   ``.\tools\test.ps1 all -Tag optimizedNN`` and update this document with the
   new reproducible checkpoint.

Decision rule for future sessions: once profiling identifies a concrete dominant
phase, optimise that phase first unless there is a correctness blocker. A
backend-specific prototype is allowed only when it answers a narrow feasibility
question; before promoting it to the production plan, demonstrate how the same
optimisation applies to the canonical ``binary`` worker or explicitly justify a
change of canonical worker.
