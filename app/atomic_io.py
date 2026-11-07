"""Helpers for crash-safe replacement of small persistent files."""
from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write beside the destination, fsync, then atomically replace it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems do not support fsync on directories.
            pass
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any, *, indent: int | None = 2) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=indent)
    atomic_write_text(path, content + "\n")


def atomic_exchange_paths(first: Path, second: Path) -> bool:
    """Atomically exchange two existing paths when Linux renameat2 is available.

    Returns False only when the filesystem/kernel does not support exchange.
    Other failures are raised so callers can safely abort.
    """
    if os.name != "posix":
        return False
    try:
        import ctypes
    except ImportError:
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2)
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
        return False
    raise OSError(error, os.strerror(error), f"{first} <-> {second}")
