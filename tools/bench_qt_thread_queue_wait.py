#!/usr/bin/env python3
"""Diagnostic-only timed/untimed qt-thread SimpleQueue response-wait A/B.

The production transport and canonical v3 benchmark are not modified.
Use the PowerShell runner to obtain repeated same-process paired measurements.
"""

from __future__ import annotations

import argparse
import json
import queue
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import psycopg

from psycopg import errors as e
from psycopg.pq import pq_qt_thread as qt
from psycopg.pq._qt_process_protocol import Operation

_ORIGINAL_DISPATCH = qt._ThreadTransport._dispatch
_METRICS = ("wall_us_per_request", "process_cpu_us_per_request", "context_switches_per_request")


def _untimedDispatch(self: Any, request: Any) -> Any:
    """Exact successful-response path with only the timed get changed.

    It intentionally demonstrates the lost liveness check when no response is
    ever sent. Never install this implementation in production.
    """
    if not self.thread.is_alive():
        reason = f": {self._stopReason}" if self._stopReason else ""
        self._markBroken()
        raise e.OperationalError(f"qpsycopg worker thread stopped{reason}")

    self._requests.put(request)
    while True:
        try:
            return self._responses.get()
        except queue.Empty:
            if self.closed:
                reason = f": {self._stopReason}" if self._stopReason else ""
                raise e.OperationalError(f"qpsycopg worker thread is closed{reason}")
            if self.thread.is_alive():
                continue
            reason = f": {self._stopReason}" if self._stopReason else ""
            self._markBroken()
            raise e.OperationalError(f"qpsycopg worker thread stopped{reason}")


def setMode(mode: str) -> None:
    qt._ThreadTransport._dispatch = (
        _ORIGINAL_DISPATCH if mode == "timed" else _untimedDispatch
    )


def statusGet(transport: Any) -> None:
    status = transport.request(Operation.GET, 1, "status")
    if int(status) != int(psycopg.pq.ConnStatus.OK):
        raise AssertionError(f"unexpected connection status: {status!r}")


def counters() -> tuple[int, int, int]:
    switches = psutil.Process().num_ctx_switches()
    return (
        time.process_time_ns(),
        switches.voluntary + switches.involuntary,
        time.thread_time_ns(),
    )


def measure(transport: Any, iterations: int) -> dict[str, float]:
    beforeCpu, beforeSwitches, beforeCaller = counters()
    start = time.perf_counter_ns()
    for _ in range(iterations):
        statusGet(transport)
    elapsed = time.perf_counter_ns() - start
    afterCpu, afterSwitches, afterCaller = counters()
    return {
        "wall_us_per_request": elapsed / iterations / 1_000,
        "process_cpu_us_per_request": (afterCpu - beforeCpu) / iterations / 1_000,
        "caller_cpu_us_per_request": (afterCaller - beforeCaller) / iterations / 1_000,
        "context_switches_per_request": (afterSwitches - beforeSwitches) / iterations,
    }


