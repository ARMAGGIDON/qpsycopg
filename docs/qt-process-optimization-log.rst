qpsycopg optimization log
=========================

This file is the live performance/optimisation handoff for the current
``qt-thread`` / ``qt-process`` work. Historical implementation details remain in
:doc:`qt-process-performance` and :doc:`qt-process-project-map`; this file records
what is being measured now, why a checkpoint exists, and what may be compared
with it.

Comparison foundation
---------------------

The comparison revision is the merge-base with ``psycopg/psycopg``. The current
comparison base is::

    upstream base  99356800e90aa0e7b604f652d714ef0f33422b47

Upstream and fork are benchmarked using isolated, non-editable environments built
with the same CPython and the same local PostgreSQL/libpq installation. Current
reference environment is:

* Python ``3.13.5``;
* PostgreSQL/libpq ``18.6``;
* PySide6 ``6.12.0``;
* psutil ``7.2.2``;
* Windows 11, AMD64, 8 logical CPUs.

Every checkpoint reruns upstream. Upstream code stays fixed at the comparison
revision, while its measurements are deliberately repeated because Windows load,
scheduling, CPU state and PostgreSQL/Docker state can move absolute timings.
Host CPU load, CPU frequency, available memory and system process count are
captured around every run.

Ratios are meaningful for performance/usage metrics. Discrete architecture
resources such as processes, threads and handles are reported as absolute deltas
instead of ratios.

Initial diagnostic checkpoint -- benchmark v1
----------------------------------------------

Checkpoint ``initial`` was captured at fork commit
``84c6ebb2eafd3562511829334704b4304cd58e79``. It established useful architecture
signals, but is not a pre-optimisation baseline because the benchmark definition
was improved afterwards.

Representative paired ratios were:

==============================  ===========  ============
Metric                          qt-thread    qt-process
==============================  ===========  ============
connect median                  1.241x       1.099x
SELECT 1 median                 1.215x       1.488x
SELECT 1 p95                    1.210x       1.427x
CPU / short query               1.644x       3.056x
context switches / query        3.009x       3.016x
100k execute                    1.076x       1.189x
100k fetch/decode               1.010x       1.010x
100k total                      1.026x       1.094x
process-tree RSS                1.362x       2.469x
process-tree private memory     1.392x       2.334x
Qt timer p95                    1.026x       1.006x
==============================  ===========  ============

Benchmark v2 -- diagnostic baseline
-----------------------------------

Checkpoint ``baseline-v2`` was captured at fork commit
``93dc15deb92c0988edaa5c0accbf110f18941deb``. Correctness smoke passed completely.
It introduced separate cold/warm connection metrics, repeated cancellation,
Qt p99 and process inventory, but pairing was still too loose for small
optimisation claims. Upstream ``SELECT 1`` median had about ``22%`` MAD and
roughly ``71%`` observed range between SQL runs.

V2 is retained as a diagnostic checkpoint only.

Benchmark v3 -- canonical optimisation methodology
--------------------------------------------------

V3 is the canonical methodology for the numbered optimisation series. Its
important rules are:

* every fork mode has its own immediately adjacent upstream run;
* pair order alternates so upstream is not systematically first or second;
* SQL short-query timing uses ``5000`` iterations after ``200`` untimed warm-up
  queries;
* warm connection timing uses ``40`` samples;
* cancellation uses ``20`` attempts per SQL run;
* concurrency uses ``20`` batches on an already warmed ``ThreadPoolExecutor``;
* concurrency reports batch median/p95 and derived queries/s;
* cancellation reports call median/p95 and end-to-end median;
* Qt uses five repeated paired runs; p95/p99 are primary and maximum stall stays
  diagnostic;
* the benchmark/reference hash includes orchestration and summary code.

The SQL pair pattern is conceptually::

    adjacent upstream <-> fork direct
    adjacent upstream <-> qt-thread
    adjacent upstream <-> qt-process

The primary ratio is the median of six adjacent ``fork / paired upstream`` ratios
for SQL metrics. The generated stability table reports MAD and observed range of
the paired ratio itself.

Canonical baseline -- baseline-v3
---------------------------------

Checkpoint ``baseline-v3`` was captured before any numbered runtime optimisation.
Correctness smoke passed and process inventory confirmed the expected topology:
upstream/direct/qt-thread use one Python process, while qt-process uses one parent
plus two child Python processes.

Selected canonical ratios are:

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

Architecture deltas at the connected-state sampling point were:

======================  ===========  ============
Resource                qt-thread    qt-process
======================  ===========  ============
processes                +0           +2
threads                  +2           +11
handles                  +14          +306
======================  ===========  ============

The stable optimisation signals are not all metrics equally. In ``baseline-v3``:

* upstream ``SELECT 1`` median MAD is about ``0.72%``;
* qt-thread paired ``SELECT 1`` median ratio MAD is about ``3.98%``;
* context-switch ratios are extremely stable (around ``0.5%`` MAD or better);
* RSS/private-memory ratios are stable (around ``1%`` MAD or better);
* Qt p95/p99 are effectively at parity and should not currently drive work;
* cancellation medians remain scheduler-sensitive and are supporting evidence,
  not the primary acceptance criterion;
* maximum Qt stall is intentionally diagnostic only because its observed spread
  is very large.

This is the first checkpoint suitable as the before-state for ``optimized01``.

Optimized01 -- lazy qt-thread cancellation setup
------------------------------------------------

The first numbered runtime change is deliberately narrow. Before this change,
``Connection.connect()`` created the full cancellation-control infrastructure for
``qt-thread`` even for a connection which was immediately closed without running
a query. That setup includes a ``PGcancelConn``, TCP listener/client and a cancel
control thread.

``optimized01`` changes only the lifecycle:

* qt-thread connect no longer opens cancellation control eagerly;
* the first ``send_query*`` request opens it immediately before the query is
  handed to the worker thread, preserving cancellation availability while the
  query is active;
* ``cancel_safe()`` retains an on-demand fallback;
* qt-process behaviour is unchanged;
* the existing semantic smoke remains the correctness gate.

Implementation commits are:

* ``4637ff9`` -- add the qt-thread lazy-cancellation patch layer;
* ``371bda8`` -- activate that patch only for ``PSYCOPG_IMPL=qt-thread``.

The primary expected effects are lower qt-thread cold/warm connection cost and
lower connected-state thread/handle/resource counts before the first query. Short
query latency is not the primary target of this change because cancellation
support is intentionally established before the first query and then reused.

Measured optimized01 result -- RETAIN
--------------------------------------

Checkpoint ``optimized01`` was measured at commit
``7c781a97a58572ffb98155961e5d4f279ee683ba`` (the runtime implementation
was already present; intervening commits updated documentation/coordination).
The archive is ``qpsycopg-benchmark-20261103-182119-072-optimized01.zip``.
It contains all six SQL rounds and five Qt rounds for each mode. The
``smoke-thread.log`` finishes with ``qpsycopg qt-thread smoke test: OK``.

The upstream comparison revision, benchmark hash
``ee425499c962b48a6c1851732cc3db642aba03dd6bcb4388f9d25cf5b2f67ead``,
reference key ``99356800e90a-1f8854bb9581``, Python/libpq versions and
v3 workload match the ``baseline-v3`` before-state.

