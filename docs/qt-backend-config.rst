Qt backend import-time configuration
====================================

Status
------

This is a bounded configuration feature on ``feature/qt-backend-config``,
separate from ``feature/qt-process-pq`` performance experiments. It is
proposed for Coordinator review before integration.

Application ``.env``
----------------------

At a normal ``import psycopg``, qpsycopg reads an optional ``.env`` from the
**current working directory**. ``QPSYCOPG_ENV_FILE`` can select another file;
relative paths are relative to the current working directory. The loader does
not search parent directories or the installed package directory.

Only four settings are consumed:

* ``PSYCOPG_IMPL`` -- ``qt-thread``, ``qt-process``, or ordinary upstream
  implementations ``c``, ``binary``, ``python``;
* ``QPSYCOPG_WORKER_IMPL`` -- ``c``, ``binary``, ``python``;
* ``QPSYCOPG_IPC`` -- the process transport setting;
* ``PSYCOPG_PG_ROOT`` -- optional PostgreSQL installation root on Windows.

Other keys, including credentials and ``QPSYCOPG_DSN``, are ignored by the
**library** loader. Existing development tools can continue loading their
own full ``.env`` as before. Parsing accepts ``NAME=value``, ``export
NAME=value``, quoted values and unquoted trailing `` # comment``.
No shell evaluation, variable interpolation or implicit credentials loading
is performed.

Example application ``.env``::

    PSYCOPG_IMPL=qt-thread
    QPSYCOPG_WORKER_IMPL=c
    PSYCOPG_PG_ROOT=C:\\PostgreSQL\\18\\pgsql

Then application code simply does::

    import psycopg
    connection = psycopg.connect(...)

Precedence and freeze point
---------------------------

For these settings the process environment takes precedence over ``.env``,
which takes precedence over the existing backend defaults. Application code
can therefore use ordinary Python before importing psycopg::

    import os
    os.environ["PSYCOPG_IMPL"] = "qt-thread"
    os.environ["QPSYCOPG_WORKER_IMPL"] = "c"

    import psycopg

The ``pq`` implementation and dependent generator/transformer/connection
classes are **fixed during import**, before any connection is created.
Subsequent edits to ``os.environ`` or ``.env`` do not change that selected
backend. Repeated imports reuse the existing module; a changed ``.env``
takes effect in a fresh interpreter. Use one backend per interpreter, not
per-connection switching.

The proposed ``import psycopg; psycopg.configure(impl="qt-thread",
worker_impl="c")`` API is **not implemented in this bounded change**.
It cannot safely be implemented by changing the environment after import:
``psycopg.pq``, ``_cmodule``, ``generators``, ``_transformer`` and
connection class aliases are already bound; the qt-thread worker implementation
is also loaded eagerly. A truthful public ``configure()`` API would require
an explicit, separately reviewed deferred-bootstrap architecture, with
import-semantic compatibility tests and no extra SQL hot-path dispatch.
The minimal alternative is early ``os.environ`` or application ``.env``,
neither requiring a qpsycopg bootstrap import.

Windows DLL bootstrap
---------------------

On Windows, ``PSYCOPG_PG_ROOT/bin`` is registered with
``os.add_dll_directory()`` **before** loading any native pq implementation.
The directory handle stays alive for the interpreter lifetime and ``PATH``
is prepended once for subprocess inheritance. A configured root without a
``bin`` directory raises ``FileNotFoundError``. An unset root does not
alter the DLL search path. The existing process-worker bootstrap continues
passing the worker's native ``PSYCOPG_IMPL`` explicitly, so an inherited
application ``.env`` cannot force a recursive Qt backend.

Validation and performance scope
--------------------------------

Run ``python -m pytest tests/test_backend_config.py -q``, then the normal
import/module tests and both Qt smoke gates in a fully provisioned Windows
environment. The changes add one bounded filesystem read and optional Windows
DLL setup at import, with **no code change in the SQL or queue/IPC hot path**.
No quantitative performance improvement or unchanged end-to-end timing is
claimed until measured. Production runtime optimisations remain owned by
``feature/qt-process-pq``.
