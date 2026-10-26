#!/usr/bin/env python3
"""Measure Qt event-loop jitter while PostgreSQL work is in progress."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time

import psycopg
from PySide6.QtCore import QCoreApplication, QTimer, Qt


def summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max_ms": ordered[-1],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--sleep-ms", type=float, default=50.0)
    parser.add_argument("--timer-ms", type=int, default=10)
    parser.add_argument("--caller", choices=("thread", "main"), default="thread")
    args = parser.parse_args()

    app = QCoreApplication([])
    done = threading.Event()
    error: list[BaseException] = []
    intervals: list[float] = []
    previous = time.perf_counter()

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(args.timer_ms)

    def tick() -> None:
        nonlocal previous
        now = time.perf_counter()
        intervals.append((now - previous) * 1000)
        previous = now
        if done.is_set() and len(intervals) >= 5:
            app.quit()

    def databaseWork() -> None:
        try:
            with psycopg.connect(args.dsn) as connection:
                with connection.cursor() as cursor:
                    for _ in range(args.queries):
                        cursor.execute("SELECT pg_sleep(%s)", (args.sleep_ms / 1000,))
                        cursor.fetchone()
        except BaseException as ex:
            error.append(ex)
        finally:
            done.set()

    timer.timeout.connect(tick)
    timer.start()

    worker: threading.Thread | None = None
    if args.caller == "thread":
        worker = threading.Thread(target=databaseWork, name="database-work")
        QTimer.singleShot(100, worker.start)
    else:
        QTimer.singleShot(100, databaseWork)

    app.exec()
    timer.stop()
    if worker is not None:
        worker.join()
    if error:
        raise error[0]

    # The first interval includes event-loop startup and isn't representative.
    samples = intervals[1:]
    stats = summary(samples)
    stats["max_stall_ms"] = max(0.0, stats["max_ms"] - args.timer_ms)
    result = {
        "impl": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "caller": args.caller,
        "queries": args.queries,
        "query_sleep_ms": args.sleep_ms,
        "timer_interval_ms": args.timer_ms,
        "timer": stats,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
