"""Import/bootstrap configuration tests, isolated from the pytest process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "psycopg"


def _run(tmp_path: Path, code: str, *, settings: dict[str, str] | None = None):
    env = os.environ.copy()
    for key in (
        "PSYCOPG_IMPL",
        "QPSYCOPG_WORKER_IMPL",
        "PSYCOPG_PG_ROOT",
        "QPSYCOPG_IPC",
        "QPSYCOPG_ENV_FILE",
    ):
        env.pop(key, None)
    env["PYTHONPATH"] = os.pathsep.join(
        (str(_PACKAGE_ROOT), env.get("PYTHONPATH", ""))
    )
    if settings:
        env.update(settings)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dotenv_selects_backend_before_pq_import(tmp_path):
    (tmp_path / ".env").write_text(
        "PSYCOPG_IMPL=python\n"
        "QPSYCOPG_WORKER_IMPL=c\n"
        "QPSYCOPG_DSN=should-not-leak\n"
        "UNRELATED_TOKEN=should-not-leak\n",
        encoding="utf-8",
    )
    result = _run(
        tmp_path,
        "import os, psycopg; from psycopg import pq; "
        "assert pq.__impl__ == 'python'; "
        "assert os.environ['QPSYCOPG_WORKER_IMPL'] == 'c'; "
        "assert 'QPSYCOPG_DSN' not in os.environ; "
        "assert 'UNRELATED_TOKEN' not in os.environ",
    )
    assert result.returncode == 0, result.stderr


def test_process_environment_overrides_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        "PSYCOPG_IMPL=invalid-value\nQPSYCOPG_WORKER_IMPL=binary\n",
        encoding="utf-8",
    )
    result = _run(
        tmp_path,
        "import os, psycopg; from psycopg import pq; "
        "assert pq.__impl__ == 'python'; "
        "assert os.environ['QPSYCOPG_WORKER_IMPL'] == 'c'",
        settings={"PSYCOPG_IMPL": "python", "QPSYCOPG_WORKER_IMPL": "c"},
    )
    assert result.returncode == 0, result.stderr


def test_explicit_relative_env_file(tmp_path):
    (tmp_path / ".env.local").write_text(
        "export PSYCOPG_IMPL='python' # this is inside the value\n"
        "QPSYCOPG_IPC=tcp # comment\n",
        encoding="utf-8",
    )
    result = _run(
        tmp_path,
        "import os, psycopg; from psycopg import pq; "
        "assert pq.__impl__ == 'python'; "
        "assert os.environ['QPSYCOPG_IPC'] == 'tcp'",
        settings={"QPSYCOPG_ENV_FILE": ".env.local"},
    )
    assert result.returncode == 0, result.stderr


def test_malformed_backend_value_reports_source(tmp_path):
    (tmp_path / ".env").write_text('PSYCOPG_IMPL="qt-thread\n', encoding="utf-8")
    result = _run(tmp_path, "import psycopg", settings={"PSYCOPG_IMPL": "python"})
    assert result.returncode != 0
    assert "unterminated quoted value for PSYCOPG_IMPL" in result.stderr


def test_no_env_file_does_not_change_upstream_selection(tmp_path):
    result = _run(
        tmp_path,
        "import psycopg; from psycopg import pq; "
        "assert pq.__impl__ == 'python'",
        settings={"PSYCOPG_IMPL": "python"},
    )
    assert result.returncode == 0, result.stderr
