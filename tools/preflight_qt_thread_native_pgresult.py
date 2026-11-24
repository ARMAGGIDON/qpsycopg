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
import faulthandler

# Enable native crash traceback even if import of a C extension faults.
faulthandler.enable(all_threads=True)

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


def trace(stage: str) -> None:
    # This is a forensic preflight, not a timing benchmark. Never log the DSN.
    print(f"NATIVE_PREFLIGHT_STAGE {stage}", file=sys.stderr, flush=True)


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
        trace("worker: before native PGconn.connect")
        conn = native_pq.PGconn.connect(dsn.encode("utf-8"))
        trace("worker: after native PGconn.connect")
        if int(conn.status) != int(psycopg.pq.ConnStatus.OK):
            raise RuntimeError(f"native PGconn not OK: {conn.get_error_message()}")

        notices: list[ResultSnapshot] = []
        borrowed: list[Any] = []

        def on_notice(res: Any) -> None:
            # libpq provides a borrowed callback PGresult; COPY it here.
            # Never enqueue this native wrapper: the native callback clears
            # its pointer immediately after returning.
            trace("worker: borrowed notice callback entered")
            borrowed.append(res)
            notices.append(pool._snapshotResult(res))
            trace("worker: borrowed notice snapshot complete")

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
            trace(f"worker: before exec {kind} ({len(sql)} bytes)")
            result = conn.exec_(sql)
            trace("worker: after exec")
            snapshot = pool._snapshotResult(result)
            trace("worker: after snapshot")
            borrowed_invalid = all(x.pgresult_ptr is None for x in borrowed)
            if kind == "query_finish":
                # Confirm result ownership does not depend on live PGconn.
                trace("worker: before PGconn.finish with result alive")
                conn.finish()
                conn = None
                trace("worker: after PGconn.finish with result alive")
            trace("worker: before queue response put")
            responses.put({
                "ok": True, "native": result, "snapshot": snapshot,
                "notices": tuple(notices),
                "notice_borrowed_invalidated": borrowed_invalid,
                "worker_finished_connection": kind == "query_finish",
            })
            trace("worker: after queue response put")
            if kind == "query_finish":
                return
    except BaseException as ex:
        trace(f"worker: Python exception {type(ex).__name__}")
        responses.put({
            "ok": False, "exception": f"{type(ex).__name__}: {ex}",
        })
    finally:
        if conn is not None:
            trace("worker: final PGconn.finish")
            conn.finish()
        trace("worker: exiting")


def decode_same_context(native: Any, snap: ResultSnapshot, cursor: Any) -> dict:
    trace("caller: before synthetic PGresult construction")
    synthetic = process_pq.PGresult(snap)
    trace("caller: before native metadata read")
    native_values = describe_result(native)
    trace("caller: before synthetic metadata read")
    snapshot_values = describe_result(synthetic)
    trace("caller: after metadata read")
    if native_values != snapshot_values:
        fields = {
            key: {"native": str(native_values[key]), "snapshot": str(snapshot_values[key])}
            for key in native_values if native_values[key] != snapshot_values[key]
        }
        raise AssertionError(f"native/snapshot metadata mismatch: {fields}")

    is_sql_error = native_values["status"] == int(psycopg.pq.ExecStatus.FATAL_ERROR)
    if is_sql_error:
        trace("caller: verifying SQL error parity (no row decoding)")
        native_error = psycopg.errors.error_from_result(native)
        snapshot_error = psycopg.errors.error_from_result(synthetic)
        if (
            type(native_error) is not type(snapshot_error)
            or str(native_error) != str(snapshot_error)
            or native_error.sqlstate != snapshot_error.sqlstate
        ):
            raise AssertionError("native/snapshot SQLSTATE exception parity failed")

    # Psycopg BaseCursor._check_results() raises for FATAL_ERROR *before*
    # set_pgresult/load_rows. Non-row-producing COMMAND_OK/DO results also
    # have no rows to decode. The old probe incorrectly called the hybrid
    # native bulk row decoder even for FATAL_ERROR/zero-column results:
    # its load_rows(0, 0) crashed Windows with 0xC0000005. This is a
    # diagnostic misuse, not evidence that cross-thread PGresult is invalid.
    row_result = (
        native_values["status"] == int(psycopg.pq.ExecStatus.TUPLES_OK)
        and native_values["nfields"] > 0
        and native_values["ntuples"] > 0
    )
    if row_result:
        trace("caller: before native C Transformer construction")
        c_tx = native_fast.Transformer(cursor)
        trace("caller: before native C Transformer.set_pgresult")
        c_tx.set_pgresult(native)
        trace("caller: before hybrid Transformer construction")
        hybrid_tx = adapt.Transformer(cursor)
        trace("caller: before hybrid Transformer.set_pgresult")
        hybrid_tx.set_pgresult(synthetic)
        nrows = int(native.ntuples)
        trace("caller: before native C Transformer.load_rows")
        c_rows = c_tx.load_rows(0, nrows, tuple)
        trace("caller: before hybrid Transformer.load_rows")
        hybrid_rows = hybrid_tx.load_rows(0, nrows, tuple)
        trace("caller: after row loads")
        if c_rows != hybrid_rows:
            raise AssertionError(
                f"native/hybrid decoded rows differ: {c_rows!r} != {hybrid_rows!r}"
            )
        if c_tx.load_row(0, tuple) != hybrid_tx.load_row(0, tuple):
            raise AssertionError("native/hybrid load_row() differs")
        trace("caller: before Transformer deletion and GC")
        del c_tx, hybrid_tx
        gc.collect()
    else:
        trace("caller: skipping row decoding for non-row PGresult")
        c_rows = []
    # Drop Transformer references before explicit clear: no consumer may
    # dereference a cleared PGresult (the same contract as upstream C).
    trace("caller: before first native PGresult.clear")
    native.clear()
    trace("caller: before second native PGresult.clear")
    native.clear()
    trace("caller: after second native PGresult.clear")
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
        "row_decoder_checked": row_result,
        "native_hybrid_transformer_parity": True if row_result else None,
        "error_parity_checked": is_sql_error,
        "clear_idempotent": True,
    }


