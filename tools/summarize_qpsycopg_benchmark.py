#!/usr/bin/env python3
"""Aggregate qpsycopg benchmark repeats against a frozen upstream baseline."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Callable

Json = dict[str, Any]
MiB = 1024**2


def loadMany(path: Path, pattern: str) -> list[Json]:
    files = sorted(path.glob(pattern))
    if not files:
        raise SystemExit(f"no benchmark files matching {path / pattern}")
    return [json.loads(file.read_text(encoding="utf-8-sig")) for file in files]


def nested(data: Json, *path: str) -> Any:
    value: Any = data
    for item in path:
        value = value[item]
    return value


def med(values: list[float]) -> float:
    return float(statistics.median(values))


def aggregate(runs: list[Json], get: Callable[[Json], float]) -> float:
    return med([float(get(run)) for run in runs])


def resource(run: Json, name: str) -> float:
    return float(nested(run, "resources_connected", "tree", name))


METRICS: list[tuple[str, str, str, Callable[[Json], float], str]] = [
    ("Connection", "Connect median", "ms/connection", lambda r: nested(r, "connect", "median_ms"), "lower"),
    ("Connection", "Connect p95", "ms/connection", lambda r: nested(r, "connect", "p95_ms"), "lower"),
    ("Short query", "SELECT 1 median", "ms/query", lambda r: nested(r, "select_1", "median_ms"), "lower"),
    ("Short query", "SELECT 1 p95", "ms/query", lambda r: nested(r, "select_1", "p95_ms"), "lower"),
    ("CPU", "CPU time per short query", "us CPU/query", lambda r: nested(r, "select_1", "resources", "tree_cpu_us_per_call"), "lower"),
    ("Scheduling", "Context switches", "switches/query", lambda r: nested(r, "select_1", "resources", "tree_ctx_switches_per_call"), "lower"),
    ("Bulk", "Execute 100k rows", "ms/100k rows", lambda r: nested(r, "fetch", "execute_seconds") * 1000, "lower"),
    ("Bulk", "Fetch/decode 100k rows", "ms/100k rows", lambda r: nested(r, "fetch", "fetchall_seconds") * 1000, "lower"),
    ("Bulk", "Total 100k rows", "ms/100k rows", lambda r: nested(r, "fetch", "seconds") * 1000, "lower"),
    ("Bulk", "Throughput", "krows/s", lambda r: nested(r, "fetch", "rows_per_second") / 1000, "higher"),
    ("Concurrency", "N-thread workload", "ms/batch", lambda r: nested(r, "threads", "seconds") * 1000, "lower"),
    ("Concurrency", "Concurrent throughput", "queries/s", lambda r: r["threads"] and r["threads"]["values"] and len(r["threads"]["values"]) / nested(r, "threads", "seconds"), "higher"),
    ("Cancellation", "Cancel-to-return median", "ms/cancel", lambda r: nested(r, "cancel", "call_seconds") * 1000, "lower"),
    ("Memory", "Process-tree RSS", "MiB", lambda r: resource(r, "rss_bytes") / MiB, "lower"),
    ("Memory", "Process-tree private memory", "MiB", lambda r: resource(r, "private_bytes") / MiB, "lower"),
]

QT_METRICS: list[tuple[str, str, str, Callable[[Json], float], str]] = [
    ("Qt responsiveness", "Event-loop/timer p95", "ms", lambda r: nested(r, "timer", "p95_ms"), "lower"),
    ("Qt responsiveness", "Maximum stall", "ms", lambda r: nested(r, "timer", "max_stall_ms"), "lower"),
]

ARCHITECTURE: list[tuple[str, str, Callable[[Json], float]]] = [
    ("Processes", "processes", lambda r: resource(r, "process_count")),
    ("Total threads", "threads", lambda r: resource(r, "threads")),
    ("Handles", "handles", lambda r: resource(r, "handles")),
]


def fmt(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.1f}"
    if abs(value) >= 100:
        return f"{value:.2f}"
    return f"{value:.3f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()

    baselineRuns = loadMany(args.baseline, "bench-*-upstream.json")
    baselineQt = loadMany(args.baseline, "qt-*-upstream.json")
    modes = ("direct", "thread", "process")
    runs = {mode: loadMany(args.checkpoint, f"bench-*-{mode}.json") for mode in modes}
    qtRuns = {mode: loadMany(args.checkpoint, f"qt-*-{mode}.json") for mode in modes}

    rows: list[Json] = []
    for category, metric, unit, getter, better in METRICS:
        upstream = aggregate(baselineRuns, getter)
        row: Json = {"category": category, "metric": metric, "unit": unit, "better": better, "upstream": upstream}
        for mode in modes:
            value = aggregate(runs[mode], getter)
            row[mode] = value
            row[f"{mode}_ratio"] = value / upstream
        rows.append(row)
    for category, metric, unit, getter, better in QT_METRICS:
        upstream = aggregate(baselineQt, getter)
        row = {"category": category, "metric": metric, "unit": unit, "better": better, "upstream": upstream}
        for mode in modes:
            value = aggregate(qtRuns[mode], getter)
            row[mode] = value
            row[f"{mode}_ratio"] = value / upstream
        rows.append(row)

    architecture: list[Json] = []
    for metric, unit, getter in ARCHITECTURE:
        upstream = aggregate(baselineRuns, getter)
        row = {"metric": metric, "unit": unit, "upstream": upstream}
        for mode in modes:
            value = aggregate(runs[mode], getter)
            row[mode] = value
            row[f"{mode}_delta"] = value - upstream
        architecture.append(row)

    baselineManifest = json.loads((args.baseline / "manifest.json").read_text(encoding="utf-8-sig"))
    checkpointManifest = json.loads((args.checkpoint / "manifest.json").read_text(encoding="utf-8-sig"))
    result = {"baseline": baselineManifest, "checkpoint": checkpointManifest, "metrics": rows, "architecture": architecture}
    (args.checkpoint / "comparison.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    header = "| Category | Metric | Unit | Upstream | Fork direct | Ratio | qt-thread | Ratio | qt-process | Ratio |"
    lines = [header, "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(
            f"| {row['category']} | {row['metric']} | {row['unit']} | {fmt(row['upstream'])} | "
            f"{fmt(row['direct'])} | {row['direct_ratio']:.3f}x | {fmt(row['thread'])} | {row['thread_ratio']:.3f}x | "
            f"{fmt(row['process'])} | {row['process_ratio']:.3f}x |"
        )
    lines += ["", "| Architecture resource | Unit | Upstream | Fork direct | Delta | qt-thread | Delta | qt-process | Delta |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in architecture:
        lines.append(
            f"| {row['metric']} | {row['unit']} | {fmt(row['upstream'])} | {fmt(row['direct'])} | {row['direct_delta']:+.1f} | "
            f"{fmt(row['thread'])} | {row['thread_delta']:+.1f} | {fmt(row['process'])} | {row['process_delta']:+.1f} |"
        )
    (args.checkpoint / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
