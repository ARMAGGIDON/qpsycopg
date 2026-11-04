#!/usr/bin/env python3
"""Compare Python and native client hot paths over the same raw Windows named pipe."""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from typing import Any, Callable

from psycopg.pq._qt_process_pipe import PipeListenerSocket, PipeStream, connectPipe
from psycopg.pq._qt_process_protocol import _recvExact
from psycopg_c._psycopg import qpsycopg_pipe_roundtrip


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def echoServer(listener: PipeListenerSocket, size: int, count: int, errors: list[str]) -> None:
    try:
        stream, _ = listener.accept()
        try:
            for _ in range(count):
                payload = _recvExact(stream, size)
                stream.sendall(payload)
        finally:
            stream.close()
    except BaseException as error:
        errors.append(repr(error))
    finally:
        listener.close()


def runSession(mode: str, size: int, warmup: int, iterations: int) -> dict[str, Any]:
    listener = PipeListenerSocket()
    errors: list[str] = []
    server = threading.Thread(target=echoServer, args=(listener, size, warmup + iterations, errors), daemon=True)
    server.start()
    stream = connectPipe(listener.address)
    request = bytes((index * 131 + 17) & 0xFF for index in range(size))
    responseBuffer = bytearray(size)
    response: bytes | None = None

    if mode == "python":
        def roundtrip() -> None:
            nonlocal response
            stream.sendall(request)
            response = bytes(_recvExact(stream, size))
    elif mode == "native":
        fd = stream.file.fileno()

        def roundtrip() -> None:
            qpsycopg_pipe_roundtrip(fd, request, responseBuffer)
    else:
        raise ValueError(mode)

    try:
        for _ in range(warmup):
            roundtrip()

        wallSamples: list[float] = []
        cpuStart = time.thread_time_ns()
        for _ in range(iterations):
            started = time.perf_counter_ns()
            roundtrip()
            wallSamples.append((time.perf_counter_ns() - started) / 1_000.0)
        cpuUsPerCall = (time.thread_time_ns() - cpuStart) / 1_000.0 / iterations
    finally:
        stream.close()
        server.join(5.0)

    if server.is_alive():
        raise RuntimeError("echo server did not terminate")
    if errors:
        raise RuntimeError(errors[0])
    if mode == "python":
        assert response == request
    else:
        assert responseBuffer == request

    return {
        "wall_median_us": statistics.median(wallSamples),
        "wall_p95_us": percentile(wallSamples, 0.95),
        "wall_mean_us": statistics.fmean(wallSamples),
        "cpu_us_per_call": cpuUsPerCall,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=1_000)
    parser.add_argument("--sizes", type=int, nargs="+", default=(128, 8192))
    options = parser.parse_args()

    result: dict[str, Any] = {
        "iterations": options.iterations,
        "warmup": options.warmup,
        "sizes": {},
    }
    for size in options.sizes:
        if size <= 0:
            raise ValueError("sizes must be positive")
        pythonResult = runSession("python", size, options.warmup, options.iterations)
        nativeResult = runSession("native", size, options.warmup, options.iterations)
        result["sizes"][str(size)] = {
            "python": pythonResult,
            "native": nativeResult,
            "native_over_python": {
                key: nativeResult[key] / pythonResult[key]
                for key in ("wall_median_us", "wall_p95_us", "wall_mean_us", "cpu_us_per_call")
            },
        }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
