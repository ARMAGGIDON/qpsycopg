#!/usr/bin/env python3
"""Compare an upstream pq backend with qpsycopg's qt-process backend.

Run this file in separate processes because PSYCOPG_IMPL is selected on import.
Examples:

    PSYCOPG_IMPL=python python tools/bench_qt_process.py DSN
    PSYCOPG_IMPL=qt-process QPSYCOPG_WORKER_IMPL=python \
        python tools/bench_qt_process.py DSN
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Callable

import psycopg


def timed(call: Callable[[], Any], count: int) -> list[float]:
    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        call()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return samples


def summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
    }


def processRss() -> int | None:
    """Return parent + child RSS if optional psutil is installed."""
    try:
        psutil = importlib.import_module("psutil")
    except ImportError:
        return None

    process = psutil.Process()
    rss = process.memory_info().rss
    for child in process.children(recursive=True):
        try:
            rss += child.memory_info().rss
        except psutil.Error:
            pass
    return int(rss)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "impl": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "iterations": args.iterations,
        "rows": args.rows,
        "threads": args.threads,
    }

    connectSamples = timed(lambda: _connectClose(args.dsn), min(20, args.iterations))
    result["connect"] = summary(connectSamples)

    with psycopg.connect(args.dsn) as connection:

        def selectOne() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)

        result["select_1"] = summary(timed(selectOne, args.iterations))

        rssBefore = processRss()
        start = time.perf_counter()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT i, i::numeric / 10, clock_timestamp() "
                "FROM generate_series(1, %s) AS s(i)",
                (args.rows,),
            )
            rows = cursor.fetchall()
        elapsed = time.perf_counter() - start
        rssAfter = processRss()
        result["fetch"] = {
            "seconds": elapsed,
            "rows": len(rows),
            "rows_per_second": len(rows) / max(elapsed, 1e-9),
        }
        if rssBefore is not None and rssAfter is not None:
            result["fetch"].update(
                {
                    "rss_before_mb": rssBefore / 1024**2,
                    "rss_after_mb": rssAfter / 1024**2,
                    "rss_delta_mb": (rssAfter - rssBefore) / 1024**2,
                }
            )
        assert rows and isinstance(rows[0][1], Decimal)

        # Same Connection, different Cursor per Python thread: this is the
        # documented Psycopg threading model and must remain serialised safely.
        barrier = threading.Barrier(args.threads)

        def threaded(index: int) -> int:
            barrier.wait()
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s::int + pg_backend_pid() * 0", (index,))
                row = cursor.fetchone()
                assert row is not None
                return int(row[0])

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            values = list(pool.map(threaded, range(args.threads)))
        result["threads"] = {
            "seconds": time.perf_counter() - start,
            "values": values,
        }

        # A normal PostgreSQL error must not kill the worker/session.
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM qpsycopg_table_that_does_not_exist")
        except psycopg.errors.UndefinedTable:
            connection.rollback()
        else:
            raise AssertionError("UndefinedTable was not raised")
        selectOne()
        result["sql_error_survival"] = True

        # Cancel is tested from another thread while the query is running.
        cancelled: list[str] = []

        def sleepingQuery() -> None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_sleep(30)")
            except psycopg.errors.QueryCanceled:
                cancelled.append("QueryCanceled")
                connection.rollback()

        thread = threading.Thread(target=sleepingQuery)
        thread.start()
        time.sleep(0.5)
        start = time.perf_counter()
        connection.cancel_safe(timeout=5.0)
        thread.join(10.0)
        result["cancel"] = {
            "seconds": time.perf_counter() - start,
            "exception": cancelled[0] if cancelled else None,
            "thread_alive": thread.is_alive(),
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _connectClose(dsn: str) -> None:
    connection = psycopg.connect(dsn)
    connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
