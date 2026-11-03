#!/usr/bin/env python3
"""Diagnostic-only full result-boundary A/B on real warmed SELECT 1.

Both modes use one native PGconn on one worker thread, the same PQexecParams
SQL operation, two SimpleQueues, and one qt-thread cursor AdaptContext.
Only the completed-result boundary differs. Not the production execution path.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
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
import psycopg_c.pq as native_pq
from psycopg import adapt
from psycopg.pq import _qt_process_worker_pool as worker_pool
from psycopg.pq import pq_qt_process as process_pq
from psycopg_c import _psycopg as native_fast

faulthandler.enable(all_threads=True)

_SQL = b"SELECT 1"
_MODES = ("snapshot", "native")
_FIELDS = (
    "wall_us_per_call", "process_cpu_us_per_call",
    "caller_cpu_us_per_call", "other_threads_cpu_us_per_call",
    "context_switches_per_call",
)


class ResultBoundary:
    """One worker, one connection, identical SQL, two queue handoffs."""

    def __init__(self, dsn: str, cursor: Any) -> None:
        self.cursor = cursor
        self.requests: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.responses: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self.worker = threading.Thread(
            target=self._run, args=(dsn,), daemon=True,
            name="diagnostic-native-result-boundary",
        )
        self.worker.start()
        status, detail = self.responses.get(timeout=15)
        if status != "ready":
            raise RuntimeError(f"worker startup failed: {detail!r}")

    def _run(self, dsn: str) -> None:
        conn: Any = None
        try:
            # This worker is the ONLY thread touching this PGconn.
            conn = native_pq.PGconn.connect(dsn.encode("utf-8"))
            if int(conn.status) != int(psycopg.pq.ConnStatus.OK):
                raise RuntimeError("native worker PGconn status is not OK")
            self.responses.put(("ready", None))
            while True:
                mode = self.requests.get()
                if mode == "stop":
                    self.responses.put(("stopped", None))
                    return
                if mode not in _MODES:
                    raise AssertionError(f"unexpected mode {mode!r}")

                # Identical extended-protocol SQL work for BOTH modes.
                result = conn.exec_params(_SQL, ())
                if mode == "snapshot":
                    payload = worker_pool._snapshotResult(result)
                    # Include the worker-side native PGresult release in
                    # the control's complete result-boundary cost.
                    result.clear()
                else:
                    payload = result
                self.responses.put(("result", payload))
                del result, payload
        except BaseException as ex:
            self.responses.put(("error", f"{type(ex).__name__}: {ex}"))
        finally:
            if conn is not None:
                conn.finish()

    def request(self, mode: str) -> None:
        if mode not in _MODES:
            raise ValueError(mode)
        if not self.worker.is_alive():
            raise RuntimeError("native boundary worker stopped")
        self.requests.put(mode)
        while True:
            try:
                status, payload = self.responses.get(timeout=0.1)
                break
            except queue.Empty:
                if not self.worker.is_alive():
                    raise RuntimeError("worker exited without a result")
        if status != "result":
            raise RuntimeError(f"native boundary worker returned {status}: {payload}")

        if mode == "snapshot":
            result = process_pq.PGresult(payload)
            transformer = adapt.Transformer(self.cursor)
        else:
            result = payload
            transformer = native_fast.Transformer(self.cursor)
        try:
            if (
                int(result.status) != int(psycopg.pq.ExecStatus.TUPLES_OK)
                or result.ntuples != 1 or result.nfields != 1
            ):
                raise AssertionError("SELECT 1 returned unexpected result metadata")
            transformer.set_pgresult(result)
            row = transformer.load_row(0, tuple)
            if row != (1,):
                raise AssertionError(f"native boundary output differs: {row!r}")
        finally:
            del transformer
            if mode == "native":
                # C PGresult is owned by the caller after queue transfer.
                result.clear()
            del result

    def close(self) -> None:
        if not self.worker.is_alive():
            raise RuntimeError("worker terminated before orderly close")
        self.requests.put("stop")
        status, _ = self.responses.get(timeout=10)
        if status != "stopped":
            raise RuntimeError(f"native worker close failed: {status}")
        self.worker.join(timeout=10)
        if self.worker.is_alive():
            raise RuntimeError("worker still alive after stop")

    def __enter__(self) -> ResultBoundary:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def stats(values: list[float]) -> dict[str, Any]:
    mid = statistics.median(values)
    return {
        "median": mid,
        "mad": statistics.median(abs(x - mid) for x in values),
        "min": min(values), "max": max(values),
        "positive_count": sum(x > 0 for x in values),
        "count": len(values),
    }


def measure(boundary: ResultBoundary, mode: str, calls: int,
            process: psutil.Process) -> dict[str, float | int]:
    before_switch = process.num_ctx_switches()
    before_cpu = time.process_time_ns()
    before_caller = time.thread_time_ns()
    start = time.perf_counter_ns()
    for _ in range(calls):
        boundary.request(mode)
    end = time.perf_counter_ns()
    process_cpu = time.process_time_ns() - before_cpu
    caller_cpu = time.thread_time_ns() - before_caller
    after_switch = process.num_ctx_switches()
    return {
        "midpoint_ns": (start + end) // 2,
        "wall_us_per_call": (end - start) / calls / 1000,
        "process_cpu_us_per_call": process_cpu / calls / 1000,
        "caller_cpu_us_per_call": caller_cpu / calls / 1000,
        "other_threads_cpu_us_per_call": (
            process_cpu - caller_cpu
        ) / calls / 1000,
        "context_switches_per_call": (
            after_switch.voluntary + after_switch.involuntary
            - before_switch.voluntary - before_switch.involuntary
        ) / calls,
    }


def run_repeat(dsn: str, repeat: int, cycles: int,
               block_size: int, warmup: int, block_warmup: int) -> dict:
    if psycopg.pq.__impl__ != "qt-thread":
        raise RuntimeError(f"unexpected implementation: {psycopg.pq.__impl__!r}")
    process = psutil.Process()
    cycle_rows: list[dict[str, Any]] = []
    benefits: list[dict[str, float]] = []
    all_blocks: dict[str, list[dict[str, float | int]]] = {
        mode: [] for mode in _MODES
    }
    # The context cursor is the same AdaptContext for both modes,
    # but performs no SQL in the measured worker connection.
    with psycopg.connect(dsn) as context_conn:
        with context_conn.cursor() as cursor:
            with ResultBoundary(dsn, cursor) as boundary:
                for mode in _MODES:
                    for _ in range(warmup):
                        boundary.request(mode)

                for cycle in range(cycles):
                    first = _MODES[(cycle + repeat) % 2]
                    second = _MODES[1 - ((cycle + repeat) % 2)]
                    order = (first, second, first, second, first)
                    blocks: list[dict[str, Any]] = []
                    for mode in order:
                        for _ in range(block_warmup):
                            boundary.request(mode)
                        metrics = measure(boundary, mode, block_size, process)
                        blocks.append({"mode": mode, "metrics": metrics})
                        all_blocks[mode].append(metrics)
                    pair_rows = []
                    for i in (1, 3):
                        target = blocks[i]
                        before = blocks[i - 1]["metrics"]
                        after = blocks[i + 1]["metrics"]
                        value = target["metrics"]
                        span = after["midpoint_ns"] - before["midpoint_ns"]
                        if span <= 0:
                            raise AssertionError("nonmonotonic block midpoints")
                        weight = (value["midpoint_ns"] - before["midpoint_ns"]) / span
                        if not 0 < weight < 1:
                            raise AssertionError("target outside reference time bracket")
                        delta: dict[str, float] = {}
                        for metric in _FIELDS:
                            reference = before[metric] + weight * (
                                after[metric] - before[metric]
                            )
                            delta[metric] = (
                                reference - value[metric]
                                if target["mode"] == "native"
                                else value[metric] - reference
                            )
                        pair_rows.append({
                            "target_mode": target["mode"],
                            "interpolation_fraction": weight,
                            "snapshot_minus_native": delta,
                        })
                        benefits.append(delta)
                    cycle_rows.append({
                        "cycle": cycle + 1,
                        "order": list(order),
                        "blocks": blocks,
                        "pairs": pair_rows,
                        "reference_wall_change_us": (
                            blocks[4]["metrics"]["wall_us_per_call"]
                            - blocks[0]["metrics"]["wall_us_per_call"]
                        ),
                    })

    drift = {}
    for mode in _MODES:
        first_blocks = [
            block["metrics"]["wall_us_per_call"]
            for cycle in cycle_rows[:3] for block in cycle["blocks"]
            if block["mode"] == mode
        ]
        last_blocks = [
            block["metrics"]["wall_us_per_call"]
            for cycle in cycle_rows[-3:] for block in cycle["blocks"]
            if block["mode"] == mode
        ]
        start, end = statistics.median(first_blocks), statistics.median(last_blocks)
        drift[mode] = {
            "first_three_cycle_median_wall_us": start,
            "last_three_cycle_median_wall_us": end,
            "relative_change": (end - start) / start,
        }

    return {
        "repeat": repeat,
        "cycles": cycle_rows,
        "paired_benefit": {
            metric: stats([row[metric] for row in benefits])
            for metric in _FIELDS
        },
        "mode_absolute": {
            mode: {
                metric: stats([row[metric] for row in all_blocks[mode]])
                for metric in _FIELDS
            }
            for mode in _MODES
        },
        "drift": {
            "per_mode": drift,
            "within_cycle_reference_wall_change_us": stats([
                row["reference_wall_change_us"] for row in cycle_rows
            ]),
        },
    }


def aggregate(reports: list[dict]) -> dict:
    result: dict[str, Any] = {
        "independent_repeats": len(reports),
        "material_cpu_threshold_us_per_select": 15.0,
        "paired_difference_definition": "snapshot minus native; positive favors native",
        "paired_benefit": {},
    }
    for metric in _FIELDS:
        medians = [r["paired_benefit"][metric]["median"] for r in reports]
        result["paired_benefit"][metric] = {
            "repeat_medians": medians,
            "across_repeat_medians": stats(medians),
            "positive_pairs_per_repeat": [
                r["paired_benefit"][metric]["positive_count"] for r in reports
            ],
            "pairs_per_repeat": [r["paired_benefit"][metric]["count"] for r in reports],
        }
    result["relative_wall_drift_by_repeat"] = {
        mode: [r["drift"]["per_mode"][mode]["relative_change"] for r in reports]
        for mode in _MODES
    }
    cpu = result["paired_benefit"]["process_cpu_us_per_call"]
    cpu_medians = cpu["repeat_medians"]
    cpu_stats = cpu["across_repeat_medians"]
    max_drift = max(
        abs(d)
        for mode in _MODES
        for d in result["relative_wall_drift_by_repeat"][mode]
    )
    noise = cpu_stats["mad"]
    result["predeclared_screen"] = {
        "target": "at least 15 us process CPU/SELECT, repeatable beyond noise and drift",
        "at_least_3_of_4_repeats_above_15us": sum(
            x >= 15 for x in cpu_medians
        ) >= 3,
        "all_repeats_positive": all(x > 0 for x in cpu_medians),
        "median_at_least_15us": cpu_stats["median"] >= 15,
        "median_exceeds_3x_mad": cpu_stats["median"] > 3 * noise,
        "no_repeat_above_15pct_wall_drift": max_drift <= 0.15,
    }
    result["predeclared_screen"]["passes"] = all(
        result["predeclared_screen"][key]
        for key in (
            "at_least_3_of_4_repeats_above_15us",
            "all_repeats_positive",
            "median_at_least_15us",
            "median_exceeds_3x_mad",
            "no_repeat_above_15pct_wall_drift",
        )
    )
    result["limitations"] = (
        "PQexecParams SELECT 1 runs identically on a single native PGconn "
        "owned by the worker for both modes. This tests the completed-result "
        "snapshot/reconstruction/hybrid decode versus native handoff/C decode "
        "boundary; it does not replicate the complete production qt-thread "
        "asynchronous execution, worker health/cache/notice logic, cursor "
        "setup, or canonical v3. No CPU delta is automatically a safe "
        "production optimization. Windows process CPU can be quantized."
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--cycles", type=int, default=12)
    parser.add_argument("--block-size", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=250)
    parser.add_argument("--block-warmup", type=int, default=32)
    parser.add_argument("--watchdog-seconds", type=float, default=120)
    parser.add_argument("--child-repeat", type=int)
    args = parser.parse_args()

    if (
        args.repeats < 4 or args.cycles < 8 or args.cycles % 2
        or args.block_size < 500 or args.warmup < 0
        or args.block_warmup < 0 or args.watchdog_seconds < 20
    ):
        parser.error("requires >=4 repeats, even cycles >=8, >=500 calls/block")

    if psycopg.pq.__impl__ != "qt-thread":
        parser.error("expected PSYCOPG_IMPL=qt-thread")
    if args.child_repeat is not None:
        print(json.dumps(run_repeat(
            args.dsn, args.child_repeat, args.cycles,
            args.block_size, args.warmup, args.block_warmup,
        )))
        return 0

    output: dict[str, Any] = {
        "diagnostic": "same-SQL full result boundary: snapshot vs native C PGresult",
        "implementation": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "python": sys.version.split()[0],
        "psycopg_module": str(psycopg.__file__),
        "sql": _SQL.decode("ascii"),
        "execution": "native PGconn.exec_params with empty params, same worker/connection",
        "queue_handoffs": 2,
        "adaptation": "one qt-thread cursor context for both modes, fresh Transformer",
        "repeats_requested": args.repeats,
        "cycles_per_repeat": args.cycles,
        "calls_per_block": args.block_size,
        "warmup_per_mode": args.warmup,
        "block_warmup": args.block_warmup,
        "watchdog_per_repeat_seconds": args.watchdog_seconds,
        "production_runtime_modified": False,
        "canonical_v3_modified": False,
        "runtime_candidate_authorized": False,
    }
    bootstrap = Path(__file__).with_name("run_with_pg_root.py")
    reports: list[dict] = []
    for repeat in range(args.repeats):
        cmd = [
            sys.executable, str(bootstrap), str(Path(__file__).resolve()),
            args.dsn, "--child-repeat", str(repeat),
            "--repeats", str(args.repeats), "--cycles", str(args.cycles),
            "--block-size", str(args.block_size), "--warmup", str(args.warmup),
            "--block-warmup", str(args.block_warmup),
        ]
        try:
            run = subprocess.run(
                cmd, text=True, capture_output=True, check=False,
                timeout=args.watchdog_seconds,
            )
        except subprocess.TimeoutExpired:
            output["error"] = {"repeat": repeat, "reason": "watchdog timeout"}
            output["repetitions"] = reports
            print(json.dumps(output, indent=2))
            return 2
        if run.returncode:
            output["error"] = {
                "repeat": repeat,
                "exit_code": run.returncode,
                "exit_code_hex": f"0x{run.returncode & 0xffffffff:08X}",
                "stderr_tail": run.stderr[-12000:],
                "stdout_tail": run.stdout[-1000:],
            }
            output["repetitions"] = reports
            print(json.dumps(output, indent=2))
            return 2
        try:
            reports.append(json.loads(run.stdout))
        except (ValueError, TypeError):
            output["error"] = {
                "repeat": repeat,
                "reason": "invalid child JSON",
                "stdout_tail": run.stdout[-1000:],
                "stderr_tail": run.stderr[-2000:],
            }
            output["repetitions"] = reports
            print(json.dumps(output, indent=2))
            return 2

    output["repetitions"] = reports
    output["summary"] = aggregate(reports)
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
