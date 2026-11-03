"""Import-time .env and Windows libpq bootstrap for qpsycopg.

This module only reads backend-related keys. In particular, application secrets
and unrelated .env entries are never copied into the process environment.
"""

from __future__ import annotations

import os
from pathlib import Path

_BACKEND_KEYS = frozenset(
    ("PSYCOPG_IMPL", "QPSYCOPG_WORKER_IMPL", "QPSYCOPG_IPC", "PSYCOPG_PG_ROOT")
)
_DLL_DIR_HANDLES: list[object] = []


def _read_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return {}

    values: dict[str, str] = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if key not in _BACKEND_KEYS:
            continue

        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing < 0:
                raise ValueError(f"{path}:{number}: unterminated quoted value for {key}")
            suffix = value[closing + 1 :].strip()
            if suffix and not suffix.startswith("#"):
                raise ValueError(f"{path}:{number}: unexpected trailing text for {key}")
            value = value[1:closing]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _setup_windows_libpq() -> None:
    if os.name != "nt":
        return
    root = os.environ.get("PSYCOPG_PG_ROOT")
    if not root:
        return

    bin_dir = Path(root).expanduser() / "bin"
    if not bin_dir.is_dir():
        raise FileNotFoundError(f"PSYCOPG_PG_ROOT/bin not found: {bin_dir}")

    directory = str(bin_dir.resolve())
    _DLL_DIR_HANDLES.append(os.add_dll_directory(directory))
    path = os.environ.get("PATH", "")
    paths = [p for p in path.split(os.pathsep) if p]
    if os.path.normcase(directory) not in {
        os.path.normcase(os.path.normpath(p)) for p in paths
    }:
        os.environ["PATH"] = directory + os.pathsep + path


def bootstrap() -> None:
    """Read application .env before pq or a native libpq binding is imported."""
    env_file = os.environ.get("QPSYCOPG_ENV_FILE", ".env")
    path = Path(env_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path

    for key, value in _read_env(path).items():
        os.environ.setdefault(key, value)

    _setup_windows_libpq()
