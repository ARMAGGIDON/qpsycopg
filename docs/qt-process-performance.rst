Qt process performance notes
============================

This document records the measured performance history of qpsycopg's
``qt-process`` backend and the evidence which redirected the project toward the
experimental ``qt-thread`` architecture.

The canonical worker for process-backed measurements is
``QPSYCOPG_WORKER_IMPL=binary``.

Performance target
------------------

For frequent database operations the target is approximately ``1x`` normal
Psycopg binary behaviour while retaining the Qt/process-isolation semantics the
experiment is testing.

Do not optimise a metric in isolation. Current comparisons include:

* wall latency (median/p95 and selected tail measurements);
* bulk throughput;
* parent + worker CPU;
* context switches;
* process-tree Working Set/RSS and Private Bytes;
* handles, threads and process count;
* cancellation latency;
* Qt timer responsiveness;
* connect/startup behaviour.

This is important because an earlier parent-only RSS comparison made qt-process
appear to use less memory than vanilla while the worker held a substantial part
of the real process-tree footprint.

Benchmark methodology
---------------------

``tools/test.ps1`` currently performs repeated qpsycopg runs but only one fresh
vanilla sentinel for an unchanged workload. ``tools/vanilla_baseline.py`` scans
compatible historical benchmark archives and constructs a reusable vanilla
baseline using robust median/MAD statistics and sample counts.

Compatibility is keyed by workload details such as worker implementation,
iterations, row count, thread count and ``benchmark_workload_version``. A new
vanilla baseline is still required when the workload changes or when a new metric
was not collected by older archives.

Resource measurements use the process tree for qt-process. Parent-only memory or
CPU is useful for phase attribution but is not a valid architecture-level total.

Checkpoint map
--------------

The preserved work contains several different kinds of checkpoints:

* ``optimizedNN``: full/end-to-end numbered benchmark checkpoints;
* transport/native validations: same-run paired A/B used to make local design
  decisions under the same environment;
* architecture validations: direct vs dedicated-thread vs process comparisons.

The latest full numbered checkpoint in the preserved handoff is ``optimized35``.
The branch contains later retained changes and targeted validations, so
``optimized35`` is not a complete description of current HEAD performance.

Major performance milestones
----------------------------

Native result packing -- optimized15
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The original large-result snapshot path built ``PackedValues`` in Python by
calling ``PGresult.get_value()`` for every cell.

For the 100k x 3 benchmark the measured value-packing cost changed roughly from::

    Python snapshot values     ~108.9 ms
    native snapshot values       ~5.1 ms

This was about a ``21x`` improvement in the packing phase and removed the first
major large-result bottleneck. The native implementation was placed in shared
Cython ``PGresult`` source so locally generated ``psycopg_binary`` can expose the
same capability. The wire layout stayed unchanged.

Cancellation isolation and WAIT batching -- optimized16
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Cancellation originally shared the main transport lock with query WAITs.
Collapsing cancel chatter alone was insufficient because cancellation still had
to wait for the query transport.

A dedicated authenticated cancellation control channel removed that dependency.
Representative targeted cancellation measurements reached roughly::

    qpsycopg cancel_call    ~6.0 ms
    vanilla cancel_call     ~6.5 ms

With cancellation isolated, the worker readable-data drain could grow from
``1 ms`` to ``10 ms``. A representative 100k x 3 profile changed from roughly
``67`` remote WAIT operations to ``11`` and reduced profiler elapsed time by
about ``11%``.

The remaining WAIT wall time was mostly real worker/libpq waiting, not avoidable
RPC chatter. This remains the reason not to keep increasing the drain merely to
minimise request count.

Compact short-query path and worker reuse -- optimized17..29
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A sequence of measured changes reduced fixed short-query overhead:

* ``Request``/``Response`` protocol simplification;
* dedicated fast-execute framing;
* compact payloads;
* marshal on the common fast path;
* warmed worker-process reuse between PostgreSQL sessions;
* stable client-encoding caching;
* removal/reuse of redundant status work;
* native result metadata capability detection.

The structural connect penalty changed most dramatically. Before worker reuse a
full checkpoint had measured connection startup around ``13.8x`` vanilla. The
first reuse checkpoint reduced this to roughly ``1.34x`` and later runs often
stayed close to parity.

One useful later checkpoint was ``optimized22`` with approximately::

    connect             1.01x
    SELECT median       1.32x
    SELECT p95          1.22x
    100k total          1.04x
    execute             1.17x
    fetchall            0.93x
    threads             1.31x
    cancel call         1.16x

