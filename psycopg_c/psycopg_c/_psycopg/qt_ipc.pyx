"""Native Windows named-pipe helpers for qpsycopg IPC."""

from cpython.bytearray cimport PyByteArray_AS_STRING, PyByteArray_GET_SIZE
from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_GET_SIZE
from libc.stdint cimport uint64_t
from libc.string cimport memcpy


cdef extern from *:
    """
#ifdef MS_WINDOWS
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <windows.h>
#include <io.h>
#include <stdint.h>

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

static int qpsycopg_socket_wait(Py_ssize_t socket_value, int mask, double timeout_seconds, int *ready_out)
{
    SOCKET sock = (SOCKET)(uintptr_t)socket_value;
    fd_set read_set;
    fd_set write_set;
    fd_set *read_ptr = NULL;
    fd_set *write_ptr = NULL;
    struct timeval timeout;
    struct timeval *timeout_ptr = NULL;
    int result;
    int ready = 0;

    if (mask & 1) {
        FD_ZERO(&read_set);
        FD_SET(sock, &read_set);
        read_ptr = &read_set;
    }
    if (mask & 2) {
        FD_ZERO(&write_set);
        FD_SET(sock, &write_set);
        write_ptr = &write_set;
    }
    if (timeout_seconds >= 0.0) {
        long seconds = (long)timeout_seconds;
        long microseconds = (long)((timeout_seconds - (double)seconds) * 1000000.0);
        if (microseconds < 0)
            microseconds = 0;
        if (microseconds >= 1000000) {
            seconds += microseconds / 1000000;
            microseconds %= 1000000;
        }
        timeout.tv_sec = seconds;
        timeout.tv_usec = microseconds;
        timeout_ptr = &timeout;
    }

    result = select(0, read_ptr, write_ptr, NULL, timeout_ptr);
    if (result == SOCKET_ERROR)
        return -WSAGetLastError();
    if (read_ptr && FD_ISSET(sock, &read_set))
        ready |= 1;
    if (write_ptr && FD_ISSET(sock, &write_set))
        ready |= 2;
    *ready_out = ready;
    return 0;
}
#else
static int qpsycopg_pipe_write_all(int fd, const char *buffer, Py_ssize_t size) { return -1; }
static int qpsycopg_pipe_read_exact(int fd, char *buffer, Py_ssize_t size) { return -1; }
static int qpsycopg_pipe_read_some(int fd, char *buffer, Py_ssize_t size, Py_ssize_t *received_out) { return -1; }
static int qpsycopg_socket_wait(Py_ssize_t socket_value, int mask, double timeout_seconds, int *ready_out) { return -1; }
#endif
    """
    int qpsycopg_pipe_write_all(int fd, const char *buffer, Py_ssize_t size) noexcept nogil
    int qpsycopg_pipe_read_exact(int fd, char *buffer, Py_ssize_t size) noexcept nogil
    int qpsycopg_pipe_read_some(
        int fd, char *buffer, Py_ssize_t size, Py_ssize_t *received_out
    ) noexcept nogil
    int qpsycopg_socket_wait(
        Py_ssize_t socket_value, int mask, double timeout_seconds, int *ready_out
    ) noexcept nogil


cdef uint64_t _FAST_REQUEST_FLAG = (<uint64_t>1) << 63
cdef uint64_t _FAST_RESPONSE_FLAG = (<uint64_t>1) << 62
cdef uint64_t _FRAME_FLAGS = _FAST_REQUEST_FLAG | _FAST_RESPONSE_FLAG
cdef uint64_t _FRAME_SIZE_MASK = _FAST_RESPONSE_FLAG - 1


cdef inline uint64_t _decode_frame(char *buffer):
    cdef unsigned char *header = <unsigned char *>buffer
    return (
        (<uint64_t>header[0] << 56) |
        (<uint64_t>header[1] << 48) |
        (<uint64_t>header[2] << 40) |
        (<uint64_t>header[3] << 32) |
        (<uint64_t>header[4] << 24) |
        (<uint64_t>header[5] << 16) |
        (<uint64_t>header[6] << 8) |
        <uint64_t>header[7]
    )


