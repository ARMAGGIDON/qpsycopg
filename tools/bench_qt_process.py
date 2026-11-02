#!/usr/bin/env python3
"""Compare an upstream pq backend with qpsycopg's qt-process backend."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Callable

import psycopg
from psycopg._transformer import Transformer

from process_resources import processResources, resourceWindow


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


def remoteRequests(connection: Any) -> int | None:
    transport = getattr(connection.pgconn, "_transport", None)
    return getattr(transport, "requestId", None)


def remoteDelta(before: int | None, after: int | None) -> int | None:
    return None if before is None or after is None else after - before


def addResources(section: dict[str, Any], before: Any, after: Any, count: int | None = None) -> None:
    window = resourceWindow(before, after)
    if window is None:
        return
    if count:
        if (cpu := window.get("tree_cpu_total_seconds")) is not None:
            window["tree_cpu_us_per_call"] = cpu * 1_000_000 / count
        if (switches := window.get("tree_ctx_total")) is not None:
            window["tree_ctx_switches_per_call"] = switches / count
    section["resources"] = window


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    defaultIpc = "pipe" if os.name == "nt" else "tcp"
    result: dict[str, Any] = {
        "impl": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "ipc": os.environ.get("QPSYCOPG_IPC", defaultIpc) if psycopg.pq.__impl__ == "qt-process" else None,
        "transformer": {
            "class": f"{Transformer.__module__}.{Transformer.__qualname__}",
            "decoder": getattr(Transformer, "__qpsycopg_decoder__", None),
        },
        "iterations": args.iterations,
        "rows": args.rows,
        "threads": args.threads,
    }

    resourcesBefore = processResources()
    connectSamples = timed(lambda: _connectClose(args.dsn), min(20, args.iterations))
    resourcesAfter = processResources()
    result["connect"] = summary(connectSamples)
    addResources(result["connect"], resourcesBefore, resourcesAfter, min(20, args.iterations))

    with psycopg.connect(args.dsn) as connection:
        result["resources_connected"] = processResources()

        def selectOne() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)

        requestsBefore = remoteRequests(connection)
        resourcesBefore = processResources()
        selectSamples = timed(selectOne, args.iterations)
        resourcesAfter = processResources()
        requestsAfter = remoteRequests(connection)
        result["select_1"] = summary(selectSamples)
        addResources(result["select_1"], resourcesBefore, resourcesAfter, args.iterations)
        if (requestCount := remoteDelta(requestsBefore, requestsAfter)) is not None:
            result["select_1"].update({
                "remote_requests": requestCount,
                "remote_requests_per_call": requestCount / args.iterations,
            })

        resourcesBefore = processResources()
        requestsBefore = remoteRequests(connection)
        start = time.perf_counter()
        with connection.cursor() as cursor:
            executeStart = time.perf_counter()
            cursor.execute(
                "SELECT i, i::numeric / 10, clock_timestamp() FROM generate_series(1, %s) AS s(i)",
                (args.rows,),
            )
            executeElapsed = time.perf_counter() - executeStart
            requestsAfterExecute = remoteRequests(connection)
            fetchStart = time.perf_counter()
            rows = cursor.fetchall()
            fetchallElapsed = time.perf_counter() - fetchStart
            requestsAfterFetch = remoteRequests(connection)
        elapsed = time.perf_counter() - start
        resourcesAfter = processResources()
        result["fetch"] = {
            "seconds": elapsed,
            "execute_seconds": executeElapsed,
            "fetchall_seconds": fetchallElapsed,
            "rows": len(rows),
            "rows_per_second": len(rows) / max(elapsed, 1e-9),
        }
        addResources(result["fetch"], resourcesBefore, resourcesAfter)
        resources = result["fetch"].get("resources", {})
        if "tree_rssmb_before" in resources:
            result["fetch"].update({
                "rss_before_mb": resources["tree_rssmb_before"],
                "rss_after_mb": resources["tree_rssmb_after"],
                "rss_delta_mb": resources["tree_rssmb_delta"],
            })
        if (count := remoteDelta(requestsBefore, requestsAfterExecute)) is not None:
            result["fetch"]["execute_remote_requests"] = count
        if (count := remoteDelta(requestsAfterExecute, requestsAfterFetch)) is not None:
            result["fetch"]["fetchall_remote_requests"] = count
        assert rows and isinstance(rows[0][1], Decimal)

        barrier = threading.Barrier(args.threads)

        def threaded(index: int) -> int:
            barrier.wait()
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s::int + pg_backend_pid() * 0", (index,))
                row = cursor.fetchone()
                assert row is not None
                return int(row[0])

        requestsBefore = remoteRequests(connection)
        resourcesBefore = processResources()
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            values = list(pool.map(threaded, range(args.threads)))
        threadsElapsed = time.perf_counter() - start
        resourcesAfter = processResources()
        requestsAfter = remoteRequests(connection)
        result["threads"] = {"seconds": threadsElapsed, "values": values}
        addResources(result["threads"], resourcesBefore, resourcesAfter, args.threads)
        if (requestCount := remoteDelta(requestsBefore, requestsAfter)) is not None:
            result["threads"]["remote_requests"] = requestCount

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM qpsycopg_table_that_does_not_exist")
        except psycopg.errors.UndefinedTable:
            connection.rollback()
        else:
            raise AssertionError("UndefinedTable was not raised")
        selectOne()
        result["sql_error_survival"] = True

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
        requestsBefore = remoteRequests(connection)
        resourcesBefore = processResources()
        start = time.perf_counter()
        connection.cancel_safe(timeout=5.0)
        cancelReturned = time.perf_counter()
        thread.join(10.0)
        finished = time.perf_counter()
        resourcesAfter = processResources()
        requestsAfter = remoteRequests(connection)
        result["cancel"] = {
            "seconds": finished - start,
            "call_seconds": cancelReturned - start,
            "query_finish_seconds": finished - cancelReturned,
            "exception": cancelled[0] if cancelled else None,
            "thread_alive": thread.is_alive(),
        }
        addResources(result["cancel"], resourcesBefore, resourcesAfter)
        if (requestCount := remoteDelta(requestsBefore, requestsAfter)) is not None:
            result["cancel"]["remote_requests"] = requestCount
        result["resources_final"] = processResources()

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _connectClose(dsn: str) -> None:
    connection = psycopg.connect(dsn)
    connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
