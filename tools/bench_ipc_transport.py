from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import socket
import statistics
import tempfile
from pathlib import Path
from time import perf_counter_ns
from typing import Callable


def recvExact(sock: socket.socket, size: int) -> bytes:
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    while offset < size:
        received = sock.recv_into(view[offset:])
        if not received:
            raise EOFError("IPC peer closed")
        offset += received
    return bytes(data)


def enableFastPath(sock: socket.socket) -> None:
    if not hasattr(socket, "SIO_LOOPBACK_FAST_PATH"):
        raise RuntimeError("SIO_LOOPBACK_FAST_PATH unavailable")
    sock.ioctl(socket.SIO_LOOPBACK_FAST_PATH, 1)


def socketChild(family: int, address: object, size: int, fast: bool) -> None:
    sock = socket.socket(family, socket.SOCK_STREAM)
    if fast:
        enableFastPath(sock)
    if family == socket.AF_INET:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect(address)
    try:
        while True:
            payload = recvExact(sock, size)
            if payload == b"\0" * size:
                return
            sock.sendall(payload)
    finally:
        sock.close()


def pipeChild(conn: object, size: int) -> None:
    try:
        while True:
            payload = conn.recv_bytes()
            if payload == b"\0" * size:
                return
            conn.send_bytes(payload)
    finally:
        conn.close()


def stats(samples: list[int]) -> tuple[float, float]:
    values = sorted(value / 1_000 for value in samples)
    median = statistics.median(values)
    p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
    return median, p95


def measureSocket(
    ctx: mp.context.BaseContext,
    family: int,
    address: object,
    size: int,
    iterations: int,
    warmup: int,
    fast: bool = False,
) -> tuple[float, float]:
    listener = socket.socket(family, socket.SOCK_STREAM)
    if fast:
        enableFastPath(listener)
    if family == socket.AF_INET:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(address)
        address = listener.getsockname()
    else:
        listener.bind(address)
    listener.listen(1)

    process = ctx.Process(target=socketChild, args=(family, address, size, fast))
    process.start()
    conn, _ = listener.accept()
    listener.close()
    if family == socket.AF_INET:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    payload = b"x" * size
    samples: list[int] = []
    try:
        for index in range(warmup + iterations):
            start = perf_counter_ns()
            conn.sendall(payload)
            recvExact(conn, size)
            elapsed = perf_counter_ns() - start
            if index >= warmup:
                samples.append(elapsed)
        conn.sendall(b"\0" * size)
    finally:
        conn.close()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
    return stats(samples)


def measurePipe(
    ctx: mp.context.BaseContext,
    size: int,
    iterations: int,
    warmup: int,
) -> tuple[float, float]:
    parent, child = ctx.Pipe(duplex=True)
    process = ctx.Process(target=pipeChild, args=(child, size))
    process.start()
    child.close()

    payload = b"x" * size
    samples: list[int] = []
    try:
        for index in range(warmup + iterations):
            start = perf_counter_ns()
            parent.send_bytes(payload)
            parent.recv_bytes()
            elapsed = perf_counter_ns() - start
            if index >= warmup:
                samples.append(elapsed)
        parent.send_bytes(b"\0" * size)
    finally:
        parent.close()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
    return stats(samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=5_000)
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument("--sizes", type=int, nargs="+", default=[64, 512])
    args = parser.parse_args()

    ctx = mp.get_context("spawn")
    print(f"platform={os.name} iterations={args.iterations} warmup={args.warmup}")
    print(f"{'transport':<16} {'bytes':>7} {'median us':>12} {'p95 us':>12}")

    for size in args.sizes:
        tests: list[tuple[str, Callable[[], tuple[float, float]]]] = [
            ("tcp", lambda s=size: measureSocket(ctx, socket.AF_INET, ("127.0.0.1", 0), s, args.iterations, args.warmup)),
        ]
        if hasattr(socket, "SIO_LOOPBACK_FAST_PATH"):
            tests.append((
                "tcp-fast",
                lambda s=size: measureSocket(ctx, socket.AF_INET, ("127.0.0.1", 0), s, args.iterations, args.warmup, True),
            ))
        if hasattr(socket, "AF_UNIX"):
            path = Path(tempfile.gettempdir()) / f"qpsycopg-ipc-{os.getpid()}-{size}.sock"
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            tests.append((
                "af-unix",
                lambda s=size, p=str(path): measureSocket(ctx, socket.AF_UNIX, p, s, args.iterations, args.warmup),
            ))
        tests.append(("pipe", lambda s=size: measurePipe(ctx, s, args.iterations, args.warmup)))

        for name, test in tests:
            try:
                median, p95 = test()
                print(f"{name:<16} {size:>7} {median:>12.2f} {p95:>12.2f}")
            except Exception as error:
                print(f"{name:<16} {size:>7} {'n/a':>12} {'n/a':>12}  {error}")
            finally:
                if name == "af-unix":
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
