"""Regression checks for the packed-cell and physical-wire contracts."""

from __future__ import annotations

import pickle
import socket
import struct
from datetime import date

import pytest

from psycopg.pq._qt_process_protocol import (
    FastExecuteFrame,
    Operation,
    PackedValues,
    Request,
    Response,
    ResultSnapshot,
    SendSnapshot,
    _FAST_REQUEST_FLAG,
    _FAST_RESPONSE_FLAG,
    _serialize,
    _wireValue,
    recvFrame,
    sendFrame,
)

_CASES = (
    (),
    (b"\x00\xff",),
    (None, b"", b"\x00\xff", b"text"),
    (b"a", None, b"", b"\x00\xff", None, b"z"),
    (None, b"a", b"", b"\x00", None, b"\xff", b"b", b"", None),
)


def _pack(cells):
    data = bytearray()
    offsets = [0]
    nulls = bytearray((len(cells) + 7) // 8)
    for index, value in enumerate(cells):
        if value is None:
            nulls[index >> 3] |= 1 << (index & 7)
        else:
            data.extend(value)
        offsets.append(len(data))
    return PackedValues(
        bytes(data),
        struct.pack(f"={len(offsets)}Q", *offsets),
        bytes(nulls),
        len(cells),
    )


def _snapshot(values):
    return ResultSnapshot(
        status=2,
        errorMessage=b"",
        errorFields=(),
        columns=((b"value", 0, 0, 1, 17, -1, -1),),
        rowCount=len(values),
        columnCount=1,
        binaryTuples=1,
        values=values,
        paramTypes=(),
        commandStatus=b"SELECT",
        commandTuples=len(values),
        oidValue=0,
    )


@pytest.mark.parametrize("cells", _CASES, ids=("zero", "one", "four", "six", "nine"))
def test_packed_values_logical_access(cells):
    values = _pack(cells)
    assert len(values) == len(cells)
    assert bool(values) == bool(cells)
    assert tuple(values[i] for i in range(len(cells))) == cells
    assert values[:] == cells
    assert values[::2] == cells[::2]
    assert values[::-1] == cells[::-1]
    assert list(reversed(values)) == list(reversed(cells))
    if cells:
        assert values[-1] == cells[-1]
        assert values[-len(cells)] == cells[0]
    with pytest.raises(IndexError):
        _ = values[len(cells)]
    with pytest.raises(IndexError):
        _ = values[-len(cells) - 1]


@pytest.mark.parametrize("cells", _CASES, ids=("zero", "one", "four", "six", "nine"))
def test_packed_values_physical_fields(cells):
    values = _pack(cells)
    physical = (values.data, values.offsets, values.nulls, values.count)
    assert tuple(values) == physical
    assert list(values) == list(physical)
    assert len(tuple(values)) == 4  # Even when there are no logical cells.
    restored = pickle.loads(pickle.dumps(values))
    assert isinstance(restored, PackedValues)
    assert tuple(restored) == physical
    assert restored[:] == cells


@pytest.mark.parametrize("cells", _CASES, ids=("zero", "one", "four", "six", "nine"))
def test_packed_fast_response_round_trip(cells):
    values = _pack(cells)
    response = Response(
        0, SendSnapshot(("done", (_snapshot(values),), ()), 0, 0, 1, 0, b"UTF8")
    )
    wire, flags = _wireValue(response)
    assert flags == _FAST_RESPONSE_FLAG
    assert wire[0][5] == tuple(values)  # Four fields, not the logical cells.
    assert _serialize(wire, flags).startswith(b"M")

    sender, receiver = socket.socketpair()
    with sender, receiver:
        sendFrame(sender, response)
        decoded = recvFrame(receiver)

    assert isinstance(decoded, SendSnapshot)
    assert decoded.status == 1
    assert decoded.clientEncoding == b"UTF8"
    state, results, notices = decoded.value
    assert state == "done" and notices == () and len(results) == 1
    result = results[0]
    assert isinstance(result.values, PackedValues)
    assert result.rowCount == len(cells)
    assert tuple(result.values[index] for index in range(len(cells))) == cells
    assert result.values[:] == cells


@pytest.mark.parametrize(
    "argument, marker",
    [(b"select 1", b"M"), (date(2026, 11, 4), b"P")],
    ids=("marshal", "pickle-fallback"),
)
def test_fast_request_serialization(argument, marker):
    # A picklable date forces the non-marshal fast-frame fallback.
    frame = FastExecuteFrame(1.0, "send_query", (argument,), ())
    wire, flags = _wireValue(frame)
    assert flags == _FAST_REQUEST_FLAG
    assert _serialize(wire, flags).startswith(marker)

    sender, receiver = socket.socketpair()
    with sender, receiver:
        sendFrame(sender, frame)
        decoded = recvFrame(receiver)

    assert isinstance(decoded, Request)
    assert decoded.operation == Operation.CALL
    assert decoded.member == "qpsycopg_execute_pending"
    assert decoded.args == (1.0, "send_query", (argument,), ())
