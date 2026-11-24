"""Bulk decoding helpers for qt-process result snapshots."""

from libc.stdint cimport uint64_t
from libc.string cimport memcpy


def qpsycopg_load_rows_sequence(
    Transformer tx,
    object values,
    int row0,
    int row1,
    object make_row,
):
    if hasattr(values, "data") and hasattr(values, "offsets") and hasattr(values, "nulls"):
        return _load_packed_rows(tx, values, row0, row1, make_row)
    return _load_sequence_rows(tx, values, row0, row1, make_row)


def qpsycopg_scan_packed(object values, int row0, int row1, int nfields):
    """Scan packed cell metadata without decoding values."""
    cdef object data = values.data
    cdef object offsets = values.offsets
    cdef object nulls = values.nulls
    cdef Py_ssize_t first_cell = row0 * nfields
    cdef Py_ssize_t last_cell = row1 * nfields
    cdef Py_ssize_t required_offsets = (last_cell + 1) * sizeof(uint64_t)
    cdef Py_ssize_t required_nulls = (last_cell + 7) // 8
    cdef Py_ssize_t data_size
    cdef Py_ssize_t offsets_size
    cdef Py_ssize_t nulls_size
    cdef Py_ssize_t index
    cdef Py_ssize_t nonnull = 0
    cdef char *data_ptr
    cdef char *offsets_ptr
    cdef char *nulls_raw
    cdef unsigned char *nulls_ptr
    cdef uint64_t start_offset
    cdef uint64_t end_offset
    cdef uint64_t total_bytes = 0

    if row0 < 0 or row1 < row0 or nfields < 0:
        raise e.InterfaceError(f"invalid packed scan: rows {row0}..{row1}, fields {nfields}")

    _buffer_as_string_and_size(data, &data_ptr, &data_size)
    _buffer_as_string_and_size(offsets, &offsets_ptr, &offsets_size)
    _buffer_as_string_and_size(nulls, &nulls_raw, &nulls_size)
    nulls_ptr = <unsigned char *>nulls_raw

    if required_offsets > offsets_size:
        raise e.InterfaceError(
            f"scan requires {required_offsets} offset bytes, got {offsets_size}"
        )
    if required_nulls > nulls_size:
        raise e.InterfaceError(f"scan requires {required_nulls} null bytes, got {nulls_size}")

    for index in range(first_cell, last_cell):
        if nulls_ptr[index >> 3] & (1 << (index & 7)):
            continue
        memcpy(&start_offset, offsets_ptr + index * sizeof(uint64_t), sizeof(uint64_t))
        memcpy(&end_offset, offsets_ptr + (index + 1) * sizeof(uint64_t), sizeof(uint64_t))
        if end_offset < start_offset or end_offset > <uint64_t>data_size:
            raise e.InterfaceError("invalid packed result offsets")
        total_bytes += end_offset - start_offset
        nonnull += 1

    return total_bytes, nonnull


cdef list _qt_native_loaders(list row_loaders):
    """Create safe C equivalents for default parent-side loader instances."""
    cdef Py_ssize_t nfields = PyList_GET_SIZE(row_loaders)
    cdef list native_loaders = PyList_New(nfields)
    cdef PyObject *row_loader
    cdef object pyloader
    cdef object cls
    cdef object native
    cdef TimestamptzLoader timestamptz
    cdef Py_ssize_t col

    for col in range(nfields):
        row_loader = PyList_GET_ITEM(row_loaders, col)
        pyloader = (<RowLoader>row_loader).pyloader
        cls = type(pyloader)
        native = None

        if cls.__module__ == "psycopg.types.numeric":
            if cls.__name__ == "IntLoader":
                native = IntLoader(pyloader.oid, None)
            elif cls.__name__ == "NumericLoader":
                native = NumericLoader(pyloader.oid, None)
        elif cls.__module__ == "psycopg.types.datetime" and cls.__name__ == "TimestamptzLoader":
            if getattr(pyloader._load_method, "__name__", "") == "_load_iso":
                timestamptz = TimestamptzLoader(pyloader.oid, None)
                timestamptz._time_zone = pyloader._timezone
                timestamptz._order = ORDER_YMD
                native = timestamptz

        Py_INCREF(native)
        PyList_SET_ITEM(native_loaders, col, native)

    return native_loaders


