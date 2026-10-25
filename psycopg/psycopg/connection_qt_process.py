"""Connection classes using the process-isolated pq implementation."""

from __future__ import annotations

import asyncio
import warnings
from time import monotonic

from . import errors as e
from .abc import RV, AdaptContext, ConnParam, PQGen, PQGenConn
from .rows import AsyncRowFactory, Row, RowFactory
from .adapt import AdaptersMap
from .cursor import Cursor
from .cursor_async import AsyncCursor
from ._compat import Self
from .conninfo import conninfo_attempts, conninfo_attempts_async, make_conninfo
from .conninfo import timeout_from_conninfo
from ._capabilities import capabilities
from ._conninfo_utils import gssapi_requested
from .connection import ACTIVE, Connection as _Connection, _INTERRUPTED as _SYNC_INTERRUPTED
from .connection import _WAIT_INTERVAL, logger
from .connection_async import AsyncConnection as _AsyncConnection
from .connection_async import _INTERRUPTED as _ASYNC_INTERRUPTED
from .pq.pq_qt_process import waitRemote


def _waitTime(interval: float, deadline: float) -> float:
    return max(0.0, min(interval, deadline - monotonic()))


def _waitRemote(
    gen: PQGen[RV],
    handle: int,
    interval: float = _WAIT_INTERVAL,
    timeout: float | None = None,
) -> RV:
    deadline = monotonic() + timeout if timeout is not None else None
    try:
        state = next(gen)
        while True:
            wait = interval if deadline is None else _waitTime(interval, deadline)
            ready = waitRemote(handle, int(state), wait)
            state = gen.send(ready)
            if deadline is not None and monotonic() >= deadline:
                raise e._WaitTimeout("wait timeout expired")
    except StopIteration as ex:
        return ex.value


def _waitRemoteConn(
    gen: PQGenConn[RV],
    interval: float = _WAIT_INTERVAL,
    timeout: float | None = None,
) -> RV:
    deadline = monotonic() + timeout if timeout is not None else None
    try:
        handle, state = next(gen)
        while True:
            wait = interval if deadline is None else _waitTime(interval, deadline)
            ready = waitRemote(handle, int(state), wait)
            handle, state = gen.send(ready)
            if deadline is not None and monotonic() >= deadline:
                raise e._WaitTimeout("wait timeout expired")
    except StopIteration as ex:
        return ex.value


async def _waitRemoteAsync(
    gen: PQGen[RV],
    handle: int,
    interval: float = _WAIT_INTERVAL,
    timeout: float | None = None,
) -> RV:
    deadline = monotonic() + timeout if timeout is not None else None
    try:
        state = next(gen)
        while True:
            wait = interval if deadline is None else _waitTime(interval, deadline)
            ready = await asyncio.to_thread(waitRemote, handle, int(state), wait)
            state = gen.send(ready)
            if deadline is not None and monotonic() >= deadline:
                raise e._WaitTimeout("wait timeout expired")
    except StopIteration as ex:
        return ex.value


async def _waitRemoteConnAsync(
    gen: PQGenConn[RV],
    interval: float = _WAIT_INTERVAL,
    timeout: float | None = None,
) -> RV:
    deadline = monotonic() + timeout if timeout is not None else None
    try:
        handle, state = next(gen)
        while True:
            wait = interval if deadline is None else _waitTime(interval, deadline)
            ready = await asyncio.to_thread(waitRemote, handle, int(state), wait)
            handle, state = gen.send(ready)
            if deadline is not None and monotonic() >= deadline:
                raise e._WaitTimeout("wait timeout expired")
    except StopIteration as ex:
        return ex.value


class Connection(_Connection[Row]):
    """Psycopg Connection whose libpq session is isolated in a QProcess."""

    __module__ = "psycopg"

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        *,
        autocommit: bool = False,
        prepare_threshold: int | None = 5,
        context: AdaptContext | None = None,
        row_factory: RowFactory[Row] | None = None,
        cursor_factory: type[Cursor[Row]] | None = None,
        **kwargs: ConnParam,
    ) -> Self:
        params = cls._get_connection_params(conninfo, **kwargs)
        timeout = timeout_from_conninfo(params)
        rv = None
        attempts = conninfo_attempts(params)
        conn_errors: list[tuple[e.Error, str]] = []

        for attempt in attempts:
            tdescr = (attempt.get("host"), attempt.get("port"), attempt.get("hostaddr"))
            descr = "host: %r, port: %r, hostaddr: %r" % tdescr
            logger.debug("connection attempt: %s", descr)
            try:
                rv = _waitRemoteConn(
                    cls._connect_gen(make_conninfo("", **attempt)),
                    interval=_WAIT_INTERVAL,
                    timeout=timeout,
                )
            except e._WaitTimeout:
                conn_errors.append((e.ConnectionTimeout("connection timeout expired"), descr))
            except e.Error as error:
                conn_errors.append((error, descr))
            except e._NO_TRACEBACK as error:
                raise error.with_traceback(None)
            else:
                break

        if not rv:
            last = conn_errors[-1][0]
            if len(conn_errors) == 1:
                raise last.with_traceback(None)
            lines = [str(last), "Multiple connection attempts failed. All failures were:"]
            lines.extend(f"- {descr}: {error}" for error, descr in conn_errors)
            raise type(last)("\n".join(lines), pgconn=last.pgconn).with_traceback(None)

        if capabilities.has_used_gssapi() and rv.pgconn.used_gssapi and not gssapi_requested(params):
            warnings.warn(
                "the connection was obtained using GSSAPI through the libpq default; "
                "set gssencmode explicitly if this behaviour matters",
                RuntimeWarning,
            )

        rv._autocommit = bool(autocommit)
        if row_factory:
            rv.row_factory = row_factory
        if cursor_factory:
            rv.cursor_factory = cursor_factory
        if context:
            rv._adapters = AdaptersMap(context.adapters)
        rv.prepare_threshold = prepare_threshold
        return rv

    def wait(
        self,
        gen: PQGen[RV],
        interval: float = _WAIT_INTERVAL,
        timeout: float | None = None,
    ) -> RV:
        try:
            return _waitRemote(gen, self.pgconn.socket, interval, timeout)
        except _SYNC_INTERRUPTED:
            if self.pgconn.transaction_status == ACTIVE:
                self._try_cancel(timeout=5.0)
                try:
                    _waitRemote(gen, self.pgconn.socket, interval, 5.0)
                except e.QueryCanceled:
                    pass
                except e._WaitTimeout:
                    logger.warning("query not terminated after cancellation: closing connection")
                    self.pgconn.finish()
            raise

    def cancel_safe(self, *, timeout: float = 30.0) -> None:
        if not self._should_cancel():
            return
        if capabilities.has_cancel_safe():
            try:
                _waitRemoteConn(self._cancel_gen(), _WAIT_INTERVAL, timeout or None)
            except e._WaitTimeout:
                raise e.CancellationTimeout("cancellation timeout expired") from None
        else:
            self.cancel()


