Qt process multi-chat coordination
==================================

Purpose
-------

This document defines how multiple ChatGPT chats cooperate on the Qt-oriented
qpsycopg work without relying on any one conversation context window.

The repository is the authoritative project memory. ChatGPT Project memory is
useful supporting context, but decisions, measurements and current work must be
recoverable from the repository alone.

Sources of truth
----------------

Use the following precedence when information conflicts:

#. current branch code and HEAD;
#. ``docs/qt-process-project-map.rst`` for current implementation direction;
#. ``docs/qt-process-performance.rst`` for measured performance evidence and
   rejected experiments;
#. ``docs/qt-process.rst`` for intended behaviour and architecture;
#. this file for collaboration protocol;
#. conversation/project memory only as supporting context.

A chat must not continue an old plan when the repository documents or branch
state have moved on.

Chat roles
----------

Coordinator / architecture reviewer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Owns direction, not implementation throughput.

Responsibilities:

* inspect the current branch and handoff documents;
* choose the next bounded problem;
* compare competing proposals and benchmark evidence;
* accept/reject architectural changes;
* ensure accepted work is reflected in the repository documentation;
* stop repeated experiments already recorded as rejected.

This chat should remain relatively small. It should not become the primary
implementation chat.

Implementation / optimisation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Owns the currently accepted implementation path.

Responsibilities:

* continue from ``Immediate next work`` in the project map;
* make code changes;
* keep semantics intact;
* run or request the narrowest useful validation first;
* update documentation together with accepted changes;
* record benchmark evidence instead of relying on intuition.

This is the normal long-running working chat. When its context becomes saturated,
it is replaced rather than treated as irreplaceable project memory.

Upstream parity reviewer
~~~~~~~~~~~~~~~~~~~~~~~~

Owns comparison with upstream Psycopg quality and hot-path design.

For any important optimisation it asks:

* what does upstream do on this path;
* why is that design cheap;
* what extra allocation, copy, syscall, serialization or scheduling boundary is
  introduced by qpsycopg;
* whether the same invariant can be preserved through the Qt-oriented backend;
* whether the proposed change reduces the real difference or only moves work.

It may propose patches, but its primary output is a concrete implementation or
measurement recommendation for the implementation chat.

Adversarial reviewer / scout
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Exists to escape local optimisation plateaus and anchoring.

It must not assume that the current implementation strategy is correct. It may
challenge the current boundary, data flow or abstraction, but must preserve the
required public semantics.

Use a fresh scout when:

* several accepted optimisations no longer materially improve the target metric;
* profiling and end-to-end results disagree;
* the active chat keeps proposing variants of already tested ideas;
* a larger architectural alternative needs an independent evaluation.

A scout starts from repository facts, constraints and measurements, not from a
long transcript of another chat's reasoning.

Documentation / history reviewer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Used when implementation work has moved faster than the handoff documents.

Responsibilities:

* reconstruct what actually happened from commits, benchmarks and prior results;
* update project map/performance documentation;
* identify stale next-work instructions;
* not continue runtime implementation unless explicitly reassigned.

One writer at a time
--------------------

Only one normal implementation chat owns the active implementation path at a
time.

Other chats may inspect the same branch and propose changes, but they should not
silently make competing runtime edits to the same branch.

For competing experiments, prefer one of these forms:

* separate temporary experiment branches;
* independent commits from the same known base, suitable for cherry-pick/revert;
* patch/proposal only, followed by one implementation owner applying the chosen
  variant.

Do not compare experiments performed sequentially on top of one another unless
the interaction itself is intentional.

Handoff contract
----------------

Before a working chat is retired, or before another chat takes ownership, the
repository must contain enough state to resume without the old conversation.

At minimum record:

* branch and relevant HEAD/checkpoint;
* accepted implementation state;
* current blocker or next bounded task;
* commands/tests that currently pass or fail;
* latest meaningful benchmark result;
* rejected experiments that are likely to be rediscovered;
* any temporary environment requirement needed to reproduce the current test.

