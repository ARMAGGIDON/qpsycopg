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


CPU/short-query profiling checkpoint -- diagnostic probe pending
--------------------------------------------------------------

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

The revised probe has **not yet run** on the target Windows machine.
Do not select a dominant function or start speculative ``optimized02``
until focused evidence is internally consistent. Any later candidate
must pass semantic smoke and unchanged paired benchmark v3, checking
CPU/query, cancellation, Qt p95/p99, bulk, connect and resources.

Next diagnostic command from the Windows checkout::

    .\tools\profile_qt_thread_short_query.ps1

Optimisation order after optimized01
------------------------------------

Do not optimise every visible ratio at once. After ``optimized01``:

#. compare connection/resource deltas and confirm smoke remains clean;
#. if the lifecycle change is supported, target the remaining qt-thread short
   query CPU/latency overhead rather than bulk decoding;
#. keep bulk decode deprioritised while it remains near upstream;
#. choose later worker reuse, thread-native command/response or cancellation
   transport work only from measured remaining cost.

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
