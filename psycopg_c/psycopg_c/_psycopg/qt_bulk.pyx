"""Bulk decoding helpers for qt-process result snapshots."""


def qpsycopg_load_rows_sequence(
    Transformer tx,
    object values,
    int row0,
    int row1,
    object make_row,
):
    cdef values_fast = PySequence_Fast(values, "'values' is not a valid sequence")
    cdef Py_ssize_t nvalues = PySequence_Fast_GET_SIZE(values_fast)
    cdef list row_loaders = tx._row_loaders
    cdef Py_ssize_t nfields = PyList_GET_SIZE(row_loaders)
    cdef Py_ssize_t required = row1 * nfields
    cdef Py_ssize_t row
    cdef Py_ssize_t col
    cdef Py_ssize_t index
    cdef PyObject *item
    cdef PyObject *loader
    cdef char *ptr
    cdef Py_ssize_t size
    cdef object record
    cdef object pyval
    cdef object records

    if row0 < 0 or row1 < row0:
        raise e.InterfaceError(f"invalid row range: {row0}..{row1}")
    if required > nvalues:
        raise e.InterfaceError(
            f"row range requires {required} values, got {nvalues}"
        )

    records = PyList_New(row1 - row0)
    for row in range(row0, row1):
        record = PyTuple_New(nfields)
        index = row * nfields
        for col in range(nfields):
            item = PySequence_Fast_GET_ITEM(values_fast, index + col)
            if item == <PyObject *>None:
                pyval = None
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
            record = PyObject_CallFunctionObjArgs(
                make_row, <PyObject *>record, NULL
            )

        Py_INCREF(record)
        PyList_SET_ITEM(records, row - row0, record)

    return records
