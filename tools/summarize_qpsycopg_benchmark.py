#!/usr/bin/env python3
"""Aggregate paired upstream/qpsycopg benchmarks and quantify run-to-run drift."""

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


def values(runs: list[Json], get: Callable[[Json], float]) -> list[float]:
    return [float(get(run)) for run in runs]


def aggregate(runs: list[Json], get: Callable[[Json], float]) -> float:
    return med(values(runs, get))


def madPct(items: list[float]) -> float:
    center = med(items)
    if center == 0:
        return 0.0
    return med([abs(value - center) for value in items]) / abs(center) * 100


def rangePct(items: list[float]) -> float:
    center = med(items)
    if center == 0:
        return 0.0
    return (max(items) - min(items)) / abs(center) * 100


def resource(run: Json, name: str) -> float:
    return float(nested(run, "resources_connected", "tree", name))


METRICS: list[tuple[str, str, str, Callable[[Json], float], str]] = [
    ("Connection", "Cold connect", "ms/connection", lambda r: nested(r, "connect_cold", "ms"), "lower"),
    ("Connection", "Warm connect median", "ms/connection", lambda r: nested(r, "connect_warm", "median_ms"), "lower"),
    ("Connection", "Warm connect p95", "ms/connection", lambda r: nested(r, "connect_warm", "p95_ms"), "lower"),
    ("Short query", "SELECT 1 median", "ms/query", lambda r: nested(r, "select_1", "median_ms"), "lower"),
    ("Short query", "SELECT 1 p95", "ms/query", lambda r: nested(r, "select_1", "p95_ms"), "lower"),
    ("Short query", "SELECT 1 p99", "ms/query", lambda r: nested(r, "select_1", "p99_ms"), "lower"),
    ("CPU", "CPU time per short query", "us CPU/query", lambda r: nested(r, "select_1", "resources", "tree_cpu_us_per_call"), "lower"),
    ("Scheduling", "Context switches", "switches/query", lambda r: nested(r, "select_1", "resources", "tree_ctx_switches_per_call"), "lower"),
    ("Bulk", "Execute 100k rows", "ms/100k rows", lambda r: nested(r, "fetch", "execute_seconds") * 1000, "lower"),
    ("Bulk", "Fetch/decode 100k rows", "ms/100k rows", lambda r: nested(r, "fetch", "fetchall_seconds") * 1000, "lower"),
    ("Bulk", "Total 100k rows", "ms/100k rows", lambda r: nested(r, "fetch", "seconds") * 1000, "lower"),
    ("Bulk", "Throughput", "krows/s", lambda r: nested(r, "fetch", "rows_per_second") / 1000, "higher"),
    ("Concurrency", "N-thread workload", "ms/batch", lambda r: nested(r, "threads", "seconds") * 1000, "lower"),
    ("Concurrency", "Concurrent throughput", "queries/s", lambda r: len(r["threads"]["values"]) / nested(r, "threads", "seconds"), "higher"),
    ("Cancellation", "Cancel call median", "ms/cancel", lambda r: nested(r, "cancel", "call", "median_ms"), "lower"),
    ("Cancellation", "Cancel call p95", "ms/cancel", lambda r: nested(r, "cancel", "call", "p95_ms"), "lower"),
    ("Cancellation", "Query finish after cancel median", "ms/cancel", lambda r: nested(r, "cancel", "query_finish", "median_ms"), "lower"),
    ("Memory", "Process-tree RSS", "MiB", lambda r: resource(r, "rss_bytes") / MiB, "lower"),
    ("Memory", "Process-tree private memory", "MiB", lambda r: resource(r, "private_bytes") / MiB, "lower"),
]

QT_METRICS: list[tuple[str, str, str, Callable[[Json], float], str]] = [
    ("Qt responsiveness", "Event-loop/timer p95", "ms", lambda r: nested(r, "timer", "p95_ms"), "lower"),
    ("Qt responsiveness", "Event-loop/timer p99", "ms", lambda r: nested(r, "timer", "p99_ms"), "lower"),
    ("Qt responsiveness", "Maximum stall", "ms", lambda r: nested(r, "timer", "max_stall_ms"), "lower"),
]

ARCHITECTURE: list[tuple[str, str, Callable[[Json], float]]] = [
    ("Processes", "processes", lambda r: resource(r, "process_count")),
    ("Total threads", "threads", lambda r: resource(r, "threads")),
    ("Handles", "handles", lambda r: resource(r, "handles")),
]

