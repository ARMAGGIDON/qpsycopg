#!/usr/bin/env python3
"""Profile direct binary libpq prepared-query send/wait/total latency."""

from __future__ import annotations

import argparse
import os
import select
import statistics
import time

os.environ.setdefault("PSYCOPG_IMPL", "binary")

from psycopg import pq


def _report(name: str, values: list[float]) -> None:
    values = [value * 1000 for value in values]
    ordered = sorted(values)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    print(
        f"{name:>6}: median={statistics.median(values):.4f} ms  "
        f"mean={statistics.mean(values):.4f} ms  p95={p95:.4f} ms"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn", nargs="?", default=os.environ.get("QPSYCOPG_DSN"))
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--calls", type=int, default=1000)
    options = parser.parse_args()
    if not options.dsn:
        parser.error("DSN is required: pass it as an argument or set QPSYCOPG_DSN")

    conn = pq.PGconn.connect(options.dsn.encode())
    name = b"qpsycopg_probe"
    result = conn.prepare(name, b"SELECT 1")
    if result.status != pq.ExecStatus.COMMAND_OK:
        raise RuntimeError(result.get_error_message())
    conn.nonblocking = 1

    sendTimes: list[float] = []
    waitTimes: list[float] = []
    totalTimes: list[float] = []

    def execute(record: bool) -> None:
        totalStart = time.perf_counter()
        sendStart = time.perf_counter()
        conn.send_query_prepared(name, None, None, 0)
        sendTime = time.perf_counter() - sendStart

        while conn.flush() != 0:
            select.select([conn.socket], [conn.socket], [])

        waitTime = 0.0
        while conn.is_busy():
            waitStart = time.perf_counter()
            select.select([conn.socket], [], [])
            waitTime += time.perf_counter() - waitStart
            conn.consume_input()

        result = conn.get_result()
        assert result is not None and result.get_value(0, 0) == b"1"
        assert conn.get_result() is None

        if record:
            sendTimes.append(sendTime)
            waitTimes.append(waitTime)
            totalTimes.append(time.perf_counter() - totalStart)

    try:
        for _ in range(options.warmup):
            execute(False)
        for _ in range(options.calls):
            execute(True)
    finally:
        conn.finish()

    _report("send", sendTimes)
    _report("wait", waitTimes)
    _report("total", totalTimes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