Primary qt-thread connection results (median paired fork/upstream ratio)::

    metric                      baseline-v3   optimized01
    cold connect                  1.366x        1.053x
    warm connect median           1.409x        1.319x
    warm connect p95              1.930x        1.291x
    SELECT 1 median               1.273x        1.262x
    CPU/short query               2.000x        2.429x
    100k total                    1.027x        1.063x
    RSS                           1.320x        1.330x
    private memory                1.377x        1.400x
    Qt timer p95                  0.982x        1.035x
    Qt timer p99                  1.008x        1.058x

Connected-state architecture resource deltas versus current upstream::

    metric                      baseline-v3   optimized01
    extra processes                    +0            +0
    extra threads                      +2            +1
    extra handles                     +14            +8

Decision: **RETAIN optimized01**. The targeted signal is credible: warm
connection median improves from ``1.409x`` to ``1.319x`` (about 6.4%
lower ratio), warm p95 from ``1.930x`` to ``1.291x``, and the exact
expected lifecycle resource reductions are observed (-1 thread and -6 handles).
Warm-connection paired-ratio MAD is approximately 0.87% in optimized01 versus
4.31% in baseline-v3. The first cold connect is noisy and is not used alone
for acceptance. Process implementation and fork direct remain controls.

There is no demonstrated short-query latency win, and no demonstrated RSS/private
memory win. Unrelated watch items must remain visible:

* CPU per short query moves from ``2.000x`` to ``2.429x``. The two runs'
  per-round ranges overlap substantially (baseline ``1.556-2.649x``,
  optimized01 ``1.955-3.000x``), and the change does not establish a
  causal regression; nevertheless it is a guardrail for the next targeted
  investigation, not something to claim away.
* Cancellation-call median moves from ``1.048x`` to ``1.553x``, with
  optimized01 paired MAD about 19% and range ``0.979-3.201x``. Cancellation
  succeeds in the semantic smoke; no reliable cancellation-latency conclusion
  follows from these noisy medians.
* Qt p95/p99 and bulk totals move modestly across the two checkpoints. Their
  jitter/ranges do not support a claim of benefit or a proven regression.
* The connected-state memory level remains about 51 MiB RSS and 37 MiB private
  for qt-thread. Removing a control thread/socket does not necessarily reduce
  the allocator's resident footprint.

No runtime changes were stacked onto the measured implementation. The next
bounded step is to isolate remaining qt-thread *short-query CPU/latency* cost
with path-level evidence and one local hypothesis, while preserving the
optimized01 lifecycle and monitoring its CPU/cancellation guardrails.


CPU profiling setup and first invalid attempt
---------------------------------------------

Coordinator decision at ``0ecd0377``: CONTINUE, but **do not start speculative
optimized02**. The next bounded stage is attribution of the remaining
``qt-thread`` warm short-query CPU/latency overhead versus pristine upstream
and fork direct, using path-level evidence before choosing one local hypothesis.

The existing ``optimized01`` v3 archive already isolates the broad
architecture difference. Raw medians across its runs, not per-pair ratios, are
approximately::

    metric                     upstream    fork direct    qt-thread
    CPU / SELECT 1              119 us        122 us       266 us
    SELECT 1 median             534 us        534 us       677 us
    context switches / query     1.00          1.00         3.04

These are *end-to-end* observations, not a CPU phase decomposition. The
upstream runs are repeated beside each fork mode; their raw median is included
only for orientation. No Python allocator, worker dispatch or scheduling stage
has yet been demonstrated to dominate.

Code inspection identifies the warm thread-specific chain::

    deferred send -> _ThreadTransport._fastExecute
      -> Request -> _dispatch (SimpleQueue handoff)
      -> Worker._handle -> _executePending (libpq + socket wait)
      -> Response -> _responseValue (restore/cache)
      -> normal cursor adaptation

Direct upstream/fork C execution has no qpsycopg request/response worker
boundary. A source-level difference is not a measured bottleneck.

Diagnostic profile archive -- partial CPU attribution, invalid cProfile details
-------------------------------------------------------------------------------

The Windows archive ``qt-thread-cpu-profile-20261103-191710-778.zip``
contains six JSON profiles (two each of pristine upstream C, fork direct C,
and qt-thread), plus a manifest. Its code revision was ``e135418a``.
This is diagnostic, not an unchanged-v3 acceptance checkpoint.

The *uninstrumented* SELECT 1 controls measured::

    metric                         upstream      fork direct     qt-thread
    process CPU/query (us)        93.8-114.6      88.5-109.4     229.2-234.4
    caller CPU/query (us)         93.8-114.6      88.5-109.4     114.6, 114.6
    wall/query (us)                 701-734         599-617        754-761
    context switches/query        1.01, 1.01      1.00, 1.01     3.03, 3.05

Subtracting caller CPU from total process CPU gives approximately
``115-120 us/query`` consumed by other threads in qt-thread. Its caller
uses about ``115 us/query``, near fork direct's ``89-109 us/query``.
Thus the additional worker-side execution is a major CPU ownership
difference, but this does *not* establish which individual worker function,
object allocation, snapshot or socket wait is dominant.

The original detailed ``cProfile`` attribution is **invalid**. Despite
separating simultaneous profiling in commit ``7e62e346``, the second run
contains caller stack frames in the worker profile and impossible negative
self-CPU entries (for example in caller ``_dispatch``). Python 3.13
monitoring callbacks with a thread-local CPU clock do not provide a
trustworthy cross-thread function profile in this configuration.
Do **not** use top-function CPU, source CPU groups or instrumented
phase CPU values from that run for a production optimisation decision.

The diagnostic tool was revised in commit ``a4d11e37``:

* ``tools/profile_qt_thread_short_query.py`` removes all ``cProfile`` use.
  Local wrappers record each caller/worker phase's inclusive wall and
  thread CPU clocks within that same function/thread. Nested timings
  overlap and are not additive.
* A cheap synthetic worker ``Operation.GET`` of PGconn status serves as a
  real request/response-handoff control without SQL or libpq socket wait.
  Direct status reads are also recorded.
* Clean pre-instrumentation SELECT and status controls remain first.
  The PowerShell runner, isolated benchmark venvs, production runtime and
  canonical paired v3 workload remain unchanged.

**Falsifiable diagnostic question:** how much of qt-thread's approximately
``130 us/query`` excess CPU over fork direct is explained by the fixed
request/response handoff rather than worker libpq execution/wait?
Compare clean ``status_control`` with clean ``unprofiled`` SELECT CPU and
the instrumented query/status phase counts and nonnegative CPU totals.
The status RPC is not the same operation as the fast-execute query; it
isolates the boundary but cannot alone prove a runtime improvement.

Completed diagnostic checkpoint -- CPU boundary attribution
-----------------------------------------------------------

The corrected Windows archive ``qt-thread-cpu-profile-20261103-192809-646.zip``
was captured at HEAD ``9b0f7108c9e2e3f5e9f84b9dccff8d62d31e12e6``.
The archive contains six valid profiles (two rounds each of pristine upstream
C, fork direct C and qt-thread) and a manifest: 3000 measured SELECTs after
200 warmups per run. The diagnostic runner uses the same isolated v3
benchmark environments, but **these are not canonical v3 acceptance ratios**.

