"""Native Windows named-pipe helpers used to measure qpsycopg IPC overhead."""

from cpython.bytearray cimport PyByteArray_AS_STRING, PyByteArray_GET_SIZE
from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_GET_SIZE


cdef extern from *:
    """
#ifdef MS_WINDOWS
#include <windows.h>
#include <io.h>

static int qpsycopg_pipe_write_all(int fd, const char *buffer, Py_ssize_t size)
{
    HANDLE handle = (HANDLE)_get_osfhandle(fd);
    if (handle == INVALID_HANDLE_VALUE)
        return -(int)GetLastError();

    while (size > 0) {
        DWORD chunk = size > 0x7fffffff ? 0x7fffffff : (DWORD)size;
        DWORD written = 0;
        if (!WriteFile(handle, buffer, chunk, &written, NULL))
            return -(int)GetLastError();
        if (!written)
            return -(int)ERROR_BROKEN_PIPE;
        buffer += written;
        size -= written;
    }
    return 0;
}

static int qpsycopg_pipe_read_exact(int fd, char *buffer, Py_ssize_t size)
{
    HANDLE handle = (HANDLE)_get_osfhandle(fd);
    if (handle == INVALID_HANDLE_VALUE)
        return -(int)GetLastError();

    while (size > 0) {
        DWORD chunk = size > 0x7fffffff ? 0x7fffffff : (DWORD)size;
        DWORD received = 0;
        if (!ReadFile(handle, buffer, chunk, &received, NULL))
            return -(int)GetLastError();
        if (!received)
            return -(int)ERROR_BROKEN_PIPE;
        buffer += received;
        size -= received;
    }
    return 0;
}
#else
static int qpsycopg_pipe_write_all(int fd, const char *buffer, Py_ssize_t size)
{
    return -1;
}

static int qpsycopg_pipe_read_exact(int fd, char *buffer, Py_ssize_t size)
{
    return -1;
}
#endif
    """
    int qpsycopg_pipe_write_all(int fd, const char *buffer, Py_ssize_t size) noexcept nogil
    int qpsycopg_pipe_read_exact(int fd, char *buffer, Py_ssize_t size) noexcept nogil


def qpsycopg_pipe_roundtrip(int fd, bytes request, bytearray response):
    """Write one request and read an equally sized response with the GIL released."""
    cdef Py_ssize_t size = PyBytes_GET_SIZE(request)
    cdef int status
    cdef const char *request_ptr
    cdef char *response_ptr

    if PyByteArray_GET_SIZE(response) != size:
        raise ValueError("request and response buffers must have the same size")
    if size == 0:
        return None

    request_ptr = PyBytes_AS_STRING(request)
    response_ptr = PyByteArray_AS_STRING(response)
    with nogil:
        status = qpsycopg_pipe_write_all(fd, request_ptr, size)
        if status == 0:
            status = qpsycopg_pipe_read_exact(fd, response_ptr, size)

    if status < 0:
        raise OSError(-status, "native qpsycopg named-pipe roundtrip failed")
    return None
