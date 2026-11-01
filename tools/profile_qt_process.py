#!/usr/bin/env python3
"""Show which low-level qt-process calls cross the process boundary."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from typing import Any

os.environ.setdefault("PSYCOPG_IMPL", "qt-process")
os.environ.setdefault("QPSYCOPG_WORKER_IMPL", "binary")

import psycopg

_DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("select", "fetch"), default="select")
    parser.add_argument("dsn", nargs="?", default=os.environ.get("QPSYCOPG_DSN", _DEFAULT_DSN))
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--calls", type=int, default=5)
    parser.add_argument("--rows", type=int, default=100_000)
    options = parser.parse_args()

    if psycopg.pq.__impl__ != "qt-process":
        raise SystemExit("set PSYCOPG_IMPL=qt-process")

    with psycopg.connect(options.dsn) as connection:
        def selectOne() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)

        for _ in range(options.warmup):
            selectOne()

        transport = connection.pgconn._transport
        original = transport.request
        events: list[dict[str, Any]] = []

        def traced(
            operation: Any,
            objectId: int = 0,
            member: str = "",
            args: tuple[Any, ...] = (),
            kwargs: tuple[tuple[str, Any], ...] = (),
        ) -> Any:
            before = transport.requestId
            value = original(operation, objectId, member, args, kwargs)
            after = transport.requestId
            events.append({
                "operation": getattr(operation, "name", str(operation)),
                "object_id": objectId,
                "member": member,
                "remote": after != before,
            })
            return value

        transport.request = traced
        try:
            start = time.perf_counter()
            if options.mode == "select":
                perCall: list[list[dict[str, Any]]] = []
                for _ in range(options.calls):
                    offset = len(events)
                    selectOne()
                    perCall.append(events[offset:])
            else:
                perCall = []
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT i, i::numeric / 10, clock_timestamp() "
                        "FROM generate_series(1, %s) AS s(i)",
                        (options.rows,),
                    )
                    assert cursor.pgresult is not None and cursor.pgresult.ntuples == options.rows
            elapsed = time.perf_counter() - start
        finally:
            transport.request = original

    remoteCounts = Counter(
        f"{event['operation']}:{event['member'] or '-'}"
        for event in events if event["remote"]
    )
    cachedCounts = Counter(
        f"{event['operation']}:{event['member'] or '-'}"
        for event in events if not event["remote"]
    )
    result: dict[str, Any] = {
        "mode": options.mode,
        "elapsed_seconds": elapsed,
        "remote_requests": sum(remoteCounts.values()),
        "remote_counts": dict(remoteCounts),
        "cached_counts": dict(cachedCounts),
    }
    if options.mode == "select":
        result.update({
            "calls": options.calls,
            "remote_per_call": sum(remoteCounts.values()) / options.calls,
            "first_call": perCall[0] if perCall else [],
        })
    else:
        result.update({"rows": options.rows, "first_events": events[:20]})

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
