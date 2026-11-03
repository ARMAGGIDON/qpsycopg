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

Architecture deltas were approximately:

======================  ===========  ============
Resource                qt-thread    qt-process
======================  ===========  ============
processes                +0           +2
threads                  +2           +11
handles                  +14          +306
======================  ===========  ============

Benchmark v2 -- diagnostic baseline
-----------------------------------

Checkpoint ``baseline-v2`` was captured at fork commit
``93dc15deb92c0988edaa5c0accbf110f18941deb``. Correctness smoke passed completely.
It introduced separate cold/warm connection metrics, repeated cancellation,
Qt p99 and process inventory. The process inventory confirmed that qt-process
contains one parent Python process plus two child Python processes while upstream,
direct and qt-thread each contain one Python process.

Selected v2 results were:

=================================  ===========  ============
Metric                             qt-thread    qt-process
=================================  ===========  ============
cold connect                       1.446x       15.449x
warm connect median                1.337x       1.105x
SELECT 1 median                    1.216x       1.234x
CPU / short query                  1.462x       2.409x
context switches / query           2.997x       3.015x
100k fetch/decode                  1.002x       0.908x
100k total                         0.977x       0.960x
cancel call median                 1.324x       2.619x
process-tree RSS                   1.335x       2.465x
process-tree private memory        1.399x       2.331x
Qt timer p99                       1.006x       1.029x
=================================  ===========  ============

These absolute ratios are useful architecture signals, but v2 exposed that the
comparison pairing was still too loose for small optimisation claims. Upstream
``SELECT 1`` median had about ``22%`` MAD and roughly ``71%`` observed range
between the six SQL runs. Several modes in one logical round were separated by
multiple complete benchmark runs, so a single per-round upstream measurement was
not necessarily representative of the machine state when each fork mode ran.

V2 also exposed two benchmark-definition problems:

* the concurrency metric measured one batch together with
  ``ThreadPoolExecutor`` construction, mixing thread-pool startup with database
  work;
* ``query finish after cancel`` used a near-zero upstream denominator and could
  produce visually huge but practically meaningless ratios.

For these reasons ``baseline-v2`` is retained as a diagnostic checkpoint but is
not the baseline for numbered runtime optimisations.

Benchmark v3 -- canonical optimisation methodology
--------------------------------------------------

V3 is the canonical methodology for the next optimisation series. Its important
changes are:

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
* the benchmark/reference hash now includes the orchestration and summary code,
  so methodology-only changes cannot silently reuse an incompatible historical
  reference.

The SQL pair pattern is conceptually::

    adjacent upstream <-> fork direct
    adjacent upstream <-> qt-thread
    adjacent upstream <-> qt-process

with pair orientation and mode order rotated between rounds. The primary ratio is
therefore the median of six adjacent ``fork / paired upstream`` ratios for SQL
metrics. The generated stability table reports the MAD and observed range of the
paired ratio itself in addition to aggregate upstream drift/noise.

The next required run is ``baseline-v3``. No runtime optimisation should be
committed before it exists, because that checkpoint is the clean before-state for
``optimized01``.

Optimisation order
------------------

Do not optimise every visible ratio at once. The current order is:

#. capture ``baseline-v3`` with the canonical benchmark;
#. inspect its paired ratio MAD/ranges and confirm which costs remain stable;
#. first runtime candidate: reduce qt-thread connection/resource overhead by
   avoiding eager cancellation-control construction when cancellation is never
   used;
#. capture ``optimized01`` using exactly the same v3 workload/hash;
#. retain or revert the optimisation according to measured paired improvement,
   architecture deltas and semantic smoke;
#. choose the next target only from the remaining measured cost.

Bulk decoding is not a current target. Both v1 and v2 showed the native
qt-thread decoder near direct/upstream behaviour for large fetch/decode work.

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
