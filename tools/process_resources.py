from __future__ import annotations

import importlib
from typing import Any


def processResources() -> dict[str, Any] | None:
    try:
        psutil = importlib.import_module("psutil")
    except ImportError:
        return None

    root = psutil.Process()
    try:
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        processes = [root]

    entries = []
    for process in processes:
        try:
            cpu = process.cpu_times()
            try:
                memory = process.memory_full_info()
            except (psutil.Error, AttributeError):
                memory = process.memory_info()
            try:
                name = process.name()
            except psutil.Error:
                name = ""
            try:
                executable = process.exe()
            except (psutil.Error, OSError):
                executable = ""
            entry: dict[str, Any] = {
                "pid": process.pid,
                "role": "parent" if process.pid == root.pid else "child",
                "name": name,
                "exe": executable,
                "cpu_user_seconds": float(cpu.user),
                "cpu_system_seconds": float(cpu.system),
                "rss_bytes": int(memory.rss),
                "vms_bytes": int(memory.vms),
                "threads": int(process.num_threads()),
            }
            for source, target in (("private", "private_bytes"), ("peak_wset", "peak_working_set_bytes")):
                if (value := getattr(memory, source, None)) is not None:
                    entry[target] = int(value)
            try:
                entry["handles"] = int(process.num_handles())
            except (psutil.Error, AttributeError):
                pass
            try:
                switches = process.num_ctx_switches()
                entry["ctx_voluntary"] = int(switches.voluntary)
                entry["ctx_involuntary"] = int(switches.involuntary)
            except (psutil.Error, AttributeError):
                pass
            try:
                io = process.io_counters()
                for name in ("read_count", "write_count", "read_bytes", "write_bytes"):
                    if (value := getattr(io, name, None)) is not None:
                        entry[f"io_{name}"] = int(value)
            except (psutil.Error, AttributeError):
                pass
            entries.append(entry)
        except psutil.Error:
            pass

    if not entries:
        return None
    parent = _aggregate(entry for entry in entries if entry["role"] == "parent")
    children = _aggregate(entry for entry in entries if entry["role"] == "child")
    return {"tree": _aggregate(entries), "parent": parent, "children": children, "processes": entries}


def resourceWindow(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, float] | None:
    if before is None or after is None:
        return None

    result: dict[str, float] = {}
    cumulative = (
        "cpu_user_seconds", "cpu_system_seconds", "ctx_voluntary", "ctx_involuntary",
        "io_read_count", "io_write_count", "io_read_bytes", "io_write_bytes",
    )
    current = (
        "rss_bytes", "vms_bytes", "private_bytes", "peak_working_set_bytes", "handles", "threads", "process_count",
    )
    for scope in ("tree", "parent", "children"):
        beforeScope, afterScope = before[scope], after[scope]
        for name in cumulative:
            if name in beforeScope and name in afterScope:
                result[f"{scope}_{name}"] = float(afterScope[name] - beforeScope[name])
        for name in current:
            if name in beforeScope and name in afterScope:
                divisor = 1024**2 if name.endswith("_bytes") else 1
                label = name[:-6] + "mb" if name.endswith("_bytes") else name
                result[f"{scope}_{label}_before"] = float(beforeScope[name] / divisor)
                result[f"{scope}_{label}_after"] = float(afterScope[name] / divisor)
                result[f"{scope}_{label}_delta"] = float((afterScope[name] - beforeScope[name]) / divisor)

    for scope in ("tree", "parent", "children"):
        user = result.get(f"{scope}_cpu_user_seconds")
        system = result.get(f"{scope}_cpu_system_seconds")
        if user is not None and system is not None:
            result[f"{scope}_cpu_total_seconds"] = user + system
        voluntary = result.get(f"{scope}_ctx_voluntary")
        involuntary = result.get(f"{scope}_ctx_involuntary")
        if voluntary is not None and involuntary is not None:
            result[f"{scope}_ctx_total"] = voluntary + involuntary
    return result


def _aggregate(entries: Any) -> dict[str, int | float]:
    result: dict[str, int | float] = {"process_count": 0}
    for entry in entries:
        result["process_count"] += 1
        for name, value in entry.items():
            if name in ("pid", "role") or not isinstance(value, (int, float)):
                continue
            result[name] = result.get(name, 0) + value
    return result
