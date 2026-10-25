#!/usr/bin/env python3
"""Small end-to-end smoke test for PSYCOPG_IMPL=qt-process."""

from __future__ import annotations

import argparse
import asyncio
import threading
from datetime import datetime, timezone
from decimal import Decimal

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
                    values.append(cursor.fetchone()[0])
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=query, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert sorted(values) == list(range(8))


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
