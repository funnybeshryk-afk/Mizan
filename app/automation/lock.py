from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psutil

from app.core.paths import DATA_DIR

LOCK_FILE_NAME = "daemon.lock"


def lock_file_path() -> Path:
    return DATA_DIR / LOCK_FILE_NAME


def _read_lock_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def pid_is_running(pid: int) -> bool:
    """Cross-platform liveness check via psutil.pid_exists() (Stage 9a).

    Deliberately not os.kill(pid, 0): on POSIX, signal 0 is a documented
    no-op used purely to probe whether a PID exists. On Windows, os.kill()
    has no such null-signal special case - passing an arbitrary signal
    number calls TerminateProcess() - so using it directly here would
    actually kill whatever process currently holds that PID instead of
    just checking it. This was hand-rolled via ctypes/OpenProcess for
    Windows only in Stage 8c; psutil.pid_exists() does the OS-appropriate
    equivalent on both Windows and POSIX (verified empirically on both
    platforms - see tests/test_daemon_lock.py and the Stage 9a report -
    that it never signals, terminates, or otherwise disturbs the process
    it's checking).

    pid 0 is excluded explicitly: it's a real, always-existing PID on
    Windows (the System Idle Process) but has a special "every process in
    my process group" meaning to POSIX kill()-family calls - neither
    reading is ever a real daemon instance, so it should never count as
    "running" here regardless of platform.
    """
    if pid <= 0:
        return False
    return psutil.pid_exists(pid)


def read_daemon_status(path: Path | None = None) -> dict | None:
    """Read-only check for the GUI's "Автоматизация" panel: is a live
    daemon holding the lock right now? Unlike DaemonLock.acquire(), this
    never writes/claims/overwrites the lock file - it just answers the
    question, using the same pid_is_running() liveness check so the GUI's
    idea of "running" never disagrees with the daemon's own.

    Returns the lock payload (pid/exe/started_at) if a live process holds
    it, otherwise None (no lock file, unreadable, or a stale/dead PID).
    """
    path = path or lock_file_path()
    if not path.exists():
        return None
    info = _read_lock_file(path)
    if info is None:
        return None
    pid = info.get("pid")
    if pid is None or not pid_is_running(pid):
        return None
    return info


@dataclass
class DaemonLock:
    """A PID lock file (data/daemon.lock) preventing two daemon processes
    from running against the same accounts at once (Stage 8c) - e.g. Task
    Scheduler's restart-on-failure policy firing over a still-alive-but-
    slow process, or the user double-launching MizanDaemon.exe by hand.

    pid_is_running is injected so tests can simulate a live/dead PID
    without touching real OS processes - see tests/test_daemon_lock.py.
    """

    path: Path = field(default_factory=lock_file_path)
    pid_is_running: Callable[[int], bool] = pid_is_running

    def _read(self) -> dict | None:
        return _read_lock_file(self.path)

    def acquire(self, logger: logging.Logger) -> bool:
        """Returns True if the lock was acquired (safe to proceed). False
        means a live instance already holds it - the caller must exit
        without touching any bot/account state.

        A lock file whose PID is no longer running is treated as stale
        (left over from an unclean shutdown, e.g. a power loss or a hard
        kill that skipped the release() below) - acquire() logs that and
        overwrites it rather than refusing to start.
        """
        existing = self._read() if self.path.exists() else None

        if existing is not None and self.pid_is_running(existing.get("pid", -1)):
            logger.error(
                "Another daemon instance is already running (pid=%s, started_at=%s) - "
                "refusing to start a second one against the same accounts. Exiting.",
                existing.get("pid"),
                existing.get("started_at"),
            )
            return False

        if existing is not None:
            logger.warning(
                "Found a stale lock file (pid=%s is no longer running) - "
                "an earlier run must have exited uncleanly. Proceeding.",
                existing.get("pid"),
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "exe": sys.executable,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        return True

    def release(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass
