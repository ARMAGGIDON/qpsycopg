#!/usr/bin/env python3
"""Run a Python script after applying the same local libpq DLL bootstrap."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: run_with_pg_root.py SCRIPT [ARGS ...]")

    dllHandle = None
    if os.name == "nt" and (root := os.environ.get("PSYCOPG_PG_ROOT")):
        pgBin = Path(root) / "bin"
        if pgBin.is_dir():
            dllHandle = os.add_dll_directory(str(pgBin))

    script, *args = sys.argv[1:]
    sys.argv = [script, *args]
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        if dllHandle is not None:
            dllHandle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
