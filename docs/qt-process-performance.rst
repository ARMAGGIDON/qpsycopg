Qt process performance notes
============================

This document records measured qpsycopg performance history, accepted/rejected
experiments and the evidence which moved active work from process micro-tuning to
the real ``qt-thread`` backend.

For the live numbered optimisation sequence see
:doc:`qt-process-optimization-log`. For current ownership and immediate next work
see :doc:`qt-process-project-map`.

Performance target
------------------

The target for frequent database operations is approximately normal upstream
Psycopg behaviour while preserving the chosen Qt worker semantics. Performance
claims must consider more than wall-clock median:

* latency median and tails;
* CPU per operation;
* context switches;
* bulk throughput;
* process-tree RSS/private memory;
* handles, threads and process count;
* connection startup;
* cancellation;
* Qt p95/p99 responsiveness.

Process-backed resource totals must use the whole parent+worker process tree.
Parent-only memory/CPU is useful for attribution, not architecture comparison.

Current canonical methodology
-----------------------------

The current acceptance benchmark is ``tools/benchmark_qpsycopg.ps1``.

The upstream comparison revision is the merge-base with ``psycopg/psycopg``::

    99356800e90aa0e7b604f652d714ef0f33422b47

Upstream and fork are installed into isolated, non-editable benchmark venvs built
from the same CPython and against the same local PostgreSQL/libpq installation.
The benchmark reruns upstream in every checkpoint because Windows scheduler/load,
CPU state and PostgreSQL/Docker state can move absolute timings.

Benchmark v3 is the canonical methodology for numbered optimisations:

* every fork mode has an immediately adjacent upstream run;
* pair orientation is alternated;
* short query timing uses 5000 measured iterations after 200 warm-ups;
* warm connection timing uses 40 samples;
* cancellation uses 20 attempts per SQL run;
* concurrency uses 20 batches on an already warmed thread pool;
* Qt uses five paired runs and treats p95/p99 as primary tail metrics;
* maximum Qt stall is diagnostic only;
* the primary ratio is the median of per-pair ``fork / upstream`` ratios;
* paired-ratio MAD/range and upstream drift are reported separately;
* discrete architecture resources are reported as deltas rather than ratios.

The historical ``initial`` and ``baseline-v2`` checkpoints used older benchmark
definitions and are diagnostic only. ``baseline-v3`` is the canonical before-state
for the current numbered series.

Canonical baseline -- baseline-v3
---------------------------------

The complete qt-thread smoke passed at ``baseline-v3``. Selected paired ratios
were:

=====================================  ===========  ============
Metric                                 qt-thread    qt-process
=====================================  ===========  ============
cold connect                           1.366x       14.504x
warm connect median                    1.409x       1.123x
warm connect p95                       1.930x       1.166x
SELECT 1 median                        1.273x       1.446x
SELECT 1 p95                           1.249x       1.542x
SELECT 1 p99                           1.395x       1.547x
CPU / short query                      2.000x       3.163x
context switches / query               3.032x       2.994x
100k execute                           1.040x       1.067x
100k fetch/decode                      1.013x       0.969x
100k total                             1.027x       0.986x
8-thread batch median                  1.341x       1.523x
concurrent throughput                  0.746x       0.659x
cancel call median                     1.048x       1.718x
process-tree RSS                       1.320x       2.467x
process-tree private memory            1.377x       2.384x
Qt timer p95                           0.982x       1.005x
Qt timer p99                           1.008x       1.005x
=====================================  ===========  ============

Connected-state architecture deltas were approximately:

======================  ===========  ============
Resource                qt-thread    qt-process
======================  ===========  ============
processes                +0           +2
threads                  +2           +11
handles                  +14          +306
======================  ===========  ============

The short-query median is sufficiently stable to drive normal optimisation:
upstream median MAD was about ``0.72%`` and the qt-thread paired ratio MAD about
``3.98%``. Context-switch and memory ratios were also stable. Cancellation
latency remains scheduler-sensitive and is supporting evidence rather than a sole
acceptance criterion.

Qt p95/p99 are effectively at parity, so current work should not target Qt timer
behaviour. Large-result decode is also near upstream and remains deprioritised.

Historical performance milestones
---------------------------------

Native result packing -- optimized15
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The original process worker created packed values through a Python per-cell
``PGresult.get_value()`` loop. For the 100k x 3 workload the value-packing phase
moved roughly from::

    Python snapshot values     ~108.9 ms
    native snapshot values       ~5.1 ms

This was about a ``21x`` phase improvement and removed the dominant early bulk
bottleneck. Do not return to per-cell packing without new profiling evidence.

Cancellation isolation and WAIT batching -- optimized16
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Cancellation originally shared the main transport lock with query WAITs. A
separate authenticated cancellation channel removed that dependency. This allowed
the worker readable-data drain to grow from ``1 ms`` to ``10 ms``; a representative
100k profile dropped from roughly 67 remote WAIT operations to 11 and improved
elapsed time by about 11%.

The remaining WAIT wall time was mostly real libpq/socket waiting. Increasing the
drain further merely to minimise request count is not an optimisation target.

Compact process short-query path and worker reuse -- optimized17..29
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Measured retained changes included:

* generic request/response simplification;
* dedicated fast-execute framing;
* compact payloads/marshal on the common path;
* warmed worker-process reuse;
* stable client-encoding caching;
* removal/reuse of redundant status work;
* native result metadata capability detection.

Worker reuse removed the structural process-start cost from most normal connect
operations. Old checkpoints which mixed worker cold-start and warm session setup
must not be interpreted as one homogeneous connect distribution.

Windows transport work -- optimized30..35
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``SIO_LOOPBACK_FAST_PATH`` improved the retained TCP fallback. A local transport
microbenchmark then showed lower fixed latency for raw Windows named pipes,
particularly for tiny request/response messages. The production Windows process
path moved to a raw byte-stream named pipe with read-ahead preservation.

A representative paired TCP-fast vs raw-pipe run measured approximately::

====================  ============  ============
Metric                TCP-fast      raw pipe
====================  ============  ============
SELECT median         0.813 ms      0.738 ms
SELECT p95            1.191 ms      1.016 ms
SELECT CPU            625 us        469 us
connect               30.02 ms      28.77 ms
100k total            0.2742 s      0.2730 s
====================  ============  ============

This established raw named pipe as the preferred Windows process transport while
retaining TCP as a fallback.

Process resource telemetry
~~~~~~~~~~~~~~~~~~~~~~~~~~

``optimized34`` exposed the real cost of process isolation once the whole process
tree was measured. Representative figures were::

    SELECT median          ~1.48x vanilla
    SELECT CPU             ~3.80x
    context switches       ~3.05x
    process-tree RSS       ~2.34x
    private memory         ~2.13x
    100k total             ~1.11x

This corrected earlier parent-only RSS interpretations.

Native parent IPC
-----------------

A synthetic Python-vs-Cython named-pipe experiment showed a real opportunity to
move the parent frame exchange below Python. For a small request/response class a
representative result was roughly::

    median       25.3 -> 19.0 us
    p95          39.1 -> 25.7 us
    CPU          14.06 -> 4.69 us

The first integration regressed because it forced separate header/payload reads.
After adding native read-ahead, a same-run process validation showed roughly:

* SELECT median ``-4.1%``;
* SELECT p95 ``-17.7%``;
* parent CPU about ``-20%``;
* process-tree CPU about ``-7.5%``;
* bulk and Qt behaviour effectively neutral.

The native parent named-pipe frame exchange is therefore retained.

Worker-side native experiments
------------------------------

Worker hot-path profiling showed that result snapshot work was already tiny while
worker wait/receive/dispatch dominated the process fixed cost. Native worker frame
receive and native Winsock wait were therefore tested separately.

A controlled three-way result was approximately::

======================  ============  ============
Mode                    SELECT median SELECT p95
======================  ============  ============
base                    ~0.728 ms     ~0.960 ms
native worker receive   ~0.731 ms     ~0.955 ms
native receive + wait   ~0.757 ms     ~1.247 ms
======================  ============  ============

Native worker receive was neutral and native socket wait was worse. Both helpers
remain experimental switches only; do not enable them by default or repeat the
same experiment without new evidence.

Architecture boundary result
----------------------------

After local process micro-optimisations stopped exposing a large missing win, the
project compared direct Psycopg, the same work through a dedicated thread and the
process backend. Representative short-query medians were::

    direct       ~533 us
    thread       ~567 us
    process      ~724 us

The dedicated thread was about ``+5%`` over direct in that synthetic architecture
gate, while qt-process was roughly ``+38.6%``. Process CPU was about ``2.44x`` the
dedicated-thread CPU. Both worker architectures were around three context switches
per query versus about one direct, so context-switch count alone did not explain
the process penalty.

