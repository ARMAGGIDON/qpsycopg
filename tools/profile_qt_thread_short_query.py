#!/usr/bin/env python3
"""Diagnostic-only CPU attribution of warm SELECT 1: upstream/fork direct vs qt-thread."""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import sys
import time
from collections import defaultdict
from functools import wraps

import psycopg
import psutil


def profileRows(profile: cProfile.Profile, iterations: int) -> dict:
    stats = pstats.Stats(profile).stats
    rows = [
        {
            "file": filename.replace("\\", "/"),
            "line": line,
            "function": function,
            "calls": calls,
            "self_cpu_us_per_query": own * 1e6 / iterations,
            "inclusive_cpu_us_per_query": cumulative * 1e6 / iterations,
        }
        for (filename, line, function), (_, calls, own, cumulative, _) in stats.items()
    ]
    groups = defaultdict(float)
    for row in rows:
        filename = row["file"].lower()
        if filename.endswith("/pq/pq_qt_thread.py"):
            category = "qt_thread_transport"
        elif "/pq/_qt_process_worker" in filename:
            category = "qt_worker_dispatch"
        elif filename.endswith("/pq/_qt_process_protocol.py"):
            category = "protocol"
        elif "/psycopg/" in filename:
            category = "psycopg_core"
        elif "queue" in filename or "_thread" in filename or "threading" in filename:
            category = "queue_threading"
        elif "psycopg_c" in filename:
            category = "native_psycopg_c"
        else:
            category = "other"
        groups[category] += row["self_cpu_us_per_query"]

    return {
        "total_profiled_cpu_us_per_query": sum(row["self_cpu_us_per_query"] for row in rows),
        "self_cpu_by_source_us_per_query": dict(sorted(groups.items())),
        "top_self_cpu": sorted(rows, key=lambda row: row["self_cpu_us_per_query"], reverse=True)[:45],
        "top_inclusive_cpu": sorted(rows, key=lambda row: row["inclusive_cpu_us_per_query"], reverse=True)[:45],
    }


def instrumentThread(phases: dict, state: dict) -> cProfile.Profile:
    from psycopg.pq import pq_qt_thread as qt
    from psycopg.pq import _qt_process_worker_pool as pool

    workerProfile = cProfile.Profile(timer=time.thread_time)

    def timed(name: str, method):
        @wraps(method)
        def call(*args, **kwargs):
            if not state["enabled"]:
                return method(*args, **kwargs)
            wallStart = time.perf_counter_ns()
            cpuStart = time.thread_time_ns()
            try:
                return method(*args, **kwargs)
            finally:
                item = phases.setdefault(name, {"calls": 0, "wall_ns": 0, "cpu_ns": 0})
                item["calls"] += 1
                item["wall_ns"] += time.perf_counter_ns() - wallStart
                item["cpu_ns"] += time.thread_time_ns() - cpuStart
        return call

    for cls, member, name in (
        (qt._ThreadTransport, "_fastExecute", "caller_fast_execute"),
        (qt._ThreadTransport, "_dispatch", "caller_dispatch"),
        (qt._ThreadTransport, "_responseValue", "caller_restore"),
        (pool.Worker, "_executePending", "worker_execute_pending"),
        (pool.Worker, "_wait", "worker_libpq_wait"),
    ):
        setattr(cls, member, timed(name, getattr(cls, member)))

    originalHandle = pool.Worker._handle
    timedHandle = timed("worker_handle", originalHandle)

    @wraps(originalHandle)
    def handle(self, request):
        # CPython 3.13 cProfile uses one monitoring tool: two simultaneously
        # active Profile objects (even on different threads) conflict.
        if state["profile_worker"] and not state["profiling"]:
            workerProfile.enable()
            state["profiling"] = True
        elif not state["profile_worker"] and state["profiling"]:
            workerProfile.disable()
            state["profiling"] = False
        return timedHandle(self, request)

    pool.Worker._handle = handle
    return workerProfile