Clean, pre-instrumentation SELECT 1 results (per run)::

    metric (us/query unless noted) upstream C     fork direct C    qt-thread
    process CPU                       88.5 / 130.2  125.0 / 145.8   255.2 / 250.0
    caller CPU                        88.5 / 130.2  125.0 / 145.8   114.6 / 135.4
    wall                              606.2 / 589.5 574.9 / 648.7   898.4 / 748.4
    context switches/query              1.00 / 1.01   1.00 / 1.02     3.12 / 3.03

Thus the qt-thread excess process CPU versus fork direct is approximately
104-130 us/query in these two rounds. The qt-thread process CPU comprises
about 115-135 us caller CPU and 115-141 us other-thread CPU. These
process-level differences are credible, but Windows CPU accounting has
coarse quantization: the direct in-process status read, for example,
shows ~0.17 us *wall* but 5.21 us *CPU* per call. Do not assign a
few microseconds of significance to individual CPU function counters.

The **clean no-SQL status GET control**, which forces one genuine
qt-thread Request/Response round trip but performs no SQL and no libpq
socket wait, measured::

    metric                              run 1         run 2
    qt-thread wall (us/GET)             45.13         40.02
    qt-thread process CPU (us/GET)      46.88         36.46
    qt-thread context switches/GET       2.04          2.01
    direct local status wall (us)        0.17          0.17

The phase instrument (not the clean control) reports one worker dispatch
per GET, with **35.24 / 39.22 us inclusive caller queue-handoff wall**
and only 5.83 / 6.39 us worker GET handler wall. This identifies the
**synchronous _dispatch / queue wakeup boundary** as the largest measured
fixed non-SQL phase, rather than the trivial status operation itself.
The GET process CPU of ~36-47 us is on the order of 35% of the
104-130 us excess SELECT CPU; this is a *scale comparison*, **not**
an additive decomposition or proof that precisely 35% of SELECT is queue
cost. The GET and fast-execute workloads are different.

For SELECT 1, the instrumented worker executes one _executePending and
one socket _wait per query. _wait includes approximately 539-581 us wall
and 73-83 us inclusive thread CPU, while its nested
_executePending/_handle clocks overlap. Most of its wall duration is
real socket waiting for PostgreSQL; neither its wall time nor inclusive
CPU can be labelled avoidable without a controlled local A/B.
Upstream's Windows waiting.py also chooses Python select.select()
instead of native wait_c because of documented CPU issues.
Do not repeat the previously rejected worker-native wait variant or
resnapshot/serialization experiments on this evidence.

**Decision -- CPU investigation COMPLETE, no runtime variant accepted.**
The next candidate is the narrowly measured fixed request/response
handoff, not a speculative rewrite of libpq socket wait.

**One falsifiable local hypothesis for coordinator review:** the timed
response-wait mechanism inside _ThreadTransport._dispatch
(SimpleQueue.get(timeout=0.1)) contributes a measurable fraction of the
~35-39 us no-SQL handoff wall cost, beyond the unavoidable two
cross-thread wakeups. First run a diagnostic-only same-machine A/B
of timed versus untimed SimpleQueue response waiting on the same
no-SQL status workload, without changing the production runtime.
The hypothesis is rejected if there is no repeatable reduction of
at least ~10 us per no-SQL round trip or if liveness/error delivery
cannot be preserved. An untimed production wait **must not** be
introduced without a design preserving fatal worker/close detection.
Only if targeted evidence supports a safe production variant may
a single local code experiment proceed through semantic smoke and
the unchanged paired v3 benchmark; CPU/query, cancellation, Qt
p95/p99, bulk, connection and resource deltas remain guardrails.

No runtime code was changed during this investigation. The
implementation owner should submit this completed profiling
checkpoint and proposed next direction to ``00 Coordinator``
for independent milestone review before starting a new runtime
optimisation direction.

Queue-response wait A/B -- REJECT, completed diagnostic
-------------------------------------------------------

The Coordinator-approved diagnostic was run on Windows at fork commit
``516ccc8ca2e627f3c1575933704077df88e60f07``; archive:
``qt-thread-queue-wait-ab-20261103-195939-054.zip``. Manifest and result
agree on branch ``feature/qt-process-pq``, CPython ``3.13.5``,
``PSYCOPG_IMPL=qt-thread``, C worker, eight alternating A/B pairs,
4000 warmed no-SQL requests per mode per pair, 300 warmup calls per mode,
and 8-second watchdog for isolated fault scenarios. The archive contains
``manifest.json``, ``results.json`` and an empty ``stderr.log``. All
eight pairs and six fault scenarios returned explicit results.
Production code and the canonical v3 benchmark were **not** modified.

Each pair uses the same connected worker and ``Operation.GET`` PGconn status
request/response path, changing only the caller's response wait between
``SimpleQueue.get(timeout=0.1)`` and raw untimed ``get()``.
Paired difference below is *timed minus untimed* (positive favors untimed)::

    Pair             1       2       3       4       5       6       7       8
    Wall delta (us) -0.865  -4.403  +1.304  +2.119  -3.192  -0.159  +0.190  +0.583

Predeclared gate and observed result::

    metric                                      observed           required
    median wall improvement                    +0.01546 us        >=10 us
    paired wall MAD                             1.08491 us         median >3*MAD
    range of paired wall delta                  -4.403 to +2.119  repeatable benefit
    pairs favoring untimed                       4/8               >=7/8
    median process CPU delta                     0 us              supporting
    median context-switch delta                 -0.001125/call     supporting

The observed wall improvement is practically zero and smaller than
measurement noise. ``hypothesis_passes_noise_gate=false``.
CPU and context-switch results show no repeatable benefit. No further
v3 benchmark was warranted, since no runtime candidate was created.

Isolated behavior tests:

* Both timed and untimed returned the expected remote ``AttributeError``,
  remained usable and closed normally. Post-close requests were rejected.
* On a deliberate worker exit **without sending a response**, timed
  waiting detected termination and returned ``OperationalError``.
* Raw untimed waiting **hung** in this scenario; the child was killed
  after the 8-second watchdog deadline.
* The harness reports ``failure_checks_pass=true`` because the expected
  *negative* untimed-liveness demonstration occurred. This does **not**
  mean raw untimed waiting is production-safe:
  ``untimed_detects_worker_exit=false`` and
  ``production_change_authorized=false``.

**Decision: REJECT the timed-vs-untimed SimpleQueue response-wait
optimisation hypothesis.** It fails the predeclared >=10 us/noise
criterion and cannot preserve fatal worker detection as a direct
production substitution. Do **not** rerun the same timed/untimed
variant or add it to production absent genuinely new evidence.
This negative experiment is retained here specifically to prevent
rediscovery.

The fixed ~40-45 us no-SQL request/response overhead reported by the
earlier CPU profile is still real, but these data show that the 100 ms
timed response wait is *not* its dominant avoidable component.
No conclusion about a different queue/scheduling architecture follows.
The next direction requires independent ``00 Coordinator`` review;
no ``optimized02`` code was created, and retained ``optimized01``
remains unchanged. CPU/query, cancellation, Qt p95/p99, bulk,
connect and resource guardrails remain applicable to any future
candidate.

