"""QP10 Windows real IPC evidence, guarded for the isolated DBPT database.

Run from the feature checkout; PYTHONPATH may point at a separate baseline
worktree. The same gates and instrumentation run on both revisions.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt

import psutil
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import _qt_process_lazy as lazy
from psycopg.pq import _qt_process_pipe as pipe
from psycopg.pq._qt_process_cancel import CancelControlClient
from psycopg.pq._qt_process_owner import newConnectAttempt


def ms(start):
    return (time.perf_counter() - start) * 1000


def snapshot():
    proc = psutil.Process()
    return {
        "rss_bytes": proc.memory_info().rss,
        "handles": proc.num_handles(),
        "threads": proc.num_threads(),
        "children": sorted(p.pid for p in proc.children(recursive=True)),
    }


def task(fn):
    result = {"errors": []}
    done = threading.Event()

    def run():
        start = time.perf_counter()
        try:
            result["value"] = fn()
        except BaseException as ex:
            result["errors"].append(type(ex).__name__)
            if ex.__cause__ is not None:
                result["cause"] = type(ex.__cause__).__name__
        finally:
            result["ms"] = ms(start)
            result["finished_at"] = time.perf_counter()
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, done, result


def wait(t, timeout):
    assert t[1].wait(timeout), "helper did not settle"
    t[0].join(1)
    assert not t[0].is_alive()
    return t[2]


def witness(owner):
    open_process = pipe._api(
        pipe._kernel, "OpenProcess",
        (wt.DWORD, wt.BOOL, wt.DWORD), wt.HANDLE,
    )
    handle = open_process(0x00101000, False, owner._osPid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def exact_exit(owner, handle):
    proof = owner.waitExit(owner.ownerToken, 10)
    assert proof is not None and proof.owner_token == owner.ownerToken
    assert proof.os_pid == owner._osPid
    assert pipe._waitEvent(handle, 10_000) == pipe._WAIT_OBJECT_0
    get_exit = pipe._api(
        pipe._kernel, "GetExitCodeProcess",
        (wt.HANDLE, ctypes.POINTER(wt.DWORD)), wt.BOOL,
    )
    code = wt.DWORD()
    assert get_exit(handle, ctypes.byref(code))
    assert code.value == (proof.exit_code & 0xffffffff)
    return {
        "owner_token": str(proof.owner_token.value), "os_pid": proof.os_pid,
        "exit_code": proof.exit_code, "exit_status": proof.exit_status,
        "reason": proof.reason, "win32_exact_exit": True,
    }


def connect(dsn, expected):
    attempt = newConnectAttempt(timeout_s=15)
    with attempt:
        connection = psycopg.connect(dsn, autocommit=True)
    transport = connection.pgconn._transport
    owner = attempt.owner
    assert transport.owner is owner and owner.attemptToken == attempt.token
    assert transport.ipcKind == expected
    assert (isinstance(transport.sock, pipe.PipeStream)) == (expected == "pipe")
    return connection, owner, witness(owner)


def cleanup(connection, owner, handle, record, helpers=()):
    forced = False
    try:
        close = task(connection.close)
        if not close[1].wait(4):
            forced = True
            owner.requestKill(owner.ownerToken)
        if owner.waitExit(owner.ownerToken, 10) is None:
            forced = True
            owner.requestKill(owner.ownerToken)
        record["exit_proof"] = exact_exit(owner, handle)
        record["close"] = wait(close, 4)
        for helper in helpers:
            wait(helper, 4)
        forced = forced or owner._killRequested or "forced" in record["exit_proof"]["reason"]
    finally:
        record["forced_owner_kill"] = forced or owner._killRequested
        pipe._closeHandle(handle)


def wait_active(neighbor, pid, query_task):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if query_task[1].is_set():
            return False
        row = neighbor.execute(
            "select state, wait_event from pg_stat_activity where pid = %s",
            (pid,),
        ).fetchone()
        if row == ("active", "PgSleep"):
            return True
        time.sleep(0.01)
    raise AssertionError("query did not enter pg_sleep")


def iteration(dsn, expected, neighbor, index):
    record = {"iteration": index, "opens": [], "requests": [], "errors": []}
    connection, owner, handle = connect(dsn, expected)
    pgconn = connection.pgconn
    transport = pgconn._transport
    record.update(owner_token=str(owner.ownerToken.value), os_pid=owner._osPid,
                  ipc=transport.ipcKind)
    pid = pgconn.backend_pid
    assert pgconn.status == psycopg.pq.ConnStatus.OK
    entered, contender, release = threading.Event(), threading.Event(), threading.Event()
    mutex = threading.Lock()
    original_open = CancelControlClient.open
    original_cancel = CancelControlClient.cancel
    helpers = []
    target_clients = set()
    condition = getattr(getattr(pgconn, "_cancelLifecycle", None), "condition", None)
    old_wait = None if condition is None else condition.wait

    def open_control(cls, pg, **kwargs):
        if pg is not pgconn:
            return original_open(pg, **kwargs)
        with mutex:
            entry = {"thread": threading.get_ident()}
            record["opens"].append(entry)
            first = len(record["opens"]) == 1
            (entered if first else contender).set()
        start = time.perf_counter()
        try:
            if first and not release.wait(5):
                raise TimeoutError("runtime setup gate timed out")
            client = original_open(pg, **kwargs)
            with mutex:
                target_clients.add(id(client))
            entry["success"] = not client.closed
            return client
        except BaseException as ex:
            entry["error"] = type(ex).__name__
            raise
        finally:
            entry["duration_ms"] = ms(start)

    def cancel_control(client, timeout):
        if id(client) not in target_clients:
            return original_cancel(client, timeout)
        entry = {"timeout_s": timeout}
        record["requests"].append(entry)
        start = time.perf_counter()
        try:
            result = original_cancel(client, timeout)
            entry["success"] = True
            return result
        except BaseException as ex:
            entry["error"] = type(ex).__name__
            raise
        finally:
            entry["duration_ms"] = ms(start)

    def condition_wait(timeout=None):
        contender.set()
        return old_wait(timeout)

    try:
        CancelControlClient.open = classmethod(open_control)
        CancelControlClient.cancel = cancel_control
        if condition is not None:
            condition.wait = condition_wait
        query = task(lambda: connection.execute("select pg_sleep(20)").fetchone())
        helpers.append(query)
        assert entered.wait(4), "first query did not initialize control"
        cold = task(lambda: connection.cancel_safe(timeout=5))
        helpers.append(cold)
        assert contender.wait(3), "concurrent cancel did not reach old boundary"
        release.set()
        record["cold_cancel"] = wait(cold, 15)
        assert not record["cold_cancel"]["errors"], record["cold_cancel"]
        assert len(record["opens"]) == 1, "duplicate control opens"
        record["publications"] = int(transport._cancelControl is not None)
        assert record["publications"] == 1
        # A cancel racing before send is not guaranteed to cancel a future
        # statement. If necessary, cancel once PgSleep is independently seen.
        if wait_active(neighbor, pid, query):
            warm = task(lambda: connection.cancel_safe(timeout=5))
            helpers.append(warm)
            record["first_query_warm_cancel"] = wait(warm, 7)
            assert not record["first_query_warm_cancel"]["errors"]
        record["first_query"] = wait(query, 7)
        assert record["first_query"]["errors"] == ["QueryCanceled"]
        assert connection.execute("select 1").fetchone() == (1,)
        # Always take a separate warm active-SQL sample.
        query2 = task(lambda: connection.execute("select pg_sleep(20)").fetchone())
        helpers.append(query2)
        assert wait_active(neighbor, pid, query2)
        start = time.perf_counter()
        warm2 = task(lambda: connection.cancel_safe(timeout=5))
        helpers.append(warm2)
        record["warm_cancel"] = wait(warm2, 7)
        assert not record["warm_cancel"]["errors"]
        record["warm_query"] = wait(query2, 7)
        assert record["warm_query"]["errors"] == ["QueryCanceled"]
        record["warm_settlement_ms"] = (record["warm_query"]["finished_at"] - start) * 1000
        assert connection.execute("select 2").fetchone() == (2,)
        assert neighbor.execute("select 3").fetchone() == (3,)
        assert transport.owner is owner and len(record["opens"]) == 1
        record["healthy_reuse"] = True
        record["neighbor_survived"] = True
    except BaseException as ex:
        record["errors"].append(type(ex).__name__)
        if isinstance(ex, AssertionError):
            # Our assertion messages contain only phase names or error types.
            record["phase"] = str(ex)
    finally:
        release.set()
        if any(not helper[1].is_set() for helper in helpers):
            owner.requestKill(owner.ownerToken)
        try:
            cleanup(connection, owner, handle, record, helpers)
        except BaseException as ex:
            record["errors"].append("cleanup:" + type(ex).__name__)
        finally:
            CancelControlClient.open = classmethod(original_open.__func__)
            CancelControlClient.cancel = original_cancel
            if condition is not None:
                condition.wait = old_wait
        record["resources"] = snapshot()
    return record


def distribution(samples):
    if not samples:
        return {}
    values = sorted(samples)
    return {"count": len(values), "min_ms": values[0],
            "median_ms": statistics.median(values), "max_ms": values[-1],
            "p95_ms": values[min(len(values)-1, (95 * len(values) + 99)//100 - 1)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=60)
    parser.add_argument("--dsn", default=os.environ.get("QPSYCOPG_OWNER_TEST_DSN"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32" or psycopg.pq.__impl__ != "qt-process":
        parser.error("requires Windows and PSYCOPG_IMPL=qt-process")
    if args.iterations < 1 or not args.dsn:
        parser.error("positive iterations and explicit test DSN required")
    params = conninfo_to_dict(args.dsn)
    if params.get("dbname") != "dbpt_regression":
        parser.error("refusing non-isolated database; require dbname=dbpt_regression")
    expected = os.environ.get("QPSYCOPG_IPC", "pipe")
    if expected not in ("pipe", "tcp"):
        parser.error("explicit pipe or tcp required")
    revision = subprocess.check_output(
        ["git", "-C", str(Path(psycopg.__file__).resolve().parents[2]), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    result = {
        "revision": revision, "import_path": psycopg.__file__,
        "requested_ipc": expected, "worker_impl": os.environ.get("QPSYCOPG_WORKER_IMPL"),
        "requested_iterations": args.iterations, "before": snapshot(), "iterations": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")

    save()
    neighbor, owner, handle = connect(args.dsn, expected)
    try:
        result["postgresql_version"] = neighbor.info.server_version
        assert 180000 <= result["postgresql_version"] < 190000
        assert neighbor.execute("select current_database()").fetchone() == ("dbpt_regression",)
        for index in range(args.iterations):
            row = iteration(args.dsn, expected, neighbor, index + 1)
            result["iterations"].append(row)
            save()
            print(json.dumps({"iteration": index + 1, "opens": len(row["opens"]),
                              "errors": row["errors"], "forced": row.get("forced_owner_kill")}),
                  flush=True)
    finally:
        result["neighbor"] = {}
        cleanup(neighbor, owner, handle, result["neighbor"])
        result["after"] = snapshot()
        gc.collect()
        result["after_gc"] = snapshot()
        rows = result["iterations"]
        result["summary"] = {
            "completed": len(rows),
            "initialization_attempts": sum(len(r["opens"]) for r in rows),
            "successful_publications": sum(r.get("publications", 0) for r in rows),
            "duplicate_opens": sum(max(0, len(r["opens"])-1) for r in rows),
            "failed_iterations": sum(bool(r["errors"]) for r in rows),
            "unexpected_forced_kills": sum(bool(r.get("forced_owner_kill")) for r in rows),
            "timeout_errors": sum(
                sum("Timeout" in str(o.get("error", "")) for o in r["opens"] + r["requests"])
                + sum("Timeout" in name for phase in ("cold_cancel", "warm_cancel", "first_query_warm_cancel")
                      for name in r.get(phase, {}).get("errors", []))
                for r in rows
            ),
            "exact_exits": sum(bool(r.get("exit_proof", {}).get("win32_exact_exit")) for r in rows),
            "initialization": distribution([o["duration_ms"] for r in rows for o in r["opens"]
                                            if o.get("success")]),
            "cold_cancel": distribution([r["cold_cancel"]["ms"] for r in rows
                                         if "cold_cancel" in r and not r["cold_cancel"]["errors"]]),
            "warm_cancel": distribution([r["warm_cancel"]["ms"] for r in rows
                                         if "warm_cancel" in r and not r["warm_cancel"]["errors"]]),
            "request": distribution([o["duration_ms"] for r in rows for o in r["requests"]
                                     if o.get("success")]),
            "warm_settlement": distribution([r["warm_settlement_ms"] for r in rows
                                             if "warm_settlement_ms" in r]),
            "remaining_children": sorted(set(result["after_gc"]["children"])
                                         - set(result["before"]["children"])),
        }
        save()
    s = result["summary"]
    print(json.dumps(s, indent=2))
    return int(not (
        s["completed"] == s["exact_exits"] == args.iterations
        and s["initialization_attempts"] == s["successful_publications"] == args.iterations
        and not any(s[k] for k in ("duplicate_opens", "failed_iterations",
                                   "unexpected_forced_kills", "timeout_errors", "remaining_children"))
        and not result["neighbor"]["forced_owner_kill"]
    ))


if __name__ == "__main__":
    raise SystemExit(main())
