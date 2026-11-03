qpsycopg optimization log
=========================

This file is the live performance/optimisation handoff for the current
``qt-thread`` / ``qt-process`` work. Historical implementation details remain in
:doc:`qt-process-performance` and :doc:`qt-process-project-map`; this file records
what is being measured now, why a checkpoint exists, and what may be compared
with it.

Comparison foundation
---------------------

The comparison revision is the merge-base with ``psycopg/psycopg``. At the
first paired run this was::

    upstream base  99356800e90aa0e7b604f652d714ef0f33422b47

Upstream and fork are benchmarked using isolated, non-editable environments built
with the same CPython and the same local PostgreSQL/libpq installation. Current
reference environment at the first run was:

* Python ``3.13.5``;
* PostgreSQL/libpq ``18.6``;
* PySide6 ``6.12.0``;
* psutil ``7.2.2``;
* Windows 11, AMD64, 8 logical CPUs.

Every checkpoint reruns upstream. The primary comparison is the median of paired
per-round ``fork / current upstream`` ratios. The first compatible upstream run
is retained only as a historical drift reference. Host CPU load, CPU frequency,
available memory and system process count are captured around every run.

The benchmark order rotates between rounds so no implementation is always first
or last::

    upstream -> direct -> qt-thread -> qt-process
    direct -> qt-thread -> qt-process -> upstream
    qt-thread -> qt-process -> upstream -> direct
    qt-process -> upstream -> direct -> qt-thread

Ratios are meaningful for performance/usage metrics. Discrete architecture
resources such as processes, threads and handles are reported as absolute deltas
instead of ratios.

Initial diagnostic checkpoint -- benchmark v1
----------------------------------------------

Checkpoint ``initial`` was captured at fork commit
``84c6ebb2eafd3562511829334704b4304cd58e79``. It established useful architecture
signals, but it is not the pre-optimisation reference for future checkpoints
because the benchmark definition was improved immediately afterwards.

Representative paired ratios from that run were:

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

Architecture deltas were approximately:

======================  ===========  ============
Resource                qt-thread    qt-process
======================  ===========  ============
processes                +0           +2
threads                  +2           +11
handles                  +14          +306
======================  ===========  ============

The same run showed two measurements which should not be optimised directly from
v1 data:

* qt-process connect p95 mixed the first cold worker start with later reused
  connections, producing an apparent ``13.5x`` tail ratio while connect median
  was only about ``1.10x``;
* one-shot cancellation and maximum Qt stall were too sensitive to individual
  scheduler events to serve as primary optimisation targets.

Benchmark v2
------------

The benchmark definition after ``initial`` deliberately changes before any
runtime optimisation is accepted:

* first connection is reported separately as ``Cold connect``;
* following connections are reported as ``Warm connect median/p95``;
* cancellation is repeated five times inside every SQL run and reported with
  median/p95 plus query-finish latency;
* SQL timing summaries include p99 in addition to median/p95;
* the Qt workload uses 60 sleep queries by default and reports p95 and p99;
* maximum Qt stall remains available but is explicitly diagnostic rather than a
  primary KPI;
* process-tree entries include process name/executable so extra process counts can
  be attributed instead of guessed.

Because these changes alter the workload definition, ``initial`` v1 and later v2
checkpoints must not be used as a direct before/after optimisation pair. The next
run must be captured as ``baseline-v2`` before changing runtime behaviour.

Optimisation order
------------------

Do not optimise every visible ratio at once. The current order is:

#. establish ``baseline-v2`` with the corrected benchmark;
#. reduce ``qt-thread`` connection/resource overhead by making cancellation
   support lazy instead of constructing it for every connection;
#. measure this as ``optimized01`` against a same-run upstream and compare with
   ``baseline-v2``;
#. only then decide whether the next target is thread request/response overhead,
   worker reuse, cancellation transport, or result handoff based on measured
   remaining cost.

Bulk decoding is not a current target: v1 measured both worker architectures at
about ``1.01x`` upstream for ``fetch/decode(100k)`` after the native decoder was
enabled for ``qt-thread``.

Checkpoint rule
---------------

A runtime optimisation is considered supported only when:

* the benchmark workload/hash is compatible;
* upstream was rerun in the same checkpoint;
* the paired ratio moves in the intended direction by more than the observed
  run-to-run noise, or the change has a directly expected architecture delta;
* correctness smoke still passes;
* unrelated important metrics do not regress materially.

If the benchmark definition changes again, capture a new named baseline before
continuing numbered optimisation checkpoints.