Minimal SimpleQueue echo versus qpsycopg status GET -- completed
----------------------------------------------------------------

Coordinator selected a diagnostic-only two-queue echo comparison following the
REJECT decision for timed-vs-untimed SimpleQueue waiting. The Windows artifact
``qt-thread-echo-baseline-20261103-202519-685.zip`` contains ``manifest.json``,
``results.json`` and an empty ``stderr.log``. The manifest reports fork HEAD
``5dcdbd51a97a9707a5a5749daff7863e22b26d2a``, branch
``feature/qt-process-pq``, CPython 3.13.5, qt-thread / C worker,
eight alternating pairs, 4000 measured requests per mode/block, 300 warmups
per block and a 90-second watchdog. There are 16 valid blocks (8 of each
mode); the results contain no error or timeout. Production runtime,
accepted ``optimized01`` and benchmark v3 were not changed.

The comparison reuses one connected qpsycopg worker alongside a dedicated
minimal echo thread. Both modes use two ``SimpleQueue`` handoffs with a
``get(timeout=0.1)`` response wait. The echo passes a single immutable
token unchanged; qpsycopg issues ``Operation.GET`` of PGconn status without SQL.
Each paired difference below is **qpsycopg minus echo** (positive means
additional qpsycopg elapsed time or CPU)::

    metric / call                 echo median  qpsycopg median paired delta
    wall (us)                       18.4069       36.3672       +18.9571
    process CPU (us)                13.6719       35.1562       +19.5312
    caller CPU (us)                  9.7656       21.4844       +11.7188
    context switches                2.0109        2.0141        +0.0066

The per-pair wall deltas (us) are::

    pair         1       2       3       4       5       6       7       8
    GET-echo  +37.552 +35.600 +28.020 +20.250 +13.360 +17.664 +13.025 +13.966

Wall difference median +18.9571 us, paired MAD 5.7641 us,
range +13.0255 to +37.5517 us and 8/8 positive pairs.
The median echo/qpsycopg wall ratio of the separate medians is 0.506;
the median of per-pair echo/GET ratios is about 0.479.
The queue/scheduling echo accounts for roughly half the observed
GET-status wall latency **in this run**, not a universal hard minimum.

Process CPU paired difference median +19.5312 us, MAD 5.8594 us,
range 0 to +42.9688 us, 7/8 positive pairs. Caller CPU paired
difference median +11.7188 us but MAD 7.8125 us, range
-15.625 to +23.4375 us; per-thread Windows CPU accounting is coarse.
Context-switch delta median +0.006625/call (MAD +0.005875,
range -0.0115 to +0.112); both modes are about two context
switches/call, so the residual does **not** demonstrate extra thread
handoffs.

**Important drift:** both raw timings decline markedly during the
paired run, with GET-echo difference median ~31.81 us in the first
four pairs versus ~13.66 us in the last four. Alternating order
limits simple order bias, but does not eliminate temporal/host drift.
Thus +18.96 us is a descriptive within-run paired median, **not**
a stable, precisely attributed cost or guaranteed optimisation budget.
Comparing this artifact numerically to the earlier ~40-45 us GET control
also requires allowing for changed warm host conditions.

**Conclusion: bounded diagnostic COMPLETE; no runtime change supported.**
A minimal two-queue / two-wakeup exchange has a substantial fixed
baseline, while the full qpsycopg GET has an additional consistently
positive cost in these eight pairs. The residual includes necessary
status access, Request/Response construction, transport locking,
liveness checks, exception handling, caching and worker bookkeeping.
These are *not separately measured here*, and no individual function
or object allocation is proven dominant or removable. The rejected
timed-vs-untimed response wait remains REJECT.

A possible next **diagnostic** (not yet authorised) is a progressively
enriched echo with matching Request/Response representation and
worker validation, comparing it against the bare-token echo in
interleaved warmed runs. This could falsify whether Python-side
protocol/bookkeeping materially explains the residual without
touching production. It must avoid claiming a safe optimisation
until it measures individual costs and preserves liveness semantics.

Record this completed significant profiling checkpoint for independent
``00 Coordinator`` review before beginning a new optimisation direction.
No ``optimized02`` candidate, semantic change or acceptance v3 run
was created for this diagnostic.

Enriched SimpleQueue echo -- measured, diagnostic COMPLETE
----------------------------------------------------------

Independent ``00 Coordinator`` approved progressively enriched echo
diagnostics after the previous GET-minus-bare echo result had shown
persistent positive overhead but severe temporal drift. The completed
Windows archive ``qt-thread-enriched-echo-20261103-205855-365.zip``
has a valid ZIP CRC, ``manifest.json``, ``results.json`` and an
empty ``stderr.log``. SHA256 of the uploaded ZIP:
``431a523188cf1929d053107c9235616bc37639ed1380b002ef1cfb9baa4b6763``.
The manifest records HEAD
``d1b6a5a1899ac5690961a2ff335b96debd24cb6e``,
branch ``feature/qt-process-pq``, CPython 3.13.5,
qt-thread with C worker, four independent subprocess repeats,
16 cycles per repeat, 600 calls per block, 250 initial
warmup calls per mode and a 90 s watchdog/repeat. All
four child processes returned successfully, yielding 64 complete
six-block cycles and 384 measured blocks (230,400 total calls).
No production runtime code or canonical v3 benchmark changed.

Each short cycle brackets four rotated/reversed stages with a
minimal two-SimpleQueue token echo **before and after**; each
stage is compared to a timestamp-interpolated bare control.
Each mode occupied each ordinal position exactly four times
within each independent repetition. This limits first-order
within-cycle temporal bias, but cannot remove nonlinear drift,
host scheduling variation or unbalanced worker activity.

**Wall results, us/call above interpolated bare echo.**
Each column is the median of 16 cycles of one independent
subprocess repeat (not a pooled pseudo-sample)::

    mode                               repeat 1  repeat 2  repeat 3  repeat 4
    Request/Response envelope          +3.209    +4.072    +3.990    +4.722
    envelope + validation              +4.084    +5.316    +5.138    +5.210
    synthetic bookkeeping              +9.673    +8.731    +9.635    +9.204
    actual qt-thread GET status        +13.505   +14.148   +14.195   +14.537

Across-repeat medians for the four **independent repeat medians**:
envelope +4.031 us (MAD 0.366); validation stage total
+5.174 us (MAD 0.089); bookkeeping total +9.420 us
(MAD 0.235); actual qpsycopg GET total +14.172 us
(MAD 0.195). The last figure differs from the earlier
~18.96 us GET-minus-echo diagnostic because the new
shorter/bracketed experimental design and host conditions
differ; it is not a new runtime speedup.

**Incremental-stage wall differences**, independently computed
as within-cycle differences and *then* summarized per repeat::

    delta                                repeat 1  repeat 2  repeat 3  repeat 4
    validation minus envelope           +0.676    +0.569    +1.033    +1.140
    bookkeeping minus validation        +5.205    +4.019    +4.155    +4.212
    actual GET minus bookkeeping        +3.410    +5.133    +5.185    +4.899

