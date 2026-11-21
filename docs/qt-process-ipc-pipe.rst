:orphan:

QP09 Windows overlapped named-pipe transport
=============================================

Scope and selection
-------------------

With PSYCOPG_IMPL=qt-process, Windows defaults to QPSYCOPG_IPC=pipe. Both
unmanaged and managed ConnectAttempt sessions create actual Windows named
pipes. QPSYCOPG_IPC=tcp explicitly retains loopback TCP. Pipe on a non-Windows
host raises an unsupported error; Pipe failures never trigger TCP fallback.
The physical data transport reports connection.pgconn._transport.ipcKind.

The independent PGcancelConn control channel also uses named Pipe when
requested, with a separately authenticated 256-bit token and server thread.
The existing frame format is unchanged. The synchronous CRT/C native pipe
framing fast paths cannot safely operate on overlapped HANDLEs and are
deliberately disabled on PipeStream. The Python framing code calls overlapped
recv_into() and sendall(), retaining independent result packing optimizations.

Overlapped lifecycle, deadline and errors
------------------------------------------

CreateNamedPipeW uses FILE_FLAG_OVERLAPPED and FILE_FLAG_FIRST_PIPE_INSTANCE.
Each ConnectNamedPipe, ReadFile and WriteFile creates a manual-reset event,
OVERLAPPED and pinned buffers. Completion is observed through
WaitForSingleObject and GetOverlappedResult. settimeout() establishes a
monotonic deadline; sendall shares one deadline across chunks. Cancellation
invokes CancelIoEx on exact operations, and closing a stream never frees
pending OVERLAPPED or HANDLE resources.

For delayed kernel cancellations a daemon reaper retains all I/O storage and
the HANDLE until completion; the calling thread returns after a bounded drain
window. Absolute OS-level drain deadlines cannot be promised, and lingering
reaper/handle growth must be regarded as a failure of resource verification.
QProcess OS exit remains independent: only the exact OwnerToken ExitProof
permits OS_EXIT_CONFIRMED; thread exit, close or KillReceipt alone do not.

Win32 outcomes:

* ERROR_IO_PENDING: wait for operation completion.
* ERROR_PIPE_CONNECTED: connection established before overlapped accept,
  therefore accept succeeds without waiting.
* ERROR_OPERATION_ABORTED: cancelled I/O; on deadline expiry surface
  socket.timeout.
* ERROR_BROKEN_PIPE and ERROR_PIPE_NOT_CONNECTED: EOF on read, broken-pipe
  error on write.
* ERROR_FILE_NOT_FOUND and ERROR_PIPE_BUSY: bounded client connection retry.
* Other Win32 failures: propagate errors; never connect over TCP instead.

Security and threat model
-------------------------

Server endpoints use random names and explicit SECURITY_ATTRIBUTES with a
protected DACL permitting the current user's SID and Local SYSTEM, but not
Everyone. Handles are non-inheritable. PIPE_REJECT_REMOTE_CLIENTS prevents
remote clients, while first-instance semantics reduce name preemption.
GetNamedPipeClientProcessId verifies the data client against the exact
QProcess PID; the worker's control listener verifies the parent PID.
Token authentication remains mandatory and the receiving side uses
secrets.compare_digest for constant-time token checks.

Named Pipes do not defend against an adversary who already has privileges
to inject into the same user's processes, duplicate handles, read process
memory, or act as Local SYSTEM. They do not add application-level encryption.

Evidence and benchmarking
-------------------------

The qt-process-pipe.yml GitHub workflow provisions Windows PostgreSQL 18,
tests real overlapped operations, two independent QProcesses and external
psutil OS-exit observation. Windows Server 2025 CI is not a substitute for
the requested local Windows 11 acceptance test.

Benchmark script: tools/bench_qp09_pipe.py. Run separately with explicit
QPSYCOPG_IPC=tcp and pipe, same host and identical PostgreSQL/libpq/Python
installations. For a previous synchronous Named Pipe baseline use QP08 commit
b7d4c38ab352fbbdef0a4ff08d2c44a72ed8e38e in an isolated checkout, with
an unmanaged Pipe connection (managed QP08 forced TCP). Alternate benchmark
order; compare repeated medians and dispersion after warm-up. Collect
connection startup, IPC status read, SELECT 1, bulk 100k rows, parent-plus-
worker CPU and RSS/private bytes. No speed improvement may be asserted without
real measurements.