Run-to-run vanilla drift remained visible, so later decisions increasingly used
same-run paired A/B tests instead of comparing isolated checkpoint ratios.

Windows TCP loopback fast path -- optimized30
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Enabling ``SIO_LOOPBACK_FAST_PATH`` on both ends of the Windows TCP main channel
was a real improvement and became the retained TCP fallback.

Compared with ``optimized29`` the paired summary moved approximately::

=====================  ============  ============
Metric                 optimized29   optimized30
=====================  ============  ============
connect ratio          1.122x        1.125x
SELECT median          1.538x        1.482x
SELECT p95             1.565x        1.530x
100k total             1.041x        1.090x
execute                1.114x        1.161x
fetchall               0.949x        1.020x
threads                1.555x        1.343x
=====================  ============  ============

Absolute qpsycopg SELECT median in that run moved from about ``0.916`` to
``0.771 ms``. The paired short-query improvement was smaller than the absolute
run-to-run difference but still positive.

Experiments immediately after optimized30
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Several plausible micro-optimisations were explicitly rejected instead of being
stacked indefinitely:

* coalesced small-frame ``recv``: no useful SELECT benefit, worse threads and a
  correctness risk if a TCP read crossed a frame boundary without preserving
  over-read;
* returning fast ``SendSnapshot`` directly: effectively neutral;
* Windows ``AF_UNIX``: rejected against the retained TCP path;
* shortened fast response state and transaction-status caching: insufficient or
  noisy benefit relative to added state-machine complexity.

The project then switched from serial full SQL A/B guesses to targeted transport
and resource measurements.

IPC microbenchmark and resource telemetry -- optimized34
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A local IPC microbenchmark measured representative 64-byte round-trip medians::

    TCP loopback             ~60.9 us
    TCP loopback fast path   ~53.6 us
    Windows named pipe       ~27.9 us

At 512 bytes the gap narrowed::

    TCP-fast                 ~55.4 us
    named pipe               ~48.1 us

This suggested that the pipe advantage was primarily lower fixed synchronization
cost for tiny messages rather than bulk bandwidth.

The benchmark harness was then extended to measure parent+worker resources.
``optimized34`` exposed the architecture cost much more clearly:

===========================  ============  ============  ========
Metric                       vanilla       qt-process    q/v
===========================  ============  ============  ========
SELECT median                0.519 ms      0.768 ms      1.48x
SELECT CPU                   156 us/call   594 us/call   3.80x
context switches             1.01/call     3.07/call     3.05x
RSS after SELECT             38.6 MB       90.3 MB       2.34x
Private Bytes                26.7 MB       56.9 MB       2.13x
handles                      197           522           2.65x
threads                      6             19            3.17x
100k total                   0.254 s       0.281 s       1.11x
100k CPU                     0.203 s       0.250 s       1.23x
100k context switches        800           835           1.04x
===========================  ============  ============  ========

This corrected the old interpretation of RSS. The process-backed architecture
uses materially more total memory/resources even when the parent process alone
can look smaller than vanilla.

CPU phase profiling also showed that serialization itself was no longer the
main issue. One 10k SELECT profile was approximately::

    wall                        ~817 us/query
    parent CPU                  ~202 us/query
    worker CPU                  ~147 us/query
    serialize                     ~3 us/query
    socket send                 ~12.5 us/query
    restore                      ~1.6 us/query

The fixed receive/IPC path was a much more promising target than result packing
or marshal/pickle micro-tuning.

Raw Windows named pipe -- optimized35
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The first named-pipe implementation used ``multiprocessing.Connection`` and lost
part of the microbenchmark advantage through extra framing/copies. It was
replaced by a raw byte stream using ``CreateNamedPipeW`` / ``CreateFileW`` and
``io.FileIO``.

A paired TCP-fast vs raw-pipe run then measured approximately:

====================  ============  ============
Metric                TCP-fast      raw pipe
====================  ============  ============
SELECT median         0.813 ms      0.738 ms
SELECT p95            1.191 ms      1.016 ms
SELECT CPU            625 us        469 us
connect               30.02 ms      28.77 ms
100k total            0.2742 s      0.2730 s
fetchall              0.1327 s      0.1319 s
====================  ============  ============

``optimized35`` subsequently reported a qpsycopg SELECT around ``0.735 ms``,
about ``1.24x`` the accumulated vanilla baseline around ``0.591 ms``. Parent CPU
and total tree CPU also improved while worker CPU stayed nearly unchanged.

