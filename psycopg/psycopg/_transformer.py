"""
Helper object to transform values between Python and PostgreSQL

This module exports the requested implementation to the rest of the package.
"""

# Copyright (C) 2023 The Psycopg Team

from __future__ import annotations

from typing import Any
from collections.abc import Sequence

from . import abc, pq
from ._cmodule import _psycopg

Transformer: type[abc.Transformer]

if _psycopg:
    Transformer = _psycopg.Transformer
elif pq.__impl__ == "qt-process":
    from . import _py_transformer

    try:
        import psycopg_c._psycopg as _qt_fast
    except ImportError:
        try:
            import psycopg_binary._psycopg as _qt_fast
        except ImportError:
            _qt_fast = None  # type: ignore[assignment]

    if _qt_fast is None:
        Transformer = _py_transformer.Transformer
    else:
        class Transformer(_py_transformer.Transformer):  # type: ignore[no-redef]
            """Python pq facade with native value decoding for remote snapshots."""

            __qpsycopg_decoder__ = _qt_fast.__name__

            def __init__(self, context: abc.AdaptContext | None = None):
                super().__init__(context)
                self._fast = _qt_fast.Transformer(context)
                self._bulkLoadRows = getattr(_qt_fast, "qpsycopg_load_rows_sequence", None)
                self._fastResult = False

            @classmethod
            def from_context(cls, context: abc.AdaptContext | None) -> Transformer:
                return context if isinstance(context, cls) else cls(context)

            def set_pgresult(
                self,
                result: pq.abc.PGresult | None,
                *,
                set_loaders: bool = True,
                format: pq.Format | None = None,
            ) -> None:
                super().set_pgresult(result, set_loaders=False, format=format)
                self._fastResult = bool(result and hasattr(result, "_values"))
                if not set_loaders:
                    return
                if not result or not result.nfields:
                    self._row_loaders = []
                    return

                fmt = pq.Format(result.fformat(0) if format is None else format)
                self._fast.set_loader_types(
                    tuple(result.ftype(i) for i in range(result.nfields)), fmt
                )

            def set_loader_types(self, types: Sequence[int], format: pq.Format) -> None:
                super().set_loader_types(types, format)
                self._fast.set_loader_types(types, format)

            def load_rows(self, row0: int, row1: int, make_row: Any) -> list[Any]:
                if not self._fastResult:
                    return super().load_rows(row0, row1, make_row)
                if not (res := self._pgresult):
                    raise RuntimeError("result not set")
                if not (0 <= row0 <= self._ntuples and 0 <= row1 <= self._ntuples):
                    return super().load_rows(row0, row1, make_row)

                values = res._values  # type: ignore[attr-defined]
                if self._bulkLoadRows is not None:
                    return self._bulkLoadRows(self._fast, values, row0, row1, make_row)

                nfields = self._nfields
                load = self._fast.load_sequence
                if make_row is tuple:
                    return [
                        load(values[row * nfields : (row + 1) * nfields])
                        for row in range(row0, row1)
                    ]
                return [
                    make_row(load(values[row * nfields : (row + 1) * nfields]))
                    for row in range(row0, row1)
                ]

            def load_row(self, row: int, make_row: Any) -> Any:
                if not self._fastResult:
                    return super().load_row(row, make_row)
                if not (res := self._pgresult) or not 0 <= row < self._ntuples:
                    return super().load_row(row, make_row)

                nfields = self._nfields
                record = self._fast.load_sequence(
                    res._values[row * nfields : (row + 1) * nfields]  # type: ignore[attr-defined]
                )
                return record if make_row is tuple else make_row(record)

            def load_sequence(self, record: Sequence[abc.Buffer | None]) -> tuple[Any, ...]:
                return self._fast.load_sequence(record)
else:
    from . import _py_transformer

    Transformer = _py_transformer.Transformer