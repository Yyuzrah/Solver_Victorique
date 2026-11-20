"""Small filesystem boundary for explicitly chosen harness artifacts."""

import json
import os
from pathlib import Path
import stat


class HarnessError(ValueError):
    pass


def checked_path(value, *, absolute=True, transient_sqlite_sidecar=False):
    path = Path(value)
    if absolute and not path.is_absolute():
        raise HarnessError("use an absolute path")
    if ".." in path.parts:
        raise HarnessError("parent traversal is not allowed")
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise HarnessError("linked/reparse path refused")
        vanished_sidecar = transient_sqlite_sidecar and item == path and info.st_nlink == 0
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1 and not vanished_sidecar:
            raise HarnessError("hard-linked file refused")
    return path


def child_path(root, relative):
    leaf = Path(relative)
    if leaf.is_absolute() or leaf.drive or ".." in leaf.parts:
        raise HarnessError("artifact path must be relative and contained")
    result = checked_path(checked_path(root) / leaf)
    if result == checked_path(root) or not result.is_relative_to(checked_path(root)):
        raise HarnessError("artifact path escapes root")
    return result


def write_new(path, data):
    target = checked_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    checked_path(target)
    with target.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if target.read_bytes() != data:
        raise HarnessError("exact copy verification failed")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _need(condition, message):
    if not condition:
        raise HarnessError(message)


def _check_path(path, *, transient_sqlite_sidecar=False):
    leaf_info = None
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if item == path:
            leaf_info = info
        _need(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400), "symlink/reparse path refused")
    # SQLite may remove an auxiliary journal between filesystem checks. Use
    # the same no-follow observation for both link/type and hard-link checks;
    # a second is_file()/stat() pair creates a spurious deletion race. This is
    # On Windows an already-unlinked optional SQLite sidecar can also appear
    # as a regular file with zero links. Only the explicit sidecar call sites
    # accept that vanished-file observation; the primary database and general
    # callers remain strict. Multiple links and reparse points stay refused.
    # This is still a path guard, not protection against later replacement.
    if leaf_info is not None and stat.S_ISREG(leaf_info.st_mode):
        _need(leaf_info.st_nlink == 1 or
              (transient_sqlite_sidecar and leaf_info.st_nlink == 0),
              "hardlinked database refused")


def _check_leaf(path, *, transient_sqlite_sidecar=False):
    """The checks _check_path makes on path itself, for a caller that has
    already checked path.parent and its parents with _check_path."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    _need(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400), "symlink/reparse path refused")
    if stat.S_ISREG(info.st_mode):
        _need(info.st_nlink == 1 or (transient_sqlite_sidecar and info.st_nlink == 0), "hardlinked database refused")
