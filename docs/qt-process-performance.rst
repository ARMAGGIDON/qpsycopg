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

Current numbered optimisation -- optimized01 (retained)
-------------------------------------------------------

The first ``qt-thread`` change is a narrowly scoped cancellation-lifecycle
optimisation. Instead of creating the cancellation-control server during
``Connection.connect()``, it constructs the control channel immediately before
the first normal query, with a fallback for ``cancel_safe()`` before that
query. The ``qt-process`` code path is not modified.

Implementation commits: ``4637ff9``, ``371bda8``. Measurement checkpoint:
``optimized01`` at commit ``7c781a97``. All six SQL and five Qt rounds
completed; the semantic smoke passed, including cancellation and
``AsyncConnection``. The upstream base, benchmark hash/reference key and
isolated C-worker environments are compatible with ``baseline-v3``.

The intended result is observed::

    qt-thread / upstream             baseline-v3   optimized01
    warm connection median             1.409x       1.319x
    warm connection p95                1.930x       1.291x
    cold connection                    1.366x       1.053x (noisy)
    extra connected-state threads         +2           +1
    extra connected-state handles        +14           +8

The warm median ratio improved about 6.4%; warm p95 improved about 33.1%.
The thread/handle counts are exact architecture deltas at the pre-query
connected-state snapshot. Cold-start figures are noisy and secondary.

Short-query SELECT median stayed near ``1.27x`` upstream (1.273x before,
1.262x after), as expected because the control channel still exists while
queries execute. There is no measured RSS/private-memory win.

**Watch items, not claimed regressions or improvements**:

* CPU/short query ratio: ``2.000x -> 2.429x``. Baseline and current per-round
  ranges overlap (``1.556-2.649x`` and ``1.955-3.000x``) and both are
  variable. This merits a focused CPU-path check before claiming the change
  neutral outside connection setup.
* Cancel call median: ``1.048x -> 1.553x``, but current paired ratio MAD
  about 19% and a ``0.979-3.201x`` range prevent a reliable latency
  conclusion; semantic cancellation still passed.
* Qt timer p95/p99 and bulk ratios fluctuate modestly across runs and do not
  establish a causal regression. Maximum timer stall is diagnostic only.

Decision: **retain optimized01** for the confirmed connection/resource benefit
and preserved semantic smoke. Do not stack another unrelated runtime change
before a bounded short-query profiling hypothesis is chosen. Full numbers,
method and historical drift are in :doc:`qt-process-optimization-log`.

Qt-thread short-query CPU profiling checkpoint
----------------------------------------------

The corrected diagnostic archive ``qt-thread-cpu-profile-20261103-192809-646.zip``
was captured at ``9b0f7108`` with two rounds each of pristine upstream,
fork direct C and qt-thread. There is no production runtime change and
no new numbered optimisation benchmark. Clean (pre-instrumentation)
process CPU/query in the two runs was::

    upstream C             88.5 / 130.2 us
    fork direct C          125.0 / 145.8 us
    qt-thread              255.2 / 250.0 us

The qt-thread CPU excess versus fork direct is ~104-130 us/query.
Its clean caller CPU is ~115-135 us/query and other-thread CPU
~115-141 us/query. Context switches are ~3.0-3.1/query in qt-thread
versus ~1.0/query in direct. Windows CPU timers are quantised and
cannot support a claim about isolated sub-microsecond function costs.

A no-SQL forced status GET exercises one full qt-thread request/response
round trip without a libpq wait: it costs 40.02-45.13 us wall,
36.46-46.88 us process CPU and 2.01-2.04 context switches.
The corresponding direct status read is only ~0.17 us wall.
In the instrumented status control, ``_dispatch`` occupies
~35.2-39.2 us inclusive wall and the worker GET handler ~5.8-6.4 us.
Thus the **synchronous queue/wakeup round trip** is the largest
measured fixed non-SQL overhead. Its cost is roughly one third of
the SELECT CPU gap in magnitude, but it is *not* an exact additive
component of fast-execute query CPU.

The query's worker ``_wait`` call uses ~539-581 us inclusive wall and
~73-83 us inclusive thread CPU. Its wall time predominantly reflects
PostgreSQL socket readiness. Nested worker phase timings overlap;
they cannot be summed or used as avoidable CPU savings.
Importantly upstream on Windows also uses Python ``select.select()``:
``waiting.py`` intentionally avoids native ``wait_c`` owing to
reported CPU issues. The old worker-native wait, native receive,
speculative WAIT result prefetch and snapshot micro-tuning
experiments remain rejected.

The earlier ``cProfile`` attribution was invalid on Python 3.13
because it captured cross-thread frames and reported negative self CPU.
The corrected profiler uses thread-local function clocks and exposes
only inclusive phase measurements; it is diagnostic rather than an
acceptance benchmark. Full raw rationale is recorded in
:doc:`qt-process-optimization-log`.

**Bounded conclusion:** cpu profiling is completed and identifies the
thread request/response handoff as the clearest *fixed, non-libpq*
target for a falsifiable diagnostic A/B. There is not yet evidence for
a safe runtime performance change.

The proposed single local hypothesis is that the timed
``SimpleQueue.get(timeout=0.1)`` path in ``_ThreadTransport._dispatch``
adds >=10 us to the ~35-39 us fixed handoff wall. Compare timed
and untimed waits in a **diagnostic-only** paired no-SQL control first.
Reject if not repeatable or if worker liveness/error handling cannot
be preserved. Never replace the production timed wait with a
potentially indefinite block without satisfying fatal worker,
close and cancellation semantics. Request independent
``00 Coordinator`` review before beginning a new optimisation
direction; retain ``optimized01`` in the meantime.

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

Timed/untimed queue-wait A/B -- rejected
----------------------------------------

The Coordinator-authorised diagnostic at fork commit ``516ccc8c``
completed with eight interleaved pairs and 4000 no-SQL GET status
requests per mode/pair. Artifact:
``qt-thread-queue-wait-ab-20261103-195939-054.zip``.

The untimed wait produced **no meaningful improvement** over timed
``SimpleQueue.get(timeout=0.1)``: median paired wall delta
(timed minus untimed) was +0.01546 us, paired MAD 1.08491 us,
range -4.403 to +2.119 us, and only 4/8 pairs favored untimed.
Median process CPU delta was 0 us and context-switch delta
-0.001125 per request. This fails all predeclared performance
gates (>=10 us median, >3x MAD, >=7/8 positive pairs).

Remote error propagation, ordinary close and post-close rejection
passed in both modes. When a worker exited without responding, the
timed mode returned an ``OperationalError``; the untimed mode
indefinitely blocked until a subprocess watchdog terminated it
after 8 seconds. The diagnostic's ``failure_checks_pass=true``
means expected outcomes were observed, **not** that untimed waiting
is safe: ``untimed_detects_worker_exit=false``.

**Decision: REJECT** raw untimed SimpleQueue response waiting.
There is no measured latency/CPU advantage and worker-exit
liveness semantics are worse. Production runtime, accepted
``optimized01`` and canonical benchmark v3 remain unchanged.
The approximately 40-45 us fixed no-SQL queue handoff remains
a valid measured cost, but the 100 ms timed poll does **not**
explain it. Do not retest this same variant without new evidence.
Full evidence is recorded in :doc:`qt-process-optimization-log`.

Minimal SimpleQueue echo baseline -- measured diagnostic
--------------------------------------------------------

The Windows diagnostic archive ``qt-thread-echo-baseline-20261103-202519-685.zip``
was collected at branch commit ``5dcdbd51`` (Python 3.13.5,
qt-thread with C worker). It contains eight alternating paired
comparisons of minimal token echo versus warmed qpsycopg no-SQL
``GET status``, 4000 calls per mode per pair and 300 block
warmups. The same original timed response wait is used in
both. The archive's error log is empty; the subprocess watchdog
did not fire.

Measured medians per call (microseconds unless noted)::

    metric                        echo      qpsycopg GET    paired GET-echo
    wall                          18.407        36.367          +18.957
    process CPU                   13.672        35.156          +19.531
    caller CPU                     9.766        21.484          +11.719
    context switches               2.011         2.014           +0.0066

Wall paired delta MAD was 5.764 us, range +13.025 to +37.552 us,
and positive in 8/8 pairs. CPU delta MAD was 5.859 us (7/8
positive). Caller CPU and context-switch delta are noisier, subject
to Windows CPU clock quantization. Both paths require approximately
two context switches; extra qpsycopg **context switches are not
demonstrated**. Minimal echo represents ~50.6% of the GET wall
median (ratio of medians); median within-pair ratio is ~47.9%.

A substantial **temporal drift** matters: the first four pairs
have median GET-echo excess ~31.81 us and the last four ~13.66 us.
Both absolute modes become faster across the run. Pairwise
alternation reduces order bias but does not establish a
stationary benchmark. The ~19 us median residual is therefore
descriptive, **not** a reliable saving target.

The minimal echo includes two SimpleQueue exchanges with the
same timed get; the full qpsycopg GET adds real Request/Response
objects, transport lock, liveness checks, status validation,
cache/worker bookkeeping and exception handling. The measured
difference **does not identify which component dominates or
whether it is removable**; no SQL-workload gain may be inferred.
Prior timed-vs-untimed response wait remains REJECT due to zero
speed signal and unsafe worker-exit behavior.

**Decision: diagnostic COMPLETE; no production optimisation
accepted.** No runtime code or canonical v3 workload changed.
No numbered ``optimized02`` experiment is warranted. One
possible next narrowly scoped diagnostic for Coordinator review
is to compare a richer echo with matched Request/Response
and validation costs against the minimal echo, without
modifying production, before attempting any local tuning.

Enriched echo protocol/bookkeeping isolation -- measured
------------------------------------------------------

The independently repeated diagnostic archive
``qt-thread-enriched-echo-20261103-205855-365.zip``
was captured at branch HEAD ``d1b6a5a`` using four fresh
Windows Python 3.13.5 subprocesses, each with a warmed
qt-thread/C connection and one synthetic worker. Each
repeat has 16 cycles with 600 calls in each of six
short blocks. Balanced ordering places the four
enriched/real modes between timestamp-interpolated
bare echo controls. All 64 cycles completed without
reported error or watchdog activation.

Wall overhead above interpolated bare echo (us/call,
median across **four independent per-repeat medians**)
was approximately::

    stage                                  wall delta     repeat range
    Request/Response envelope               +4.031        3.209..4.722
    envelope + validation                   +5.174        4.084..5.316
    synthetic bookkeeping                   +9.420        8.731..9.673
    real qt-thread GET status              +14.172       13.505..14.537

The directly paired incremental-stage wall deltas,
summarized separately per repeat, were +0.855 us
validation over envelope (positive in 44/64 cycles),
+4.184 us synthetic bookkeeping over validation
(61/64), and +5.016 us actual GET over synthetic
bookkeeping (59/64). Envelope over bare was positive
in 60/64 cycles. The small validation increment
is not stable at the within-cycle level, whereas
synthetic representation and bookkeeping are more
consistently measurable.

**Drift caution:** repeat 1's bare baseline rose
131.8% from first to last control (15.486 to
35.892 us/call). The other repeats changed
+6.5%, +3.5% and +6.7%; transient cycle-level
spikes still occurred. Linear interpolation by
actual block midpoints reduces local drift, not
all nonlinear scheduling and thermal variation.
The earlier ~18.96 us GET-minus-bare observation
and this ~14.17 us repeat-median result cannot
be interpreted as a runtime speedup or stable
reduction in production work.

Windows CPU measurements across 600-call blocks
are coarsely quantized and not reliable at the
individual few-microsecond stage level; inferred
other-thread increments sometimes become
negative. Context-switch deltas near zero do
not establish new scheduling boundaries.
Representation/validation/bookkeeping stages
are **synthetic approximations**. The worker
uses a fake PGconn and simplified object,
status, notices, lock, cache and error paths.
They cannot isolate one real qpsycopg function
as dominant or show that removing an operation
would preserve semantics. No SQL query
latency claim follows.

**Decision: diagnostic COMPLETE, no runtime
candidate accepted.** ``optimized01`` stays
RETAIN; timed-vs-untimed SimpleQueue wait
stays REJECT. No code outside docs changed
and canonical benchmark v3 is untouched.
No ``optimized02`` or acceptance benchmark
is justified by this synthetic result.
Detailed per-repeat results, drift and SHA
of the locally supplied ZIP are in
:doc:`qt-process-optimization-log`.

Next work
---------

#. Route this significant completed diagnostic
   checkpoint to the existing ``00 Coordinator``
   for independent review. The decision concerns
   whether real-path profiling is justified
   after the synthetic attribution plateau.
#. Do not repeat synthetic enrichment, the
   rejected untimed response wait, worker-native
   wait, speculative WAIT prefetch or generic
   snapshot/serialization tuning without
   substantially new evidence.
#. Any Coordinator-approved next diagnostic
   should isolate a real qpsycopg GET/worker
   phase, controlling drift with repeated
   short paired blocks. Do not start
   ``optimized02`` without a falsifiable
   semantically safe production hypothesis.
#. If a future runtime candidate emerges,
   require qt-thread semantic smoke and
   unchanged adjacent paired benchmark v3
   with CPU/query, cancellation latency,
   Qt p95/p99, bulk, connection and
   resource guardrails.
