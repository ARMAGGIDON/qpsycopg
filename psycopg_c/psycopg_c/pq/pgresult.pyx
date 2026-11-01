"""
psycopg_c.pq.PGresult object implementation.
"""

# Copyright (C) 2020 The Psycopg Team

cimport cython
from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_FromStringAndSize
from cpython.mem cimport PyMem_Free, PyMem_Malloc
from libc.stdint cimport uint64_t
from libc.string cimport memcpy, memset

from psycopg.pq.misc import PGresAttDesc
from psycopg.pq._enums import ExecStatus


@cython.freelist(8)
cdef class PGresult:
    def __cinit__(self):
        self._pgresult_ptr = NULL

    @staticmethod
    cdef PGresult _from_ptr(libpq.PGresult *ptr):
        cdef PGresult rv = PGresult.__new__(PGresult)
        rv._pgresult_ptr = ptr
        return rv

    def __dealloc__(self) -> None:
        self.clear()

    def __repr__(self) -> str:
        cls = f"{self.__class__.__module__}.{self.__class__.__qualname__}"
        status = ExecStatus(self.status)
        return f"<{cls} [{status.name}] at 0x{id(self):x}>"

    def clear(self) -> None:
        if self._pgresult_ptr is not NULL:
            libpq.PQclear(self._pgresult_ptr)
            self._pgresult_ptr = NULL

    @property
    def pgresult_ptr(self) -> int | None:
        if self._pgresult_ptr:
            return <long long><void *>self._pgresult_ptr
        else:
            return None

    @property
    def status(self) -> int:
        return libpq.PQresultStatus(self._pgresult_ptr)

    @property
    def error_message(self) -> bytes:
        return libpq.PQresultErrorMessage(self._pgresult_ptr)

    def get_error_message(self, encoding: str = "utf-8") -> str:
        return _clean_error_message(self.error_message, encoding)

    def error_field(self, int fieldcode) -> bytes | None:
        cdef char * rv = libpq.PQresultErrorField(self._pgresult_ptr, fieldcode)
        if rv is not NULL:
            return rv
        else:
            return None

    @property
    def ntuples(self) -> int:
        return libpq.PQntuples(self._pgresult_ptr)

    @property
    def nfields(self) -> int:
        return libpq.PQnfields(self._pgresult_ptr)

    def fname(self, int column_number) -> bytes | None:
        cdef char *rv = libpq.PQfname(self._pgresult_ptr, column_number)
        if rv is not NULL:
            return rv
        else:
            return None

    def ftable(self, int column_number) -> int:
        return libpq.PQftable(self._pgresult_ptr, column_number)

    def ftablecol(self, int column_number) -> int:
        return libpq.PQftablecol(self._pgresult_ptr, column_number)

    def fformat(self, int column_number) -> int:
        return libpq.PQfformat(self._pgresult_ptr, column_number)

    def ftype(self, int column_number) -> int:
        return libpq.PQftype(self._pgresult_ptr, column_number)

    def fmod(self, int column_number) -> int:
        return libpq.PQfmod(self._pgresult_ptr, column_number)

    def fsize(self, int column_number) -> int:
        return libpq.PQfsize(self._pgresult_ptr, column_number)

    @property
    def binary_tuples(self) -> int:
        return libpq.PQbinaryTuples(self._pgresult_ptr)

    def get_value(self, int row_number, int column_number) -> bytes | None:
        cdef int crow = row_number
        cdef int ccol = column_number
        cdef int length = libpq.PQgetlength(self._pgresult_ptr, crow, ccol)
        cdef char *v
        if length:
            v = libpq.PQgetvalue(self._pgresult_ptr, crow, ccol)
            # TODO: avoid copy
            return v[:length]
        else:
            if libpq.PQgetisnull(self._pgresult_ptr, crow, ccol):
                return None
            else:
                return b""

    def _qpsycopg_pack_values(self) -> tuple[bytes, bytes, bytes, int]:
        """Pack result cells into qpsycopg's data/offset/NULL wire layout."""
        cdef int ntuples = libpq.PQntuples(self._pgresult_ptr)
        cdef int nfields = libpq.PQnfields(self._pgresult_ptr)
        cdef uint64_t cellCount = <uint64_t>ntuples * <uint64_t>nfields
        cdef uint64_t maxPySize = (<uint64_t>1 << (sizeof(Py_ssize_t) * 8 - 1)) - 1
        cdef uint64_t offsetsSize
        cdef uint64_t nullsSize
        cdef uint64_t dataSize = 0
        cdef uint64_t cellIndex = 0
        cdef int row
        cdef int column
        cdef int length
        cdef char *value

        if cellCount > maxPySize // sizeof(uint64_t) - 1:
            raise OverflowError("PGresult has too many cells to pack")
        offsetsSize = (cellCount + 1) * sizeof(uint64_t)
        nullsSize = (cellCount + 7) // 8

        cdef bytes offsets = PyBytes_FromStringAndSize(NULL, <Py_ssize_t>offsetsSize)
        cdef bytes nulls = PyBytes_FromStringAndSize(NULL, <Py_ssize_t>nullsSize)
        cdef char *offsetsPtr = PyBytes_AS_STRING(offsets)
        cdef unsigned char *nullsPtr = <unsigned char *>PyBytes_AS_STRING(nulls)
        if nullsSize:
            memset(nullsPtr, 0, <size_t>nullsSize)

        for row in range(ntuples):
            for column in range(nfields):
                memcpy(
                    offsetsPtr + <Py_ssize_t>(cellIndex * sizeof(uint64_t)),
                    &dataSize,
                    sizeof(uint64_t),
                )
                length = libpq.PQgetlength(self._pgresult_ptr, row, column)
                if length:
                    dataSize += <uint64_t>length
                    if dataSize > maxPySize:
                        raise OverflowError("PGresult data is too large to pack")
                elif libpq.PQgetisnull(self._pgresult_ptr, row, column):
                    nullsPtr[cellIndex >> 3] |= <unsigned char>(1 << (cellIndex & 7))
                cellIndex += 1

        memcpy(
            offsetsPtr + <Py_ssize_t>(cellIndex * sizeof(uint64_t)),
            &dataSize,
            sizeof(uint64_t),
        )

        cdef bytes data = PyBytes_FromStringAndSize(NULL, <Py_ssize_t>dataSize)
        cdef char *dataPtr = PyBytes_AS_STRING(data)
        cdef Py_ssize_t dataOffset = 0
        for row in range(ntuples):
            for column in range(nfields):
                length = libpq.PQgetlength(self._pgresult_ptr, row, column)
                if length:
                    value = libpq.PQgetvalue(self._pgresult_ptr, row, column)
                    memcpy(dataPtr + dataOffset, value, <size_t>length)
                    dataOffset += length

        return data, offsets, nulls, cellCount

    @property
    def nparams(self) -> int:
        return libpq.PQnparams(self._pgresult_ptr)

    def param_type(self, int param_number) -> int:
        return libpq.PQparamtype(self._pgresult_ptr, param_number)

    @property
    def command_status(self) -> bytes | None:
        cdef char *rv = libpq.PQcmdStatus(self._pgresult_ptr)
        if rv is not NULL:
            return rv
        else:
            return None

    @property
    def command_tuples(self) -> int | None:
        cdef char *rv = libpq.PQcmdTuples(self._pgresult_ptr)
        if rv is NULL:
            return None
        cdef bytes brv = rv
        return int(brv) if brv else None

    @property
    def oid_value(self) -> int:
        return libpq.PQoidValue(self._pgresult_ptr)

    def set_attributes(self, descriptions: list[PGresAttDesc]):
        cdef Py_ssize_t num = len(descriptions)
        cdef libpq.PGresAttDesc *attrs = <libpq.PGresAttDesc *>PyMem_Malloc(
            num * sizeof(libpq.PGresAttDesc))

        for i in range(num):
            descr = descriptions[i]
            attrs[i].name = descr.name
            attrs[i].tableid = descr.tableid
            attrs[i].columnid = descr.columnid
            attrs[i].format = descr.format
            attrs[i].typid = descr.typid
            attrs[i].typlen = descr.typlen
            attrs[i].atttypmod = descr.atttypmod

        cdef int res = libpq.PQsetResultAttrs(self._pgresult_ptr, <int>num, attrs)
        PyMem_Free(attrs)
        if (res == 0):
            raise e.OperationalError("PQsetResultAttrs failed")