Paired Qt validation showed the occasional large timer stalls were not specific
to named pipes; TCP runs could show similar scheduler spikes. Raw named pipe was
therefore made the Windows default, with TCP retained through
``QPSYCOPG_IPC=tcp``.

Native parent IPC
-----------------

Why move the data plane below Python
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A synthetic Python-vs-Cython named-pipe experiment tested the architecture
question directly rather than assuming Python overhead was dominant.

For a 128-byte request/response class it measured approximately::

    median       25.3 -> 19.0 us   (-24.9%)
    p95          39.1 -> 25.7 us   (-34.3%)
    CPU          14.06 -> 4.69 us  (-66.7%)

At 8 KiB the median improvement was about ``21%`` and CPU reduction about ``43%``.
This was strong enough evidence to integrate a native parent frame exchange into
the real named-pipe fast path.

The first integration regressed because it forced two ``ReadFile`` operations
(header then payload), while the Python read-ahead path often obtained both in
one kernel read. The implementation was corrected to use a reusable read-ahead
buffer and parse the header in C.

A same-run Python-pipe vs native-pipe validation then showed the retained native
parent exchange was worthwhile:

* SELECT median: about ``-4.1%`` paired median;
* SELECT p95: about ``-17.7%``;
* SELECT mean: about ``-12.6%``;
* parent CPU: ``184.4 -> 146.9 us/query`` (about ``-20.3%``);
* process-tree CPU: ``310.9 -> 287.5 us/query`` (about ``-7.5%``);
* context switches: effectively unchanged;
* bulk: statistically neutral;
* Qt median/p95: neutral.

This is the current reason the preferred Windows parent fast path uses the Cython
named-pipe frame exchange when available.

Worker-side native experiments
------------------------------

Worker CPU phase profiling
~~~~~~~~~~~~~~~~~~~~~~~~~~

A worker hot-path profile decomposed approximately::

    worker total CPU          ~195.3 us/query
    recv request frame         ~95.3 us
    handle/dispatch            ~93.8 us
    send response frame         ~6.3 us

Most of ``handle`` was the query execution/wait path::

    _executePending            ~87.5 us CPU
    _wait                      ~85.9 us CPU-accounted time
    select/libpq wait          ~523.7 us wall

Result work was already tiny::

    PGresult snapshot CPU       ~1.6 us
    value packing CPU           ~1.6 us

This prevented another round of unnecessary snapshot/serialization work.

Native worker receive and native Winsock wait
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Native worker frame receive gave only a small signal in an initial paired run.
A later controlled three-way test isolated the choices:

======================  ============  ============
Mode                    SELECT median SELECT p95
======================  ============  ============
base                    ~0.728 ms     ~0.960 ms
native worker receive   ~0.731 ms     ~0.955 ms
native receive + wait   ~0.757 ms     ~1.247 ms
======================  ============  ============

The native receive result was essentially neutral and native Winsock ``select``
was clearly worse. Bulk also showed no stable win.

Therefore the production default was restored to:

* native parent named-pipe frame exchange;
* Python worker frame receive;
* Python ``select.select()`` for the libpq socket.

The native worker helpers remain useful experiment switches but must not be
confused with the current default.

Architecture boundary benchmark
-------------------------------

After the worker-native experiments failed to expose a large remaining local
Python bottleneck, the project tested the process boundary itself.

The benchmark compared:

* direct normal Psycopg binary;
* the same normal Psycopg connection executed through a dedicated thread;
* current qt-process.

Short-query median results were approximately::

    direct       ~533 us
    thread       ~567 us
    process      ~724 us

Paired interpretation:

* thread vs direct: about ``+5.0%`` median;
* process vs direct: about ``+38.6%`` median;
* process vs thread: about ``+31.2%`` median;
* process CPU vs thread: about ``2.44x``;
* 100k-row fetch: thread approximately direct, process about ``+6.6%`` versus
  thread.

Context switches per query were approximately::

    direct       ~1
    thread       ~3
    process      ~3

This is a critical result. The process backend is not slower simply because it
performs more context switches than the thread architecture; both worker
architectures are around three. The remaining process penalty includes the
process boundary itself, kernel IPC, serialization/framing and a second Python
runtime.

Qt and cancellation architecture gate
-------------------------------------

A focused gate compared direct, dedicated-thread and qt-process behaviour.

