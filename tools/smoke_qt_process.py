#!/usr/bin/env python3
"""Small end-to-end smoke test for PSYCOPG_IMPL=qt-process."""

from __future__ import annotations

import time
import asyncio
import argparse
import threading
from decimal import Decimal
from datetime import datetime, timezone

import psycopg


def syncSmoke(dsn: str) -> None:
    assert psycopg.pq.__impl__ == "qt-process", psycopg.pq.__impl__

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

        # SQL errors are returned to the synchronous call site and don't kill
        # a healthy PostgreSQL session.
        try:
            connection.execute("SELECT * FROM qpsycopg_missing_table")
        except psycopg.errors.UndefinedTable:
            connection.rollback()
        else:
            raise AssertionError("UndefinedTable wasn't propagated")
        assert connection.execute("SELECT 1").fetchone() == (1,)

        # One Connection, independent cursors from several Python threads.
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
            thread.join()
        assert not errors, errors
        assert sorted(values) == list(range(8))

        # Query cancellation must return QueryCanceled without killing the
        # connection worker.
        cancelled: list[BaseException] = []

        def sleepQuery() -> None:
            try:
                connection.execute("SELECT pg_sleep(30)")
            except BaseException as error:
                cancelled.append(error)

        thread = threading.Thread(target=sleepQuery)
        thread.start()
        time.sleep(0.3)
        connection.cancel_safe(timeout=5.0)
        thread.join(10.0)
        assert not thread.is_alive()
        assert len(cancelled) == 1
        assert isinstance(cancelled[0], psycopg.errors.QueryCanceled)
        connection.rollback()
        assert connection.execute("SELECT 1").fetchone() == (1,)

        try:
            connection.fileno()
        except psycopg.NotSupportedError:
            pass
        else:
            raise AssertionError("qt-process fileno() must not expose a fake fd")

    fatalConnectionSmoke(dsn)


def fatalConnectionSmoke(dsn: str) -> None:
    victim = psycopg.connect(dsn)
    killer = psycopg.connect(dsn, autocommit=True)
    try:
        pid = victim.info.backend_pid
        assert killer.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone() == (
            True,
        )
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
    print("qpsycopg qt-process smoke test: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