Across-repeat median increments are +0.855 us validation,
+4.184 us bookkeeping and +5.016 us actual GET above the
synthetic bookkeeping proxy. Across **all 64 within-cycle
comparisons**, envelope/bare was positive in 60/64,
validation/envelope in only 44/64,
bookkeeping/validation in 61/64, and
GET/bookkeeping in 59/64. Thus the validation increment
is weak relative to within-cycle variability; even the
more consistent ~4-5 us stage increments are *synthetic
diagnostic signals*, not proof of removable implementation
work. Medians of nested stage differences need not
add exactly to medians of their totals.

**Temporal drift:** first-to-last bare baseline changed
+131.77% in repeat 1 (15.486 -> 35.892 us/call),
+6.50% in repeat 2, +3.53% in repeat 3,
and +6.70% in repeat 4. One of four repeats
exceeded the predeclared 15% drift flag. Even repeat
2 and repeat 3 had transient bare-control spikes
within individual cycles. Timestamp-based linear
bracketing improves comparison but cannot guarantee
a stationary or causal cost decomposition.
Do not use the first repeat's raw absolute timings
as an optimisation reference.

**CPU and context-switch limitations:** 600-call
Windows process/thread CPU counters are quantized:
some incremental CPU medians are exactly zero,
some implied other-thread CPU deltas are negative
after subtraction, and repeated caller CPU estimates
disagree. They cannot localize several microseconds
of Python work reliably. The per-stage
context-switch difference stays near zero;
no new scheduling boundary is established.

**Decision: diagnostic COMPLETE; no production candidate.**
A no-SQL status round trip costs materially more than a
bare two-queue token echo. The progressive synthetic
analogue suggests representation and bookkeeping each
have a positive measurable wall effect. But the
bookkeeping stage is *not* real qpsycopg code: it has
a fake PGconn, simplified state, synthetic notices,
and different validation/cache/exception paths.
The remaining ~5 us real GET-minus-proxy gap
likewise cannot be assigned to one function.
No single actual qpsycopg allocation/check or
semantically removable operation was identified,
and no SQL-query speedup is justified. The
earlier timed-vs-untimed response wait remains
**REJECT** for no speed signal and lost worker-exit
detection; ``optimized01`` remains **RETAIN**.
Neither ``optimized02`` nor a v3 acceptance benchmark
is warranted absent a runtime variant.

**Proposed next decision for Coordinator:** treat this
as a bounded attribution plateau; choose whether
further profiling should instrument *real* qpsycopg
GET status / worker bookkeeping in a new narrowly
controlled diagnostic, or whether independent
exploration is warranted. Do not stack another
synthetic enriched echo or repeat prior rejected
native wait, WAIT-prefetch or serialization work
on these data alone.

**Evidence locality:** the ZIP was supplied as a
conversation attachment and is not a committed
GitHub artifact. Preserve the user's original
``test-results\qt-thread-enriched-echo-20261103-205855-365.zip``;
the measured summary and SHA256 are committed here
for future repository-first recovery. There are
no production runtime modifications in this stage.

Real qt-thread GET status -- measured diagnostic, no actionable target
----------------------------------------------------------------------

Coordinator approved a bounded **real-path, diagnostic-only**
``GET status`` phase investigation at
``0fc10d668ac9166f07ae3a575049da7b4b14ccd1``.
The completed Windows archive is
``qt-thread-real-get-profile-20261103-213627-044.zip``, SHA256
``05a5887fff00d8873cca977fedf84f0d9063ea3a0a6dde75edec9c78646ce558``.
It is a **conversation-uploaded / Windows-local artifact**, not a
GitHub-committed binary. Preserve the original ZIP. The archive CRC
passed; it contains a manifest, valid results JSON and empty stderr.

The manifest records fork commit
``1c11f6525bfc75e3db27aeeae65c0498a56df4cd``,
``feature/qt-process-pq``, Python 3.13.5, qt-thread
with C worker, four independent subprocess repetitions,
12 ``clean-profiled-clean-profiled-clean`` cycles/repeat,
1200 warmed GET status calls/block and a 90-second
child watchdog. All **48 cycles / 240 blocks**
completed; **115,200 profiled calls** and
172,800 clean-control calls, excluding warmup.
No errors or watchdog timeouts occurred. All twelve
expected phase counters registered **exactly once
per profiled GET** in every repetition. No production
runtime or canonical benchmark-v3 file was changed.

Clean controls use one original qt-thread connection
with unwrapped methods and raw queues. Profiled blocks
use another real connection with diagnostic-only
method wrappers and worker request/response queue
observers. All wrappers are local to the diagnostic
process; the original worker ``_run`` algorithm executes.
The worker-run observation covers request receipt to
*before* response queue publication. Calls are paired
against timestamp-interpolated neighboring clean controls.
This methodology introduces considerable overhead and
cannot support exact function self-CPU attribution.

**Actual clean vs profiled wall measurements (us/GET)**::

    independent repeat                  1       2       3       4
    clean-control block median       30.999  30.266  30.079  29.878
    instrumented block median        50.752  49.063  49.178  48.248
    median paired profiled-clean     18.082  18.348  18.740  18.270
    clean baseline start/end drift   +2.51%  -4.25%  +61.18% -5.04%

All **96/96 profiled blocks** had a positive difference
from their bracketed clean controls. Across four
independent-repeat median paired differences,
the wall excess is +18.309 us (MAD 0.133 us;
range +18.082..+18.740 us). The excess is an
**instrumented-vs-clean measurement difference**,
not a removable production cost. In addition to
wrapper/timer/queue-proxy overhead, separate
control/profiled connections may differ. The
third repetition's +61.18% first-to-last
baseline drift and transient within-repeat
spikes prevent treating the system as stationary.

**Inclusive phase wall-time**, median of four
independent pooled per-repeat estimates (us/GET)::

    caller request                    50.906
      caller _perform                 48.311
        caller _dispatch              38.412
      caller _responseValue            3.504
        caller _restore                0.991
      caller _wire                     1.820
    active worker _run turn           14.137
      worker pool _handle              6.264
        worker base _handle            3.965
          worker _normalize           1.298
      worker _connectionReady          0.816
      worker _connectionBroken         1.018

The visual indentation is illustrative only; these
are **inclusive, overlapping timings**, not an
additive decomposition. In particular ``_dispatch``
waits for the worker and overlaps its wall-time;
``_run`` timing excludes idle request waiting and
the response queue publication/wakeup. These phase
values include instrumentation overhead, so a
``_dispatch`` wall reading above the 30 us clean
GET baseline **is not evidence of a larger
removable cost**. No single measured handler,
status check or restore operation can credibly
explain or recover the full qpsycopg-vs-echo gap.

**Windows thread CPU is not reliable at the
per-method scale.** For all four repeats,
worker ``_handle``, ``_normalize``,
``_connectionReady``, ``_connectionBroken``
and caller ``_responseValue``, ``_wire``/
``_restore`` recorded **0 us** inclusive thread
CPU after pooling 28,800 calls per repeat.
The worker _run-turn CPU was only 0 or
0.543 us/GET, reflecting coarse Windows
clock accounting, not negligible actual work.
Paired profiled-clean process CPU medians
ranged approximately +13.0..+19.9 us/GET,
but these include probe overhead. The
context-switch delta median across repeats
is about +0.0069/call, no evidence of
an extra scheduling handoff.

