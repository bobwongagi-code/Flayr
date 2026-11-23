"""Process locks for local paths whose contents may be atomically replaced."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class FileLockBusy(OSError):
    """Another local process holds the lock."""


@contextmanager
def exclusive_file_lock(path: Path, *, timeout: float = 0) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    # Keep a stable inode. On POSIX, closing the last duplicate descriptor
    # releases the lock, including a duplicate explicitly passed to a child.
    with path.open("a+b") as handle:
        fd = handle.fileno()
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FileLockBusy(f"Another process holds the lock: {path}") from exc
                time.sleep(min(0.05, remaining))
        yield fd
