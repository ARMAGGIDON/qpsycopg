#!/usr/bin/env python3
"""Diagnostic-only comparison of minimal SimpleQueue echo and qt-thread status GET.

No modification to the production transport, benchmark v3, or wait mode.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import psutil
import psycopg

from psycopg.pq._qt_process_protocol import Operation

_POLL_SECONDS = 0.1
_TOKEN = object()
_STOP = object()
_METRICS = (
    "wall_us_per_call",
    "process_cpu_us_per_call",
    "caller_cpu_us_per_call",
    "other_threads_cpu_us_per_call",
    "context_switches_per_call",
)


class SimpleQueueEcho:
    """Minimal 2-queue, 1-worker echo with the same timed response wait."""

    def __init__(self) -> None:
        self.requests: queue.SimpleQueue[object] = queue.SimpleQueue()
        self.responses: queue.SimpleQueue[object] = queue.SimpleQueue()
        self.worker = threading.Thread(
            target=self._run, name="qpsycopg-diagnostic-echo", daemon=True
        )
        self.worker.start()

    def _run(self) -> None:
        while True:
            item = self.requests.get()
            if item is _STOP:
                self.responses.put(_STOP)
                return
            self.responses.put(item)

    def echo(self) -> None:
        self.requests.put(_TOKEN)
        while True:
            try:
                item = self.responses.get(timeout=_POLL_SECONDS)
                break
            except queue.Empty:
                if not self.worker.is_alive():
                    raise RuntimeError("minimal echo worker stopped without response")
        if item is not _TOKEN:
            raise AssertionError("unexpected echo response")

    def close(self) -> None:
        self.requests.put(_STOP)
        if self.responses.get(timeout=2.0) is not _STOP:
            raise AssertionError("echo worker stop response mismatch")
        self.worker.join(2.0)
        if self.worker.is_alive():
            raise RuntimeError("echo worker remained alive after close")

    def __enter__(self) -> SimpleQueueEcho:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def getStatus(transport: Any) -> None:
    value = transport.request(Operation.GET, 1, "status")
    if int(value) != int(psycopg.pq.ConnStatus.OK):
        raise AssertionError(f"unexpected qpsycopg worker status: {value!r}")


def counters() -> tuple[int, int, int]:
    switches = psutil.Process().num_ctx_switches()
    return (
        time.process_time_ns(),
        time.thread_time_ns(),
        switches.voluntary + switches.involuntary,
    )


def measure(func: Callable[[], None], count: int) -> dict[str, float]:
    processBefore, callerBefore, switchBefore = counters()
    started = time.perf_counter_ns()
    for _ in range(count):
        func()
    elapsed = time.perf_counter_ns() - started
    processAfter, callerAfter, switchAfter = counters()
    processCpu = processAfter - processBefore
    callerCpu = callerAfter - callerBefore
    return {
        "wall_us_per_call": elapsed / count / 1_000,
        "process_cpu_us_per_call": processCpu / count / 1_000,
        "caller_cpu_us_per_call": callerCpu / count / 1_000,
        "other_threads_cpu_us_per_call": (processCpu - callerCpu) / count / 1_000,
        "context_switches_per_call": (switchAfter - switchBefore) / count,
    }


def stats(values: list[float]) -> dict[str, Any]:
    median = statistics.median(values)
    return {
        "median": median,
        "mad": statistics.median(abs(value - median) for value in values),
        "min": min(values),
        "max": max(values),
        "positive_pairs": sum(value > 0 for value in values),
        "pair_count": len(values),
        "paired_differences": values,
    }


def summarize(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metric in _METRICS:
        delta = [row["qpsycopg"][metric] - row["echo"][metric] for row in pairs]
        result[metric] = {
            "difference": stats(delta),
            "echo": stats([row["echo"][metric] for row in pairs]),
            "qpsycopg": stats([row["qpsycopg"][metric] for row in pairs]),
            "interpretation": "qpsycopg GET status minus minimal echo, same units",
        }
    wall = result["wall_us_per_call"]
    result["echo_share_of_qpsycopg_wall_median"] = (
        wall["echo"]["median"] / wall["qpsycopg"]["median"]
    )
    result["note"] = (
        "Echo isolates two SimpleQueue handoffs and cross-thread scheduling. "
        "The GET-minus-echo difference includes status access, Request/Response "
        "creation, transport lock/cache, liveness checks and worker bookkeeping; "
        "it is not proven removable CPU overhead."
    )
    return result


def runMeasurement(dsn: str, pairs: int, iterations: int, warmup: int) -> dict:
    with psycopg.connect(dsn) as connection, SimpleQueueEcho() as echo:
        transport = connection.pgconn._transport
        work: dict[str, Callable[[], None]] = {
            "echo": echo.echo,
            "qpsycopg": lambda: getStatus(transport),
        }
        for func in work.values():
            for _ in range(warmup):
                func()

        samples: list[dict[str, Any]] = []
        for i in range(pairs):
            order = ("echo", "qpsycopg") if i % 2 == 0 else ("qpsycopg", "echo")
            row: dict[str, Any] = {"pair": i + 1, "order": list(order)}
            for mode in order:
                func = work[mode]
                for _ in range(warmup):
                    func()
                row[mode] = measure(func, iterations)
            samples.append(row)
    return {"pairs": samples, "summary": summarize(samples)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--pairs", type=int, default=8)
    parser.add_argument("--iterations", type=int, default=4000)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--watchdog-seconds", type=float, default=90.0)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()

    if args.pairs < 4 or args.iterations < 100 or args.warmup < 0:
        parser.error("expected pairs >=4, iterations >=100, warmup >=0")
    if args.watchdog_seconds < 10.0:
        parser.error("watchdog must be at least 10 seconds")
    if psycopg.pq.__impl__ != "qt-thread":
        parser.error(f"expected PSYCOPG_IMPL=qt-thread, got {psycopg.pq.__impl__}")

    if args.child:
        print(json.dumps(
            runMeasurement(args.dsn, args.pairs, args.iterations, args.warmup),
            indent=2,
        ))
        return 0

    result: dict[str, Any] = {
        "diagnostic": "minimal SimpleQueue echo versus qt-thread no-SQL status GET",
        "python": sys.version.split()[0],
        "psycopg_module": str(psycopg.__file__),
        "implementation": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "pairs_requested": args.pairs,
        "iterations": args.iterations,
        "warmup_per_mode_per_pair": args.warmup,
        "echo_response_timeout_seconds": _POLL_SECONDS,
        "watchdog_seconds": args.watchdog_seconds,
        "production_runtime_modified": False,
        "canonical_v3_modified": False,
    }
    runner = Path(__file__).with_name("run_with_pg_root.py")
    cmd = [
        sys.executable, str(runner), str(Path(__file__).resolve()), args.dsn,
        "--child", "--pairs", str(args.pairs),
        "--iterations", str(args.iterations), "--warmup", str(args.warmup),
    ]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=args.watchdog_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["error"] = "measurement watchdog terminated child"
        print(json.dumps(result, indent=2))
        return 2

    if completed.returncode:
        result["error"] = {
            "exit_code": completed.returncode,
            "stderr_tail": completed.stderr[-2500:],
        }
        print(json.dumps(result, indent=2))
        return 2
    try:
        result.update(json.loads(completed.stdout))
    except (ValueError, TypeError):
        result["error"] = {
            "reason": "unparseable child JSON",
            "stdout_tail": completed.stdout[-1000:],
            "stderr_tail": completed.stderr[-1000:],
        }
        print(json.dumps(result, indent=2))
        return 2

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