This result motivated the real ``PSYCOPG_IMPL=qt-thread`` backend instead of
continuing random process-IPC micro-tuning.

Real qt-thread validation
-------------------------

The real qt-thread implementation initially exposed several correctness/setup
issues which are now resolved:

* Python generator-layer selection;
* shared-interpreter worker pq namespace consistency;
* connection-handshake lifecycle/fatal-health checks;
* dispatch deadlock after worker exit;
* Windows libpq/DLL bootstrap for validation tooling;
* native packed-decoder selection for qt-thread.

The standalone semantic smoke now passes fully, including COPY, multiple result
sets, cancellation, fatal backend termination, shared connection access and
``AsyncConnection``.

An important measurement correction was also made: an early qt-thread bulk
regression came from accidentally using ``psycopg.adapt.Transformer`` without the
native remote decoder. Enabling the same native packed decoder used by qt-process
brought 100k fetch/decode back near direct/upstream. Do not treat the pre-fix bulk
numbers as architectural evidence.

Rejected / deprioritised experiments
------------------------------------

Protocol-5 out-of-band pickle buffers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Slower in earlier measurements; rolled back.

Speculative ``get_result()`` during WAIT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Changed result-boundary timing and broke fatal/COPY semantics. Do not
reintroduce without a new design argument.

Longer WAIT drain as a goal by itself
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``10 ms`` drain already removed most avoidable WAIT crossings. Remaining wall
time is largely real libpq/socket waiting.

Unsafe one-read TCP receive
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Did not improve SELECT and risked consuming bytes from a following frame without
preserving the over-read. The retained named-pipe adapter explicitly preserves
read-ahead state.

Windows AF_UNIX
~~~~~~~~~~~~~~~

Did not beat the retained Windows transport path and was removed.

Direct ``SendSnapshot`` shortcut and small state-cache variants
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

No useful stable end-to-end improvement relative to added state-machine
complexity; reverted/deprioritised.

Native worker receive / native wait
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Receive was neutral; native wait was slower. Disabled by default.

Generic serialization/snapshot micro-tuning
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Profiling no longer showed these phases as the dominant process cost. Do not
resume them without fresh phase evidence.

Bulk decoder tuning
~~~~~~~~~~~~~~~~~~~

Current qt-thread/process bulk decode is near upstream. Do not spend another
iteration here while short-query CPU/latency remains materially higher.

Current numbered optimisation -- optimized01
---------------------------------------------

``optimized01`` is a narrow qt-thread lifecycle experiment. Before the change,
``Connection.connect()`` created the full cancel-control infrastructure even for a
connection immediately closed without running a query. The implementation now:

* skips eager cancel-control creation for qt-thread;
* opens it immediately before the first normal ``send_query*``;
* retains an on-demand ``cancel_safe()`` fallback;
* leaves qt-process unchanged.

Implementation commits are ``4637ff9`` and ``371bda8``.

Expected signals are reduced qt-thread connection/resource overhead before the
first query. Short-query latency is not the primary target because cancellation
support is still established before the first query and then reused.

``optimized01`` is not accepted yet. The next required action is to run the
unchanged v3 benchmark as checkpoint ``optimized01`` and compare it directly with
``baseline-v3``. Do not stack another runtime optimisation before this result is
available.

Performance rules for future work
---------------------------------

#. Use compatible adjacent paired upstream/fork measurements for optimisation
   claims.
#. Keep upstream code fixed while rerunning its measurements in every checkpoint.
#. Treat historical upstream runs as drift evidence, not the sole denominator.
#. Measure process-backed resources across the whole process tree.
#. Keep raw named pipe + native parent exchange as the measured process default.
#. Keep native worker receive/wait disabled by default.
#. Do not infer GUI responsiveness from worker topology.
#. Do not optimise bulk decode or Qt p95/p99 while they remain near upstream.
#. Change one bounded runtime hypothesis at a time and measure before stacking the
   next optimisation.

Next work
---------

#. Run ``optimized01`` with ``tools/benchmark_qpsycopg.ps1`` using the unchanged
   v3 benchmark.
#. Confirm the semantic smoke remains clean.
#. Compare connection/resource deltas and unrelated regressions against
   ``baseline-v3``.
#. Retain or revert ``optimized01`` from evidence, and document the result.
#. If retained, select the next bounded target from the remaining measured cost;
   current evidence points to qt-thread short-query CPU/latency, not bulk decode
   or process-IPC micro-tuning.
