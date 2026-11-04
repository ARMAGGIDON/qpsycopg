Qt backend import-time configuration
====================================

Status
------

**MERGED:** configuration PR #2 was integrated into
``feature/qt-process-pq`` at merge commit
``9126cddfb9db45b787ea7ea626c3e152f1d1ac24``.
The original implementation was developed separately on
``feature/qt-backend-config`` and approved by Coordinator
(review ``5765518048``) before integration. The merged change
adds import-time backend configuration and Windows libpq DLL
bootstrap without altering the SQL hot path.

Post-merge Tests run ``41801317209``, attempt 2, was
accepted **56/56 SUCCESS after exactly one isolated retry**
of a previously observed async pool/proxy timeout. Its root
cause remains unproven. Baseline lint/Sphinx failures are
accepted exceptions, **not** passing checks. The subsequent bounded Windows compiled
``worker_impl=c`` follow-up **PASSED** in Qt
workflow run ``41809431708``, job
``126504663426``, on Windows 2025/Python
3.13. The explicit ``COMPILED_C_WORKER_VALIDATED``
assertions verified the compiled ``.pyd``
and C worker-module bindings, and the full
``tools/smoke_qt_thread.py`` ended with
``qpsycopg qt-thread smoke test: OK``.
This is bounded C-worker smoke evidence,
not broad Windows/native parity. The
overall Qt workflow remains FAILURE
because of accepted baseline Sphinx
documentation problems.

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
separately. At integration, repository-wide lint errors were accepted as documented
baseline exceptions after review; they were not reported as clean checks.
The full post-merge Tests matrix was subsequently accepted at 56/56
after one isolated retry. No SQL performance ratios were claimed for
this bounded configuration feature.

Coordinator review #5758449221 accepted the bounded import-time alternative
and explicitly deferred ``psycopg.configure()`` to a separate architectural
decision. PR #2 was subsequently merged following independent integration
review. Per-connection switching remains outside scope.

Integration-readiness checks
----------------------------

The dedicated ``.github/workflows/qt-process.yml``
retains warnings-as-errors Sphinx build,
Windows native ``libpq.dll`` connection
validation with Python worker, and now
contains a separate ``windows-c-worker-smoke``
job. Unlike the earlier Python-worker
connection smoke, the new job uses
Windows ``psycopg_c`` wheel provisioning
already exercised by the upstream Tests
C matrix, asserts that the actual
``qt-thread`` worker modules use
``psycopg_c.pq`` without fallback,
and runs the full existing semantic smoke
against local Windows PostgreSQL.

**Validation PASS (C worker only):**
GitHub run ``41809431708``, job
``126504663426``, with evidence
artifact ``windows-qt-thread-c-worker-smoke``
(ID ``13310819123``). The artifact's
``qt-thread-c-worker-evidence.log``
contains the native ``.pyd`` identity
and all smoke stages ending ``OK``.
Downloaded ZIP SHA256:
``679eb5ba320eff8bb25bb6f3e54911118b632cd066361cdbc725543a7ad4bea6``.
No production runtime, benchmark or
performance algorithm changed.

The full Tests matrix was accepted post-merge (run ``41801317209``,
attempt 2, 56/56 after one isolated job retry). Repository lint/Sphinx
baseline exceptions remain documented rather than treated as passing,
and the separately compiled Windows
``worker_impl=c`` smoke gap is now
closed by the **narrow PASS** above.
The Qt workflow's separate documentation
failure remains an accepted baseline
exception; this C-worker result does
not make the entire workflow green.