HOST_METRICS: list[tuple[str, str, Callable[[Json], float | None]]] = [
    ("System CPU load", "%", lambda r: r.get("cpu_percent")),
    ("CPU frequency", "MHz", lambda r: r.get("cpu_frequency_mhz")),
    ("Available memory", "MiB", lambda r: r.get("memory_available_mib")),
    ("System process count", "processes", lambda r: r.get("process_count")),
]


def fmt(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.1f}"
    if abs(value) >= 100:
        return f"{value:.2f}"
    return f"{value:.3f}"


def ratioStats(upstreamRuns: list[Json], modeRuns: list[Json], getter: Callable[[Json], float]) -> Json:
    if len(upstreamRuns) != len(modeRuns):
        raise SystemExit(f"paired benchmark repeat count differs: upstream={len(upstreamRuns)}, mode={len(modeRuns)}")
    ratios = [float(getter(mode)) / float(getter(upstream)) for upstream, mode in zip(upstreamRuns, modeRuns)]
    return {
        "median": med(ratios),
        "min": min(ratios),
        "max": max(ratios),
        "mad_pct": madPct(ratios),
        "all_below_one": all(ratio < 1.0 for ratio in ratios),
        "all_above_one": all(ratio > 1.0 for ratio in ratios),
        "samples": ratios,
    }


def buildRows(
    metrics: list[tuple[str, str, str, Callable[[Json], float], str]],
    reference: list[Json],
    upstream: list[Json],
    runs: dict[str, list[Json]],
) -> list[Json]:
    rows: list[Json] = []
    for category, metric, unit, getter, better in metrics:
        referenceValues = values(reference, getter)
        upstreamValues = values(upstream, getter)
        referenceValue = med(referenceValues)
        upstreamValue = med(upstreamValues)
        row: Json = {
            "category": category,
            "metric": metric,
            "unit": unit,
            "better": better,
            "reference_upstream": referenceValue,
            "upstream": upstreamValue,
            "upstream_drift_ratio": upstreamValue / referenceValue,
            "upstream_mad_pct": madPct(upstreamValues),
            "upstream_range_pct": rangePct(upstreamValues),
        }
        for mode, modeRuns in runs.items():
            row[mode] = aggregate(modeRuns, getter)
            paired = ratioStats(upstream, modeRuns, getter)
            row[f"{mode}_ratio"] = paired["median"]
            row[f"{mode}_ratio_stats"] = paired
        rows.append(row)
    return rows


def hostRows(referenceDir: Path, checkpointDir: Path, modes: tuple[str, ...]) -> list[Json]:
    reference = loadMany(referenceDir, "host-*-upstream-*.json")
    current = {mode: loadMany(checkpointDir, f"host-*-{mode}-*.json") for mode in ("upstream", *modes)}
    rows: list[Json] = []
    for metric, unit, getter in HOST_METRICS:
        referenceItems = [float(value) for item in reference if (value := getter(item)) is not None]
        row: Json = {"metric": metric, "unit": unit, "reference_upstream": med(referenceItems)}
        for mode, modeRuns in current.items():
            items = [float(value) for item in modeRuns if (value := getter(item)) is not None]
            row[mode] = med(items)
        rows.append(row)
    return rows


