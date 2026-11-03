Qt backend import-time configuration
====================================

Status
------

This is a bounded configuration feature on ``feature/qt-backend-config``,
separate from ``feature/qt-process-pq`` performance experiments.
Coordinator review of PR #2 accepted the import-time architecture
(review ID 5758449221); merge remains blocked pending integration checks.

Application ``.env``
--------------------

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
own full ``.env`` as before. Parsing accepts ``NAME=value``,
``export NAME=value``, quoted values
and unquoted trailing `` # comment``.
No shell evaluation, variable interpolation or implicit credentials loading
is performed.

Example application ``.env``::

    PSYCOPG_IMPL=qt-thread
    QPSYCOPG_WORKER_IMPL=c
    PSYCOPG_PG_ROOT=C:\PostgreSQL\18\pgsql

Then application code simply does::

    import psycopg
    connection = psycopg.connect(...)

Deployment and current-working-directory constraints
----------------------------------------------------

``.env`` is loaded **automatically during the first import** of
``psycopg``. The default path is ``Path.cwd() / ".env"``, **not** the
directory containing the imported script, the installed package, or the
application's configuration module. A service manager, scheduled task,
notebook kernel, test runner, web server, or process launched via a different
working directory may therefore select a different file. Changing the
current directory *after* importing ``psycopg`` has no effect on the chosen
top-level implementation.

In deployments, set ``QPSYCOPG_ENV_FILE`` to an **absolute path** for a
trusted, access-controlled configuration file *before* Python imports
``psycopg``, or set the individual backend keys in the process environment.
Relative values of ``QPSYCOPG_ENV_FILE`` are resolved against the current
working directory at import time. A missing file is ignored; unreadable
files and malformed values in the selected backend keys raise during import.
``.env`` values never override corresponding process environment keys.

Do not run applications that implicitly read ``.env`` from an untrusted
working directory. Although unrelated keys and credentials are ignored,
these backend keys affect native library choice, and Windows
``PSYCOPG_PG_ROOT`` can alter the DLL search location. A writable
working directory can therefore become a **native-library loading trust
boundary**. In services accepting user-controlled paths, explicitly provide
a trusted absolute ``QPSYCOPG_ENV_FILE`` instead of relying on CWD.

The settings apply to the **whole Python interpreter**, not just one
connection or thread. Importing from plugins, tests, or application startup
code can freeze the selection earlier than expected. Set configuration
before any direct or indirect ``import psycopg``; changing the environment
later is not supported as a backend-switching API. Processes with independent
Python interpreters initialize separately.

Precedence and freeze point
---------------------------

For these settings the process environment takes precedence over ``.env``,
which takes precedence over the existing backend defaults. Values in ``.env``
shadowed by process environment keys are not parsed. Application code
can therefore use ordinary Python before importing psycopg::

    import os
    os.environ["PSYCOPG_IMPL"] = "qt-thread"
    os.environ["QPSYCOPG_WORKER_IMPL"] = "c"

    import psycopg

The selected top-level ``pq`` implementation and its dependent
classes are **fixed during import**, before any connection is created.
Subsequent edits to ``PSYCOPG_IMPL`` or the application ``.env`` do not
change that selected backend. The worker backend for ``qt-thread`` is
loaded at import; ``qt-process`` currently reads its worker setting when
starting a subprocess, so modifying ``QPSYCOPG_WORKER_IMPL`` later is not
a supported reconfiguration mechanism. Repeated imports reuse the
existing module; a changed ``.env``
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

Validation checkpoint
---------------------

The dedicated Qt CI workflow validates the configuration independently from
performance experiments. Its Linux-focused subprocess suite passed
``10 passed, 2 skipped`` (Windows-only cases) on commit ``ffd01a2``,
and both ``qt-thread`` and ``qt-process`` semantic smoke steps passed.
On commit ``a08dec6`` the same focused tests and smokes passed, and a
separate Windows runner passed the native DLL-directory registration and
invalid-root checks. These checks do not establish a full Windows PostgreSQL
connection through every native binding.

The repository-wide lint workflow still reports many existing errors in
other Qt/profiling files. Feature-owned formatting diagnostics were corrected
separately. Record the final-head CI outcome in the PR review evidence before
integration; do not treat a failed repository-wide lint run as feature-clean
without checking its diagnostics. The full upstream test matrix is a separate
CI gate, and no SQL performance ratios have been claimed.

Coordinator review should decide whether to accept this minimal, import-time
configuration alternative or require a separate deferred-bootstrap design for
the proposed post-import ``psycopg.configure()`` API. No runtime owner should
attempt per-connection backend switching as a substitute.
