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

Current checkpoint: optimized14
-------------------------------

The latest full benchmark checkpoint is ``optimized14``:

* branch: ``feature/qt-process-pq``
* code commit: ``efc8400e2505889f44d24e8c0b9822dba9964789``
* timestamp: ``2026-11-01T22:15:55+02:00``
* worker implementation: ``binary``
* repetitions: ``3``
* SELECT iterations per run: ``500``
* bulk result: ``100000`` rows, three columns
* threading test: ``8`` threads sharing one connection with separate cursors
* smoke suite: PASS

The summary medians were:

========================  ============  ============  =============
Metric                    vanilla       qt-process    summary ratio
========================  ============  ============  =============
connect                    34.84 ms      636.54 ms     18.27x
SELECT 1 median            0.967 ms      1.727 ms      1.79x
SELECT 1 p95               1.383 ms      2.082 ms      1.51x
100k execute               0.170 s       0.431 s       2.53x
100k fetchall              0.187 s       0.207 s       1.11x
100k total                 0.344 s       0.638 s       1.85x
8 shared-connection threads 14.57 ms     24.04 ms      1.65x
cancel call                6.36 ms       13.55 ms      2.13x
RSS delta for 100k result  39.37 MiB     34.75 MiB     0.88x
Qt timer median            9.934 ms      9.940 ms      ~=1x
Qt timer p95               10.459 ms     10.481 ms     ~=1x
========================  ============  ============  =============

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

The remaining large-result bottleneck is now query completion/``execute()``.
The current path still crosses the process boundary roughly 80--100 times for a
100k-row query, mostly through ``WAIT`` operations, before the packed result is
available to the parent.

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

The likely architectural solution for strict low cancellation latency is a
separate authenticated control/cancel channel so cancellation does not contend
with the main query-response transport lock.

Experiments already rejected
----------------------------

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
    result layout, and decoder phases. Use it to identify where time is spent;
    do not treat its deliberately repeated decode phases as end-to-end timings.

Next performance work
---------------------

The next investigation should decompose the large-query ``execute`` path into:

#. PostgreSQL/libpq wait time;
#. worker ``consume_input`` / busy polling;
#. result snapshot packing;
#. pickle serialization;
#. socket transfer;
#. parent unpickle/restore.

The immediate goal is to determine how much of the remaining execute overhead is
caused by repeated WAIT round-trips versus snapshot/transport work. Only then
choose between adaptive WAIT batching, a dedicated control/cancel channel, or a
further snapshot/transport optimisation.
