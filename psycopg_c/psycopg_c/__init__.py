"""
psycopg -- PostgreSQL database adapter for Python -- C optimization package
"""

# Copyright (C) 2020 The Psycopg Team

import os
import sys

# This package shouldn't be imported before psycopg itself, or weird things
# will happen
if "psycopg" not in sys.modules:
    raise ImportError("the psycopg package should be imported before psycopg_c")

_dllDirectory = None
if sys.platform == "win32" and (pgRoot := os.environ.get("PSYCOPG_PG_ROOT")):
    binDir = os.path.join(pgRoot, "bin")
    if os.path.isdir(binDir):
        _dllDirectory = os.add_dll_directory(binDir)

from .version import __version__ as __version__  # noqa
