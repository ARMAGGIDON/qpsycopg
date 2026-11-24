#!/usr/bin/env python3
"""Diagnostic-only real qt-thread GET-status phase attribution.

Uses uninstrumented GET controls on a separate real qt-thread connection,
short C-P-C-P-C blocks, and local worker/caller CPU timers. Never changes
production source, replaces worker algorithms, or uses cProfile.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import psutil
import psycopg

from psycopg.pq import _qt_process_worker as worker_base
from psycopg.pq import _qt_process_worker_pool as worker_pool
from psycopg.pq import pq_qt_process as base
from psycopg.pq import pq_qt_thread as thread
from psycopg.pq._qt_process_protocol import Operation

_FIELDS = (
    "wall_us_per_call",
    "process_cpu_us_per_call",
    "caller_cpu_us_per_call",
    "other_threads_cpu_us_per_call",
    "context_switches_per_call",
)
_PHASES = (
    "caller_request",
    "caller_perform",
    "caller_dispatch",
    "caller_response_value",
    "caller_wire",
    "caller_restore",
    "worker_run_turn",
    "worker_pool_handle",
    "worker_base_handle",
    "worker_normalize",
    "worker_connection_ready",
    "worker_connection_broken",
)


def describe(values: list[float]) -> dict[str, Any]:
    center = statistics.median(values)
    return {
        "median": center,
        "mad": statistics.median(abs(value - center) for value in values),
        "min": min(values),
        "max": max(values),
        "positive_count": sum(value > 0 for value in values),
        "count": len(values),
    }


def status_get(transport: Any) -> None:
    value = transport.request(Operation.GET, 1, "status")
    if int(value) != int(psycopg.pq.ConnStatus.OK):
        raise AssertionError(f"unexpected GET status: {value!r}")


def read_counters(process: psutil.Process) -> tuple[int, int, int]:
    switches = process.num_ctx_switches()
    return (
        time.process_time_ns(),
        time.thread_time_ns(),
        switches.voluntary + switches.involuntary,
    )


def measure(operation: Callable[[], None], calls: int,
            process: psutil.Process) -> dict[str, float | int]:
    before_cpu, before_caller, before_switches = read_counters(process)
    start = time.perf_counter_ns()
    for _ in range(calls):
        operation()
    end = time.perf_counter_ns()
    after_cpu, after_caller, after_switches = read_counters(process)
    cpu = after_cpu - before_cpu
    caller = after_caller - before_caller
    return {
        "midpoint_ns": (start + end) // 2,
        "wall_us_per_call": (end - start) / calls / 1_000,
        "process_cpu_us_per_call": cpu / calls / 1_000,
        "caller_cpu_us_per_call": caller / calls / 1_000,
        "other_threads_cpu_us_per_call": (cpu - caller) / calls / 1_000,
        "context_switches_per_call": (after_switches - before_switches) / calls,
    }


class Phases:
    """Accumulate inclusive phase timings on the thread executing each call."""

    def __init__(self) -> None:
        self.recording = False
        self.rows: dict[str, dict[str, int]] = {}
        self.worker_start: tuple[int, int] | None = None

    def begin(self) -> None:
        self.rows = defaultdict(lambda: {"calls": 0, "wall_ns": 0, "cpu_ns": 0})
        self.worker_start = None
        self.recording = True

    def end(self) -> dict[str, dict[str, int]]:
        self.recording = False
        if self.worker_start is not None:
            raise AssertionError("a worker turn was left unpaired")
        return {key: dict(row) for key, row in self.rows.items()}

    def add(self, name: str, wall_ns: int, cpu_ns: int) -> None:
        row = self.rows[name]
        row["calls"] += 1
        row["wall_ns"] += wall_ns
        row["cpu_ns"] += cpu_ns

    def wrap(self, name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def traced(*args: Any, **kwargs: Any) -> Any:
            if not self.recording:
                return original(*args, **kwargs)
            start = time.perf_counter_ns()
            cpu_start = time.thread_time_ns()
            try:
                return original(*args, **kwargs)
            finally:
                cpu = time.thread_time_ns() - cpu_start
                wall = time.perf_counter_ns() - start
                self.add(name, wall, cpu)
        return traced


class RequestQueueProbe:
    """Worker-side queue receipt marker; wraps the original SimpleQueue."""

    def __init__(self, raw: Any, phases: Phases) -> None:
        self.raw = raw
        self.phases = phases

    def get(self, *args: Any, **kwargs: Any) -> Any:
        request = self.raw.get(*args, **kwargs)
        if self.phases.recording:
            self.phases.worker_start = (
                time.perf_counter_ns(), time.thread_time_ns()
            )
        return request

    def put(self, *args: Any, **kwargs: Any) -> Any:
        return self.raw.put(*args, **kwargs)


class ResponseQueueProbe:
    """Worker response publication marker; end-to-end _run() turn duration."""

    def __init__(self, raw: Any, phases: Phases) -> None:
        self.raw = raw
        self.phases = phases

    def put(self, value: Any) -> None:
        start = self.phases.worker_start
        if self.phases.recording and start is not None:
            cpu = time.thread_time_ns() - start[1]
            wall = time.perf_counter_ns() - start[0]
            self.phases.add("worker_run_turn", wall, cpu)
            self.phases.worker_start = None
        self.raw.put(value)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self.raw.get(*args, **kwargs)


def install_worker_run_observer(phases: Phases) -> Callable[[], None]:
    """Temporarily wrap _run only while creating the profiled transport.

    The original _run remains the executed worker algorithm. Its *instance*
    queues point to transparent proxies that record receive-to-response
    publication; the clean-control connection retains the raw queues.
    """
    original = thread._ThreadTransport._run

    @wraps(original)
    def observed_run(transport: Any) -> None:
        transport._requests = RequestQueueProbe(transport._requests, phases)
        transport._responses = ResponseQueueProbe(transport._responses, phases)
        return original(transport)

    thread._ThreadTransport._run = observed_run
    return lambda: setattr(thread._ThreadTransport, "_run", original)


class MethodHooks:
    """Temporary method-level wrappers; no profile/tracing global hooks."""

    def __init__(self, phases: Phases) -> None:
        self.phases = phases
        self.saved: list[tuple[Any, str, Any]] = []

    def __enter__(self) -> MethodHooks:
        methods = (
            (thread._ThreadTransport, "request", "caller_request", False),
            (thread._ThreadTransport, "_perform", "caller_perform", False),
            (thread._ThreadTransport, "_dispatch", "caller_dispatch", False),
            (thread._ThreadTransport, "_responseValue", "caller_response_value", False),
            (thread._ThreadTransport, "_connectionReady", "worker_connection_ready", True),
            (thread._ThreadTransport, "_connectionBroken", "worker_connection_broken", True),
            (worker_pool.Worker, "_handle", "worker_pool_handle", False),
            (worker_base.Worker, "_handle", "worker_base_handle", False),
            (worker_base.Worker, "_normalize", "worker_normalize", False),
            (base, "_wire", "caller_wire", False),
            (base, "_restore", "caller_restore", False),
        )
        try:
            for owner, member, name, static in methods:
                original_raw = owner.__dict__[member]
                self.saved.append((owner, member, original_raw))
                original = getattr(owner, member)
                wrapped = self.phases.wrap(name, original)
                setattr(owner, member, staticmethod(wrapped) if static else wrapped)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        for owner, member, original in reversed(self.saved):
            setattr(owner, member, original)
        self.saved.clear()


def phase_per_call(rows: dict[str, dict[str, int]], calls: int) -> dict:
    return {
        name: {
            "calls": row["calls"],
            "calls_per_get": row["calls"] / calls,
            "inclusive_wall_us_per_get": row["wall_ns"] / calls / 1_000,
            "inclusive_thread_cpu_us_per_get": row["cpu_ns"] / calls / 1_000,
        }
        for name, row in sorted(rows.items())
    }


def run_repeat(dsn: str, repeat: int, cycles: int, calls: int,
               warmup: int, block_warmup: int) -> dict:
    if psycopg.pq.__impl__ != "qt-thread":
        raise RuntimeError(f"wrong pq backend: {psycopg.pq.__impl__!r}")

    phases = Phases()
    process = psutil.Process()
    cycle_rows: list[dict[str, Any]] = []
    pooled: dict[str, dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "wall_ns": 0, "cpu_ns": 0}
    )
    total_profile_calls = 0

    # The control transport is created before any monkeypatch. It uses
    # original _run, original queues and unwrapped methods in clean blocks.
    with psycopg.connect(dsn) as clean_connection:
        restore_run = install_worker_run_observer(phases)
        try:
            profiled_connection = psycopg.connect(dsn)
        finally:
            restore_run()

        with profiled_connection:
            clean_transport = clean_connection.pgconn._transport
            profiled_transport = profiled_connection.pgconn._transport

            def clean() -> None:
                status_get(clean_transport)

            def profiled() -> None:
                status_get(profiled_transport)

            for _ in range(warmup):
                clean()
                profiled()

            for cycle in range(cycles):
                controls: list[dict] = []
                instrumented: list[dict] = []
                for block in range(5):
                    if block % 2 == 0:
                        # No method monkeypatch at all in these real GET controls.
                        for _ in range(block_warmup):
                            clean()
                        controls.append(measure(clean, calls, process))
                    else:
                        with MethodHooks(phases):
                            for _ in range(block_warmup):
                                profiled()
                            phases.begin()
                            try:
                                values = measure(profiled, calls, process)
                            finally:
                                rows = phases.end()
                        normalized = phase_per_call(rows, calls)
                        missing = set(_PHASES) - set(normalized)
                        if missing or any(
                            normalized[name]["calls"] != calls for name in _PHASES
                        ):
                            raise AssertionError(
                                f"incomplete profiled GET phase counts: {missing!r}; "
                                f"{ {name: item['calls'] for name, item in normalized.items()}!r}"
                            )
                        instrumented.append({
                            "metrics": values, "phases": normalized,
                        })
                        total_profile_calls += calls
                        for name, entry in rows.items():
                            for key in ("calls", "wall_ns", "cpu_ns"):
                                pooled[name][key] += entry[key]

                for j, block in enumerate(instrumented):
                    before, after = controls[j], controls[j + 1]
                    mid = block["metrics"]["midpoint_ns"]
                    span = after["midpoint_ns"] - before["midpoint_ns"]
                    if span <= 0 or not before["midpoint_ns"] < mid < after["midpoint_ns"]:
                        raise AssertionError("invalid clean-control timestamp bracket")
                    proportion = (mid - before["midpoint_ns"]) / span
                    block["interpolation_fraction"] = proportion
                    block["paired_difference_from_clean"] = {
                        metric: block["metrics"][metric] - (
                            before[metric] + proportion * (after[metric] - before[metric])
                        )
                        for metric in _FIELDS
                    }
                cycle_rows.append({
                    "cycle": cycle + 1,
                    "mode_order": ["clean", "instrumented", "clean", "instrumented", "clean"],
                    "clean_controls": controls,
                    "instrumented_blocks": instrumented,
                    "cycle_clean_wall_drift_us": (
                        controls[-1]["wall_us_per_call"] - controls[0]["wall_us_per_call"]
                    ),
                })

    blocks = [b for c in cycle_rows for b in c["instrumented_blocks"]]
    output: dict[str, Any] = {
        "repeat": repeat,
        "cycles": cycle_rows,
        "instrumented_calls": total_profile_calls,
        "pooled_phases": phase_per_call(dict(pooled), total_profile_calls),
        "paired_delta": {
            metric: describe([
                b["paired_difference_from_clean"][metric] for b in blocks
            ])
            for metric in _FIELDS
        },
        "phase_block_medians": {
            phase: {
                field: describe([
                    b["phases"][phase][field] for b in blocks
                ])
                for field in ("inclusive_wall_us_per_get", "inclusive_thread_cpu_us_per_get")
            }
            for phase in _PHASES
            if all(phase in b["phases"] for b in blocks)
        },
        "drift": {
            "first_clean_wall_us": cycle_rows[0]["clean_controls"][0]["wall_us_per_call"],
            "last_clean_wall_us": cycle_rows[-1]["clean_controls"][-1]["wall_us_per_call"],
            "cycle_clean_wall_change_us": describe([
                c["cycle_clean_wall_drift_us"] for c in cycle_rows
            ]),
        },
    }
    start = output["drift"]["first_clean_wall_us"]
    end = output["drift"]["last_clean_wall_us"]
    output["drift"]["first_to_last_fraction"] = (end - start) / start
    return output


def aggregate(reports: list[dict]) -> dict[str, Any]:
    # Independent subprocess repetitions, not per-block samples, are the
    # primary unit for cross-repeat conclusions.
    names = sorted(set.intersection(*(
        set(r["pooled_phases"]) for r in reports
    )))
    return {
        "independent_repeats": len(reports),
        "phase_costs": {
            name: {
                metric: {
                    "summary": describe([
                        r["pooled_phases"][name][metric] for r in reports
                    ]),
                    "repeat_values": [
                        r["pooled_phases"][name][metric] for r in reports
                    ],
                }
                for metric in (
                    "calls_per_get",
                    "inclusive_wall_us_per_get",
                    "inclusive_thread_cpu_us_per_get",
                )
            }
            for name in names
        },
        "instrumentation_delta": {
            metric: describe([
                r["paired_delta"][metric]["median"] for r in reports
            ])
            for metric in _FIELDS
        },
        "drift": describe([
            r["drift"]["first_to_last_fraction"] for r in reports
        ]),
        "warnings": [
            "All timings are inclusive and phases are nested; do not sum them.",
            "worker_run_turn covers request receipt through response preparation; "
            "it excludes idle _run waiting and queue.put wakeup.",
            "The clean control is another qt-thread connection; "
            "session-specific effects remain possible.",
            "Local method hooks and queue proxies perturb measured timings. "
            "The profiled-minus-clean delta is not a removable runtime cost.",
            "Windows thread CPU clocks may be quantized on short blocks. "
            "Use pooled repeat totals over isolated per-block CPU.",
            "A status GET is a diagnostic RPC and cannot by itself predict SQL performance.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--cycles", type=int, default=12)
    parser.add_argument("--block-size", type=int, default=1200)
    parser.add_argument("--warmup", type=int, default=250)
    parser.add_argument("--block-warmup", type=int, default=32)
    parser.add_argument("--watchdog-seconds", type=float, default=90)
    parser.add_argument("--child-repeat", type=int)
    args = parser.parse_args()

    if (args.repeats < 3 or args.cycles < 8 or args.block_size < 200
            or args.warmup < 0 or args.block_warmup < 0):
        parser.error("expected >=3 repeats, >=8 cycles, >=200 calls/block, nonnegative warmups")
    if args.watchdog_seconds < 10:
        parser.error("watchdog must be >=10 seconds")
    if psycopg.pq.__impl__ != "qt-thread":
        parser.error(f"expected PSYCOPG_IMPL=qt-thread, got {psycopg.pq.__impl__!r}")

    if args.child_repeat is not None:
        print(json.dumps(run_repeat(
            args.dsn, args.child_repeat, args.cycles, args.block_size,
            args.warmup, args.block_warmup,
        )))
        return 0

    result: dict[str, Any] = {
        "diagnostic": "real qt-thread GET status with local worker-run/method phase clocks",
        "python": sys.version.split()[0],
        "implementation": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "psycopg_module": str(psycopg.__file__),
        "repeats_requested": args.repeats,
        "cycles_per_repeat": args.cycles,
        "calls_per_block": args.block_size,
        "warmup_per_connection": args.warmup,
        "warmup_per_block": args.block_warmup,
        "watchdog_per_repeat_seconds": args.watchdog_seconds,
        "production_runtime_modified": False,
        "canonical_benchmark_v3_modified": False,
    }
    runner = Path(__file__).with_name("run_with_pg_root.py")
    reports: list[dict] = []
    for repeat in range(args.repeats):
        cmd = [
            sys.executable, str(runner), str(Path(__file__).resolve()), args.dsn,
            "--child-repeat", str(repeat), "--repeats", str(args.repeats),
            "--cycles", str(args.cycles), "--block-size", str(args.block_size),
            "--warmup", str(args.warmup), "--block-warmup", str(args.block_warmup),
        ]
        try:
            cp = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=args.watchdog_seconds, check=False,
            )
        except subprocess.TimeoutExpired:
            result["error"] = {"repeat": repeat, "reason": "child watchdog timeout"}
            result["repetitions"] = reports
            print(json.dumps(result, indent=2))
            return 2
        if cp.returncode:
            result["error"] = {
                "repeat": repeat, "exit_code": cp.returncode,
                "stderr_tail": cp.stderr[-2500:],
            }
            result["repetitions"] = reports
            print(json.dumps(result, indent=2))
            return 2
        try:
            reports.append(json.loads(cp.stdout))
        except (ValueError, TypeError):
            result["error"] = {
                "repeat": repeat, "reason": "invalid child JSON",
                "stdout_tail": cp.stdout[-1200:],
            }
            result["repetitions"] = reports
            print(json.dumps(result, indent=2))
            return 2

    result["repetitions"] = reports
    result["summary"] = aggregate(reports)
    result["runtime_candidate_supported"] = False
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
