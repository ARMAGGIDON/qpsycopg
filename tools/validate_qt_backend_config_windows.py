"""Exercise Windows DLL bootstrap without needing a PostgreSQL server."""

from __future__ import annotations

import os
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows-only bootstrap check")

    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        os.environ["QPSYCOPG_ENV_FILE"] = str(directory / "missing.env")
        os.environ.pop("PSYCOPG_PG_ROOT", None)

        path = (
            Path(__file__).resolve().parents[1]
            / "psycopg/psycopg/_qt_backend_config.py"
        )
        spec = importlib.util.spec_from_file_location("_qt_backend_config_check", path)
        assert spec is not None and spec.loader is not None
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        assert not config._DLL_DIR_HANDLES

        root = directory / "postgres"
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True)
        os.environ["PSYCOPG_PG_ROOT"] = str(root)
        config.bootstrap()
        assert len(config._DLL_DIR_HANDLES) == 1
        assert os.environ["PATH"].split(os.pathsep)[0] == str(bin_dir.resolve())

        os.environ["PSYCOPG_PG_ROOT"] = str(directory / "missing-root")
        try:
            config._setup_windows_libpq()
        except FileNotFoundError as error:
            assert "PSYCOPG_PG_ROOT/bin not found" in str(error)
        else:
            raise AssertionError("missing PostgreSQL bin was silently accepted")

    print("Windows DLL bootstrap OK")


if __name__ == "__main__":
    main()