**Decision: COMPLETE -- NO ACTIONABLE RUNTIME
TARGET.** The diagnostic confirms that
real GET path work consists of nested caller
queue waiting/response handling and worker
request/bookkeeping; it does **not** produce
a defensible *exclusive* cost for a
specific semantically removable operation.
Skipping worker health checks, error/notice
processing or response validation would
require its own correctness rationale and
controlled production A/B; nothing here
justifies that change. There is no
supported ``optimized02`` hypothesis.
``optimized01`` remains RETAIN.
Timed-vs-untimed queue waiting, native
worker socket waiting, speculative WAIT
prefetch and generic serialization/snapshot
experiments remain rejected.

No new runtime changes, semantic smoke
or adjacent-paired v3 acceptance benchmark
were required for this negative
diagnostic-only checkpoint.
The next direction belongs to
**independent 00 Coordinator review**:
consider whether the present qpsycopg
GET/echo profiling line has reached an
attribution plateau, and whether a
different evidence-backed target or
independent Scout is warranted. Avoid
another heavier instrumentation layer
without a falsifiable reason to expect
better production attribution.

Native PGresult two-queue handoff -- feasibility preflight pending
------------------------------------------------------------------

Independent Scouts A and B were reviewed by the existing
``00 Coordinator``; the selected **INVESTIGATE** direction
is one diagnostic-only native-result boundary screen at
base HEAD ``98a6ad178d03ff0ad94f1b28c6d3ddac6cd164a1``.
This does not authorise runtime changes or ``optimized02``.

Source-based findings at the reviewed HEAD:

* PostgreSQL 18 libpq explicitly permits completed, normally
  read-only ``PGresult`` objects to move between threads, while
  forbidding concurrent manipulation of the same ``PGconn``
  (or simultaneous mutation of a single ``PGresult``):
  https://www.postgresql.org/docs/18/libpq-threading.html.
* ``psycopg_c/psycopg_c/pq/pgresult.pyx`` owns a
  ``libpq.PGresult*`` and invokes ``PQclear`` in
  ``clear()``/``__dealloc__``. Native C
  ``_psycopg.Transformer.set_pgresult`` accepts exactly
  native C ``psycopg_c.pq.PGresult``.
  The current qt-thread client chooses the Python/native
  hybrid Transformer because its PGconn is synthetic;
  that does **not** by itself establish native
  Transformer compatibility with a qt-thread cursor's
  adaptation context.
* The native C connection's notice receiver constructs a
  **borrowed** result wrapper whose pointer is set to NULL
  after its callback. It must never be enqueued as an owned
  cross-thread native result. Existing worker notice
  snapshots remain necessary.
* Worker ``_executePending`` creates ``ResultSnapshot``
  and ``PackedValues``. The worker's
  ``_completedHealthy`` and ``_refreshesParameters``
  inspect ``ResultSnapshot`` specifically, while
  caller ``_responseValue``/``_restore`` and the
  hybrid cursor adaptation expect reconstructed
  results. Therefore a plain identity replacement
  of ``_snapshotResult`` is **not** a semantics-safe
  production design. FATAL_ERROR, SET/RESET/DISCARD,
  pipeline, COPY, notices, error/cache state and
  cancellation/close must continue to work.

The newly committed ``tools/preflight_qt_thread_native_pgresult.py``
and ``tools/preflight_qt_thread_native_pgresult.ps1``
perform a **separate safety/compatibility preflight**
inside a watchdog-protected subprocess.
A native C PGconn is created, used and finished **only in
the worker thread**. Completed native PGresults are sent
through two SimpleQueues to the caller. The caller compares
native and current snapshot metadata, NULLs, values,
SQLSTATE exception classification and decoded rows with
the **same qt-thread cursor adaptation context**, using
native C versus current hybrid Transformer. It additionally
tests a result surviving worker PGconn.finish(), explicit
double clear/destructor, and preserves notice diagnostics
by snapshotting the borrowed callback result before return.
The production qt-thread worker, Transformer selection,
benchmark v3 and SQL execution implementation are untouched.

**Preflight status: BLOCKED; forensic crash localized to invalid probe row decoding.**
The first Windows attempt (details below) did not reach a Python
PASS or a meaningful semantic assertion; it ended in a native
process crash. The entire native-handoff performance A/B stays
blocked. Source plausibility is not empirical feasibility.

First Windows preflight attempt -- native access violation
----------------------------------------------------------

Uploaded ZIP: ``qt-thread-native-result-preflight-20261103-223124-116.zip``.
SHA256: ``8a3e2324bb51a5297b63d1c7ab1b424320c7c799c13a18265c15ce56689b58f7``.
Manifest confirms ``dc25abe6c1e98c9c349f5ce9596f99a93d2ba5b9``,
branch ``feature/qt-process-pq``, CPython 3.13.5,
qt-thread / C worker, 40-second watchdog and unchanged runtime/v3.
ZIP CRC passes. ``results.json`` records Python diagnostic
exit 2 because its **child process** returned Windows
``3221225477`` (``0xC0000005``, access violation).
The captured child's stdout/stderr and ZIP ``stderr.log``
are all empty. This was **not** a watchdog timeout and there
are no completed per-case results, so the crash location,
PGresult lifetime and native Transformer parity are
**unknown**. Treating this as proof that the proposed
architecture is impossible would be premature; treating
it as a PASS would be unsafe.

**Bounded forensic correction, diagnostics only:** commit
``ad3fc11b`` added ``faulthandler`` and flushed stage
markers around native connect, worker exec/snapshot,
queue response, metadata, both Transformers, clear,
notice callback and connection finish. Commit
``e0189753`` preserved the last marker, Windows
hex exit code and an extended child stderr stack
in ``results.json``. These changes do not alter
the native calls, production source or v3 benchmark.

The forensic rerun was performed and localized the crash,
as recorded in the second-attempt section below. Do not repeat
the old diagnostic with invalid row decoding. The >=15 us
CPU/SELECT material screen remains deferred; no
``optimized02`` candidate exists.

Second Windows native preflight -- fatal-error row decoder misuse
-----------------------------------------------------------------

Forensic artifact:
``qt-thread-native-result-preflight-20261103-224615-903.zip``,
SHA256
``b476c81827bccc0d93e0b445ce2128290d9f7168b3834aaf5e66a92119fb4fcc``.
ZIP CRC passed. The manifest records ``9e5ddb547b6d0d2a0c605aa939f7f908d9591643``,
``feature/qt-process-pq``, CPython 3.13.5,
qt-thread / C worker, 40-second watchdog and no
production/v3 change. Child returned Windows
``3221225477`` (``0xC0000005``) again;
``results.json`` carries the flushed stage trace
and ``faulthandler`` stacks. It was **not**
a watchdog expiry. The separate ZIP ``stderr.log``
is empty because child stderr is in the JSON error payload.

**Localization:** the first ``select`` case completed native C
``PGresult`` handoff, metadata/snapshot parity,
both native C and hybrid Transformer row loads,
explicit double ``clear()``, GC and end-of-case checks.
The ``sql_error`` case then completed worker
``exec_(b"SELECT 1 / 0")``, snapshot and two-queue
handoff; both metadata and SQLSTATE exception checks
passed, and the native C Transformer ``load_rows()``
returned. The last flushed marker was
``caller: before hybrid Transformer.load_rows``;
the current thread's crash stack points to
``psycopg/_transformer.py:84``, calling
``_bulkLoadRows`` from ``load_rows(0, 0)``
on a ``FATAL_ERROR`` result without tuple columns.

