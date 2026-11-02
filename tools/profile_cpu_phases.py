#!/usr/bin/env python3
"""Measure qpsycopg parent/worker CPU and parent hot-path phases."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from typing import Any, Callable

import psutil
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=200)
    args = parser.parse_args()

    parent = psutil.Process()
    with psycopg.connect(args.dsn) as connection:
        runSelect(connection, args.warmup)
        workers = workerProcesses(parent)

        parentBefore = cpuTime(parent)
        workerBefore = sum(cpuTime(process) for process in workers)
        wallStart = time.perf_counter_ns()
        runSelect(connection, args.iterations)
        wallNs = time.perf_counter_ns() - wallStart
        parentCpu = cpuTime(parent) - parentBefore
        workerCpu = sum(cpuTime(process) for process in workers) - workerBefore

        protocol = __import__("psycopg.pq._qt_process_protocol", fromlist=["x"])
        pqModule = __import__("psycopg.pq.pq_qt_process", fromlist=["x"])
        lazy = __import__("psycopg.pq._qt_process_lazy", fromlist=["x"])
        timings: dict[str, int] = defaultdict(int)
        calls: dict[str, int] = defaultdict(int)
        originals: list[tuple[Any, str, Any]] = []

        def patch(module: Any, attr: str, name: str, outerOnly: bool = False) -> None:
            original = getattr(module, attr)
            originals.append((module, attr, original))
            depth = 0

            def wrapped(*a: Any, **kw: Any) -> Any:
                nonlocal depth
                outer = depth == 0
                depth += 1
                start = time.thread_time_ns() if outer or not outerOnly else 0
                try:
                    return original(*a, **kw)
                finally:
                    depth -= 1
                    if outer or not outerOnly:
                        timings[name] += time.thread_time_ns() - start
                        calls[name] += 1

            setattr(module, attr, wrapped)

        patch(lazy, "_fastExecute", "fast_execute", True)
        patch(protocol, "_serialize", "serialize")
        patch(protocol, "_deserialize", "deserialize")
        patch(protocol, "_sendPayload", "socket_send")
        patch(protocol, "_readHeader", "read_header")
        patch(protocol, "_recvExact", "recv_exact")
        patch(protocol, "_restoreWireValue", "restore_wire")
        patch(pqModule, "_wire", "wire", True)
        patch(pqModule, "_restore", "restore", True)

        try:
            runSelect(connection, args.warmup)
            timings.clear()
            calls.clear()
            phaseStart = time.thread_time_ns()
            runSelect(connection, args.iterations)
            mainThreadNs = time.thread_time_ns() - phaseStart
        finally:
            for module, attr, original in reversed(originals):
                setattr(module, attr, original)

    phases = {
        name: {
            "calls": calls[name],
            "cpu_us_per_query": ns / args.iterations / 1_000,
        }
        for name, ns in sorted(timings.items(), key=lambda item: item[1], reverse=True)
    }
    print(json.dumps({
        "impl": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "ipc": os.environ.get("QPSYCOPG_IPC"),
        "iterations": args.iterations,
        "wall_us_per_query": wallNs / args.iterations / 1_000,
        "parent_cpu_us_per_query": parentCpu * 1_000_000 / args.iterations,
        "worker_cpu_us_per_query": workerCpu * 1_000_000 / args.iterations,
        "tree_cpu_us_per_query": (parentCpu + workerCpu) * 1_000_000 / args.iterations,
        "profiled_main_thread_cpu_us_per_query": mainThreadNs / args.iterations / 1_000,
        "worker_processes": len(workers),
        "phases": phases,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
