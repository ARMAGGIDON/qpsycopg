"""Real PostgreSQL coverage for QP10 and the shared qt-thread adapter."""

from __future__ import annotations

import asyncio
import os
import threading
from time import monotonic, sleep

import pytest
import psycopg
from psycopg import errors as e

_DSN = os.environ.get("QPSYCOPG_OWNER_TEST_DSN")
_IMPL = psycopg.pq.__impl__
pytestmark = pytest.mark.skipif(
    not _DSN or _IMPL not in ("qt-process", "qt-thread"),
    reason="requires explicit Qt backend and isolated PostgreSQL DSN",
)


@pytest.fixture
def dsn():
    assert _DSN
    if _IMPL == "qt-process":
        from psycopg.pq import _qt_process_lazy as lazy
        lazy._closeWorkerPool()
        try:
            yield _DSN
        finally:
            lazy._closeWorkerPool()
    else:
        yield _DSN


def _wait_active(observer, pid):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        row = observer.execute(
            "select state, wait_event from pg_stat_activity where pid = %s", (pid,)
        ).fetchone()
        if row == ("active", "PgSleep"):
            return
        sleep(0.01)
    raise AssertionError("query did not enter pg_sleep")


@pytest.mark.skipif(_IMPL != "qt-process", reason="QProcess pool generation test")
def test_real_pooled_generation_rejects_stale_control(dsn):
    first = psycopg.connect(dsn, autocommit=True)
    second = None
    old = first.pgconn
    transport = old._transport
    try:
        assert first.execute("select 1").fetchone() == (1,)
        client = old._cancelLifecycle.client
        assert client is not None
        first.close()
        assert client.closed and old._cancelLifecycle.state == "RETIRED"
        assert old._qpsycopgWorkerReleased
        second = psycopg.connect(dsn, autocommit=True)
        assert second.pgconn._transport is transport
        assert second.pgconn is not old
        assert second.execute("select 2").fetchone() == (2,)
        current = second.pgconn._cancelLifecycle.client
        assert current is not None and current is not client
        with pytest.raises(e.OperationalError):
            old._cancelLifecycle.ensure(old)
        with pytest.raises(e.OperationalError):
            client.cancel(1)
        old._cancelLifecycle.fail_if_closed(old, client)
        first.close()
        assert second.execute("select 3").fetchone() == (3,)
        assert not current.closed
    finally:
        if second is not None:
            second.close()
        first.close()


@pytest.mark.skipif(_IMPL != "qt-process", reason="QProcess failure retirement test")
def test_real_open_failure_retires_owner(dsn, monkeypatch):
    from psycopg.pq._qt_process_cancel import CancelControlClient
    from psycopg.pq._qt_process_owner import newConnectAttempt

    attempt = newConnectAttempt(timeout_s=15)
    with attempt:
        connection = psycopg.connect(dsn, autocommit=True)
    owner = attempt.owner
    assert owner is not None
    lifecycle = connection.pgconn._cancelLifecycle
    original = CancelControlClient.open.__func__
    clients = []

    def fail_after_ack(cls, pgconn, **kwargs):
        client = original(cls, pgconn, **kwargs)
        clients.append(client)
        client.abort()
        raise TimeoutError("injected lost control publication after real handshake")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(CancelControlClient, "open", classmethod(fail_after_ack))
            with pytest.raises(TimeoutError):
                lifecycle.ensure(connection.pgconn, 5)
            with pytest.raises(e.OperationalError):
                lifecycle.ensure(connection.pgconn, 5)
        assert len(clients) == 1 and clients[0].closed
        assert lifecycle.client is None and lifecycle.state == "RETIRED"
        assert connection.pgconn._transport.closed
        proof = owner.waitExit(owner.ownerToken, 10)
        assert proof is not None and proof.os_pid == owner._osPid
    finally:
        connection.close()
        owner.requestKill(owner.ownerToken)
        owner.thread.join(10)
        assert not owner.thread.is_alive()


def test_real_sync_cancel_and_close(dsn):
    connection = psycopg.connect(dsn, autocommit=True)
    observer = psycopg.connect(dsn, autocommit=True)
    errors = []
    worker = None

    def query():
        try:
            connection.execute("select pg_sleep(20)")
        except BaseException as error:
            errors.append(error)

    try:
        assert connection.execute("select 1").fetchone() == (1,)
        transport = connection.pgconn._transport
        client = transport._cancelControl
        assert client is not None and not client.closed
        pid = connection.pgconn.backend_pid
        worker = threading.Thread(target=query, daemon=True)
        worker.start()
        _wait_active(observer, pid)
        connection.cancel_safe(timeout=5)
        worker.join(5)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], e.QueryCanceled)
        assert connection.execute("select 2").fetchone() == (2,)
        connection.close()
        assert client.closed and transport._cancelControl is None
        assert observer.execute("select 3").fetchone() == (3,)
    finally:
        connection.close()
        observer.close()
        if worker is not None:
            worker.join(5)


def test_real_async_cancel_and_reuse(dsn):
    async def run():
        connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        observer = psycopg.connect(dsn, autocommit=True)
        query = None
        try:
            cursor = await connection.execute("select 1")
            assert await cursor.fetchone() == (1,)
            transport = connection.pgconn._transport
            client = transport._cancelControl
            pid = connection.pgconn.backend_pid
            query = asyncio.create_task(connection.execute("select pg_sleep(20)"))
            await asyncio.to_thread(_wait_active, observer, pid)
            await asyncio.wait_for(connection.cancel_safe(timeout=5), 6)
            with pytest.raises(e.QueryCanceled):
                await asyncio.wait_for(query, 5)
            cursor = await connection.execute("select 2")
            assert await cursor.fetchone() == (2,)
            await connection.close()
            assert client.closed and transport._cancelControl is None
            assert observer.execute("select 3").fetchone() == (3,)
        finally:
            await connection.close()
            observer.close()
            if query is not None and not query.done():
                query.cancel()
                await asyncio.gather(query, return_exceptions=True)

    asyncio.run(run())
