"""Reading run files while another thread replaces them atomically."""
import json
import os
import threading
import time

import pytest

from victorique import shared_read


def _payload(number):
    return json.dumps({"version": number, "padding": "x" * 4096}).encode("utf-8")


def _replace_repeatedly(path, count, replace, retries, failures, gave_up, done):
    try:
        for number in range(1, count + 1):
            temporary = path.with_name(f"{path.name}.{number}.tmp")
            temporary.write_bytes(_payload(number))
            for attempt in range(retries + 1):
                try:
                    replace(temporary, path)
                    break
                except OSError as error:
                    failures.append((number, attempt, repr(error)))
                    time.sleep(0.001)
            else:
                gave_up.append(number)
                temporary.unlink()
    finally:
        done.set()


def _overlap(path, count, replace, retries, reader, max_errors=20):
    """Replace ``path`` ``count`` times in a thread while reading it in this one."""
    failures, gave_up, errors, seen, done = [], [], [], set(), threading.Event()
    writer = threading.Thread(target=_replace_repeatedly,
                              args=(path, count, replace, retries, failures, gave_up, done))
    writer.start()
    reads = 0
    while not done.is_set() or reads == 0:
        try:
            seen.add(reader(path)["version"])
            reads += 1
        except Exception as error:  # Every reader error is reported to the test.
            errors.append(repr(error))
            if max_errors is not None and len(errors) > max_errors:
                break
        time.sleep(0)
    writer.join(120)
    assert not writer.is_alive()
    return {"failures": failures, "gave_up": gave_up, "errors": errors, "reads": reads, "seen": seen}


def test_reader_never_fails_while_a_thread_replaces_the_file(tmp_path):
    # A classic os.replace (MoveFileExW) on Windows fails while any handle is open on the
    # target, whatever its sharing mode, so this writer retries for a moment, as the run
    # schedulers do. The reader must never fail and must only ever see whole documents.
    path = tmp_path / "summary.json"
    path.write_bytes(_payload(0))
    result = _overlap(path, 300, os.replace, 500, shared_read.read_json)
    assert result["errors"] == []
    assert result["gave_up"] == []
    assert result["reads"] > 0 and len(result["seen"]) > 1
    assert json.loads(path.read_bytes())["version"] == 300
    assert not list(tmp_path.glob("*.tmp"))


def test_read_helpers_handle_lines_tails_and_missing_files(tmp_path):
    path = tmp_path / "calls.jsonl"
    path.write_bytes(b'{"a": 1}\n\nnot json\n{"b": 2}\n')
    assert shared_read.read_jsonl(path) == [{"a": 1}, {"b": 2}]
    assert shared_read.read_tail(path, 9) == b'{"b": 2}\n'
    assert shared_read.read_text(path, limit=100).startswith('{"a": 1}')
    with pytest.raises(ValueError):
        shared_read.read_bytes(path, limit=4)
    with pytest.raises(FileNotFoundError):
        shared_read.read_json(tmp_path / "absent.json")
    with shared_read.open_shared(path) as stream:
        assert stream.read(8) == b'{"a": 1}'


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    class _RenameInfo(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * 1)]

    def _posix_replace(source, target):
        """Rename with FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS."""
        handle = _kernel32.CreateFileW(str(source), 0x00010000 | 0x00100000, 0x7, None, 3, 0x80, None)
        if handle is None or handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            name = os.path.abspath(target)
            size = ctypes.sizeof(_RenameInfo) + 2 * len(name)
            buffer = ctypes.create_string_buffer(size)
            info = _RenameInfo.from_buffer(buffer)
            info.Flags = 0x1 | 0x2
            info.FileNameLength = 2 * len(name)
            ctypes.memmove(ctypes.addressof(info) + _RenameInfo.FileName.offset,
                           ctypes.create_unicode_buffer(name), 2 * len(name))
            if not _kernel32.SetFileInformationByHandle(handle, 22, buffer, size):  # FileRenameInfoEx
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing modes")
def test_shared_reader_never_blocks_a_posix_semantics_replace(tmp_path):
    path = tmp_path / "summary.json"
    path.write_bytes(_payload(0))
    probe = tmp_path / "probe.tmp"
    probe.write_bytes(_payload(0))
    try:
        _posix_replace(probe, path)
    except OSError as error:
        pytest.skip("this volume has no POSIX rename semantics: " + str(error))

    # The shared reader keeps FILE_SHARE_DELETE, so not one replacement fails or retries.
    result = _overlap(path, 300, _posix_replace, 0, shared_read.read_json)
    assert result["failures"] == [] and result["errors"] == []
    assert result["reads"] > 0 and len(result["seen"]) > 1

    # An ordinary open() denies delete sharing and blocks the same replacements, which shows
    # that this loop really overlaps reads with replacements.
    def plain(target):
        with open(target, "rb") as stream:
            return json.loads(stream.read())
    result = _overlap(path, 300, _posix_replace, 500, plain, max_errors=None)
    assert result["failures"]
