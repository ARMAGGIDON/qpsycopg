#!/usr/bin/env python3
"""Capture lightweight host state around a benchmark run."""

from __future__ import annotations

import json
import os
import platform
import time

import psutil


def main() -> int:
    memory = psutil.virtual_memory()
    frequency = psutil.cpu_freq()
    result = {
        "timestamp": time.time(),
        "platform": platform.platform(),
        "cpu_logical": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "cpu_frequency_mhz": None if frequency is None else frequency.current,
        "memory_available_mib": memory.available / 1024**2,
        "memory_percent": memory.percent,
        "process_count": len(psutil.pids()),
        "pid": os.getpid(),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