**Root-cause classification: diagnostic misuse, not a
demonstrated cross-thread PGresult safety failure.**
Production ``BaseCursor._check_results()`` detects a
``FATAL_ERROR`` and raises before attempting row
decoding; the preflight incorrectly applied normal
row decoding to that error result. The hybrid
bulk-row helper's crash for invalid input is
not evidence that native result transfer failed.
It is not a general confirmation of native
safety either: notice, PGconn.finish and their
associated clear/lifetime checks had not run.

A minimal **diagnostic-only** correction in commit
``54bccf73`` retains native/snapshot metadata and
SQLSTATE error parity but only calls either row decoder
for tuple-bearing, nonempty ``TUPLES_OK`` results.
``FATAL_ERROR`` and ``COMMAND_OK``/notice results
are no longer submitted to ``load_rows()``.
A per-case ``row_decoder_checked`` flag and
end-of-test assertions ensure that real SELECT and
PGconn-finished result parity must still pass,
whereas error/DO cannot masquerade as row-decoding
checks. The same existing two queues and
watchdog are retained. No production source,
C extension or benchmark v3 was changed.

**Gate: STILL BLOCKED — corrected Windows feasibility
rerun required.** After synchronizing the branch,
execute the existing preflight runner once::

    .\tools\preflight_qt_thread_native_pgresult.ps1

Review its new ZIP here before any native SELECT
performance A/B. If it reveals an actual
ownership/Transformer/notice lifetime failure,
STOP, document and refer to Coordinator.
If the corrected preflight passes all cases,
that establishes only the bounded standalone
feasibility gate; a separate full current-vs-native
boundary A/B and the >=15 us CPU/SELECT
screen would still be required.

Native PGresult feasibility PASS -- whole-result-boundary A/B staged
--------------------------------------------------------------------

The corrected **third Windows preflight** completed at
``c303749b6b1551901eb5fc7bf4c2bcaeb2f2ae56``, after the
earlier forensic crash had been traced to erroneous row decoding on
a FATAL_ERROR result. Artifact:
``qt-thread-native-result-preflight-20261103-225359-731.zip``,
SHA256 ``590ae47053e5b117d75ed596fceb23b71e50c78b37e97797162deac842b5145f``.
The archive CRC passed, ``stderr.log`` was empty, and ``results.json``
reported ``preflight_passed=true``,
``native_handoff_ab_authorized=true``, CPython 3.13.5,
``PSYCOPG_IMPL=qt-thread``, native C worker, no production
or canonical v3 change. This ZIP is a conversation upload/
Windows-local artifact, **not** a committed repository file.
Preserve it for review.

The four tests completed:

* ``select``: metadata/field/cell/NULL parity,
  native C and hybrid Transformer decoding with the **same**
  qt-thread cursor adaptation context, expected row
  ``(1, None, "qpsycopg", True)`` and idempotent
  double native ``clear()``.
* ``sql_error``: FATAL_ERROR status and matching SQLSTATE,
  exception type and message; the corrected probe
  **does not** decode non-row error results.
* ``notice``: command/no-row metadata parity, a preserved
  notice snapshot created during the borrowed native
  callback, no transfer of the callback's invalid pointer,
  and double clear.
* ``connection_finished``: a completed native result
  remains usable on the caller thread **after worker
  PGconn.finish()**, decoded row ``(17,)`` and double clear.

**Decision: PASS the *bounded standalone* native C PGresult
ownership, lifetime, notice and basic adaptation preflight.**
It is **not** proof that a production qt-thread worker can
skip snapshot-specific health/error/cache logic or retain
pipeline, COPY, multi-result, cancellation and close semantics.
The prior Windows crashes are retained as historical
diagnostic misuses, not architectural FAIL results.
Their older BLOCKED statements are superseded by this PASS.

The selected next bounded stage is a single diagnostic-only
current-vs-native **complete result-boundary** CPU screen.
New tools ``tools/bench_qt_thread_native_result_boundary.py``
and ``tools/bench_qt_thread_native_result_boundary.ps1``
have been committed; **Windows A/B results are pending**.
They use one native C PGconn, created, used and finished
only inside one worker thread, plus two SimpleQueues
and a single fixed qt-thread cursor adaptation context.
Both modes run the **same SQL and server execution**:
``PGconn.exec_params(b"SELECT 1", ())`` on that same
worker connection with identical request/response waits.
The *only* alternative processing boundary after the
completed C PGresult is:

* current control: worker native ``_snapshotResult``,
  including metadata/``PackedValues``; synthesize
  ``pq_qt_process.PGresult`` on caller; create a fresh
  hybrid ``adapt.Transformer(cursor)`` and decode row;
* native alternative: enqueue the owned C PGresult,
  create a fresh ``psycopg_c._psycopg.Transformer(cursor)``,
  decode the same row, then explicitly release native
  result ownership in the caller.

Both paths verify exactly one integer row ``(1,)``,
and both include native result release in their respective
full-boundary lifetime. The worker and SQL code are the
same, and the two queue handoffs are unchanged.
The result cannot be directly equated with the full
production asynchronous qt-thread SELECT 1 path:
the experiment uses synchronous native PQexecParams,
does not execute production health/notice/cache machinery,
and holds the cursor context on a separate idle connection.
Only the **incremental result-boundary difference** is
being screened; never infer complete SQL latency savings.

**Drift and noise controls:** four independent subprocess
repeats; each contains 12 short alternating five-block
cycles with 1000 calls/block and warmup. The modes rotate
``snapshot-native-snapshot-native-snapshot`` versus
``native-snapshot-native-snapshot-native``,
so workload and order balance across cycles. For each
target block, interpolate surrounding opposite-mode
controls using actual block midpoint times. Report
paired ``snapshot minus native`` process CPU/query,
wall/query, caller/other-thread CPU, context switches,
MAD/range/sign count; preserve raw blocks and between-repeat
medians. Report per-mode first/last wall drift.

**Predeclared material-benefit screen:**
at least **15 us process CPU/SELECT** in the
independent-repeat median, at least 3 of 4
repeat medians >=15 us, all repeat medians
positive, median >3 times between-repeat
MAD, and no per-mode repeat >15% baseline
drift. These conservative checks are a
screen rather than a statistical proof.
If the observed benefit is below the
threshold or lost in noise/drift, **REJECT**
without a runtime candidate. A positive
diagnostic does not authorise production
code changes, ``optimized02`` or a v3
benchmark until independent Coordinator
review and an actual semantics-preserving
design.

Run once from the synced Windows checkout::

    .\tools\bench_qt_thread_native_result_boundary.ps1

Upload ``test-results\qt-thread-native-boundary-ab-*.zip`` to
the **current Implementation chat** for evaluation,
documentation and Coordinator routing on completion.

Native PGresult complete result-boundary A/B -- REJECT on drift/noise gate
--------------------------------------------------------------------------