cdef object _load_packed_rows(
    Transformer tx,
    object values,
    int row0,
    int row1,
    object make_row,
):
    cdef object data = values.data
    cdef object offsets = values.offsets
    cdef object nulls = values.nulls
    cdef object data_view = memoryview(data)
    cdef list row_loaders = tx._row_loaders
    cdef list native_loaders = _qt_native_loaders(row_loaders)
    cdef Py_ssize_t nfields = PyList_GET_SIZE(row_loaders)
    cdef Py_ssize_t required_cells = row1 * nfields
    cdef Py_ssize_t required_offsets = (required_cells + 1) * sizeof(uint64_t)
    cdef Py_ssize_t required_nulls = (required_cells + 7) // 8
    cdef Py_ssize_t data_size
    cdef Py_ssize_t offsets_size
    cdef Py_ssize_t nulls_size
    cdef Py_ssize_t row
    cdef Py_ssize_t col
    cdef Py_ssize_t index
    cdef char *data_ptr
    cdef char *offsets_ptr
    cdef char *nulls_raw
    cdef unsigned char *nulls_ptr
    cdef uint64_t start_offset
    cdef uint64_t end_offset
    cdef PyObject *loader
    cdef PyObject *native_loader
    cdef object item
    cdef object record
    cdef object pyval
    cdef object records

    if row0 < 0 or row1 < row0:
        raise e.InterfaceError(f"invalid row range: {row0}..{row1}")

    _buffer_as_string_and_size(data, &data_ptr, &data_size)
    _buffer_as_string_and_size(offsets, &offsets_ptr, &offsets_size)
    _buffer_as_string_and_size(nulls, &nulls_raw, &nulls_size)
    nulls_ptr = <unsigned char *>nulls_raw

    if required_offsets > offsets_size:
        raise e.InterfaceError(
            f"row range requires {required_offsets} offset bytes, got {offsets_size}"
        )
    if required_nulls > nulls_size:
        raise e.InterfaceError(
            f"row range requires {required_nulls} null bytes, got {nulls_size}"
        )

    records = PyList_New(row1 - row0)
    for row in range(row0, row1):
        record = PyTuple_New(nfields)
        index = row * nfields
        for col in range(nfields):
            if nulls_ptr[(index + col) >> 3] & (1 << ((index + col) & 7)):
                pyval = None
            else:
                memcpy(
                    &start_offset,
                    offsets_ptr + (index + col) * sizeof(uint64_t),
                    sizeof(uint64_t),
                )
                memcpy(
                    &end_offset,
                    offsets_ptr + (index + col + 1) * sizeof(uint64_t),
                    sizeof(uint64_t),
                )
                if end_offset < start_offset or end_offset > <uint64_t>data_size:
                    raise e.InterfaceError("invalid packed result offsets")

                native_loader = PyList_GET_ITEM(native_loaders, col)
                if native_loader != <PyObject *>None:
                    pyval = (<CLoader>native_loader).cload(
                        data_ptr + start_offset, end_offset - start_offset
                    )
                else:
                    loader = PyList_GET_ITEM(row_loaders, col)
                    if (<RowLoader>loader).cloader is not None:
                        pyval = (<RowLoader>loader).cloader.cload(
                            data_ptr + start_offset, end_offset - start_offset
                        )
                    else:
                        item = data_view[start_offset:end_offset]
                        pyval = PyObject_CallFunctionObjArgs(
                            (<RowLoader>loader).loadfunc, <PyObject *>item, NULL
                        )

            Py_INCREF(pyval)
            PyTuple_SET_ITEM(record, col, pyval)

        if make_row is not tuple:
            record = PyObject_CallFunctionObjArgs(make_row, <PyObject *>record, NULL)

        Py_INCREF(record)
        PyList_SET_ITEM(records, row - row0, record)

    return records


cdef object _load_sequence_rows(
    Transformer tx,
    object values,
    int row0,
    int row1,
    object make_row,
):
    cdef values_fast = PySequence_Fast(values, "'values' is not a valid sequence")
    cdef Py_ssize_t nvalues = PySequence_Fast_GET_SIZE(values_fast)
    cdef list row_loaders = tx._row_loaders
    cdef list native_loaders = _qt_native_loaders(row_loaders)
    cdef Py_ssize_t nfields = PyList_GET_SIZE(row_loaders)
    cdef Py_ssize_t required = row1 * nfields
    cdef Py_ssize_t row
    cdef Py_ssize_t col
    cdef Py_ssize_t index
    cdef PyObject *item
    cdef PyObject *loader
    cdef PyObject *native_loader
    cdef char *ptr
    cdef Py_ssize_t size
    cdef object record
    cdef object pyval
    cdef object records

    if row0 < 0 or row1 < row0:
        raise e.InterfaceError(f"invalid row range: {row0}..{row1}")
    if required > nvalues:
        raise e.InterfaceError(f"row range requires {required} values, got {nvalues}")

    records = PyList_New(row1 - row0)
    for row in range(row0, row1):
        record = PyTuple_New(nfields)
        index = row * nfields
        for col in range(nfields):
            item = PySequence_Fast_GET_ITEM(values_fast, index + col)
            if item == <PyObject *>None:
                pyval = None
            else:
                native_loader = PyList_GET_ITEM(native_loaders, col)
                if native_loader != <PyObject *>None:
                    _buffer_as_string_and_size(<object>item, &ptr, &size)
                    pyval = (<CLoader>native_loader).cload(ptr, size)
                else:
                    loader = PyList_GET_ITEM(row_loaders, col)
                    if (<RowLoader>loader).cloader is not None:
                        _buffer_as_string_and_size(<object>item, &ptr, &size)
                        pyval = (<RowLoader>loader).cloader.cload(ptr, size)
                    else:
                        pyval = PyObject_CallFunctionObjArgs(
                            (<RowLoader>loader).loadfunc, item, NULL
                        )

            Py_INCREF(pyval)
            PyTuple_SET_ITEM(record, col, pyval)

        if make_row is not tuple:
            record = PyObject_CallFunctionObjArgs(make_row, <PyObject *>record, NULL)

        Py_INCREF(record)
        PyList_SET_ITEM(records, row - row0, record)

    return records
