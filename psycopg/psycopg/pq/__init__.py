"""
psycopg libpq wrapper

This package exposes the libpq functionalities as Python objects and functions.

The real implementation (the binding to the C library) is
implementation-dependant but all the implementations share the same interface.
"""

# Copyright (C) 2020 The Psycopg Team

from __future__ import annotations

import os
import logging
from collections.abc import Callable

from . import abc
from .misc import ConninfoOption, PGnotify, PGresAttDesc, error_message, version_pretty
from ._enums import ConnStatus, DiagnosticField, ExecStatus, Format, Ping
from ._enums import PipelineStatus, PollingStatus, Trace, TransactionStatus

logger = logging.getLogger(__name__)

__impl__: str
"""The currently loaded implementation of the `!psycopg.pq` package.

Possible values include ``python``, ``c``, ``binary``, ``qt-process``.
"""

__build_version__: int
"""The libpq version the C package was built with.

A number in the same format of `~psycopg.ConnectionInfo.server_version`
representing the libpq used to build the speedup module (``c``, ``binary``) if
available.

Certain features might not be available if the built version is too old.
"""

version: Callable[[], int]
PGconn: type[abc.PGconn]
PGresult: type[abc.PGresult]
Conninfo: type[abc.Conninfo]
Escaping: type[abc.Escaping]
PGcancel: type[abc.PGcancel]
PGcancelConn: type[abc.PGcancelConn]


def import_from_libpq() -> None:
    """
    Import pq objects implementation from the best libpq wrapper available.

    If an implementation is requested try to import only it, otherwise
    try to import the best implementation available.
    """
    global __impl__, version, __build_version__
    global PGconn, PGresult, Conninfo, Escaping, PGcancel, PGcancelConn

    impl = os.environ.get("PSYCOPG_IMPL", "").lower()
    module = None
    attempts: list[str] = []

    def handle_error(name: str, error: Exception) -> None:
        if not impl:
            attempts.append(f"couldn't import psycopg '{name}' implementation: {error}")
        else:
            msg = f"couldn't import requested psycopg '{name}' implementation: {error}"
            raise ImportError(msg) from error

    # qpsycopg opt-in implementation. Never select it implicitly: keeping the
    # upstream default makes comparison and upstream synchronisation trivial.
    if impl == "qt-process":
        try:
            from . import pq_qt_process as module  # type: ignore[assignment]
        except Exception as error:
            handle_error("qt-process", error)

    # The best implementation: fast but requires the system libpq installed
    if not module and (not impl or impl == "c"):
        try:
            from psycopg_c import pq as module  # type: ignore
        except Exception as error:
            handle_error("c", error)

    # Second best implementation: fast and stand-alone
    if not module and (not impl or impl == "binary"):
        try:
            from psycopg_binary import pq as module  # type: ignore
        except Exception as error:
            handle_error("binary", error)

    # Pure Python implementation, slow and requires the system libpq installed.
    if not module and (not impl or impl == "python"):
        try:
            from . import pq_ctypes as module  # type: ignore[assignment]
        except Exception as error:
            handle_error("python", error)

    if module:
        __impl__ = module.__impl__
        version = module.version
        PGconn = module.PGconn
        PGresult = module.PGresult
        Conninfo = module.Conninfo
        Escaping = module.Escaping
        PGcancel = module.PGcancel
        PGcancelConn = module.PGcancelConn
        __build_version__ = module.__build_version__
    elif impl:
        raise ImportError(f"requested psycopg implementation '{impl}' unknown")
    else:
        sattempts = "\n".join(f"- {attempt}" for attempt in attempts)
        raise ImportError(f"""\
no pq wrapper available.
Attempts made:
{sattempts}""")


import_from_libpq()

__all__ = (
    "ConnStatus",
    "PipelineStatus",
    "PollingStatus",
    "TransactionStatus",
    "ExecStatus",
    "Ping",
    "DiagnosticField",
    "Format",
    "Trace",
    "PGconn",
    "PGnotify",
    "Conninfo",
    "PGresAttDesc",
    "error_message",
    "ConninfoOption",
    "version",
    "version_pretty",
)
