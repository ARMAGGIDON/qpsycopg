from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import time
from io import StringIO

import psycopg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--iterations", type=int, default=2_000)
    parser.add_argument("--top", type=int, default=40)
    args = parser.parse_args()

    with psycopg.connect(args.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        profiler = cProfile.Profile(timer=time.process_time)
        profiler.enable()
        for _ in range(args.iterations):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
        profiler.disable()

    stats = pstats.Stats(profiler).strip_dirs().sort_stats("cumtime")
    stream = StringIO()
    stats.stream = stream
    stats.print_stats(args.top)
    print(json.dumps({
        "impl": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "ipc": os.environ.get("QPSYCOPG_IPC"),
        "iterations": args.iterations,
        "process_cpu_seconds": stats.total_tt,
        "cpu_us_per_call": stats.total_tt * 1_000_000 / args.iterations,
        "profile": stream.getvalue(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
