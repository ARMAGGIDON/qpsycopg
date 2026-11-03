#!/usr/bin/env python3
"""Diagnostic-only staged SimpleQueue echo versus qt-thread GET status.

One real qpsycopg connection and one synthetic echo worker per independent
child process. No monkeypatches, production edits, or v3 workload changes.
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

from psycopg.pq import pq_qt_process as _base
from psycopg.pq._qt_process_protocol import Operation, Request, Response

_OK = int(psycopg.pq.ConnStatus.OK)
_BAD = int(psycopg.pq.ConnStatus.BAD)
_TOKEN = object()
_STOP = object()
_MODES = ("bare", "envelope", "validation", "bookkeeping", "qpsycopg")
_ENRICHED = _MODES[1:]
_METRICS = (
    "wall_us_per_call",
    "process_cpu_us_per_call",
    "caller_cpu_us_per_call",
    "other_threads_cpu_us_per_call",
    "context_switches_per_call",
)


class FakeConn:
    status = _OK


class Echo:
    """Synthetic worker with progressively enriched, documented behavior."""

    def __init__(self) -> None:
        self.requests: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self.responses: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self.mode = "bare"
        self.request_id = 0
        self.lock = threading.RLock()
        self.closed = False
        self.pending = None
        self.objects = {1: FakeConn()}
        self.notices: list[Any] = []
        self.connection_ready = True
        self.worker = threading.Thread(
            target=self._run, name="diagnostic-enriched-echo", daemon=True
        )
        self.worker.start()

    def set_mode(self, mode: str) -> None:
        if mode not in _MODES[:-1]:
            raise ValueError(mode)
        self.mode = mode

    def _run(self) -> None:
        while True:
            request = self.requests.get()
            if request is _STOP:
                self.responses.put(_STOP)
                return

            mode = self.mode
            if mode == "bare":
                self.responses.put(request)
                continue

            if mode == "envelope":
                self.responses.put(Response(request.requestId, value=_OK))
                continue

            if mode == "validation":
                if request.operation != Operation.GET or request.member != "status":
                    raise AssertionError("wrong validation-stage request")
                if request.objectId != 1:
                    raise AssertionError("wrong validation-stage object ID")
                self.responses.put(Response(request.requestId, value=_OK))
                continue

            # Bookkeeping stage: a synthetic analogue of the worker's object
            # table, status check, notices and exceptional response handling.
            # It does not call real libpq, nor model all worker semantics.
            try:
                obj = self.objects[request.objectId]
                if request.operation != Operation.GET:
                    raise AttributeError("unsupported diagnostic operation")
                value = getattr(obj, request.member)
                self.connection_ready = self.connection_ready and (
                    obj.status != _BAD
                )
                response = Response(
                    request.requestId, value=value, notices=tuple(self.notices)
                )
            except BaseException as ex:
                raise AssertionError(
                    f"unexpected bookkeeping-stage failure: {ex!r}"
                ) from ex
            finally:
                self.notices.clear()
            self.responses.put(response)

    def _roundtrip(self, request: Any) -> Any:
        if not self.worker.is_alive():
            raise RuntimeError("synthetic worker stopped")
        self.requests.put(request)
        while True:
            try:
                return self.responses.get(timeout=0.1)
            except queue.Empty:
                if not self.worker.is_alive():
                    raise RuntimeError("synthetic worker stopped without response")

    def _enveloped(self, mode: str) -> None:
        if mode == "bookkeeping":
            with self.lock:
                if self.closed:
                    raise RuntimeError("synthetic transport closed")
                if self.pending is not None:
                    raise AssertionError("unexpected pending synthetic operation")
                self.request_id += 1
                request = Request(
                    self.request_id, Operation.GET, 1, "status", _base._wire(()), ()
                )
                response = self._roundtrip(request)
                self._check_response(request, response)
                if _base._restore(response.value, self) != _OK:
                    raise AssertionError("wrong bookkeeping response value")
            return

        self.request_id += 1
        request = Request(self.request_id, Operation.GET, 1, "status")
        response = self._roundtrip(request)
        if mode == "validation":
            self._check_response(request, response)
        if response.value != _OK:
            raise AssertionError("wrong envelope response value")

    def _check_response(self, request: Request, response: Response) -> None:
        if response.requestId != request.requestId:
            raise AssertionError("response request ID mismatch")
        if response.error is not None or response.notices:
            raise AssertionError("unexpected synthetic error or notice")

    def invoke(self) -> None:
        if self.mode == "bare":
            response = self._roundtrip(_TOKEN)
            if response is not _TOKEN:
                raise AssertionError("wrong token response")
        else:
            self._enveloped(self.mode)

    def close(self) -> None:
        self.closed = True
        self.requests.put(_STOP)
        if self.responses.get(timeout=2) is not _STOP:
            raise AssertionError("synthetic worker close response mismatch")
        self.worker.join(2)
        if self.worker.is_alive():
            raise RuntimeError("synthetic worker did not stop")

    def __enter__(self) -> Echo:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def status_get(transport: Any) -> None:
    if int(transport.request(Operation.GET, 1, "status")) != _OK:
        raise AssertionError("qpsycopg GET returned unexpected status")


def measure(func: Callable[[], None], calls: int) -> dict[str, float]:
    proc = psutil.Process()
    beforeSwitch = proc.num_ctx_switches()
    beforeCpu, beforeCaller, started = (
        time.process_time_ns(), time.thread_time_ns(), time.perf_counter_ns()
    )
    for _ in range(calls):
        func()
    wall = time.perf_counter_ns() - started
    processCpu = time.process_time_ns() - beforeCpu
    callerCpu = time.thread_time_ns() - beforeCaller
    afterSwitch = proc.num_ctx_switches()
    return {
        "wall_us_per_call": wall / calls / 1000,
        "process_cpu_us_per_call": processCpu / calls / 1000,
        "caller_cpu_us_per_call": callerCpu / calls / 1000,
        "other_threads_cpu_us_per_call": (processCpu - callerCpu) / calls / 1000,
        "context_switches_per_call": (
            (afterSwitch.voluntary + afterSwitch.involuntary)
            - (beforeSwitch.voluntary + beforeSwitch.involuntary)
        ) / calls,
    }


def summary(values: list[float]) -> dict[str, Any]:
    median = statistics.median(values)
    return {
        "median": median,
        "mad": statistics.median(abs(x - median) for x in values),
        "range": [min(values), max(values)],
        "positive": sum(x > 0 for x in values),
        "n": len(values),
    }


def order_for_cycle(cycle: int, repeat: int) -> tuple[str, ...]:
    # Every four cycles rotates each mode through every ordinal position.
    # The next four cycles reverse order; repeats have different offsets.
    shift = (cycle + repeat) % len(_ENRICHED)
    items = list(_ENRICHED[shift:] + _ENRICHED[:shift])
    if (cycle // len(_ENRICHED)) % 2:
        items.reverse()
    return tuple(items)


def run_repeat(dsn: str, repeat: int, cycles: int, calls: int, warmup: int) -> dict:
    if psycopg.pq.__impl__ != "qt-thread":
        raise RuntimeError("expected qt-thread in benchmark subprocess")
    records: list[dict[str, Any]] = []
    with psycopg.connect(dsn) as connection, Echo() as echo:
        transport = connection.pgconn._transport

        def invoke(mode: str) -> None:
            if mode == "qpsycopg":
                status_get(transport)
            else:
                echo.set_mode(mode)
                echo.invoke()

        for mode in _MODES:
            for _ in range(warmup):
                invoke(mode)

        for cycle in range(cycles):
            order = order_for_cycle(cycle, repeat)
            baseline_before = measure(lambda: invoke("bare"), calls)
            observations: dict[str, dict[str, float]] = {}
            for mode in order:
                if mode != "qpsycopg":
                    echo.set_mode(mode)
                    op = echo.invoke
                else:
                    op = lambda: status_get(transport)
                observations[mode] = measure(op, calls)
            baseline_after = measure(lambda: invoke("bare"), calls)

            # Bracketed bare-echo controls: interpolate each mode's position
            # between surrounding controls. This removes *linear* local drift,
            # but cannot compensate nonlinear thermal or scheduling variation.
            residuals: dict[str, dict[str, float]] = {}
            for index, mode in enumerate(order, start=1):
                share = index / (len(order) + 1)
                residuals[mode] = {
                    key: observations[mode][key] - (
                        baseline_before[key]
                        + share * (baseline_after[key] - baseline_before[key])
                    )
                    for key in _METRICS
                }
            records.append({
                "cycle": cycle + 1,
                "order": list(order),
                "bare_before": baseline_before,
                "modes": observations,
                "bare_after": baseline_after,
                "residual_vs_interpolated_bare": residuals,
            })
    report: dict[str, Any] = {"repeat": repeat, "cycles": records}
    report["per_mode"] = {
        mode: {
            metric: summary([
                cycle["residual_vs_interpolated_bare"][mode][metric]
                for cycle in records
            ])
            for metric in _METRICS
        }
        for mode in _ENRICHED
    }
    report["stage_differences"] = {
        f"{current}_minus_{previous}": {
            metric: summary([
                cycle["residual_vs_interpolated_bare"][current][metric]
                - cycle["residual_vs_interpolated_bare"][previous][metric]
                for cycle in records
            ])
            for metric in _METRICS
        }
        for previous, current in zip(_ENRICHED[:-1], _ENRICHED[1:])
    }
    bareStart = records[0]["bare_before"]["wall_us_per_call"]
    bareEnd = records[-1]["bare_after"]["wall_us_per_call"]
    report["drift"] = {
        "start_bare_wall_us": bareStart,
        "end_bare_wall_us": bareEnd,
        "relative_start_to_end": (bareEnd - bareStart) / bareStart,
        "per_cycle_bare_wall_change_us": summary([
            c["bare_after"]["wall_us_per_call"]
            - c["bare_before"]["wall_us_per_call"] for c in records
        ]),
    }
    return report


def aggregate(reports: list[dict[str, Any]]) -> dict[str, Any]:
    # Independent child-process repetitions are the primary statistical unit.
    result: dict[str, Any] = {}
    for section in ("per_mode", "stage_differences"):
        names = reports[0][section]
        result[section] = {
            name: {
                metric: {
                    "across_independent_repeats": summary([
                        r[section][name][metric]["median"] for r in reports
                    ]),
                    "repeat_medians": [
                        r[section][name][metric]["median"] for r in reports
                    ],
                }
                for metric in _METRICS
            }
            for name in names
        }
    drift = [r["drift"]["relative_start_to_end"] for r in reports]
    result["relative_baseline_drift"] = summary(drift)
    result["repeats_with_over_15pct_absolute_drift"] = sum(
        abs(x) > 0.15 for x in drift
    )
    result["interpretation"] = (
        "Each enriched stage is a synthetic approximation, not a qpsycopg "
        "function-level CPU allocation profile. Bracket interpolation only "
        "corrects approximately linear within-cycle drift. Process CPU "
        "quantization and scheduler variation require caution. "
        "No observed residual is automatically removable or a SQL speedup."
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--cycles", type=int, default=16)
    parser.add_argument("--block-size", type=int, default=600)
    parser.add_argument("--warmup", type=int, default=250)
    parser.add_argument("--watchdog-seconds", type=float, default=90)
    parser.add_argument("--child-repeat", type=int)
    args = parser.parse_args()

    if args.repeats < 3 or args.cycles < 8 or args.block_size < 200:
        parser.error("requires >=3 repeats, >=8 cycles, >=200 calls/block")
    if args.warmup < 0 or args.watchdog_seconds < 10:
        parser.error("requires nonnegative warmup and >=10 s watchdog")
    if psycopg.pq.__impl__ != "qt-thread":
        parser.error(f"expected qt-thread, got {psycopg.pq.__impl__!r}")

    if args.child_repeat is not None:
        print(json.dumps(run_repeat(
            args.dsn, args.child_repeat, args.cycles,
            args.block_size, args.warmup,
        )))
        return 0

    result: dict[str, Any] = {
        "diagnostic": "bracketed enriched echo: representation, validation, worker bookkeeping, real GET",
        "python": sys.version.split()[0],
        "implementation": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "psycopg_module": str(psycopg.__file__),
        "repeats_requested": args.repeats,
        "cycles_per_repeat": args.cycles,
        "block_size": args.block_size,
        "warmup_per_mode_per_repeat": args.warmup,
        "response_wait_timeout_seconds": 0.1,
        "watchdog_per_repeat_seconds": args.watchdog_seconds,
        "production_changed": False,
        "benchmark_v3_changed": False,
        "mode_definitions": {
            "bare": "single identical token, two SimpleQueue handoffs",
            "envelope": "fresh protocol Request and Response NamedTuples, fixed status value",
            "validation": "envelope plus worker request and caller response checks",
            "bookkeeping": "validation plus synthetic object lookup/status, lock, liveness, notices and _wire/_restore",
            "qpsycopg": "real qt-thread transport GET of PGconn status, no SQL",
        },
    }
    reports: list[dict[str, Any]] = []
    runner = Path(__file__).with_name("run_with_pg_root.py")
    for index in range(args.repeats):
        cmd = [
            sys.executable, str(runner), str(Path(__file__).resolve()), args.dsn,
            "--child-repeat", str(index), "--repeats", str(args.repeats),
            "--cycles", str(args.cycles), "--block-size", str(args.block_size),
            "--warmup", str(args.warmup),
        ]
        try:
            cp = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=args.watchdog_seconds, check=False,
            )
        except subprocess.TimeoutExpired:
            result["error"] = {
                "repeat": index, "reason": "child watchdog timeout",
            }
            result["repetitions"] = reports
            print(json.dumps(result, indent=2))
            return 2
        if cp.returncode:
            result["error"] = {
                "repeat": index, "exit_code": cp.returncode,
                "stderr_tail": cp.stderr[-2500:],
            }
            result["repetitions"] = reports
            print(json.dumps(result, indent=2))
            return 2
        try:
            reports.append(json.loads(cp.stdout))
        except (ValueError, TypeError):
            result["error"] = {
                "repeat": index, "reason": "invalid child JSON",
                "stdout_tail": cp.stdout[-1000:],
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