def cpuCounters() -> dict:
    proc = psutil.Process()
    switches = proc.num_ctx_switches()
    return {
        "process_cpu_ns": time.process_time_ns(),
        "caller_cpu_ns": time.thread_time_ns(),
        "context_switches": switches.voluntary + switches.involuntary,
    }


def selectOne(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        if cursor.fetchone() != (1,):
            raise AssertionError("SELECT 1 result mismatch")


def measure(connection, iterations: int) -> dict:
    before = cpuCounters()
    start = time.perf_counter_ns()
    for _ in range(iterations):
        selectOne(connection)
    elapsed = time.perf_counter_ns() - start
    after = cpuCounters()
    return {
        "wall_us_per_query": elapsed / iterations / 1e3,
        "process_cpu_us_per_query": (after["process_cpu_ns"] - before["process_cpu_ns"]) / iterations / 1e3,
        "caller_cpu_us_per_query": (after["caller_cpu_ns"] - before["caller_cpu_ns"]) / iterations / 1e3,
        "context_switches_per_query": (after["context_switches"] - before["context_switches"]) / iterations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--iterations", type=int, default=3000)
    parser.add_argument("--warmup", type=int, default=200)
    args = parser.parse_args()
    if args.iterations <= 0 or args.warmup < 0:
        parser.error("iterations must be positive and warmup nonnegative")

    impl = psycopg.pq.__impl__
    if impl not in ("c", "qt-thread"):
        parser.error(f"unexpected pq implementation: {impl!r}")
    phases: dict = {}
    state = {"enabled": False, "profile_worker": False, "profiling": False}
    workerProfile = None
    workerProfiled = None
    callerProfile = cProfile.Profile(timer=time.thread_time)
    with psycopg.connect(args.dsn) as connection:
        for _ in range(args.warmup):
            selectOne(connection)

        # Clean warm control: no hooks or profilers are installed yet.
        unprofiled = measure(connection, args.iterations)
        if impl == "qt-thread":
            workerProfile = instrumentThread(phases, state)

        # Pass 1: caller profile only. Concurrent cProfile objects conflict on
        # Python 3.13, so the worker must not enable its profiler here.
        callerProfile.enable()
        try:
            callerProfiled = measure(connection, args.iterations)
        finally:
            callerProfile.disable()

        if impl == "qt-thread":
            # Pass 2: worker profile and phase timers, with caller profile OFF.
            # Even if measurement fails, disable worker profiling before the
            # connection context manager attempts COMMIT/ROLLBACK/close.
            from psycopg.pq._qt_process_protocol import Operation

            state["enabled"] = True
            state["profile_worker"] = True
            try:
                workerProfiled = measure(connection, args.iterations)
            finally:
                state["enabled"] = False
                state["profile_worker"] = False
                connection.pgconn._transport.request(Operation.GET, 1, "status")
                if state["profiling"]:
                    raise AssertionError("worker profiler did not stop")

    result = {
        "impl": impl,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "python": sys.version.split()[0],
        "psycopg_module": str(psycopg.__file__),
        "iterations": args.iterations,
        "warmup": args.warmup,
        "note": "Diagnostic only: unprofiled control, caller-only cProfile pass, then worker-only cProfile/phase pass. Profiled timings are not v3 acceptance data.",
        "unprofiled": unprofiled,
        "caller_profiled": callerProfiled,
        "caller_profile": profileRows(callerProfile, args.iterations),
    }
    if workerProfile is not None:
        result["worker_profiled"] = workerProfiled
        result["worker_profile"] = profileRows(workerProfile, args.iterations)
        result["phase_totals"] = {
            name: {
                "calls": counts["calls"],
                "calls_per_query": counts["calls"] / args.iterations,
                "wall_us_per_query": counts["wall_ns"] / args.iterations / 1e3,
                "thread_cpu_us_per_query": counts["cpu_ns"] / args.iterations / 1e3,
            }
            for name, counts in sorted(phases.items())
        }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
