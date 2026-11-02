"""Native Windows named-pipe helpers for qpsycopg IPC."""

from cpython.bytearray cimport PyByteArray_AS_STRING, PyByteArray_GET_SIZE
from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_GET_SIZE
from libc.stdint cimport uint64_t
from libc.string cimport memcpy


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

static int qpsycopg_pipe_read_some(int fd, char *buffer, Py_ssize_t size, Py_ssize_t *received_out)
{
    HANDLE handle = (HANDLE)_get_osfhandle(fd);
    DWORD chunk;
    DWORD received = 0;

    if (handle == INVALID_HANDLE_VALUE)
        return -(int)GetLastError();
    if (size <= 0)
        return -(int)ERROR_INSUFFICIENT_BUFFER;

    chunk = size > 0x7fffffff ? 0x7fffffff : (DWORD)size;
    if (!ReadFile(handle, buffer, chunk, &received, NULL))
        return -(int)GetLastError();
    if (!received)
        return -(int)ERROR_BROKEN_PIPE;

    *received_out = (Py_ssize_t)received;
    return 0;
}
#else
static int qpsycopg_pipe_write_all(int fd, const char *buffer, Py_ssize_t size) { return -1; }
static int qpsycopg_pipe_read_exact(int fd, char *buffer, Py_ssize_t size) { return -1; }
static int qpsycopg_pipe_read_some(int fd, char *buffer, Py_ssize_t size, Py_ssize_t *received_out) { return -1; }
#endif
    """
    int qpsycopg_pipe_write_all(int fd, const char *buffer, Py_ssize_t size) noexcept nogil
    int qpsycopg_pipe_read_exact(int fd, char *buffer, Py_ssize_t size) noexcept nogil
    int qpsycopg_pipe_read_some(
        int fd, char *buffer, Py_ssize_t size, Py_ssize_t *received_out
    ) noexcept nogil


cdef uint64_t _FAST_REQUEST_FLAG = (<uint64_t>1) << 63
cdef uint64_t _FAST_RESPONSE_FLAG = (<uint64_t>1) << 62
cdef uint64_t _FRAME_FLAGS = _FAST_REQUEST_FLAG | _FAST_RESPONSE_FLAG
cdef uint64_t _FRAME_SIZE_MASK = _FAST_RESPONSE_FLAG - 1


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


def qpsycopg_pipe_frame_roundtrip(int fd, bytes request, bytearray scratch, Py_ssize_t max_frame):
    """Exchange one frame using a reusable read-ahead buffer with the GIL released."""
    cdef Py_ssize_t request_size = PyBytes_GET_SIZE(request)
    cdef const char *request_ptr = PyBytes_AS_STRING(request)
    cdef Py_ssize_t scratch_size = PyByteArray_GET_SIZE(scratch)
    cdef char *scratch_ptr = PyByteArray_AS_STRING(scratch)
    cdef unsigned char *header
    cdef Py_ssize_t received_total = 0
    cdef Py_ssize_t received = 0
    cdef Py_ssize_t available
    cdef Py_ssize_t copied
    cdef uint64_t frame
    cdef uint64_t size
    cdef uint64_t flags
    cdef bytearray payload
    cdef char *payload_ptr
    cdef int status

    if scratch_size < 8:
        raise ValueError("native qpsycopg read-ahead buffer is too small")

    with nogil:
        status = qpsycopg_pipe_write_all(fd, request_ptr, request_size)
        while status == 0 and received_total < 8:
            status = qpsycopg_pipe_read_some(
                fd, scratch_ptr + received_total, scratch_size - received_total, &received
            )
            if status == 0:
                received_total += received

    if status < 0:
        raise OSError(-status, "native qpsycopg named-pipe frame roundtrip failed")

    header = <unsigned char *>scratch_ptr
    frame = (
        (<uint64_t>header[0] << 56) |
        (<uint64_t>header[1] << 48) |
        (<uint64_t>header[2] << 40) |
        (<uint64_t>header[3] << 32) |
        (<uint64_t>header[4] << 24) |
        (<uint64_t>header[5] << 16) |
        (<uint64_t>header[6] << 8) |
        <uint64_t>header[7]
    )
    flags = frame & _FRAME_FLAGS
    if flags == _FRAME_FLAGS:
        raise ValueError("invalid qpsycopg frame flags")
    size = frame & _FRAME_SIZE_MASK
    if size > <uint64_t>max_frame:
        raise ValueError(f"invalid qpsycopg frame size: {size}")

    available = received_total - 8
    if available > <Py_ssize_t>size:
        raise ValueError("native qpsycopg frame read crossed a response boundary")

    payload = bytearray(<Py_ssize_t>size)
    if size:
        payload_ptr = PyByteArray_AS_STRING(payload)
        copied = available if available < <Py_ssize_t>size else <Py_ssize_t>size
        if copied:
            memcpy(payload_ptr, scratch_ptr + 8, copied)
        if copied < <Py_ssize_t>size:
            with nogil:
                status = qpsycopg_pipe_read_exact(
                    fd, payload_ptr + copied, <Py_ssize_t>size - copied
                )
            if status < 0:
                raise OSError(-status, "native qpsycopg named-pipe frame payload read failed")

    return frame, payload
