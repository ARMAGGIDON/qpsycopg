#!/usr/bin/env python3
"""Compare direct, dedicated-thread and qt-process short-query architectures."""

from __future__ import annotations

import argparse
import json
import queue
import statistics
import threading
import time
from typing import Any, Callable

import psutil
import psycopg


def summarize(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "mean_us": statistics.fmean(samples),
        "median_us": statistics.median(samples),
        "p95_us": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
        "min_us": ordered[0],
        "max_us": ordered[-1],
    }


def cpuSeconds(process: psutil.Process) -> float:
    value = process.cpu_times()
    total = float(value.user + value.system)
    for child in process.children(recursive=True):
        try:
            value = child.cpu_times()
            total += float(value.user + value.system)
        except psutil.Error:
            pass
    return total


def contextSwitches(process: psutil.Process) -> int:
    total = 0
    for item in [process, *process.children(recursive=True)]:
        try:
            value = item.num_ctx_switches()
            total += int(value.voluntary + value.involuntary)
        except psutil.Error:
            pass
    return total


class ThreadConnection:
    def __init__(self, dsn: str):
        self.requests: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self.responses: queue.SimpleQueue[tuple[bool, Any]] = queue.SimpleQueue()
        self.connection: Any = None
        self.thread = threading.Thread(target=self._run, args=(dsn,), name="qpsycopg-architecture-worker")
        self.thread.start()
        ok, value = self.responses.get()
        if not ok:
            self.thread.join()
            raise value

    def _run(self, dsn: str) -> None:
        try:
            connection = psycopg.connect(dsn)
            self.connection = connection
        except BaseException as error:
            self.responses.put((False, error))
            return
        self.responses.put((True, None))
        try:
            while True:
                operation, value = self.requests.get()
                try:
                    if operation == "close":
                        self.responses.put((True, None))
                        return
                    if operation == "select":
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT 1")
                            self.responses.put((True, cursor.fetchone()))
                    elif operation == "fetch":
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT i FROM generate_series(1, %s) AS s(i)", (value,))
                            self.responses.put((True, cursor.fetchall()))
                    elif operation == "sleep":
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT pg_sleep(%s)", (float(value),))
                            self.responses.put((True, cursor.fetchone()))
                    elif operation == "sleep_batch":
                        count, seconds = value
                        with connection.cursor() as cursor:
                            for _ in range(int(count)):
                                cursor.execute("SELECT pg_sleep(%s)", (float(seconds),))
                                cursor.fetchone()
                        self.responses.put((True, None))
                    else:
                        raise ValueError(operation)
                except BaseException as error:
                    if isinstance(error, psycopg.errors.QueryCanceled):
                        connection.rollback()
                    self.responses.put((False, error))
        finally:
            self.connection = None
            connection.close()

    def call(self, operation: str, value: Any = None) -> Any:
        self.requests.put((operation, value))
        ok, result = self.responses.get()
        if not ok:
            raise result
        return result

    def cancel(self, timeout: float = 5.0) -> None:
        connection = self.connection
        if connection is None:
            raise RuntimeError("thread connection is not available")
        connection.cancel_safe(timeout=timeout)

    def close(self) -> None:
        self.call("close")
        self.thread.join()


def measure(call: Callable[[], Any], iterations: int) -> tuple[list[float], float, float]:
    process = psutil.Process()
    cpuBefore = cpuSeconds(process)
    ctxBefore = contextSwitches(process)
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        call()
        samples.append((time.perf_counter_ns() - started) / 1_000)
    return samples, (cpuSeconds(process) - cpuBefore) * 1_000_000 / iterations, (contextSwitches(process) - ctxBefore) / iterations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--mode", choices=("direct", "thread"), default="direct")
    parser.add_argument("--iterations", type=int, default=2_000)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--rows", type=int, default=100_000)
    args = parser.parse_args()

    if args.mode == "thread":
        worker = ThreadConnection(args.dsn)
        selectOne = lambda: worker.call("select")
        fetchRows = lambda: worker.call("fetch", args.rows)
        close = worker.close
    else:
        connection = psycopg.connect(args.dsn)

        def selectOne() -> Any:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone()

        def fetchRows() -> Any:
            with connection.cursor() as cursor:
                cursor.execute("SELECT i FROM generate_series(1, %s) AS s(i)", (args.rows,))
                return cursor.fetchall()

        close = connection.close

    try:
        for _ in range(args.warmup):
            assert selectOne() == (1,)
        samples, cpuPerQuery, ctxPerQuery = measure(selectOne, args.iterations)
        started = time.perf_counter()
        rows = fetchRows()
        fetchSeconds = time.perf_counter() - started
        assert len(rows) == args.rows
    finally:
        close()

    print(json.dumps({
        "mode": args.mode,
        "impl": psycopg.pq.__impl__,
        "iterations": args.iterations,
        "select": summarize(samples),
        "tree_cpu_us_per_query": cpuPerQuery,
        "tree_ctx_switches_per_query": ctxPerQuery,
        "fetch_seconds": fetchSeconds,
        "fetch_rows_per_second": args.rows / fetchSeconds,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