def summarizePairs(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric in _METRICS:
        values = [
            item["timed"][metric] - item["untimed"][metric] for item in pairs
        ]
        med = statistics.median(values)
        mad = statistics.median(abs(value - med) for value in values)
        out[metric] = {
            "interpretation": "timed minus untimed; positive favors untimed",
            "median": med,
            "mad": mad,
            "min": min(values),
            "max": max(values),
            "positive_pairs": sum(value > 0 for value in values),
            "pair_count": len(values),
            "paired_differences": values,
        }
    wall = out["wall_us_per_request"]
    out["hypothesis_passes_noise_gate"] = bool(
        wall["median"] >= 10.0
        and wall["median"] > 3.0 * wall["mad"]
        and wall["positive_pairs"] >= len(pairs) - 1
    )
    return out


class _ExitWithoutResponse:
    """Replace only the response queue in an isolated child process."""

    def __init__(self, original: Any) -> None:
        self._original = original

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._original.get(*args, **kwargs)

    def put(self, *args: Any, **kwargs: Any) -> None:
        raise SystemExit("intentional worker exit before responding")


def runChildScenario(dsn: str, mode: str, scenario: str) -> int:
    setMode(mode)
    # A deliberate worker crash must not print a misleading traceback.
    threading.excepthook = lambda args: None
    connection = psycopg.connect(dsn)
    transport = connection.pgconn._transport
    statusGet(transport)

    if scenario == "error_propagation":
        try:
            transport.request(Operation.GET, 1, "__qpsycopg_ab_missing__")
        except AttributeError:
            statusGet(transport)
            connection.close()
            result = {"scenario": scenario, "mode": mode, "ok": True}
        else:
            result = {"scenario": scenario, "mode": mode, "ok": False,
                      "reason": "remote AttributeError not propagated"}
    elif scenario == "close":
        connection.close()
        stopped = transport.closed and not transport.thread.is_alive()
        try:
            transport.request(Operation.GET, 1, "status")
        except e.OperationalError:
            rejected = True
        else:
            rejected = False
        result = {"scenario": scenario, "mode": mode, "ok": bool(stopped and rejected),
                  "worker_stopped": stopped, "post_close_rejected": rejected}
    elif scenario == "worker_exit_no_response":
        transport._responses = _ExitWithoutResponse(transport._responses)
        try:
            transport.request(Operation.GET, 1, "status")
        except e.OperationalError:
            result = {"scenario": scenario, "mode": mode,
                      "ok": not transport.thread.is_alive(),
                      "detected_worker_exit": True}
        else:
            result = {"scenario": scenario, "mode": mode, "ok": False,
                      "reason": "missing response was accepted"}
        # No connection cleanup after deliberate worker termination.
    else:
        raise ValueError(scenario)

    print(json.dumps(result), flush=True)
    return 0 if result["ok"] else 1


def checkScenario(dsn: str, mode: str, scenario: str, timeout: float) -> dict[str, Any]:
    runner = Path(__file__).with_name("run_with_pg_root.py")
    cmd = [
        sys.executable, str(runner), str(Path(__file__).resolve()), dsn,
        "--child-mode", mode, "--child-scenario", scenario,
    ]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the child: cannot leave a hung worker.
        return {"mode": mode, "scenario": scenario, "outcome": "watchdog_timeout",
                "timed_out": True, "passed": scenario == "worker_exit_no_response"
                and mode == "untimed", "production_safe": False}

    try:
        detail = json.loads(completed.stdout.strip())
    except (ValueError, TypeError):
        detail = {}
    passed = completed.returncode == 0 and detail.get("ok") is True
    if scenario == "worker_exit_no_response" and mode == "untimed":
        # A surprisingly quick return also deserves recording, but no change
        # is safe until the actual worker-liveness mechanism is understood.
        passed = False
    return {
        "mode": mode, "scenario": scenario, "outcome": "returned",
        "timed_out": False, "passed": passed,
        "returncode": completed.returncode, "detail": detail,
        "stderr_tail": completed.stderr[-800:] if completed.returncode else "",
        "production_safe": scenario != "worker_exit_no_response" or mode == "timed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--pairs", type=int, default=8)
    parser.add_argument("--iterations", type=int, default=4000)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--watchdog-seconds", type=float, default=8.0)
    parser.add_argument("--child-mode", choices=["timed", "untimed"])
    parser.add_argument("--child-scenario", choices=[
        "error_propagation", "close", "worker_exit_no_response",
    ])
    args = parser.parse_args()
    if args.child_mode or args.child_scenario:
        if not args.child_mode or not args.child_scenario:
            parser.error("--child-mode and --child-scenario must be used together")
        return runChildScenario(args.dsn, args.child_mode, args.child_scenario)
    if args.pairs < 4 or args.iterations < 100 or args.warmup < 0:
        parser.error("requires >=4 pairs, >=100 iterations, nonnegative warmup")
    if args.watchdog_seconds < 2:
        parser.error("watchdog must be at least 2 seconds")

    result: dict[str, Any] = {
        "method": "diagnostic-only qt-thread A/B; timed production _dispatch versus local untimed variant",
        "python": sys.version.split()[0],
        "psycopg_module": str(psycopg.__file__),
        "impl": psycopg.pq.__impl__,
        "worker_impl": None,
        "pairs_requested": args.pairs,
        "iterations": args.iterations,
        "warmup_per_mode": args.warmup,
        "watchdog_seconds": args.watchdog_seconds,
        "warnings": [
            "Synthetic GET status is not a SQL fast-execute request.",
            "The untimed path is deliberately unsafe when worker exits without response.",
            "Do not deploy the untimed variant; no runtime or benchmark-v3 change is made.",
        ],
    }
    if psycopg.pq.__impl__ != "qt-thread":
        raise RuntimeError("this diagnostic requires PSYCOPG_IMPL=qt-thread")

    result["worker_impl"] = __import__("os").environ.get("QPSYCOPG_WORKER_IMPL")
    samples: list[dict[str, Any]] = []
    try:
        with psycopg.connect(args.dsn) as connection:
            transport = connection.pgconn._transport
            for pair in range(args.pairs):
                order = ("timed", "untimed") if pair % 2 == 0 else ("untimed", "timed")
                row: dict[str, Any] = {"pair": pair + 1, "order": list(order)}
                for mode in order:
                    setMode(mode)
                    for _ in range(args.warmup):
                        statusGet(transport)
                    row[mode] = measure(transport, args.iterations)
                samples.append(row)
    finally:
        setMode("timed")

    result["pairs"] = samples
    result["summary"] = summarizePairs(samples)
    checks: list[dict[str, Any]] = []
    for scenario in ("error_propagation", "close", "worker_exit_no_response"):
        for mode in ("timed", "untimed"):
            checks.append(checkScenario(
                args.dsn, mode, scenario, args.watchdog_seconds,
            ))
    result["failure_scenarios"] = checks
    result["failure_checks_pass"] = all(item["passed"] for item in checks)
    result["untimed_detects_worker_exit"] = any(
        item["mode"] == "untimed"
        and item["scenario"] == "worker_exit_no_response"
        and item["outcome"] == "returned"
        for item in checks
    )
    result["production_change_authorized"] = False
    print(json.dumps(result, indent=2))
    return 0 if result["failure_checks_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
