"""Preserve PostgreSQL startup errors across Qt worker boundaries.

Qt CI sets QPSYCOPG_CONNECT_ERROR_DSN to an existing PostgreSQL database
and runs these tests in separate upstream/Qt backend processes.
"""

from __future__ import annotations

import os
import asyncio

import pytest

import psycopg
from psycopg.conninfo import make_conninfo

_DSN = os.environ.get("QPSYCOPG_CONNECT_ERROR_DSN")
_MISSING_DB = "qpsycopg_connect_error_missing_database_92746"

pytestmark = pytest.mark.skipif(
    not _DSN, reason="requires QPSYCOPG_CONNECT_ERROR_DSN and PostgreSQL"
)


def _missing_database_dsn() -> str:
    assert _DSN is not None
    return make_conninfo(_DSN, dbname=_MISSING_DB, connect_timeout=5)


def _assert_server_error(error: psycopg.OperationalError) -> None:
    message = str(error)
    assert f'database "{_MISSING_DB}" does not exist' in message
    assert "FATAL" in message
    assert "qpsycopg worker process disconnected" not in message
    assert "qpsycopg worker thread stopped" not in message


def _assert_usable_connection() -> None:
    assert _DSN is not None
    with psycopg.connect(_DSN) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_missing_database_sync_error_and_reconnect() -> None:
    with pytest.raises(psycopg.OperationalError) as ex:
        psycopg.connect(_missing_database_dsn())
    _assert_server_error(ex.value)
    _assert_usable_connection()


def test_missing_database_async_error_and_reconnect() -> None:
    async def check() -> None:
        with pytest.raises(psycopg.OperationalError) as ex:
            await psycopg.AsyncConnection.connect(_missing_database_dsn())
        _assert_server_error(ex.value)

        assert _DSN is not None
        async with await psycopg.AsyncConnection.connect(_DSN) as conn:
            cursor = await conn.execute("SELECT 1")
            assert await cursor.fetchone() == (1,)

    asyncio.run(check())