The preferred locations are ``qt-process-project-map.rst`` and
``qt-process-performance.rst`` rather than a chat-only summary.

Resume protocol
---------------

A fresh implementation/optimisation chat should receive only a short bootstrap
instruction. It must then recover the current state from the repository.

Required reading order:

#. ``docs/qt-process.rst``;
#. ``docs/qt-process-project-map.rst``;
#. ``docs/qt-process-performance.rst``;
#. ``docs/qt-process-coordination.rst``;
#. current branch HEAD/diff relevant to ``Immediate next work``.

Then it should state, before making a substantial change:

* what it believes the current blocker/target is;
* what it will measure or change first;
* which recorded experiments it will intentionally not repeat.

If that summary contradicts the repository, fix the misunderstanding before
continuing.

Optimisation loop
-----------------

For ordinary optimisation work use this loop:

#. establish the exact base revision;
#. state one hypothesis about a measurable cost;
#. make the smallest change that tests it;
#. run the narrow semantic gate;
#. run the relevant performance measurement;
#. compare against a compatible baseline;
#. accept, revise or reject;
#. commit accepted work and update the relevant documentation.

Do not accumulate multiple unrelated performance changes before measuring unless
there is a strong dependency between them.

Plateau / exploration protocol
------------------------------

When normal optimisation reaches a plateau, temporarily stop feeding the same
reasoning back into the same chat.

Create two or three independent scout chats from the same repository revision.
Give all scouts the same factual packet:

* target metric;
* base revision;
* constraints/invariants;
* current benchmark result;
* relevant profiler output;
* rejected experiments.

Do not give them each other's proposed explanations at first.

Each scout returns:

* suspected dominant cost;
* mechanism;
* proposed experiment/change;
* expected observable result;
* semantic risk;
* estimated implementation scope.

The coordinator selects zero or one candidate for implementation. Benchmark
results, not voting between chats, decide whether the candidate survives.

Context saturation
------------------

A long chat is disposable. Replace it when it starts to show one or more of these
patterns:

* repeatedly rediscovers already rejected experiments;
* loses track of current branch/checkpoint state;
* keeps extending one approach despite contrary measurements;
* requires long transcript reminders to recover basic project facts;
* produces increasingly broad changes for narrowly measured problems.

Do not ask a saturated chat to generate the only handoff summary. Update the
repository first, then start a fresh chat from repository state.

Project memory
--------------

Keep the chats in the same ChatGPT Project with project memory enabled. This is
helpful for names, previous discussions and cross-chat context, but it is not a
transaction log and must not replace repository documentation.

A new chat should be able to operate correctly even if project memory retrieves
nothing useful.

Default bootstrap prompts
-------------------------

Implementation / optimisation chat::

    Continue ARMAGGIDON/qpsycopg on branch feature/qt-process-pq.
    You are the active implementation/optimisation owner.
    First read docs/qt-process.rst, docs/qt-process-project-map.rst,
    docs/qt-process-performance.rst and docs/qt-process-coordination.rst,
    then inspect current HEAD. Continue only from the repository's current
    Immediate next work. Preserve semantics and upstream-level code quality.
    Do not repeat rejected experiments. Measure before accepting an optimisation,
    and keep the relevant documentation updated with accepted work.

Coordinator chat::

    Review ARMAGGIDON/qpsycopg on branch feature/qt-process-pq as coordinator.
    Read the four qt-process documents and current HEAD. Do not become the main
    implementation worker. Identify the current bounded target, verify that the
    active implementation direction still follows measured evidence, and decide
    whether normal implementation should continue or independent scouts are
    justified.

Fresh adversarial scout::

    Independently review the current qpsycopg Qt performance bottleneck from the
    repository state. Read the four qt-process documents and current relevant
    code, but do not assume the present implementation strategy is optimal.
    Do not repeat experiments recorded as rejected. Return the strongest new
    measurable hypothesis, why it differs from the current path, the smallest
    experiment that could falsify it, semantic risks, and expected performance
    signal. Do not make broad runtime changes unless specifically assigned.
