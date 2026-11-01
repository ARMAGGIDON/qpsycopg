#!/usr/bin/env python3
"""Show which low-level qt-process calls cross the process boundary."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

import psycopg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--calls", type=int, default=5)
    args = parser.parse_args()

    if psycopg.pq.__impl__ != "qt-process":
        raise SystemExit("set PSYCOPG_IMPL=qt-process")

    with psycopg.connect(args.dsn) as connection:
        def selectOne() -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)

        for _ in range(args.warmup):
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
            perCall: list[list[dict[str, Any]]] = []
            for _ in range(args.calls):
                start = len(events)
                selectOne()
                perCall.append(events[start:])
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
    print(json.dumps({
        "calls": args.calls,
        "remote_per_call": sum(remoteCounts.values()) / args.calls,
        "remote_counts": dict(remoteCounts),
        "cached_counts": dict(cachedCounts),
        "first_call": perCall[0] if perCall else [],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
