#!/usr/bin/env python3
"""Diagnostic-only warm-query CPU attribution; no runtime or benchmark changes."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from functools import wraps
from typing import Any, Callable

import psycopg
import psutil


def counters() -> dict[str, int]:
    process = psutil.Process()
    switches = process.num_ctx_switches()
    return {
        "process_cpu_ns": time.process_time_ns(),
        "caller_cpu_ns": time.thread_time_ns(),
        "context_switches": switches.voluntary + switches.involuntary,
    }


def measure(call: Callable[[], Any], count: int) -> dict[str, float]:
    before = counters()
    started = time.perf_counter_ns()
    for _ in range(count):
        call()
    wall = time.perf_counter_ns() - started
    after = counters()
    processCpu = after["process_cpu_ns"] - before["process_cpu_ns"]
    callerCpu = after["caller_cpu_ns"] - before["caller_cpu_ns"]
    return {
        "wall_us_per_call": wall / count / 1_000,
        "process_cpu_us_per_call": processCpu / count / 1_000,
        "caller_cpu_us_per_call": callerCpu / count / 1_000,
        "other_threads_cpu_us_per_call": (processCpu - callerCpu) / count / 1_000,
        "context_switches_per_call": (
            after["context_switches"] - before["context_switches"]
        ) / count,
    }


def installThreadPhases(state: dict[str, Any]) -> None:
    """Wrap selected *existing* functions; never install global monitoring hooks."""
    from psycopg.pq import _qt_process_worker as worker
    from psycopg.pq import _qt_process_worker_pool as pool
    from psycopg.pq import pq_qt_process as base
    from psycopg.pq import pq_qt_thread as thread

    def traced(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def call(*args: Any, **kwargs: Any) -> Any:
            phase = state["phase"]
            if phase is None:
                return original(*args, **kwargs)
            wallStart = time.perf_counter_ns()
            cpuStart = time.thread_time_ns()
            try:
                return original(*args, **kwargs)
            finally:
                key = (phase, name)
                item = state["data"][key]
                item["calls"] += 1
                item["wall_ns"] += time.perf_counter_ns() - wallStart
                item["cpu_ns"] += time.thread_time_ns() - cpuStart

        return call

    for cls, member, name in (
        (thread._ThreadTransport, "_fastExecute", "caller_fast_execute"),
        (thread._ThreadTransport, "_perform", "caller_generic_request"),
        (thread._ThreadTransport, "_dispatch", "caller_queue_handoff"),
        (thread._ThreadTransport, "_responseValue", "caller_response_restore"),
        (pool.Worker, "_handle", "worker_handle"),
        (worker.Worker, "_handle", "worker_base_dispatch"),
        (pool.Worker, "_executePending", "worker_execute_pending"),
        (worker.Worker, "_executePending", "worker_base_execute"),
        (pool.Worker, "_wait", "worker_wait"),
    ):
        setattr(cls, member, traced(name, getattr(cls, member)))

    # These are looked up by module global name on the active thread path.
    for module, member, name in (
        (base, "_wire", "caller_wire_conversion"),
        (base, "_restore", "caller_restore_conversion"),
        (worker, "_snapshotResult", "worker_result_snapshot"),
    ):
        setattr(module, member, traced(name, getattr(module, member)))


def summarizePhases(state: dict[str, Any], phase: str, count: int) -> dict:
    return {
        name: {
            "calls": item["calls"],
            "calls_per_operation": item["calls"] / count,
            "inclusive_wall_us_per_operation": item["wall_ns"] / count / 1_000,
            "inclusive_thread_cpu_us_per_operation": item["cpu_ns"] / count / 1_000,
        }
        for (keyPhase, name), item in sorted(state["data"].items())
        if keyPhase == phase
    }


def selectOne(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        if cursor.fetchone() != (1,):
            raise AssertionError("SELECT 1 returned an unexpected value")


def statusRead(connection: Any, isThread: bool) -> None:
    if isThread:
        from psycopg.pq._qt_process_protocol import Operation

        # This exercises real thread Request/Response handoff, but no SQL or
        # libpq socket wait. It is a diagnostic control, not a public API path.
        value = connection.pgconn._transport.request(Operation.GET, 1, "status")
    else:
        value = connection.pgconn.status
    if int(value) != int(psycopg.pq.ConnStatus.OK):
        raise AssertionError(f"unexpected connection status: {value!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--iterations", type=int, default=3_000)
    parser.add_argument("--warmup", type=int, default=200)
    args = parser.parse_args()
    if args.iterations <= 0 or args.warmup < 0:
        parser.error("iterations must be positive and warmup nonnegative")

    impl = psycopg.pq.__impl__
    if impl not in ("c", "qt-thread"):
        parser.error(f"unexpected pq implementation: {impl!r}")

    isThread = impl == "qt-thread"
    state: dict[str, Any] = {
        "phase": None,
        "data": defaultdict(lambda: {"calls": 0, "wall_ns": 0, "cpu_ns": 0}),
    }
    result: dict[str, Any] = {
        "impl": impl,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "python": sys.version.split()[0],
        "psycopg_module": str(psycopg.__file__),
        "iterations": args.iterations,
        "warmup": args.warmup,
        "method": "Clean controls first; targeted local inclusive phase timers after. No cProfile.",
        "warnings": [
            "Instrumented phase times overlap across nested calls and are not additive.",
            "Windows thread CPU clock is coarse for short functions; compare aggregated repeated calls.",
            "Use unchanged adjacent-paired benchmark v3 for optimisation acceptance.",
            "Status GET is a synthetic diagnostic RPC, not the same operation as SELECT 1.",
        ],
    }

    with psycopg.connect(args.dsn) as connection:
        for _ in range(args.warmup):
            selectOne(connection)
        query = lambda: selectOne(connection)
        status = lambda: statusRead(connection, isThread)

        # Both control workloads run before any instrumentation is installed.
        result["unprofiled"] = measure(query, args.iterations)
        result["status_control"] = measure(status, args.iterations)

        if isThread:
            installThreadPhases(state)
            state["phase"] = "query"
            try:
                result["instrumented_query"] = measure(query, args.iterations)
            finally:
                state["phase"] = None

            result["query_phases"] = summarizePhases(
                state, "query", args.iterations
            )
            state["phase"] = "status"
            try:
                result["instrumented_status"] = measure(status, args.iterations)
            finally:
                state["phase"] = None

            result["status_phases"] = summarizePhases(
                state, "status", args.iterations
            )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