cdef inline tuple _validate_frame(uint64_t frame, Py_ssize_t max_frame):
    cdef uint64_t flags = frame & _FRAME_FLAGS
    cdef uint64_t size = frame & _FRAME_SIZE_MASK
    if flags == _FRAME_FLAGS:
        raise ValueError("invalid qpsycopg frame flags")
    if size > <uint64_t>max_frame:
        raise ValueError(f"invalid qpsycopg frame size: {size}")
    return size, flags


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
    cdef Py_ssize_t received_total = 0
    cdef Py_ssize_t received = 0
    cdef Py_ssize_t available
    cdef Py_ssize_t copied
    cdef uint64_t frame
    cdef uint64_t size
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

    frame = _decode_frame(scratch_ptr)
    size, _ = _validate_frame(frame, max_frame)
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
                status = qpsycopg_pipe_read_exact(fd, payload_ptr + copied, <Py_ssize_t>size - copied)
            if status < 0:
                raise OSError(-status, "native qpsycopg named-pipe frame payload read failed")

    return frame, payload


def qpsycopg_pipe_recv_frame(int fd, bytearray scratch, Py_ssize_t max_frame):
    """Read one frame with native read-ahead, preserving bytes from a following frame."""
    cdef Py_ssize_t scratch_size = PyByteArray_GET_SIZE(scratch)
    cdef char *scratch_ptr = PyByteArray_AS_STRING(scratch)
    cdef Py_ssize_t received_total = 0
    cdef Py_ssize_t received = 0
    cdef Py_ssize_t frame_end
    cdef Py_ssize_t available
    cdef Py_ssize_t copied
    cdef uint64_t frame
    cdef uint64_t size
    cdef bytearray payload
    cdef char *payload_ptr
    cdef int status = 0

    if scratch_size < 8:
        raise ValueError("native qpsycopg read-ahead buffer is too small")

    with nogil:
        while status == 0 and received_total < 8:
            status = qpsycopg_pipe_read_some(
                fd, scratch_ptr + received_total, scratch_size - received_total, &received
            )
            if status == 0:
                received_total += received

    if status < 0:
        raise OSError(-status, "native qpsycopg named-pipe frame read failed")

    frame = _decode_frame(scratch_ptr)
    size, _ = _validate_frame(frame, max_frame)
    frame_end = 8 + <Py_ssize_t>size
    available = received_total - 8

    payload = bytearray(<Py_ssize_t>size)
    if size:
        payload_ptr = PyByteArray_AS_STRING(payload)
        copied = available if available < <Py_ssize_t>size else <Py_ssize_t>size
        if copied:
            memcpy(payload_ptr, scratch_ptr + 8, copied)
        if copied < <Py_ssize_t>size:
            with nogil:
                status = qpsycopg_pipe_read_exact(fd, payload_ptr + copied, <Py_ssize_t>size - copied)
            if status < 0:
                raise OSError(-status, "native qpsycopg named-pipe frame payload read failed")
            received_total = frame_end

    if received_total > frame_end:
        return frame, payload, frame_end, received_total
    return frame, payload, 0, 0


def qpsycopg_wait_socket(Py_ssize_t socket_value, int mask, timeout):
    """Wait for one libpq socket using Winsock select with the GIL released."""
    cdef double timeout_seconds = -1.0
    cdef int ready = 0
    cdef int status

    if timeout is not None:
        timeout_seconds = float(timeout)
        if timeout_seconds < 0.0:
            raise ValueError("timeout must be non-negative or None")

    with nogil:
        status = qpsycopg_socket_wait(socket_value, mask, timeout_seconds, &ready)
    if status < 0:
        raise OSError(-status, "native qpsycopg socket wait failed")
    return ready
