"""Tests for FileLocker — OS-level single-instance lock."""

from __future__ import annotations

import errno
import os
import sys
import tempfile
from pathlib import Path

import pytest

from fis_monitor.domain.errors import AlreadyRunningError
from fis_monitor.infra.lock import FileLocker


class TestFileLockerUnix:
    """Unix-specific tests for fcntl.flock."""

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_acquire_creates_file_with_pid(self, tmp_path: Path) -> None:
        """FileLocker.acquire() creates lock-file and writes current PID."""
        lock_path = tmp_path / "test.lock"
        locker = FileLocker(lock_path)

        handle = locker.acquire()

        assert lock_path.exists()
        assert handle.fd >= 0
        assert handle.pid == os.getpid()
        assert str(handle.path) == str(lock_path)

        # Check PID is in the file
        with open(lock_path) as f:
            content = f.read().strip()
            assert content == str(os.getpid())

        locker.release(handle)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_double_acquire_raises_already_running_error(
        self, tmp_path: Path
    ) -> None:
        """Second acquire() on locked file raises AlreadyRunningError."""
        lock_path = tmp_path / "test.lock"
        locker = FileLocker(lock_path)

        handle1 = locker.acquire()
        first_pid = os.getpid()

        locker2 = FileLocker(lock_path)
        with pytest.raises(AlreadyRunningError) as exc_info:
            locker2.acquire()

        assert exc_info.value.holder_pid == first_pid
        locker.release(handle1)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_release_unlocks_and_unlinks(self, tmp_path: Path) -> None:
        """release() unlocks and removes the lock-file."""
        lock_path = tmp_path / "test.lock"
        locker = FileLocker(lock_path)

        handle = locker.acquire()
        assert lock_path.exists()

        locker.release(handle)
        assert not lock_path.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_stale_lock_pid_not_used_for_arbitration(self, tmp_path: Path) -> None:
        """Lock-file with non-existent PID can be re-acquired."""
        lock_path = tmp_path / "test.lock"

        # Create a lock-file with a non-existent PID (99999 is unlikely to be running)
        lock_path.write_text("99999")
        # Note: We don't actually lock it with flock. This simulates a stale
        # lock file (file exists, PID doesn't, no OS-level lock held).

        # Now try to acquire: should succeed because PID 99999 is not running
        # and OS-level lock is the SSOT (not PID info)
        locker = FileLocker(lock_path)
        handle = locker.acquire()

        assert handle.pid == os.getpid()
        locker.release(handle)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_release_idempotent_on_unlink(self, tmp_path: Path) -> None:
        """release() handles missing lock-file gracefully."""
        lock_path = tmp_path / "test.lock"
        locker = FileLocker(lock_path)

        handle = locker.acquire()
        locker.release(handle)

        # Second release on non-existent file should not raise
        locker.release(handle)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_symlink_attack_blocked(self, tmp_path: Path) -> None:
        """acquire() with O_NOFOLLOW prevents symlink-based attacks."""
        lock_path = tmp_path / "test.lock"
        target = tmp_path / "target"
        target.write_text("sensitive data")

        # Create a symlink from lock_path to target
        lock_path.symlink_to(target)

        locker = FileLocker(lock_path)

        # On Unix, O_NOFOLLOW should cause an error (ELOOP)
        with pytest.raises(OSError) as exc_info:
            locker.acquire()

        # Verify it's a symlink error, not some other error
        assert exc_info.value.errno == errno.ELOOP

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_acquire_with_nonexistent_parent_dir(self, tmp_path: Path) -> None:
        """acquire() fails gracefully if parent directory doesn't exist."""
        lock_path = tmp_path / "nonexistent" / "test.lock"
        locker = FileLocker(lock_path)

        with pytest.raises(OSError):
            locker.acquire()

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_double_acquire_with_corrupted_pid_file(self, tmp_path: Path) -> None:
        """When lock file has garbage instead of PID, holder_pid is None but error still raised."""
        lock_path = tmp_path / "app.lock"
        locker1 = FileLocker(lock_path)
        handle1 = locker1.acquire()
        try:
            # Overwrite PID with garbage (lock still held by locker1 OS-side)
            lock_path.write_text("not-a-pid\n")
            locker2 = FileLocker(lock_path)
            with pytest.raises(AlreadyRunningError) as exc_info:
                locker2.acquire()
            assert exc_info.value.holder_pid is None
        finally:
            locker1.release(handle1)


class TestFileLockerWindows:
    """Windows-specific tests for msvcrt.locking."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_acquire_succeeds_on_windows(self) -> None:
        """FileLocker.acquire() works on Windows with msvcrt.locking."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            locker = FileLocker(lock_path)

            handle = locker.acquire()

            assert lock_path.exists()
            assert handle.fd >= 0
            assert handle.pid == os.getpid()

            locker.release(handle)
            assert not lock_path.exists()


class TestFileLockerCrossplatform:
    """Cross-platform tests for FileLocker."""

    def test_locker_protocol_compliance(self) -> None:
        """FileLocker implements Locker protocol."""
        from fis_monitor.domain.interfaces import Locker

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            locker = FileLocker(lock_path)

            # Should be compatible with Locker protocol
            _: Locker = locker

            handle = locker.acquire()
            locker.release(handle)

    def test_acquire_with_pathlib_and_string(self) -> None:
        """FileLocker accepts both Path and str."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"

            # With Path
            locker1 = FileLocker(lock_path)
            handle1 = locker1.acquire()
            locker1.release(handle1)

            # With string
            locker2 = FileLocker(str(lock_path))
            handle2 = locker2.acquire()
            locker2.release(handle2)
