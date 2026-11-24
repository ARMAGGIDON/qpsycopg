#!/usr/bin/env python3
"""Profile qt-process IPC and row decoding against native Psycopg."""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import time
from collections import Counter
from typing import Any

os.environ.setdefault("PSYCOPG_IMPL", "qt-process")
os.environ.setdefault("QPSYCOPG_WORKER_IMPL", "binary")
os.environ.setdefault("QPSYCOPG_PROFILE_PHASES", "1")

import psycopg

_DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
_QUERY = (
    "SELECT i, i::numeric / 10, clock_timestamp() "
    "FROM generate_series(1, %s) AS s(i)"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("select", "fetch", "decode"), default="select")
    parser.add_argument("dsn", nargs="?", default=os.environ.get("QPSYCOPG_DSN", _DEFAULT_DSN))
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--calls", type=int, default=5)
    parser.add_argument("--rows", type=int, default=100_000)
    options = parser.parse_args()

    decodeTimings: dict[str, float] = {}
    decoder: str | None = None
    bulkDecoder = False
    loaderTypes: list[str] = []
    valueLayout: str | None = None
    valueDataBytes = 0
    valueOffsetBytes = 0
    valueNullBytes = 0
    events: list[dict[str, Any]] = []
    executeEvents: list[dict[str, Any]] = []
    parentPhases: dict[str, float | int] = {}
    workerPhases: dict[str, float | int] = {}

    pqModule = importlib.import_module("psycopg.pq.pq_qt_process")
    originalSendFrame = pqModule.sendFrame
    originalRecvFrame = pqModule.recvFrame
    originalRestore = pqModule._restore
    restoreDepth = 0

    def profiledSendFrame(sock: Any, value: Any) -> None:
        originalSendFrame(sock, value, parentPhases, "parent_request")

    def profiledRecvFrame(sock: Any) -> Any:
        return originalRecvFrame(sock, parentPhases, "parent_response")

    def profiledRestore(value: Any, transport: Any) -> Any:
        nonlocal restoreDepth
        outer = restoreDepth == 0
        restoreDepth += 1
        started = time.perf_counter() if outer else 0.0
        try:
            return originalRestore(value, transport)
        finally:
            restoreDepth -= 1
            if outer:
                parentPhases["restore_seconds"] = parentPhases.get("restore_seconds", 0) + time.perf_counter() - started
                parentPhases["restore_calls"] = parentPhases.get("restore_calls", 0) + 1

    pqModule.sendFrame = profiledSendFrame
    pqModule.recvFrame = profiledRecvFrame
    pqModule._restore = profiledRestore
    try:
        with psycopg.connect(options.dsn) as connection:
            def selectOne() -> None:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    assert cursor.fetchone() == (1,)

            for _ in range(options.warmup):
                selectOne()

            transport = getattr(connection.pgconn, "_transport", None)
            original = transport.request if transport is not None else None

            def traced(
                operation: Any,
                objectId: int = 0,
                member: str = "",
                args: tuple[Any, ...] = (),
                kwargs: tuple[tuple[str, Any], ...] = (),
            ) -> Any:
                assert transport is not None and original is not None
                before = transport.requestId
                started = time.perf_counter()
                value = original(operation, objectId, member, args, kwargs)
                elapsed = time.perf_counter() - started
                after = transport.requestId
                events.append({
                    "operation": getattr(operation, "name", str(operation)),
                    "object_id": objectId,
                    "member": member,
                    "remote": after != before,
                    "seconds": elapsed,
                })
                return value

            if transport is not None:
                transport.request = traced
            try:
                connection.pgconn.qpsycopg_profile_take()
                events.clear()
                parentPhases.clear()

                start = time.perf_counter()
                if options.mode == "select":
                    perCall: list[list[dict[str, Any]]] = []
                    for _ in range(options.calls):
                        offset = len(events)
                        selectOne()
                        perCall.append(events[offset:])
                    executeEvents = list(events)
                    executeParentPhases = dict(parentPhases)
                    workerPhases = connection.pgconn.qpsycopg_profile_take()
                    parentPhases.clear()
                    parentPhases.update(executeParentPhases)
                else:
                    perCall = []
                    with connection.cursor() as cursor:
                        executeOffset = len(events)
                        cursor.execute(_QUERY, (options.rows,))
                        executeEvents = events[executeOffset:]
                        executeParentPhases = dict(parentPhases)
                        workerPhases = connection.pgconn.qpsycopg_profile_take()
                        parentPhases.clear()
                        parentPhases.update(executeParentPhases)

                        res = cursor.pgresult
                        assert res is not None and res.ntuples == options.rows

                        tx = cursor._tx
                        fast = getattr(tx, "_fast", None)
                        decoder = getattr(tx, "__qpsycopg_decoder__", None)
                        if decoder is None:
                            decoder = f"{type(tx).__module__}.{type(tx).__qualname__}"
                        bulkDecoder = getattr(tx, "_bulkLoadRows", None) is not None

                        loaderOwner = fast if fast is not None else tx
                        loaderTypes = [
                            f"{type(loader).__module__}.{type(loader).__qualname__}"
                            for loader in (
                                loaderOwner.get_loader(res.ftype(column), psycopg.pq.Format(res.fformat(column)))
                                for column in range(res.nfields)
                            )
                        ]

                        values = getattr(res, "_values", None)
                        if values is not None:
                            valueLayout = type(values).__name__
                            valueDataBytes = len(getattr(values, "data", b""))
                            valueOffsetBytes = len(getattr(values, "offsets", b""))
                            valueNullBytes = len(getattr(values, "nulls", b""))
                        else:
                            valueLayout = f"{type(res).__module__}.{type(res).__qualname__}"

                        if options.mode == "decode":
                            nfields = res.nfields
                            if values is not None and fast is not None:
                                fastModule = importlib.import_module(type(fast).__module__)
                                scanPacked = getattr(fastModule, "qpsycopg_scan_packed", None)
                                if scanPacked is not None and hasattr(values, "data"):
                                    gc.collect()
                                    phase = time.perf_counter()
                                    totalBytes, nonnull = scanPacked(values, 0, options.rows, nfields)
                                    decodeTimings["packed_scan_seconds"] = time.perf_counter() - phase
                                    decodeTimings["packed_scan_bytes"] = int(totalBytes)
                                    decodeTimings["packed_scan_nonnull"] = int(nonnull)

                                gc.collect()
                                phase = time.perf_counter()
                                rowSlices = [
                                    values[offset : offset + nfields]
                                    for offset in range(0, len(values), nfields)
                                ]
                                decodeTimings["slice_rows_seconds"] = time.perf_counter() - phase

                                gc.collect()
                                phase = time.perf_counter()
                                decoded = [fast.load_sequence(row) for row in rowSlices]
                                decodeTimings["decode_presliced_seconds"] = time.perf_counter() - phase
                                assert len(decoded) == options.rows
                                decodeTimings["presliced_total_seconds"] = (
                                    decodeTimings["slice_rows_seconds"]
                                    + decodeTimings["decode_presliced_seconds"]
                                )
                                del decoded, rowSlices

                            gc.collect()
                            phase = time.perf_counter()
                            decoded = tx.load_rows(0, options.rows, tuple)
                            decodeTimings["current_load_rows_seconds"] = time.perf_counter() - phase
                            assert len(decoded) == options.rows
                elapsed = time.perf_counter() - start
            finally:
                if transport is not None and original is not None:
                    transport.request = original
    finally:
        pqModule.sendFrame = originalSendFrame
        pqModule.recvFrame = originalRecvFrame
        pqModule._restore = originalRestore

    remoteCounts = Counter(
        f"{event['operation']}:{event['member'] or '-'}"
        for event in executeEvents if event["remote"]
    )
    cachedCounts = Counter(
        f"{event['operation']}:{event['member'] or '-'}"
        for event in executeEvents if not event["remote"]
    )
    remoteSeconds = sum(float(event["seconds"]) for event in executeEvents if event["remote"])
    waitEvents = [event for event in executeEvents if event["remote"] and event["operation"] == "WAIT"]
    result: dict[str, Any] = {
        "mode": options.mode,
        "impl": psycopg.pq.__impl__,
        "elapsed_seconds": elapsed,
        "remote_requests": sum(remoteCounts.values()),
        "remote_seconds": remoteSeconds,
        "remote_counts": dict(remoteCounts),
        "cached_counts": dict(cachedCounts),
        "wait_remote_requests": len(waitEvents),
        "wait_remote_seconds": sum(float(event["seconds"]) for event in waitEvents),
        "parent_phases": parentPhases,
        "worker_phases": workerPhases,
    }
    if options.mode == "select":
        result.update({
            "calls": options.calls,
            "remote_per_call": sum(remoteCounts.values()) / options.calls,
            "first_call": perCall[0] if perCall else [],
        })
    else:
        result.update({
            "rows": options.rows,
            "decoder": decoder,
            "bulk_decoder": bulkDecoder,
            "loader_types": loaderTypes,
            "value_layout": valueLayout,
            "value_data_bytes": valueDataBytes,
            "value_offset_bytes": valueOffsetBytes,
            "value_null_bytes": valueNullBytes,
            "first_events": executeEvents[:20],
        })
        result.update(decodeTimings)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