class AsyncConnection(_AsyncConnection[Row]):
    """AsyncConnection backed by the same isolated QProcess session."""

    __module__ = "psycopg"

    @classmethod
    async def connect(
        cls,
        conninfo: str = "",
        *,
        autocommit: bool = False,
        prepare_threshold: int | None = 5,
        context: AdaptContext | None = None,
        row_factory: AsyncRowFactory[Row] | None = None,
        cursor_factory: type[AsyncCursor[Row]] | None = None,
        **kwargs: ConnParam,
    ) -> Self:
        # No add_reader()/add_writer(): Windows ProactorEventLoop is supported.
        params = await cls._get_connection_params(conninfo, **kwargs)
        timeout = timeout_from_conninfo(params)
        rv = None
        attempts = await conninfo_attempts_async(params)
        conn_errors: list[tuple[e.Error, str]] = []

        for attempt in attempts:
            tdescr = (attempt.get("host"), attempt.get("port"), attempt.get("hostaddr"))
            descr = "host: %r, port: %r, hostaddr: %r" % tdescr
            try:
                rv = await _waitRemoteConnAsync(
                    cls._connect_gen(make_conninfo("", **attempt)),
                    _WAIT_INTERVAL,
                    timeout,
                )
            except e._WaitTimeout:
                conn_errors.append((e.ConnectionTimeout("connection timeout expired"), descr))
            except e.Error as error:
                conn_errors.append((error, descr))
            except e._NO_TRACEBACK as error:
                raise error.with_traceback(None)
            else:
                break

        if not rv:
            last = conn_errors[-1][0]
            if len(conn_errors) == 1:
                raise last.with_traceback(None)
            lines = [str(last), "Multiple connection attempts failed. All failures were:"]
            lines.extend(f"- {descr}: {error}" for error, descr in conn_errors)
            raise type(last)("\n".join(lines), pgconn=last.pgconn).with_traceback(None)

        if capabilities.has_used_gssapi() and rv.pgconn.used_gssapi and not gssapi_requested(params):
            warnings.warn(
                "the connection was obtained using GSSAPI through the libpq default; "
                "set gssencmode explicitly if this behaviour matters",
                RuntimeWarning,
            )

        rv._autocommit = bool(autocommit)
        if row_factory:
            rv.row_factory = row_factory
        if cursor_factory:
            rv.cursor_factory = cursor_factory
        if context:
            rv._adapters = AdaptersMap(context.adapters)
        rv.prepare_threshold = prepare_threshold
        return rv

    async def wait(
        self,
        gen: PQGen[RV],
        interval: float = _WAIT_INTERVAL,
        timeout: float | None = None,
    ) -> RV:
        try:
            return await _waitRemoteAsync(gen, self.pgconn.socket, interval, timeout)
        except _ASYNC_INTERRUPTED:
            if self.pgconn.transaction_status == ACTIVE:
                await self._try_cancel(timeout=5.0)
                try:
                    await _waitRemoteAsync(gen, self.pgconn.socket, interval, 5.0)
                except e.QueryCanceled:
                    pass
                except e._WaitTimeout:
                    logger.warning("query not terminated after cancellation: closing connection")
                    self.pgconn.finish()
            raise

    async def cancel_safe(self, *, timeout: float = 30.0) -> None:
        if not self._should_cancel():
            return
        if capabilities.has_cancel_safe():
            try:
                await _waitRemoteConnAsync(self._cancel_gen(), _WAIT_INTERVAL, timeout or None)
            except e._WaitTimeout:
                raise e.CancellationTimeout("cancellation timeout expired") from None
        else:
            await asyncio.to_thread(self.cancel)
