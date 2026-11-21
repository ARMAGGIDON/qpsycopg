"""Explicit, per-attempt ownership for the optional qt-process backend.

No DBPT imports. The caller must retain ConnectAttempt while its watchdog is
active. A context is bound to the thread executing psycopg.connect().
"""

from __future__ import annotations

from enum import Enum
from time import monotonic
from uuid import UUID, uuid4
from typing import Any, Callable
from weakref import WeakValueDictionary
from threading import Event, Lock
from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class OwnerToken:
    value: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class AttemptToken:
    value: UUID = field(default_factory=uuid4)


class ExitState(str, Enum):
    KILL_QUEUED = "KILL_QUEUED"
    KILL_DISPATCHED = "KILL_DISPATCHED"
    OS_EXIT_CONFIRMED = "OS_EXIT_CONFIRMED"
    EXIT_UNCONFIRMED = "EXIT_UNCONFIRMED"


@dataclass(frozen=True, slots=True)
class KillReceipt:
    owner_token: OwnerToken
    state: ExitState
    timestamp: float
    os_pid: int | None
    diagnostic: str = ""


@dataclass(frozen=True, slots=True)
class ExitProof:
    owner_token: OwnerToken
    os_pid: int
    exit_code: int
    exit_status: int
    timestamp: float
    reason: str


_current: ContextVar[ConnectAttempt | None] = ContextVar(
    "qpsycopg_connect_attempt", default=None
)
_attempts: WeakValueDictionary[AttemptToken, ConnectAttempt] = WeakValueDictionary()
_attempts_lock = Lock()


class ConnectAttempt:
    """Scoped connection attempt with a kill handle available before connect I/O.

    Callbacks run synchronously in the connecting thread and must not touch
    widgets or block. Tokens are never inferred from PID, connection or profile.
    """

    def __init__(
        self,
        onOwnerCreated: Callable[[AttemptToken, OwnerToken, Any], None] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> None:
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.token = AttemptToken()
        self.deadline = monotonic() + timeout_s if timeout_s is not None else None
        self._callback = onOwnerCreated
        self._lock = Lock()
        self._cancelled = Event()
        self._owner: Any = None
        self._transport: Any = None
        self._owners: dict[OwnerToken, tuple[Any, Any]] = {}
        self._context: Token[ConnectAttempt | None] | None = None
        self._entered = False
        with _attempts_lock:
            _attempts[self.token] = self

    def __enter__(self) -> ConnectAttempt:
        with self._lock:
            if self._entered:
                raise RuntimeError("connect attempt token cannot be reused")
            self._entered = True
        self._context = _current.set(self)
        return self

    def __exit__(self, *_: object) -> None:
        token = self._context
        if token is None:
            raise RuntimeError("connect attempt was not entered")
        _current.reset(token)
        self._context = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def owner(self) -> Any:
        with self._lock:
            return self._owner

    @property
    def owners(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(pair[0] for pair in self._owners.values())

    @property
    def ownerToken(self) -> OwnerToken | None:
        owner = self.owner
        return None if owner is None else owner.ownerToken

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - monotonic())

    def check(self) -> None:
        if self.cancelled:
            raise RuntimeError(
                f"qpsycopg connecting attempt {self.token.value} cancelled"
            )
        if self.deadline is not None and monotonic() >= self.deadline:
            raise TimeoutError(
                f"qpsycopg connecting attempt {self.token.value} expired"
            )

    def _bind(self, owner: Any, transport: Any) -> None:
        with self._lock:
            if owner.ownerToken in self._owners:
                raise RuntimeError("duplicate physical owner token")
            self._owner = owner
            self._transport = transport
            self._owners[owner.ownerToken] = (owner, transport)
            cancelled = self._cancelled.is_set()
        # Publish before owner.thread.start(), QProcess.start(), accept and connect.
        try:
            if self._callback is not None:
                self._callback(self.token, owner.ownerToken, owner)
        finally:
            if cancelled or self._cancelled.is_set():
                owner.requestKill(owner.ownerToken)
                transport._abortConnect()
        self.check()

    def cancel(self) -> KillReceipt | None:
        """Never waits for the blocked CONNECTING thread or Qt owner thread."""
        self._cancelled.set()
        with self._lock:
            owners = tuple(self._owners.values())
        if not owners:
            return None  # No QProcess exists yet: not a successful OS-exit.
        receipt = None
        # Every owner was registered by this exact attempt token. Multi-host
        # fallback does not permit a stale earlier worker to survive cancel.
        for owner, transport in owners:
            transport._abortConnect()
            receipt = owner.requestKill(owner.ownerToken)
        return receipt

    def exitProof(self) -> ExitProof | None:
        owner = self.owner
        return None if owner is None else owner.exitProof(owner.ownerToken)

    def waitExit(self, timeout: float) -> ExitProof | None:
        owner = self.owner
        return None if owner is None else owner.waitExit(owner.ownerToken, timeout)


def currentConnectAttempt() -> ConnectAttempt | None:
    return _current.get()


def newConnectAttempt(
    onOwnerCreated: Callable[[AttemptToken, OwnerToken, Any], None] | None = None,
    *,
    timeout_s: float | None = None,
) -> ConnectAttempt:
    return ConnectAttempt(onOwnerCreated, timeout_s=timeout_s)


def cancelConnect(token: AttemptToken) -> KillReceipt | None:
    """Fail closed for missing/stale tokens; never use OS PID as an identifier."""
    if not isinstance(token, AttemptToken):
        raise TypeError("expected AttemptToken")
    with _attempts_lock:
        attempt = _attempts.get(token)
    if attempt is None:
        raise LookupError("unknown or expired qpsycopg connect attempt")
    return attempt.cancel()