**Completed diagnostic (no runtime candidate).** The previously passed
standalone native-result preflight authorised one diagnostic-only whole
result-boundary comparison, not a production branch. The Windows
artifact is ``qt-thread-native-boundary-ab-20261103-230648-799.zip``,
SHA256 ``997eb072dbaf3a148ef886fe3a353e1ddd16c6ca0a028605d7eb04855109f6c3``.
The ZIP's CRC passes and contains ``manifest.json``,
``results.json`` and empty ``stderr.log``. This is a
user-uploaded/Windows-local ZIP, **not committed to GitHub**.
Preserve the original artifact for future independent reanalysis.

Manifest: captured branch HEAD
``f19aafcee7d07441e704d22567f0e4abae780c74``,
``feature/qt-process-pq``, CPython 3.13.5,
``qt-thread`` with C worker, four independent child
process repeats, 12 five-block cycles/repeat,
1000 measured SELECT 1 calls/block (plus warmup),
120 s watchdog per child. All four children finished,
with 48 cycles / 240 measured blocks / 240,000
actual query executions, and 96 locally bracketed
comparisons. There is no reported crash, timeout
or result mismatch. The script asserts the decoded
``(1,)`` row on every operation. It records no
production/v3 edits and does not authorise
``optimized02``.

Both modes run **identical** worker-thread
``native_pq.PGconn.exec_params(b"SELECT 1", ())``
on the same worker-owned PGconn and exchange
requests/results through two ``SimpleQueue``
instances with the same response timeout.
Both use the same caller qt-thread cursor
adaptation context and fresh Transformer,
but different completed-result boundaries:
current ``_snapshotResult`` / ``PackedValues``
→ synthetic PGresult → hybrid Transformer,
versus owned native C PGresult → C Transformer.
Worker/client result ownership and release
are included in each path. The benchmark is
not the full production asynchronous
``qt-thread`` SELECT path or canonical v3,
and does not exercise all worker health,
cache, notice, COPY, pipeline or
cancellation semantics.

**Measured paired difference:** all values
below are **snapshot minus native**;
positive means native is faster/uses less
process CPU. Each figure is the median
of 24 adjacent timestamp-interpolated
comparisons within one independent
repeat, not an independent 96-sample
population::

    repeat       CPU/query    wall/query   CPU positive   wall positive
       1         +31.276 us    +35.380 us       16/24           17/24
       2         +54.671 us    +51.540 us       20/24           20/24
       3         +29.817 us    +77.012 us       17/24           20/24
       4         +38.567 us    +25.192 us       18/24           15/24

Across four **independent repeat medians**:
process CPU difference +34.921 us/SELECT,
MAD 4.375 us, range +29.817..+54.671 us;
wall difference +43.460 us/SELECT,
MAD 13.174 us, range +25.192..+77.012 us.
All four independent CPU median signs
are positive; across individual pairs,
CPU difference is positive in **71/96**
and wall difference in **72/96**.
Caller thread CPU delta between repeats
has median +23.896 us; other-thread
CPU delta median +17.357 us. Both are
noisy and need not sum to the independent
median process CPU result.
Context-switch delta median +0.0091
switches/SELECT, near zero in practical
terms and not evidence of a changed
scheduling topology.

**Noise and drift:** the per-pair CPU
MAD within individual repetitions
was **43.569, 34.974, 30.117 and
42.770 us/SELECT** respectively, i.e.
comparable to the size of the observed
effect. The modes themselves had
absolute wall medians roughly
630..748 us/SELECT, reflecting
synchronous native PQexecParams,
not previously measured async
qt-thread short-query latency.

The predeclared first-to-last
**mode-specific wall drift** (median
of first vs last three cycles) was::

    repeat       snapshot             native
       1           -19.59%             -17.06%
       2            -2.08%              +3.41%
       3           +52.64%             +33.87%
       4           +55.18%             +16.29%

Thus **3/4 independent repeats** breach
the predeclared 15% drift ceiling;
only repeat 2 stays within it.
Large local fluctuations also remain
despite five-block counterbalancing
and timestamp-bracket interpolation,
which corrects approximately linear
local drift, not nonstationary host
contention. Windows process CPU
accounting is quantized at the block
scale. A positive paired effect is
not enough to assert a reliable,
portable CPU savings magnitude.

**Predeclared material-benefit screen: FAIL.**
The threshold >=15 us CPU/SELECT,
positive median in every repeat,
>=3/4 repeat medians above threshold,
and independent-repeat median
exceeding 3x MAD all pass. The
separately required **no mode/repeat
with >15% wall drift** condition fails.
The script's own ``predeclared_screen.passes``
is ``false``. This condition was set
**before** capture; it must not
be weakened post hoc because the
observed median CPU delta is positive.

**Decision: REJECT this native-handoff
performance screen as insufficiently
stable for a runtime target.**
This is **not** a finding that native
PGresult transfer is impossible or
intrinsically slow; the standalone
feasibility preflight remains PASS.
It is also not evidence of an
end-to-end qt-thread SQL improvement.
No runtime candidate, ``optimized02``
or new benchmark-v3 baseline is
authorised by these measurements.
Do not repeat the same A/B merely
to obtain a cleaner or more favorable
number without independent new
evidence and direction review.

Next owner: **existing 00 Coordinator**,
for independent review of this
completed significant negative
checkpoint. Coordinator should
choose a documented STOP/plateau
or a *different*, evidence-backed
bounded target, taking the positive
but nonqualifying directionality
and high drift into account.
The active Implementation chat
remains sole ordinary runtime
owner until any explicit transfer.

Post-optimized01 priorities
---------------------------

``optimized01`` remains **RETAIN**; historical
timed/untimed queue waiting, native worker
wait, speculative WAIT prefetch, GET/echo
attribution and generic snapshot microtuning
remain rejected/completed. The standalone
native C PGresult handoff feasibility gate
passed, but the entire diagnostic-only
native-result boundary SELECT 1 A/B
**FAILED its predeclared material-benefit
screen** due to severe temporal drift.
Do not mistake a positive median for
accepted production speedup.

#. Route measured evidence and this
   **REJECT** checkpoint to existing
   ``00 Coordinator`` for independent
   review before any new optimisation
   direction.
#. Do not rerun the same native boundary
   A/B or relax its drift condition without
   a new, independent, falsifiable reason.
   Preserve both the positive CPU direction
   and its noise/drift limitation.
#. No ``optimized02``, production runtime
   change, canonical benchmark-v3 change
   or speculative generic snapshot tuning.
#. Any new Coordinator-selected candidate
   must first address public Psycopg
   semantics and worker health, errors,
   notices, close, cancellation, pipeline,
   COPY, multi-result and non-C fallback,
   then pass qt-thread semantic smoke and
   unchanged adjacent-paired v3 with
   CPU/query, Qt p95/p99, bulk, resource
   and connection guardrails.

Checkpoint rule
---------------

A runtime optimisation is considered supported only when:

* the benchmark workload/hash is compatible;
* upstream was rerun adjacent to the tested fork mode;
* the paired ratio moves in the intended direction by more than the observed
  paired-ratio noise, or the change has a directly expected architecture delta;
* correctness smoke still passes;
* unrelated important metrics do not regress materially.

If the benchmark definition changes again, capture a new named baseline before
continuing numbered optimisation checkpoints.
