from __future__ import annotations

import argparse
import json
import statistics
import zipfile
from pathlib import Path
from typing import Any, Callable

WORKLOAD_VERSION = 1


def nested(value: dict[str, Any], *path: str) -> float | None:
    current: Any = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return float(current) if isinstance(current, (int, float)) else None


def first(*getters: Callable[[dict[str, Any]], float | None]) -> Callable[[dict[str, Any]], float | None]:
    def get(value: dict[str, Any]) -> float | None:
        for getter in getters:
            result = getter(value)
            if result is not None:
                return result
        return None

    return get


def at(*path: str) -> Callable[[dict[str, Any]], float | None]:
    return lambda value: nested(value, *path)


METRICS: dict[str, Callable[[dict[str, Any]], float | None]] = {
    "connect_median_ms": at("connect", "median_ms"),
    "select_1_median_ms": at("select_1", "median_ms"),
    "select_1_p95_ms": at("select_1", "p95_ms"),
    "select_cpu_us_per_call": at("select_1", "resources", "tree_cpu_us_per_call"),
    "select_ctx_switches_per_call": at("select_1", "resources", "tree_ctx_switches_per_call"),
    "select_rss_mb": at("select_1", "resources", "tree_rssmb_after"),
    "select_private_mb": at("select_1", "resources", "tree_privatemb_after"),
    "select_handles": at("select_1", "resources", "tree_handles_after"),
    "select_threads": at("select_1", "resources", "tree_threads_after"),
    "select_processes": at("select_1", "resources", "tree_process_count_after"),
    "fetch_seconds": at("fetch", "seconds"),
    "fetch_execute_seconds": at("fetch", "execute_seconds"),
    "fetchall_seconds": at("fetch", "fetchall_seconds"),
    "fetch_rows_per_second": at("fetch", "rows_per_second"),
    "fetch_rss_before_mb": first(
        at("fetch", "resources", "tree_rssmb_before"), at("fetch", "rss_before_mb")
    ),
    "fetch_rss_after_mb": first(
        at("fetch", "resources", "tree_rssmb_after"), at("fetch", "rss_after_mb")
    ),
    "fetch_rss_delta_mb": first(
        at("fetch", "resources", "tree_rssmb_delta"), at("fetch", "rss_delta_mb")
    ),
    "fetch_private_mb": at("fetch", "resources", "tree_privatemb_after"),
    "fetch_cpu_seconds": at("fetch", "resources", "tree_cpu_total_seconds"),
    "fetch_ctx_switches": at("fetch", "resources", "tree_ctx_total"),
    "threads_seconds": at("threads", "seconds"),
    "cancel_call_seconds": at("cancel", "call_seconds"),
}


def robustSummary(values: list[float]) -> dict[str, float | int]:
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    return {
        "median": median,
        "mad": statistics.median(deviations),
        "samples": len(values),
    }


def compatible(manifest: dict[str, Any], args: argparse.Namespace) -> bool:
    return (
        manifest.get("mode") == "bench"
        and manifest.get("worker") == args.worker
        and int(manifest.get("iterations", -1)) == args.iterations
        and int(manifest.get("rows", -1)) == args.rows
        and int(manifest.get("threads", -1)) == args.threads
        and int(manifest.get("benchmark_workload_version", WORKLOAD_VERSION)) == WORKLOAD_VERSION
    )


def readArchive(path: Path, args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names:
                return None
            manifest = json.loads(archive.read("manifest.json").decode("utf-8-sig"))
            if not compatible(manifest, args):
                return None
            runs = [
                json.loads(archive.read(name).decode("utf-8-sig"))
                for name in sorted(names)
                if name.startswith("bench-") and name.endswith("-vanilla.json")
            ]
            return manifest, runs
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_root", type=Path)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--iterations", type=int, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--threads", type=int, required=True)
    args = parser.parse_args()

    values: dict[str, list[float]] = {name: [] for name in METRICS}
    archives = 0
    runs = 0
    tags: list[str] = []

    for path in sorted(args.results_root.glob("qpsycopg-bench-*.zip")):
        loaded = readArchive(path, args)
        if loaded is None:
            continue
        manifest, archiveRuns = loaded
        if not archiveRuns:
            continue
        archives += 1
        runs += len(archiveRuns)
        if tag := manifest.get("tag"):
            tags.append(str(tag))
        for run in archiveRuns:
            for name, getter in METRICS.items():
                if (value := getter(run)) is not None:
                    values[name].append(value)

    print(json.dumps({
        "workload_version": WORKLOAD_VERSION,
        "worker": args.worker,
        "iterations": args.iterations,
        "rows": args.rows,
        "threads": args.threads,
        "archives": archives,
        "runs": runs,
        "tags": tags,
        "metrics": {name: robustSummary(samples) for name, samples in values.items() if samples},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