def processInventory(runs: list[Json]) -> list[Json]:
    countsByRun: list[dict[tuple[str, str, str], int]] = []
    signatures: set[tuple[str, str, str]] = set()
    for run in runs:
        counts: dict[tuple[str, str, str], int] = {}
        for process in nested(run, "resources_connected", "processes"):
            executable = str(process.get("exe", "")).replace("\\", "/").rsplit("/", 1)[-1]
            signature = (str(process.get("role", "")), str(process.get("name", "")), executable)
            counts[signature] = counts.get(signature, 0) + 1
            signatures.add(signature)
        countsByRun.append(counts)

    result = []
    for role, name, executable in sorted(signatures):
        count = med([float(run.get((role, name, executable), 0)) for run in countsByRun])
        result.append({"role": role, "name": name, "exe": executable, "count": count})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()

    modes = ("direct", "thread", "process")
    referenceRuns = loadMany(args.reference, "bench-*-upstream.json")
    referenceQt = loadMany(args.reference, "qt-*-upstream.json")
    upstreamRuns = loadMany(args.checkpoint, "bench-*-upstream.json")
    upstreamQt = loadMany(args.checkpoint, "qt-*-upstream.json")
    runs = {mode: loadMany(args.checkpoint, f"bench-*-{mode}.json") for mode in modes}
    qtRuns = {mode: loadMany(args.checkpoint, f"qt-*-{mode}.json") for mode in modes}

    rows = buildRows(METRICS, referenceRuns, upstreamRuns, runs)
    rows += buildRows(QT_METRICS, referenceQt, upstreamQt, qtRuns)

    architecture: list[Json] = []
    for metric, unit, getter in ARCHITECTURE:
        upstream = aggregate(upstreamRuns, getter)
        row: Json = {"metric": metric, "unit": unit, "upstream": upstream}
        for mode in modes:
            value = aggregate(runs[mode], getter)
            row[mode] = value
            row[f"{mode}_delta"] = value - upstream
        architecture.append(row)

    referenceManifest = json.loads((args.reference / "manifest.json").read_text(encoding="utf-8-sig"))
    checkpointManifest = json.loads((args.checkpoint / "manifest.json").read_text(encoding="utf-8-sig"))
    hosts = hostRows(args.reference, args.checkpoint, modes)
    inventories = {"upstream": processInventory(upstreamRuns)}
    inventories.update({mode: processInventory(modeRuns) for mode, modeRuns in runs.items()})
    result = {
        "method": {
            "primary_ratio": "median of per-round fork/upstream ratios",
            "drift": "current upstream median / historical reference upstream median",
            "mad_pct": "median absolute deviation / median * 100",
            "range_pct": "(max - min) / median * 100",
            "connect": "first connection is cold; following connections are reported separately as warm",
            "cancellation": "multiple cancellation attempts per SQL run",
            "qt": "p95 and p99 are primary tail metrics; max stall is diagnostic only",
        },
        "reference": referenceManifest,
        "checkpoint": checkpointManifest,
        "metrics": rows,
        "architecture": architecture,
        "process_inventory": inventories,
        "host_state": hosts,
    }
    (args.checkpoint / "comparison.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "Primary Ratio is the median of per-round Fork/Upstream ratios. Current Upstream is rerun in every checkpoint.",
        "Historical Reference is used only to show machine/environment drift. MAD and observed range describe measured noise; they are not confidence intervals.",
        "Cold and warm connection startup are separate. Cancellation uses repeated attempts. Qt p95/p99 are primary; maximum stall is diagnostic.",
        "",
        "| Category | Metric | Unit | Current Upstream | Fork direct | Ratio | qt-thread | Ratio | qt-process | Ratio |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['category']} | {row['metric']} | {row['unit']} | {fmt(row['upstream'])} | "
            f"{fmt(row['direct'])} | {row['direct_ratio']:.3f}x | {fmt(row['thread'])} | {row['thread_ratio']:.3f}x | "
            f"{fmt(row['process'])} | {row['process_ratio']:.3f}x |"
        )

    lines += [
        "",
        "| Measurement stability | Unit | Historical upstream | Current upstream | Drift | Upstream MAD | Upstream range | Direct ratio range | Thread ratio range | Process ratio range |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        direct, thread, process = (row[f"{mode}_ratio_stats"] for mode in modes)
        lines.append(
            f"| {row['metric']} | {row['unit']} | {fmt(row['reference_upstream'])} | {fmt(row['upstream'])} | "
            f"{row['upstream_drift_ratio']:.3f}x | {row['upstream_mad_pct']:.2f}% | {row['upstream_range_pct']:.2f}% | "
            f"{direct['min']:.3f}-{direct['max']:.3f}x | {thread['min']:.3f}-{thread['max']:.3f}x | "
            f"{process['min']:.3f}-{process['max']:.3f}x |"
        )

    lines += [
        "",
        "| Architecture resource | Unit | Current upstream | Fork direct | Delta | qt-thread | Delta | qt-process | Delta |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in architecture:
        lines.append(
            f"| {row['metric']} | {row['unit']} | {fmt(row['upstream'])} | {fmt(row['direct'])} | {row['direct_delta']:+.1f} | "
            f"{fmt(row['thread'])} | {row['thread_delta']:+.1f} | {fmt(row['process'])} | {row['process_delta']:+.1f} |"
        )

    lines += [
        "",
        "| Process inventory | Role | Name | Executable | Median count |",
        "|---|---|---|---|---:|",
    ]
    for mode, inventory in inventories.items():
        for item in inventory:
            lines.append(
                f"| {mode} | {item['role']} | {item['name'] or '-'} | {item['exe'] or '-'} | {fmt(item['count'])} |"
            )

    lines += [
        "",
        "| Host state | Unit | Historical upstream | Current upstream | Fork direct | qt-thread | qt-process |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in hosts:
        lines.append(
            f"| {row['metric']} | {row['unit']} | {fmt(row['reference_upstream'])} | {fmt(row['upstream'])} | "
            f"{fmt(row['direct'])} | {fmt(row['thread'])} | {fmt(row['process'])} |"
        )

    (args.checkpoint / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
