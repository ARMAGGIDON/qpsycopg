Qt process performance notes
============================

This document records the current performance state of the experimental
``qt-process`` backend, the measurements which led to the present architecture,
and the next optimisation targets. The canonical worker for performance work is
``QPSYCOPG_WORKER_IMPL=binary``.

Performance target
------------------

For frequent database operations the target is approximately ``1x`` the normal
Psycopg binary implementation. Connection startup and final worker shutdown are
structural exceptions because one PostgreSQL connection intentionally owns one
``QProcess`` worker.

Stable ``2x`` or ``3x`` regressions on normal warmed query/fetch paths remain
unresolved even if they are acceptable for a GUI workload.

Current full checkpoint: optimized16
------------------------------------

The latest full end-to-end benchmark checkpoint is ``optimized16``:

* branch: ``feature/qt-process-pq``
* measured runtime code commit: ``e24e4169b000ef052f07b90924c45cd625f96791``
* timestamp: ``2026-11-02T00:44:32+02:00``
* worker implementation: ``binary``
* repetitions: ``3``
* SELECT iterations per run: ``500``
* bulk result: ``100000`` rows, three columns
* threading test: ``8`` threads sharing one connection with separate cursors
* smoke suite: PASS

Summary medians:

===========================  ============  ============  =============
Metric                       vanilla       qt-process    summary ratio
===========================  ============  ============  =============
connect                       34.94 ms      481.94 ms     13.79x
SELECT 1 median               0.754 ms      1.585 ms      2.10x
SELECT 1 p95                  1.358 ms      2.123 ms      1.56x
100k execute                  0.151 s       0.200 s       1.32x
100k fetchall                 0.156 s       0.153 s       0.98x
100k total                    0.310 s       0.352 s       1.14x
8 shared-connection threads   14.32 ms      23.89 ms      1.67x
cancel call                   5.88 ms       6.12 ms       1.04x
RSS delta for 100k result     39.39 MiB     34.81 MiB     0.88x
Qt timer median               9.962 ms      10.004 ms     ~=1x
Qt timer p95                  10.517 ms     10.597 ms     ~=1x
===========================  ============  ============  =============

The run-to-run spread is material, especially for bulk execute and cancellation.
Median paired ratios for the three runs were approximately:

* connect: ``14.18x``;
* SELECT median: ``1.88x``;
* SELECT p95: ``1.50x``;
* 100k execute: ``1.45x``;
* 100k fetchall: ``1.05x``;
* 100k total: ``1.32x``;
* threads: ``1.69x``;
* cancel: ``1.04x``.

One qpsycopg cancel run was about ``22 ms`` and one vanilla cancel run was about
``21 ms``. Do not interpret either outlier as the steady-state cancellation cost;
the medians and the dedicated cancellation experiment show practical parity.

Progress since optimized14/optimized15
--------------------------------------

``optimized14`` was the last checkpoint before native result packing. It had
approximately ``1.85x`` bulk-total overhead and ``2.13x`` cancellation overhead.

``optimized15`` measured commit ``cf4d51c`` after the native binary-compatible
``PGresult -> PackedValues`` packer. Its important result was not one noisy total
number but the removal of the snapshot bottleneck:

* Python snapshot value packing: about ``108.9 ms``;
* native snapshot value packing: about ``5.1 ms``;
* reduction: about ``95%`` / ``21x``;
* full snapshot total: about ``110.4 -> 5.1 ms``.

The native path is implemented in the shared Cython ``PGresult`` source so the
generated ``psycopg_binary`` package receives the same capability. The worker
uses capability detection and retains a Python fallback.

Cancellation was still poor in ``optimized15`` because normal WAIT and cancel
shared the same transport lock. Collapsing cancellation to one worker RPC reduced
RPC chatter but did not remove the lock dependency: a targeted checkpoint still
measured roughly ``15.6 ms`` qpsycopg cancellation versus ``6.5 ms`` vanilla.

Dedicated cancellation control channel
--------------------------------------

Commits ``f6f62a6``, ``1ffbd3e`` and ``dfa4f46`` introduced a dedicated
authenticated localhost control socket for safe cancellation.

The main invariants are:

* the normal query/data channel and its lock are not used by ``cancel_safe()``;
* ``PGcancelConn`` is created by the worker main thread from the real ``PGconn``;
* only the control thread owns and drives that ``PGcancelConn`` afterwards;
* repeated cancellation reuses the cancel object through ``reset()``;
* the control channel is closed before the worker/session is torn down.

The targeted ``cancel-control`` benchmark measured median ``cancel_call`` around
``6.02 ms`` for qpsycopg versus ``6.56 ms`` for vanilla. The full
``optimized16`` checkpoint measured ``6.12 ms`` versus ``5.88 ms``. Cancellation
is therefore no longer the reason to keep the query WAIT drain short.

Commit ``a186786`` subsequently removed the obsolete parent pending-handle gate
and the old main-channel fast-cancel path. This cleanup was made after the
``optimized16`` performance checkpoint and should be smoke-tested before the
next tagged performance checkpoint.

Current phase profile: native packer + 10 ms WAIT drain
-------------------------------------------------------

A representative 100k x 3 ``fetch`` profile with the binary worker and
``_WAIT_DRAIN_GRACE = 0.010`` reported::

    elapsed_seconds                         0.15128
    remote_requests                              15
    WAIT remote requests                         11
    WAIT remote seconds                     0.10935

    worker libpq_wait_seconds                0.08587
    worker is_busy_seconds                   0.01515
    worker consume_input_seconds             0.00639
    worker snapshot_values_seconds           0.00510
    worker snapshot_total_seconds            0.00514

    parent request_pickle_seconds            0.00070
    parent request_socket_send_seconds       0.00125
    parent response_header_wait_seconds      0.12711
    parent response_payload_recv_seconds     0.00564
    parent response_unpickle_seconds         0.00253
    parent restore_seconds                   0.00003

For comparison, the earlier native-packer profile with a ``1 ms`` WAIT drain had
approximately ``67`` WAIT RPCs, ``71`` total remote requests and ``0.170 s``
elapsed time. Increasing the drain to ``10 ms`` therefore reduced WAIT RPC count
by about ``84%`` and total profiler elapsed time by about ``11%`` while the
separate control channel kept cancellation near vanilla.

Do not add ``parent_response_header_wait_seconds`` to worker phase timings. It is
parent wall-clock waiting which overlaps worker libpq wait, consume/busy work,
snapshot construction and response serialisation.

Interpretation of the current large-result path
-----------------------------------------------

The two large bottlenecks identified by the original phase decomposition have
now been addressed:

#. Python per-cell snapshot construction was replaced by native packing.
#. WAIT RPC count was reduced aggressively after cancellation was isolated from
   the main transport.

The remaining ``WAIT remote seconds`` is no longer mainly avoidable RPC churn.
About ``0.086 s`` of the ``0.109 s`` representative WAIT wall time is measured
inside worker libpq waits. A longer drain may reduce a few more RPCs but is
unlikely to produce another large step improvement and should not be increased
without evidence.

``fetchall()`` is at practical parity in ``optimized16``. The current bulk path
is noisy but broadly close enough that the next performance investigation should
not return to row decoding or snapshot packing unless a profile regresses.

PackedValues and native packing
-------------------------------

``ResultSnapshot`` carries cell data as ``PackedValues``:

* one contiguous data buffer;
* an offset table;
* a NULL bitmap;
* the cell count.

For the 100k x 3 benchmark a typical payload is approximately:

* data: ``5.47 MiB``;
* offsets: ``2.40 MiB``;
* NULL bitmap: ``37.5 KiB``.

The binary-compatible native packer writes the same layout directly from
``PGresult``. Preserve this contract. NULL and empty values are explicitly
covered by smoke tests.

The parent Cython bulk decoder reads the packed buffers directly and specialises
standard loaders. Unsupported/custom loaders retain Python fallback semantics.
Do not replace the global qt-process adapter map with the native C adapter map:
connection-sensitive native loaders may assume a local native ``PGconn``.

Small-query RPC reduction
-------------------------

A warmed ``SELECT 1`` normally uses one process-boundary request through deferred
``send_query*`` + ``qpsycopg_execute_pending`` coalescing. Parent connection/query
state caches avoid hot follow-up status/flush/busy requests.

Despite reaching the one-RPC shape, ``optimized16`` still measures about
``2.10x`` on the summary SELECT median (paired median about ``1.88x``). This is
now a more important unresolved hot-path gap than bulk ``fetchall()``.