def run_child(dsn: str) -> dict:
    trace("child: entered after imports")
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
    trace("child: before worker.start")
    worker.start()
    trace("child: after worker.start")
    results: dict[str, Any] = {}
    tests = (
        ("select", "query", b"SELECT 1::int4 AS one, NULL::text AS missing, "
                            b"'qpsycopg'::text AS label, true::bool AS flag"),
        ("sql_error", "query", b"SELECT 1 / 0"),
        ("notice", "query", b"DO $$BEGIN RAISE NOTICE 'qpsycopg preflight'; END$$"),
        ("connection_finished", "query_finish", b"SELECT 17::int4 AS retained"),
    )
    trace("child: before context psycopg.connect")
    with psycopg.connect(dsn) as context_conn:
        trace("child: before context cursor creation")
        with context_conn.cursor() as cursor:
            trace("child: after context cursor creation")
            for name, kind, sql in tests:
                trace(f"child: starting case {name}")
                requests.put((kind, sql))
                try:
                    answer = responses.get(timeout=12)
                except queue.Empty as ex:
                    raise RuntimeError(f"worker did not answer {name}") from ex
                trace(f"child: received response for {name}")
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

                trace(f"child: before decode parity {name}")
                detail = decode_same_context(native, snap, cursor)
                trace(f"child: passed decode parity {name}")
                detail["notice_snapshots"] = len(notices)
                detail["worker_finished_connection"] = answer["worker_finished_connection"]
                results[name] = detail

    trace("child: before worker.join")
    worker.join(timeout=12)
    trace("child: after worker.join")
    if worker.is_alive():
        raise RuntimeError("native worker did not terminate")
    if (
        results["select"]["decoded_rows"] != "[(1, None, 'qpsycopg', True)]"
        or not results["select"]["row_decoder_checked"]
    ):
        raise AssertionError("SELECT native/hybrid row parity not demonstrated")
    if (
        results["sql_error"]["status"] != int(psycopg.pq.ExecStatus.FATAL_ERROR)
        or not results["sql_error"]["error_parity_checked"]
        or results["sql_error"]["row_decoder_checked"]
    ):
        raise AssertionError("fatal SQL result was not handled as an error")
    if results["notice"]["row_decoder_checked"]:
        raise AssertionError("DO notice command unexpectedly ran row decoding")
    if (
        results["connection_finished"]["decoded_rows"] != "[(17,)]"
        or not results["connection_finished"]["row_decoder_checked"]
    ):
        raise AssertionError("native result did not survive PGconn.finish()")

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
        last_stage = next(
            (line for line in reversed(run.stderr.splitlines())
             if line.startswith("NATIVE_PREFLIGHT_STAGE ")), None
        )
        result["error"] = {
            "exit_code": run.returncode,
            "exit_code_hex": f"0x{run.returncode & 0xffffffff:08X}",
            "last_stage": last_stage,
            "stderr_tail": run.stderr[-20000:],
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