With synchronous DB work invoked from a worker thread, Qt timer behaviour was
approximately::

==================  ============  ============  ============
                    direct        thread        qt-process
==================  ============  ============  ============
timer median        ~9.90 ms      ~10.05 ms     ~9.98 ms
timer p95           ~11.67 ms     ~11.52 ms     ~11.55 ms
max stall           ~3.78 ms      ~3.23 ms      ~3.04 ms
==================  ============  ============  ============

Cancellation succeeded for all three and the connection remained usable after
rollback/recovery.

When the synchronous query was invoked from the Qt main thread, all three blocked
the event loop, approximately::

    direct       ~579 ms
    thread       ~594 ms
    process      ~701 ms

Therefore worker topology does not make the synchronous API non-blocking. The
relevant result is that the dedicated-thread architecture passed the same
worker-thread Qt/cancellation gate while remaining much closer to direct Psycopg
performance.

Current architectural conclusion
--------------------------------

The remaining dominant question is no longer a missing process-IPC
micro-optimisation.

The evidence now says:

#. large-result packing/decoding is near native;
#. cancellation is isolated and near practical parity;
#. worker reuse removes most startup penalty;
#. raw named pipe + native parent exchange has reduced process IPC cost;
#. worker-side receive/wait native rewrites do not expose a large additional win;
#. a dedicated thread is much closer to direct Psycopg than the separate process.

This motivated a real ``PSYCOPG_IMPL=qt-thread`` proof of concept which reuses the
full synthetic pq facade/cache/snapshot semantics through ``SimpleQueue`` instead
of process IPC.

That proof of concept is not yet validated. The current branch handoff has fixed
its Python generator selection and shared-interpreter worker backend namespace,
but the standalone smoke still fails during initial connection setup. No
``qt-thread`` performance result should be treated as representative until the
semantic smoke suite passes.

Rejected or deprioritised experiments
-------------------------------------

Protocol-5 out-of-band pickle buffers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Slower in earlier measurements; rolled back.

Speculative ``get_result()`` during WAIT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Changed result-boundary timing and broke correctness/fatal semantics. Do not
reintroduce without a new design argument.

Longer WAIT drain as a goal by itself
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``10 ms`` drain already removed most avoidable WAIT RPC count. Remaining
wall time is mostly real libpq waiting.

TCP one-read over-read experiment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A naive large ``recv`` did not improve SELECT and could consume bytes from the
next frame without preserving them. The named-pipe adapter instead uses explicit
read-ahead state.

Windows AF_UNIX
~~~~~~~~~~~~~~~

Did not beat the retained Windows transport path and was removed.

Direct ``SendSnapshot`` return shortcut
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

No useful real-world improvement; removed.

Small state/cache micro-optimisations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Several response-state shortening and transaction-status cache experiments were
neutral or negative relative to complexity and were reverted.

Native worker receive
~~~~~~~~~~~~~~~~~~~~~

Controlled result was essentially neutral. Helper remains experimental only.

Native worker socket wait
~~~~~~~~~~~~~~~~~~~~~~~~~

Clearly worse in the controlled three-way test. Disabled by default.

Performance rules for future work
---------------------------------

#. Prefer same-run paired A/B for small changes; absolute Windows timing drifts
   materially between runs.
#. Do not repeat unchanged vanilla seven times. Use the accumulated compatible
   baseline plus a sentinel.
#. Measure qt-process resources as parent + worker tree.
#. Do not optimise serialization or snapshot code without a phase profile showing
   it dominates again.
#. Keep the current raw named pipe and native parent exchange while evaluating
   the architecture; they are the measured best process-backed data path.
#. Keep worker native receive/wait disabled by default.
#. Do not interpret process isolation as GUI asynchrony.
#. The next meaningful performance checkpoint is a smoke-clean real ``qt-thread``
   backend compared with direct binary and qt-process under the same full Psycopg
   proxy/cache/snapshot semantics.

Next work
---------

#. Fix the current ``qt-thread`` initial connection/lifecycle failure using only
   the standalone smoke while iterating.
#. Complete the entire semantic smoke suite before running full performance
   comparisons.
#. Run the balanced ``binary/direct`` vs ``qt-thread`` vs ``qt-process``
   validation after smoke passes.
#. If real qt-thread remains close to the earlier dedicated-thread architecture
   result and preserves semantics, make the architecture decision before doing
   more process-IPC micro-optimisation.
#. After that decision, create a new full numbered checkpoint representing the
   chosen current runtime and update this document.
