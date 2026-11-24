#!/usr/bin/env python3
"""Profile worker-side qpsycopg CPU phases for repeated short queries."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import psutil

os.environ.setdefault("PSYCOPG_IMPL", "qt-process")
os.environ.setdefault("QPSYCOPG_WORKER_IMPL", "binary")
os.environ.setdefault("QPSYCOPG_IPC", "pipe")
os.environ.setdefault("QPSYCOPG_NATIVE_IPC", "1")
os.environ.setdefault("QPSYCOPG_PROFILE_PHASES", "1")

import psycopg


def cpuTime(process: psutil.Process) -> float:
    try:
        value = process.cpu_times()
        return float(value.user + value.system)
    except psutil.Error:
        return 0.0


def workerProcesses(parent: psutil.Process) -> list[psutil.Process]:
    result = []
    for child in parent.children(recursive=True):
        try:
            if "psycopg.pq._qt_process_worker_pool" in " ".join(child.cmdline()):
                result.append(child)
        except psutil.Error:
            pass
    return result


def runSelect(connection: Any, count: int) -> None:
    for _ in range(count):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)


def normalize(profile: dict[str, float | int], iterations: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in sorted(profile.items()):
        if key.endswith("_cpu_ns"):
            result[key.removesuffix("_cpu_ns") + "_cpu_us_per_query"] = float(value) / iterations / 1_000
        elif key.endswith("_seconds"):
            result[key.removesuffix("_seconds") + "_wall_us_per_query"] = float(value) * 1_000_000 / iterations
        elif key.endswith("_calls"):
            result[key.removesuffix("_calls") + "_calls_per_query"] = float(value) / iterations
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=200)
    args = parser.parse_args()

    parent = psutil.Process()
    with psycopg.connect(args.dsn) as connection:
        runSelect(connection, args.warmup)
        connection.pgconn.qpsycopg_profile_take()
        workers = workerProcesses(parent)

        parentBefore = cpuTime(parent)
        workerBefore = sum(cpuTime(process) for process in workers)
        wallStart = time.perf_counter_ns()
        runSelect(connection, args.iterations)
        wallNs = time.perf_counter_ns() - wallStart
        workerProfile = connection.pgconn.qpsycopg_profile_take()
        parentCpu = cpuTime(parent) - parentBefore
        workerCpu = sum(cpuTime(process) for process in workers) - workerBefore

    print(json.dumps({
        "impl": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "ipc": os.environ.get("QPSYCOPG_IPC"),
        "native_ipc": os.environ.get("QPSYCOPG_NATIVE_IPC"),
        "iterations": args.iterations,
        "wall_us_per_query": wallNs / args.iterations / 1_000,
        "parent_cpu_us_per_query": parentCpu * 1_000_000 / args.iterations,
        "worker_cpu_us_per_query": workerCpu * 1_000_000 / args.iterations,
        "tree_cpu_us_per_query": (parentCpu + workerCpu) * 1_000_000 / args.iterations,
        "worker_processes": len(workers),
        "worker_phases": normalize(workerProfile, args.iterations),
        "worker_raw": workerProfile,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
