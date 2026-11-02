#!/usr/bin/env python3
"""Measure Qt responsiveness and cancellation for direct, thread and qt-process architectures."""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from typing import Any

import psycopg
from PySide6.QtCore import QCoreApplication, QTimer, Qt

from bench_worker_architecture import ThreadConnection


def summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
        "max_ms": ordered[-1],
    }


class DirectConnection:
    def __init__(self, dsn: str):
        self.connection = psycopg.connect(dsn)

    def sleepBatch(self, count: int, seconds: float) -> None:
        with self.connection.cursor() as cursor:
            for _ in range(count):
                cursor.execute("SELECT pg_sleep(%s)", (seconds,))
                cursor.fetchone()

    def sleep(self, seconds: float) -> Any:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT pg_sleep(%s)", (seconds,))
            return cursor.fetchone()

    def selectOne(self) -> Any:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone()

    def cancel(self, timeout: float = 5.0) -> None:
        self.connection.cancel_safe(timeout=timeout)

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


def qtPhase(database: Any, caller: str, queries: int, sleepSeconds: float, timerMs: int) -> dict[str, Any]:
    app = QCoreApplication.instance() or QCoreApplication([])
    done = threading.Event()
    errors: list[BaseException] = []
    intervals: list[float] = []
    previous = time.perf_counter()

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(timerMs)

    def tick() -> None:
        nonlocal previous
        now = time.perf_counter()
        intervals.append((now - previous) * 1000)
        previous = now
        if done.is_set() and len(intervals) >= 5:
            app.quit()

    def databaseWork() -> None:
        try:
            if isinstance(database, ThreadConnection):
                database.call("sleep_batch", (queries, sleepSeconds))
            else:
                database.sleepBatch(queries, sleepSeconds)
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    timer.timeout.connect(tick)
    timer.start()
    worker: threading.Thread | None = None
    if caller == "thread":
        worker = threading.Thread(target=databaseWork, name="qpsycopg-qt-caller")
        QTimer.singleShot(100, worker.start)
    else:
        QTimer.singleShot(100, databaseWork)

    app.exec()
    timer.stop()
    if worker is not None:
        worker.join()
    if errors:
        raise errors[0]

    samples = intervals[1:]
    stats = summary(samples)
    stats["max_stall_ms"] = max(0.0, stats["max_ms"] - timerMs)
    return {"caller": caller, "timer": stats, "samples": len(samples)}


def cancelPhase(database: Any, delay: float) -> dict[str, Any]:
    result: list[str] = []
    errors: list[BaseException] = []

    def sleeping() -> None:
        try:
            if isinstance(database, ThreadConnection):
                database.call("sleep", 30.0)
            else:
                database.sleep(30.0)
        except psycopg.errors.QueryCanceled:
            result.append("QueryCanceled")
            if not isinstance(database, ThreadConnection):
                database.rollback()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=sleeping, name="qpsycopg-cancel-query")
    thread.start()
    time.sleep(delay)
    started = time.perf_counter()
    database.cancel(timeout=5.0)
    cancelReturned = time.perf_counter()
    thread.join(10.0)
    finished = time.perf_counter()

    if errors:
        raise errors[0]
    if thread.is_alive():
        raise RuntimeError("cancelled query thread did not stop")
    if result != ["QueryCanceled"]:
        raise RuntimeError(f"unexpected cancellation result: {result!r}")

    if isinstance(database, ThreadConnection):
        assert database.call("select") == (1,)
    else:
        assert database.selectOne() == (1,)
    return {
        "delay_seconds": delay,
        "cancel_call_seconds": cancelReturned - started,
        "query_finish_seconds": finished - cancelReturned,
        "total_seconds": finished - started,
        "exception": result[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--mode", choices=("direct", "thread"), default="direct")
    parser.add_argument("--caller", choices=("thread", "main"), default="thread")
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--sleep-ms", type=float, default=25.0)
    parser.add_argument("--timer-ms", type=int, default=10)
    parser.add_argument("--cancel-delay", type=float, default=.2)
    args = parser.parse_args()

    database: Any = ThreadConnection(args.dsn) if args.mode == "thread" else DirectConnection(args.dsn)
    try:
        qt = qtPhase(database, args.caller, args.queries, args.sleep_ms / 1000, args.timer_ms)
        cancel = cancelPhase(database, args.cancel_delay)
    finally:
        database.close()

    print(json.dumps({
        "mode": args.mode,
        "impl": psycopg.pq.__impl__,
        "qt": qt,
        "cancel": cancel,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
