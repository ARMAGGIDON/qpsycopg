#!/usr/bin/env python3
"""Small end-to-end smoke test for PSYCOPG_IMPL=qt-process."""

from __future__ import annotations

import argparse
import asyncio
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import psycopg


def stage(name: str) -> None:
    print(f"[qt-process smoke] {name}", flush=True)


def syncSmoke(dsn: str) -> None:
    assert psycopg.pq.__impl__ == "qt-process", psycopg.pq.__impl__

    stage("basic adaptation")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            value = (42, Decimal("12.50"), datetime.now(timezone.utc), b"abc")
            cursor.execute(
                "SELECT %s::int, %s::numeric, %s::timestamptz, %s::bytea",
                value,
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == value[0]
            assert row[1] == value[1]
            assert row[3] == value[3]

        stage("SQL error survival")
        try:
            connection.execute("SELECT * FROM qpsycopg_missing_table")
        except psycopg.errors.UndefinedTable:
            connection.rollback()
        else:
            raise AssertionError("UndefinedTable wasn't propagated")
        assert connection.execute("SELECT 1").fetchone() == (1,)

        stage("shared connection / per-thread cursors")
        values: list[int] = []
        errors: list[BaseException] = []

        def query(number: int) -> None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s::int", (number,))
                    row = cursor.fetchone()
                    assert row is not None
                    values.append(row[0])
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=query, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
            assert not thread.is_alive(), "threaded cursor query timed out"
        assert not errors, errors
        assert sorted(values) == list(range(8))

        stage("query cancellation")
        cancelled: list[BaseException] = []

        def sleepQuery() -> None:
            try:
                connection.execute("SELECT pg_sleep(10)")
            except BaseException as error:
                cancelled.append(error)

        thread = threading.Thread(target=sleepQuery)
        thread.start()
        time.sleep(0.3)
        connection.cancel_safe(timeout=3.0)
        thread.join(5.0)
        assert not thread.is_alive(), "cancelled query didn't terminate"
        assert len(cancelled) == 1, cancelled
        assert isinstance(cancelled[0], psycopg.errors.QueryCanceled), cancelled[0]
        connection.rollback()
        assert connection.execute("SELECT 1").fetchone() == (1,)

        stage("unsupported parent-process fd")
        try:
            connection.fileno()
        except psycopg.NotSupportedError:
            pass
        else:
            raise AssertionError("qt-process fileno() must not expose a fake fd")

    fatalConnectionSmoke(dsn)


def fatalConnectionSmoke(dsn: str) -> None:
    stage("fatal backend termination")
    victim = psycopg.connect(dsn)
    killer = psycopg.connect(dsn, autocommit=True)
    try:
        pid = victim.info.backend_pid
        result = killer.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()
        assert result == (True,)
        try:
            victim.execute("SELECT 1")
        except psycopg.OperationalError:
            pass
        else:
            raise AssertionError("terminated backend didn't break the connection")
        assert victim.broken
    finally:
        victim.close()
        killer.close()


async def asyncSmoke(dsn: str) -> None:
    stage("AsyncConnection")
    async with await psycopg.AsyncConnection.connect(dsn) as connection:
        cursor = await connection.execute("SELECT %s::int", (123,))
        assert await cursor.fetchone() == (123,)


async def mainAsync(dsn: str) -> None:
    syncSmoke(dsn)
    await asyncSmoke(dsn)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    args = parser.parse_args()
    asyncio.run(mainAsync(args.dsn))
    print("qpsycopg qt-process smoke test: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
