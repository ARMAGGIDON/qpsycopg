#!/usr/bin/env python3
"""Compare upstream and qpsycopg pq backends under the same SQL workload."""

from __future__ import annotations

import argparse
import json
import math
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


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))]


def summary(samples: list[float]) -> dict[str, float]:
    return {
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": percentile(samples, 0.95),
        "p99_ms": percentile(samples, 0.99),
        "min_ms": min(samples),
        "max_ms": max(samples),
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
    parser.add_argument("--iterations", type=int, default=5_000)
    parser.add_argument("--warmup-iterations", type=int, default=200)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--thread-batches", type=int, default=20)
    parser.add_argument("--connect-iterations", type=int, default=40)
    parser.add_argument("--cancel-iterations", type=int, default=20)
    parser.add_argument("--cancel-settle-ms", type=float, default=100.0)
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
        "warmup_iterations": args.warmup_iterations,
        "rows": args.rows,
        "threads": args.threads,
        "thread_batches": args.thread_batches,
        "connect_iterations": args.connect_iterations,
        "cancel_iterations": args.cancel_iterations,
        "cancel_settle_ms": args.cancel_settle_ms,
    }

    resourcesBefore = processResources()
    start = time.perf_counter_ns()
    _connectClose(args.dsn)
    coldConnectMs = (time.perf_counter_ns() - start) / 1_000_000
    resourcesAfter = processResources()
    result["connect_cold"] = {"ms": coldConnectMs}
    addResources(result["connect_cold"], resourcesBefore, resourcesAfter, 1)

    resourcesBefore = processResources()
    connectSamples = timed(lambda: _connectClose(args.dsn), args.connect_iterations)
    resourcesAfter = processResources()
    result["connect_warm"] = summary(connectSamples)
    addResources(result["connect_warm"], resourcesBefore, resourcesAfter, args.connect_iterations)

    with psycopg.connect(args.dsn) as connection:
        result["resources_connected"] = processResources()

        def selectOne() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)

        for _ in range(args.warmup_iterations):
            selectOne()

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

        def threaded(index: int, barrier: threading.Barrier) -> int:
            barrier.wait()
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s::int + pg_backend_pid() * 0", (index,))
                row = cursor.fetchone()
                assert row is not None
                return int(row[0])

        def threadBatch(pool: ThreadPoolExecutor) -> tuple[float, list[int]]:
            barrier = threading.Barrier(args.threads + 1)
            futures = [pool.submit(threaded, index, barrier) for index in range(args.threads)]
            start = time.perf_counter_ns()
            barrier.wait()
            values = [future.result() for future in futures]
            return (time.perf_counter_ns() - start) / 1_000_000, values

        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            threadBatch(pool)  # Warm worker threads; don't benchmark pool creation.
            requestsBefore = remoteRequests(connection)
            resourcesBefore = processResources()
            threadSamples = []
            values = []
            for _ in range(args.thread_batches):
                elapsedMs, values = threadBatch(pool)
                threadSamples.append(elapsedMs)
            resourcesAfter = processResources()
            requestsAfter = remoteRequests(connection)

        result["threads"] = {
            "batch": summary(threadSamples),
            "batches": args.thread_batches,
            "queries_per_batch": args.threads,
            "queries_per_second": args.threads / (statistics.median(threadSamples) / 1000),
            "values": values,
        }
        addResources(result["threads"], resourcesBefore, resourcesAfter, args.threads * args.thread_batches)
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

        requestsBefore = remoteRequests(connection)
        resourcesBefore = processResources()
        cancelCallSamples: list[float] = []
        cancelFinishSamples: list[float] = []
        cancelTotalSamples: list[float] = []
        cancelExceptions: list[str] = []

        for _ in range(args.cancel_iterations):
            queryStarted = threading.Event()
            cancelled: list[str] = []
            queryErrors: list[BaseException] = []

            def sleepingQuery() -> None:
                try:
                    with connection.cursor() as cursor:
                        queryStarted.set()
                        cursor.execute("SELECT pg_sleep(30)")
                except psycopg.errors.QueryCanceled:
                    cancelled.append("QueryCanceled")
                    connection.rollback()
                except BaseException as error:
                    queryErrors.append(error)

            thread = threading.Thread(target=sleepingQuery, name="qpsycopg-cancel-bench")
            thread.start()
            if not queryStarted.wait(2.0):
                raise AssertionError("cancellation benchmark query did not start")
            time.sleep(args.cancel_settle_ms / 1000)

            start = time.perf_counter_ns()
            connection.cancel_safe(timeout=5.0)
            cancelReturned = time.perf_counter_ns()
            thread.join(10.0)
            finished = time.perf_counter_ns()

            if thread.is_alive():
                raise AssertionError("cancellation benchmark query did not finish")
            if queryErrors:
                raise queryErrors[0]
            if cancelled != ["QueryCanceled"]:
                raise AssertionError(f"unexpected cancellation result: {cancelled!r}")

            cancelCallSamples.append((cancelReturned - start) / 1_000_000)
            cancelFinishSamples.append((finished - cancelReturned) / 1_000_000)
            cancelTotalSamples.append((finished - start) / 1_000_000)
            cancelExceptions.append(cancelled[0])

        resourcesAfter = processResources()
        requestsAfter = remoteRequests(connection)
        result["cancel"] = {
            "iterations": args.cancel_iterations,
            "call": summary(cancelCallSamples),
            "query_finish": summary(cancelFinishSamples),
            "total": summary(cancelTotalSamples),
            "exceptions": cancelExceptions,
            "thread_alive": False,
        }
        addResources(result["cancel"], resourcesBefore, resourcesAfter, args.cancel_iterations)
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
