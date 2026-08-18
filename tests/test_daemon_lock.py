from __future__ import annotations

import json
import logging

import pytest

from app.automation.lock import DaemonLock


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "daemon.lock"


def test_acquire_creates_a_lock_file_with_pid_and_exe(lock_path):
    lock = DaemonLock(path=lock_path, pid_is_running=lambda pid: False)
    logger = logging.getLogger("test.lock.create")

    assert lock.acquire(logger) is True

    payload = json.loads(lock_path.read_text())
    assert "pid" in payload
    assert "exe" in payload
    assert "started_at" in payload


def test_acquire_fails_when_a_live_pid_holds_the_lock(lock_path, caplog):
    lock_path.write_text(json.dumps({"pid": 4242, "exe": "MizanDaemon.exe", "started_at": "2026-01-01T00:00:00+00:00"}))
    lock = DaemonLock(path=lock_path, pid_is_running=lambda pid: pid == 4242)
    logger = logging.getLogger("test.lock.conflict")

    with caplog.at_level(logging.ERROR, logger="test.lock.conflict"):
        acquired = lock.acquire(logger)

    assert acquired is False
    assert "already running" in caplog.text
    assert "4242" in caplog.text
    # Refusing to acquire must never touch the existing file.
    assert json.loads(lock_path.read_text())["pid"] == 4242


def test_acquire_overwrites_a_stale_lock_from_a_dead_pid(lock_path, caplog):
    lock_path.write_text(json.dumps({"pid": 9999, "exe": "MizanDaemon.exe", "started_at": "2026-01-01T00:00:00+00:00"}))
    lock = DaemonLock(path=lock_path, pid_is_running=lambda pid: False)
    logger = logging.getLogger("test.lock.stale")

    with caplog.at_level(logging.WARNING, logger="test.lock.stale"):
        acquired = lock.acquire(logger)

    assert acquired is True
    assert "stale lock" in caplog.text
    new_payload = json.loads(lock_path.read_text())
    assert new_payload["pid"] != 9999


def test_release_removes_the_lock_file(lock_path):
    lock = DaemonLock(path=lock_path, pid_is_running=lambda pid: False)
    lock.acquire(logging.getLogger("test.lock.release"))
    assert lock_path.exists()

    lock.release()

    assert not lock_path.exists()


def test_release_is_a_safe_no_op_when_no_lock_file_exists(lock_path):
    lock = DaemonLock(path=lock_path, pid_is_running=lambda pid: False)
    lock.release()  # must not raise


def test_pid_is_running_reflects_the_current_process(lock_path):
    from app.automation.lock import pid_is_running
    import os

    assert pid_is_running(os.getpid()) is True
    # A PID of 0 (or negative) is never a valid user process on Windows.
    assert pid_is_running(0) is False
