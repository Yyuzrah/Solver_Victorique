"""Read files that other processes may replace or delete while they are read.

On Windows an ordinary ``open()`` does not grant FILE_SHARE_DELETE. While such
a handle is open, no other process can delete the file or rename a new
version over it, so a run or scheduler that replaces its records can fail.
These helpers open with FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
read the whole file in one call and close the handle at once. On other systems
they use the ordinary ``open()``, which does not block renames there.

A classic ``os.replace`` (MoveFileExW) on Windows still fails while any
process holds the target open, whatever its sharing mode; writers that replace
files should retry for a moment. Renames with POSIX semantics
(FileRenameInfoEx) are not blocked by these readers.
"""
import json
import os
import time

__all__ = ["open_shared", "read_bytes", "read_tail", "read_text", "read_json", "read_jsonl"]

# ERROR_ACCESS_DENIED (also returned for a file whose deletion is pending),
# ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION clear up within moments.
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.restype = wintypes.HANDLE
    _create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _close_handle = _kernel32.CloseHandle
    _close_handle.restype = wintypes.BOOL
    _close_handle.argtypes = (wintypes.HANDLE,)

    def _native(path):
        text = os.path.abspath(os.fspath(path))
        if len(text) < 240 or text.startswith("\\\\?\\"):
            return text
        return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text

    def open_shared(path):
        """Open ``path`` for binary reading without denying delete or rename sharing."""
        handle = _create_file(_native(path), _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING,
                              _FILE_ATTRIBUTE_NORMAL, None)
        if handle is None or handle == _INVALID_HANDLE:
            code = ctypes.get_last_error()
            # OSError maps the Windows code to FileNotFoundError, PermissionError, ...
            raise OSError(None, ctypes.FormatError(code).strip(), os.fspath(path), code)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _close_handle(handle)
            raise
        try:
            return open(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
else:
    def open_shared(path):
        """Open ``path`` for binary reading."""
        return open(path, "rb")


def _read(path, attempts, delay, read):
    for attempt in range(attempts):
        try:
            with open_shared(path) as stream:
                return read(stream)
        except PermissionError as error:
            if getattr(error, "winerror", None) not in _TRANSIENT_WINERRORS or attempt + 1 >= attempts:
                raise
            time.sleep(delay)


def read_bytes(path, *, limit=None, attempts=20, delay=0.01):
    """Return the file's bytes, retrying briefly while Windows reports a passing sharing state."""
    data = _read(path, attempts, delay, lambda stream: stream.read() if limit is None else stream.read(limit + 1))
    if limit is not None and len(data) > limit:
        raise ValueError(f"{os.fspath(path)} is larger than {limit} bytes")
    return data


def read_tail(path, size, *, attempts=20, delay=0.01):
    """Return at most the last ``size`` bytes of the file."""
    def tail(stream):
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - size))
        return stream.read(size)
    return _read(path, attempts, delay, tail)


def read_text(path, *, limit=None, errors="strict"):
    """Return the file decoded as UTF-8."""
    return read_bytes(path, limit=limit).decode("utf-8", errors)


def read_json(path, *, limit=None, attempts=5, delay=0.05):
    """Parse one JSON document; reread while a writer may still be rewriting the file."""
    for attempt in range(attempts):
        data = read_bytes(path, limit=limit)
        try:
            return json.loads(data.decode("utf-8").removeprefix("﻿"))
        except ValueError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(delay)


def read_jsonl(path, *, limit=None, attempts=3, delay=0.05):
    """Return the JSON objects of a JSON Lines file, skipping lines that do not parse.

    A file that does not end with a newline is reread a few times, since a
    writer may be part way through rewriting it.
    """
    for attempt in range(attempts):
        data = read_bytes(path, limit=limit)
        if not data or data.endswith(b"\n") or attempt + 1 >= attempts:
            break
        time.sleep(delay)
    rows = []
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
