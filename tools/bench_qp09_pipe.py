"""QP09 same-host, per-transport benchmark (run separately for TCP/Pipe).

Set PSYCOPG_IMPL=qt-process and QPSYCOPG_IPC=tcp|pipe before importing psycopg.
Run QP08's previous Pipe implementation from a separate isolated environment.
Only compare medians of repeated, adjacent runs on the same Windows host.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time

import psutil
import psycopg
from psycopg.pq._qt_process_protocol import Operation


def _samples(func, *, warmup=50, repeat=5, iterations=100):
    for _ in range(warmup):
        func()
    durations = []
    for _ in range(repeat):
        begin = time.perf_counter()
        for _ in range(iterations):
            func()
        durations.append((time.perf_counter() - begin) / iterations * 1000)
    return {
        "median_ms": statistics.median(durations),
        "repeats_ms": durations,
        "min_ms": min(durations),
        "max_ms": max(durations),
    }


def _process_tree():
    parent = psutil.Process()
    processes = [parent, *parent.children(recursive=True)]
    rss = cpu = private = 0
    for process in processes:
        try:
            memory = process.memory_info()
            rss += memory.rss
            private += getattr(memory, "private", 0)
            times = process.cpu_times()
            cpu += times.user + times.system
        except psutil.Error:
            pass
    return {
        "rss_bytes": rss, "private_bytes": private,
        "cpu_seconds": cpu, "processes": len(processes),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("QPSYCOPG_OWNER_TEST_DSN"))
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--lazy", action="store_true",
                        help="enable the historical lazy worker-pool module on both revisions")
    args = parser.parse_args()
    if args.lazy:
        # QP08 selected its historical Pipe transport in this optional layer.
        from psycopg.pq import _qt_process_lazy as _lazy  # noqa: F401
    if not args.dsn:
        parser.error("--dsn or QPSYCOPG_OWNER_TEST_DSN required")
    if psycopg.pq.__impl__ != "qt-process":
        parser.error("requires PSYCOPG_IMPL=qt-process")
    if args.repeat < 3 or args.iterations < 10:
        parser.error("use at least three repeats and ten iterations")

    startup = []
    for _ in range(args.repeat):
        begin = time.perf_counter()
        with psycopg.connect(args.dsn) as connection:
            assert connection.execute("select 1").fetchone() == (1,)
            startup.append((time.perf_counter() - begin) * 1000)

    with psycopg.connect(args.dsn) as connection:
        transport = connection.pgconn._transport
        actual = getattr(transport, "ipcKind", None)
        if actual is None:
            actual = "pipe" if type(transport.sock).__name__ == "PipeStream" else "tcp"
        before = _process_tree()
        ipc = _samples(
            lambda: transport.request(Operation.GET, 1, "status"),
            warmup=200, repeat=args.repeat, iterations=args.iterations,
        )
        simple = _samples(
            lambda: connection.execute("select 1").fetchone(),
            warmup=200, repeat=args.repeat, iterations=args.iterations,
        )
        bulk = _samples(
            lambda: connection.execute("select generate_series(1, 100000)").fetchall(),
            warmup=2, repeat=args.repeat, iterations=1,
        )
        after = _process_tree()
        output = {
            "platform": os.name, "backend": psycopg.pq.__impl__,
            "ipc_configured": os.environ.get("QPSYCOPG_IPC", ""),
            "ipc_actual": actual, "worker_pid": transport.owner._osPid,
            "lazy_enabled": args.lazy,
            "postgresql_version": connection.execute(
                "show server_version"
            ).fetchone()[0],
            "startup_ms": startup, "startup_median_ms": statistics.median(startup),
            "ipc_roundtrip": ipc, "select_1": simple, "bulk_100k": bulk,
            "resources_before": before, "resources_after": after,
            "cpu_delta_seconds": after["cpu_seconds"] - before["cpu_seconds"],
        }
        print(json.dumps(output, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
