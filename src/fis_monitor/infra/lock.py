"""FileLocker — OS-level single-instance lock using fcntl.flock or msvcrt.locking.

Invariant: lock is acquired via OS-level mechanism (fcntl.flock on Unix,
msvcrt.locking on Windows). PID is written for diagnostics only and is NOT
used for arbitration.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

from fis_monitor.domain.errors import AlreadyRunningError
from fis_monitor.domain.models import LockHandle

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class FileLocker:
    """OS-level single-instance lock.

    Uses fcntl.flock on Unix and msvcrt.locking on Windows with O_NOFOLLOW
    flag. PID is stored in the lock-file for information purposes
    (diagnostics, "who holds the lock?"), not for arbitration.
    O_EXCL is intentionally omitted — combining it with O_CREAT would prevent
    re-acquiring a stale lock file.

    Attributes:
        path: Path to the lock-file.
    """

    def __init__(self, path: Path) -> None:
        """Initialise FileLocker with a lock-file path.

        Args:
            path: Absolute path to the lock-file. Directory must exist.
        """
        self.path = Path(path) if not isinstance(path, Path) else path

    def acquire(self) -> LockHandle:
        """Acquire the OS-level lock.

        Opens or creates the lock-file with O_CREAT | O_RDWR | O_NOFOLLOW
        (Unix) and attempts to lock it exclusively without blocking.

        On success, writes the current process PID to the file and returns
        a LockHandle.

        Raises:
            AlreadyRunningError: If another instance holds the lock. The
                exception's `holder_pid` attribute contains the PID from
                the lock-file (may be None if unreadable).
            OSError: For other OS-level errors (permission, etc.).
        """
        flags = os.O_CREAT | os.O_RDWR

        if sys.platform != "win32":
            # Unix: O_NOFOLLOW prevents following symlinks
            flags |= os.O_NOFOLLOW

        fd = os.open(str(self.path), flags, 0o600)

        try:
            if sys.platform == "win32":
                # Windows: msvcrt.locking with LK_NBLCK (no-block)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                except OSError as e:
                    # Lock failed; read PID from file for diagnostics
                    holder_pid = self._read_pid_from_file(fd)
                    os.close(fd)
                    raise AlreadyRunningError(holder_pid=holder_pid) from e
            else:
                # Unix: fcntl.flock with LOCK_EX | LOCK_NB
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as e:
                    # Lock failed; read PID from file for diagnostics
                    holder_pid = self._read_pid_from_file(fd)
                    os.close(fd)
                    raise AlreadyRunningError(holder_pid=holder_pid) from e

            # Lock acquired; write current PID, flush, and return handle
            pid = os.getpid()
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(pid).encode("utf-8"))
            os.fsync(fd)

            return LockHandle(fd=fd, pid=pid, path=str(self.path))

        except Exception:
            # Ensure fd is closed on any other exception
            with contextlib.suppress(OSError):
                os.close(fd)
            raise

    def release(self, handle: LockHandle) -> None:
        """Release the OS-level lock.

        Unlocks the lock-file, closes the file descriptor, and attempts
        to unlink the lock-file (best-effort; errors are ignored).

        Args:
            handle: The LockHandle returned by acquire().
        """
        fd = handle.fd

        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                # Windows: unlock
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                # Unix: fcntl.flock with LOCK_UN
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)

            os.close(fd)

        # Best-effort unlink
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(str(self.path))

    @staticmethod
    def _read_pid_from_file(fd: int) -> int | None:
        """Read and parse the PID from an already-open lock-file.

        Args:
            fd: Open file descriptor to read from.

        Returns:
            The parsed PID, or None if the file is empty or unreadable.
        """
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            content = os.read(fd, 1024).decode("utf-8", errors="ignore").strip()
            if content:
                return int(content)
        except (ValueError, OSError):
            pass
        return None
