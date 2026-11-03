#!/usr/bin/env python3
"""Diagnostic-only native PGresult handoff feasibility gate.

A *new* C PGconn is used only by the worker thread. Each completed PGresult
is passed through the two SimpleQueues to the caller thread; native and
snapshot decoding use the same cursor adaptation context. The production
qt-thread worker, cursor and benchmark v3 remain untouched.

Run in a subprocess: native extension failures can terminate the interpreter.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import psycopg
import psycopg_c.pq as native_pq
from psycopg import adapt
from psycopg.pq import pq_qt_process as process_pq
from psycopg.pq import pq_qt_thread as thread_pq
from psycopg.pq import _qt_process_worker_pool as pool
from psycopg.pq._qt_process_protocol import ResultSnapshot

try:
    from psycopg_c import _psycopg as native_fast
except ImportError as ex:
    raise RuntimeError("native C Transformer is not importable") from ex


def describe_result(res: Any) -> dict[str, Any]:
    return {
        "status": int(res.status),
        "error_message": bytes(res.error_message),
        "fields": tuple(
            (int(field), res.error_field(int(field)))
            for field in psycopg.pq.DiagnosticField
            if res.error_field(int(field)) is not None
        ),
        "ntuples": int(res.ntuples),
        "nfields": int(res.nfields),
        "columns": tuple(
            (res.fname(i), res.ftable(i), res.ftablecol(i), res.fformat(i),
             res.ftype(i), res.fmod(i), res.fsize(i))
            for i in range(res.nfields)
        ),
        "nparams": int(res.nparams),
        "param_types": tuple(res.param_type(i) for i in range(res.nparams)),
        "binary_tuples": int(res.binary_tuples),
        "command_status": res.command_status,
        "command_tuples": res.command_tuples,
        "oid_value": int(res.oid_value),
        "cells": tuple(
            tuple(res.get_value(row, col) for col in range(res.nfields))
            for row in range(res.ntuples)
        ),
    }


def dispatch_worker(dsn: str, requests: queue.SimpleQueue,
                    responses: queue.SimpleQueue) -> None:
    """The only thread allowed to use PGconn, including finish()."""
    conn: Any = None
    try:
        conn = native_pq.PGconn.connect(dsn.encode("utf-8"))
        if int(conn.status) != int(psycopg.pq.ConnStatus.OK):
            raise RuntimeError(f"native PGconn not OK: {conn.get_error_message()}")

        notices: list[ResultSnapshot] = []
        borrowed: list[Any] = []

        def on_notice(res: Any) -> None:
            # libpq provides a borrowed callback PGresult; COPY it here.
            # Never enqueue this native wrapper: the native callback clears
            # its pointer immediately after returning.
            borrowed.append(res)
            notices.append(pool._snapshotResult(res))

        conn.notice_handler = on_notice
        while True:
            kind, sql = requests.get()
            if kind == "stop":
                responses.put({"ok": True, "stopped": True})
                break
            if kind not in ("query", "query_finish"):
                raise ValueError(kind)

            notices.clear()
            borrowed.clear()
            result = conn.exec_(sql)
            snapshot = pool._snapshotResult(result)
            borrowed_invalid = all(x.pgresult_ptr is None for x in borrowed)
            if kind == "query_finish":
                # Confirm result ownership does not depend on live PGconn.
                conn.finish()
                conn = None
            responses.put({
                "ok": True, "native": result, "snapshot": snapshot,
                "notices": tuple(notices),
                "notice_borrowed_invalidated": borrowed_invalid,
                "worker_finished_connection": kind == "query_finish",
            })
            if kind == "query_finish":
                return
    except BaseException as ex:
        responses.put({
            "ok": False, "exception": f"{type(ex).__name__}: {ex}",
        })
    finally:
        if conn is not None:
            conn.finish()


def decode_same_context(native: Any, snap: ResultSnapshot, cursor: Any) -> dict:
    synthetic = process_pq.PGresult(snap)
    native_values = describe_result(native)
    snapshot_values = describe_result(synthetic)
    if native_values != snapshot_values:
        fields = {
            key: {"native": str(native_values[key]), "snapshot": str(snapshot_values[key])}
            for key in native_values if native_values[key] != snapshot_values[key]
        }
        raise AssertionError(f"native/snapshot metadata mismatch: {fields}")

    if native_values["status"] == int(psycopg.pq.ExecStatus.FATAL_ERROR):
        native_error = psycopg.errors.error_from_result(native)
        snapshot_error = psycopg.errors.error_from_result(synthetic)
        if (
            type(native_error) is not type(snapshot_error)
            or str(native_error) != str(snapshot_error)
            or native_error.sqlstate != snapshot_error.sqlstate
        ):
            raise AssertionError("native/snapshot SQLSTATE exception parity failed")

    c_tx = native_fast.Transformer(cursor)
    c_tx.set_pgresult(native)
    hybrid_tx = adapt.Transformer(cursor)
    hybrid_tx.set_pgresult(synthetic)
    nrows = int(native.ntuples)
    c_rows = c_tx.load_rows(0, nrows, tuple)
    hybrid_rows = hybrid_tx.load_rows(0, nrows, tuple)
    if c_rows != hybrid_rows:
        raise AssertionError(f"native/hybrid decoded rows differ: {c_rows!r} != {hybrid_rows!r}")
    if nrows and c_tx.load_row(0, tuple) != hybrid_tx.load_row(0, tuple):
        raise AssertionError("native/hybrid load_row() differs")

    # Drop Transformer references before explicit clear: no consumer may
    # dereference a cleared PGresult (the same contract as upstream C).
    del c_tx, hybrid_tx
    gc.collect()
    native.clear()
    native.clear()
    if native.pgresult_ptr is not None:
        raise AssertionError("native PGresult.clear() did not release its pointer")
    del native
    gc.collect()
    return {
        "status": native_values["status"],
        "ntuples": native_values["ntuples"],
        "nfields": native_values["nfields"],
        "decoded_rows": repr(c_rows),
        "metadata_parity": True,
        "native_hybrid_transformer_parity": True,
        "clear_idempotent": True,
    }


def run_child(dsn: str) -> dict:
    if psycopg.pq.__impl__ != "qt-thread":
        raise RuntimeError(f"expected qt-thread, got {psycopg.pq.__impl__!r}")
    if thread_pq._workerPq.PGresult is not native_pq.PGresult:
        raise RuntimeError("qt-thread C worker PGresult differs from selected native binding")
    if not hasattr(native_fast, "Transformer"):
        raise RuntimeError("native Transformer is unavailable")

    requests: queue.SimpleQueue[Any] = queue.SimpleQueue()
    responses: queue.SimpleQueue[Any] = queue.SimpleQueue()
    worker = threading.Thread(
        target=dispatch_worker, args=(dsn, requests, responses),
        daemon=True, name="native-pgresult-preflight-worker",
    )
    worker.start()
    results: dict[str, Any] = {}
    tests = (
        ("select", "query", b"SELECT 1::int4 AS one, NULL::text AS missing, "
                            b"'qpsycopg'::text AS label, true::bool AS flag"),
        ("sql_error", "query", b"SELECT 1 / 0"),
        ("notice", "query", b"DO $$BEGIN RAISE NOTICE 'qpsycopg preflight'; END$$"),
        ("connection_finished", "query_finish", b"SELECT 17::int4 AS retained"),
    )
    with psycopg.connect(dsn) as context_conn:
        with context_conn.cursor() as cursor:
            for name, kind, sql in tests:
                requests.put((kind, sql))
                try:
                    answer = responses.get(timeout=12)
                except queue.Empty as ex:
                    raise RuntimeError(f"worker did not answer {name}") from ex
                if not answer.get("ok"):
                    raise RuntimeError(f"worker failed {name}: {answer}")
                native = answer["native"]
                snap = answer["snapshot"]
                if type(native) is not native_pq.PGresult:
                    raise AssertionError(f"unexpected native type: {type(native)!r}")
                if not isinstance(snap, ResultSnapshot):
                    raise AssertionError(f"unexpected snapshot type: {type(snap)!r}")
                if not answer["notice_borrowed_invalidated"]:
                    raise AssertionError("borrowed notice PGresult was not invalidated")

                notices = answer["notices"]
                if name == "notice" and not notices:
                    raise AssertionError("server notice was not received")
                if name == "notice":
                    for notice in notices:
                        if not isinstance(notice, ResultSnapshot):
                            raise AssertionError("notice must be snapshotted before callback returns")

                detail = decode_same_context(native, snap, cursor)
                detail["notice_snapshots"] = len(notices)
                detail["worker_finished_connection"] = answer["worker_finished_connection"]
                results[name] = detail

    worker.join(timeout=12)
    if worker.is_alive():
        raise RuntimeError("native worker did not terminate")
    if results["select"]["decoded_rows"] != "[(1, None, 'qpsycopg', True)]":
        raise AssertionError("unexpected adapted SELECT output")
    if results["sql_error"]["status"] != int(psycopg.pq.ExecStatus.FATAL_ERROR):
        raise AssertionError("fatal SQL error status was not preserved")
    if results["connection_finished"]["decoded_rows"] != "[(17,)]":
        raise AssertionError("result failed to survive owner PGconn.finish()")

    return {
        "preflight_passed": True,
        "native_pgresult_type": f"{native_pq.PGresult.__module__}.{native_pq.PGresult.__name__}",
        "transformer_type": f"{native_fast.Transformer.__module__}.{native_fast.Transformer.__name__}",
        "two_queue_handoff": True,
        "no_cross_thread_pgconn_use": True,
        "native_result_survives_worker_pgconn_finish": True,
        "notice_native_pointer_not_transferred": True,
        "tests": results,
        "limitation": (
            "This proves only basic standalone native result ownership and "
            "C Transformer parity; it does not authorize bypassing the real "
            "qt-thread worker's ResultSnapshot-dependent health/error/cache "
            "logic, or transferring borrowed notice results."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dsn")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--watchdog-seconds", type=float, default=40)
    args = parser.parse_args()
    if args.watchdog_seconds < 10:
        parser.error("watchdog must be >=10 seconds")
    if args.child:
        print(json.dumps(run_child(args.dsn), indent=2), flush=True)
        return 0

    result: dict[str, Any] = {
        "diagnostic": "native C PGresult handoff and Transformer feasibility",
        "python": sys.version.split()[0],
        "pq_impl": psycopg.pq.__impl__,
        "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "production_runtime_changed": False,
        "benchmark_v3_changed": False,
        "native_handoff_ab_authorized": False,
    }
    bootstrap = Path(__file__).with_name("run_with_pg_root.py")
    cmd = [
        sys.executable, str(bootstrap), str(Path(__file__).resolve()),
        args.dsn, "--child",
    ]
    try:
        run = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=args.watchdog_seconds,
        )
    except subprocess.TimeoutExpired:
        result["error"] = "native preflight subprocess watchdog timeout"
        print(json.dumps(result, indent=2))
        return 2
    if run.returncode:
        result["error"] = {
            "exit_code": run.returncode, "stderr_tail": run.stderr[-2500:],
            "stdout_tail": run.stdout[-1200:],
        }
        print(json.dumps(result, indent=2))
        return 2
    try:
        result.update(json.loads(run.stdout))
    except (ValueError, TypeError):
        result["error"] = {
            "reason": "invalid child JSON", "stdout_tail": run.stdout[-1500:],
        }
        print(json.dumps(result, indent=2))
        return 2
    result["native_handoff_ab_authorized"] = bool(result["preflight_passed"])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
