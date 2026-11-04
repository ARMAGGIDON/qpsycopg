#!/usr/bin/env python3
"""Load local qpsycopg development configuration from the repository .env."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_ENV = _REPO_ROOT / ".env"
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXPANSION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DLL_DIR_HANDLES: list[object] = []


def _envPath(path: str | os.PathLike[str] | None = None) -> Path:
    if path is None:
        path = os.environ.get("QPSYCOPG_ENV_FILE")
    if path is None:
        return _DEFAULT_ENV
    value = Path(path).expanduser()
    return value if value.is_absolute() else _REPO_ROOT / value


def readEnv(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    envPath = _envPath(path)
    if not envPath.is_file():
        return {}

    values: dict[str, str] = {}
    for lineNo, raw in enumerate(envPath.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{envPath}:{lineNo}: expected NAME=VALUE")

        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not _KEY.fullmatch(key):
            raise ValueError(f"{envPath}:{lineNo}: invalid environment variable name {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        value = _EXPANSION.sub(lambda match: values.get(match.group(1), os.environ.get(match.group(1), match.group(0))), value)
        values[key] = value

    return values


def _applyPgRuntime() -> None:
    root = os.environ.get("PSYCOPG_PG_ROOT")
    if not root:
        return
    binDir = Path(root) / "bin"
    if not binDir.is_dir():
        return

    binValue = str(binDir)
    pathItems = os.environ.get("PATH", "").split(os.pathsep)
    if os.path.normcase(binValue) not in {os.path.normcase(item) for item in pathItems if item}:
        os.environ["PATH"] = binValue + os.pathsep + os.environ.get("PATH", "")

    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        _DLL_DIR_HANDLES.append(os.add_dll_directory(binValue))


def loadEnv(path: str | os.PathLike[str] | None = None, *, override: bool = False) -> dict[str, str]:
    values = readEnv(path)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    _applyPgRuntime()
    return values


def dsn(explicit: str | None = None) -> str:
    value = explicit or os.environ.get("QPSYCOPG_DSN")
    if value:
        return value
    raise SystemExit("QPSYCOPG_DSN is not set; add it to .env or pass the DSN argument")


loadEnv()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    values = readEnv()
    if args.json:
        print(json.dumps(values))
    else:
        for name in args.names:
            if name in os.environ:
                print(os.environ[name])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
