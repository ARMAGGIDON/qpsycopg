#!/usr/bin/env python3
"""End-to-end smoke test for PSYCOPG_IMPL=qt-thread."""

from __future__ import annotations

import argparse
import asyncio
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from qpsycopg_env import dsn  # isort: skip

import psycopg


def stage(name: str) -> None:
    print(f"[qt-thread smoke] {name}", flush=True)


def fatalConnectionSmoke(dsn: str) -> None:
    stage("fatal backend termination")
    stage("fatal / connect victim")
    victim = psycopg.connect(dsn)
    stage("fatal / connect killer")
    killer = psycopg.connect(dsn, autocommit=True)
    errors: list[BaseException] = []
    try:
        pid = victim.info.backend_pid
        stage("fatal / terminate backend")
        assert killer.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone() == (True,)

        stage("fatal / query terminated backend")

        def queryVictim() -> None:
            try:
                victim.execute("SELECT 1")
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=queryVictim, name="qt-thread-fatal-query", daemon=True)
        thread.start()
        thread.join(5.0)
        if thread.is_alive():
            transport = getattr(victim.pgconn, "_transport", None)
            if transport is not None:
                transport.kill()
            thread.join(1.0)
            raise AssertionError("terminated-backend query hung for more than 5 seconds")

        assert len(errors) == 1, errors
        assert isinstance(errors[0], psycopg.OperationalError), errors[0]
        stage("fatal / verify broken")
        assert victim.broken
    finally:
        stage("fatal / close victim")
        victim.close()
        stage("fatal / close killer")
        killer.close()
        stage("fatal / cleanup complete")


def syncSmoke(dsn: str) -> None:
    assert psycopg.pq.__impl__ == "qt-thread", psycopg.pq.__impl__

    stage("basic adaptation")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            value = (42, Decimal("12.50"), datetime.now(timezone.utc), b"abc")
            cursor.execute("SELECT %s::int, %s::numeric, %s::timestamptz, %s::bytea", value)
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == value[0]
            assert row[1] == value[1]
            assert row[3] == value[3]

        stage("packed NULL / empty semantics")
        with connection.cursor() as cursor:
            cursor.execute("SELECT NULL::text, ''::text, NULL::bytea, ''::bytea")
            assert cursor.fetchone() == (None, "", None, b"")

        stage("multiple result sets")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1; SELECT 2")
            assert cursor.fetchone() == (1,)
            assert cursor.nextset()
            assert cursor.fetchone() == (2,)
            assert cursor.nextset() is None

        stage("COPY result boundary")
        with connection.cursor() as cursor:
            with cursor.copy("COPY (SELECT 1) TO STDOUT") as copy:
                copied = b"".join(bytes(chunk) for chunk in copy)
            assert copied == b"1\n", copied

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
                row = connection.execute("SELECT %s::int", (number,)).fetchone()
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

        stage("unsupported caller fd")
        try:
            connection.fileno()
        except psycopg.NotSupportedError:
            pass
        else:
            raise AssertionError("qt-thread fileno() must not expose the worker socket")

    fatalConnectionSmoke(dsn)


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
    parser.add_argument("dsn", nargs="?", help="override QPSYCOPG_DSN from .env/environment")
    args = parser.parse_args()
    asyncio.run(mainAsync(dsn(args.dsn)))
    print("qpsycopg qt-thread smoke test: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