The next profiling work should therefore concentrate on the fixed cost of a
warmed one-RPC short query: request framing/pickle/send, worker dispatch, the
extra parent-worker scheduling hop, worker libpq round trip, response framing and
parent unpickle/restore. Do not add more caching until a profile identifies a
specific additional remote request or Python phase.

Threaded shared-connection work also remains around ``1.7x`` in ``optimized16``.
Because Psycopg intentionally serialises operations on one shared connection,
this may largely reflect the same per-operation fixed IPC cost as short SELECT.
Profile before introducing thread-specific architecture.

Connection startup
------------------

Connection startup remains structurally expensive: ``481.9 ms`` versus
``34.9 ms`` in ``optimized16``. This includes QProcess startup, authenticated
main transport establishment, PostgreSQL connection establishment and the
cancellation-control setup.

Startup is not the current frequent-operation target. Optimise it only if the
intended application creates connections frequently rather than pooling/reusing
them. A startup optimisation must preserve process isolation and worker lifecycle
correctness.

Qt responsiveness
-----------------

Thread-caller Qt timer median/p95 remain effectively the same as vanilla. A
single run may contain scheduler outliers; use repeated medians/p95 rather than
one maximum stall.

``--caller main`` is intentionally a blocking control. Synchronous Psycopg calls
block whichever thread invokes them; process isolation does not make the API
asynchronous.

Rejected/deprioritised experiments
----------------------------------

Protocol 5 out-of-band pickle buffers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Earlier protocol-5/OOB experiments were slower and were rolled back. The current
framing uses one pickle payload; small frames may coalesce header+payload.

Aggressive result prefetch during WAIT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Do not speculatively call ``get_result()`` from the WAIT path. Earlier attempts
changed libpq/Psycopg result timing and broke fatal-termination semantics. WAIT
may consume input, inspect busy state and collect notifications; result
boundaries remain in the normal generator/result path.

Python row pre-slicing
~~~~~~~~~~~~~~~~~~~~~~

Pre-slicing packed cells into Python row tuples recreates allocations that the
packed representation intentionally avoids and was slower.

Worker ``binary -> c`` switching
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A C-worker A/B run did not improve the old snapshot path. The canonical worker
remains ``binary``. The native packer was deliberately placed in shared Cython
``PGresult`` code so the generated binary package receives it; do not switch the
canonical worker merely to reach native state.

Benchmark tools
---------------

``tools/test.ps1``
    Reproducible Windows runner. Modes: smoke, bench, qt, gui, all. It writes
    per-run JSON, ``summary.json``, ``manifest.json`` and a timestamped ZIP.

``tools/bench_qt_process.py``
    Measures connect, warmed SELECT, bulk execute/fetchall, RSS,
    shared-connection threads, SQL-error survival and cancellation.

``tools/bench_qt_responsiveness.py``
    Measures Qt timer behaviour. Thread-caller mode represents the intended
    synchronous GUI integration pattern.

``tools/profile_qt_process.py``
    Reports remote operations and parent/worker phase timings. Parent/worker
    timing views overlap and must not be summed naively.

Next performance work -- follow in this order
---------------------------------------------

#. Smoke-test commit ``a186786`` which removes obsolete cancellation gating.
#. Profile a warmed short SELECT with the canonical ``binary`` worker and one-RPC
   lazy-send path. Identify the fixed parent/worker/libpq cost before changing
   transport architecture.
#. Inspect the ``8``-thread shared-connection benchmark together with the short
   SELECT profile; determine whether its remaining overhead is simply repeated
   fixed IPC cost under Psycopg's normal connection lock.
#. Keep the current native ``PackedValues`` packer and ``10 ms`` WAIT drain as
   baselines unless a new profile demonstrates regression.
#. Do not increase WAIT drain merely to minimise request count: the current
   profile is already dominated by real libpq wait inside those requests.
#. Treat connection startup separately from hot-path query latency. Optimise it
   only if application requirements justify the structural work.
#. After the next validated runtime improvement, run a full tagged checkpoint
   and update this document.

Decision rule: optimise a measured dominant phase, not the most visually obvious
request count. Preserve high-level Psycopg semantics in the parent and native
libpq/session state in the worker.
