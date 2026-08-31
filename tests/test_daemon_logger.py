from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler

import pytest

import app.automation.daemon as daemon_module
from app.automation.daemon import DAEMON_LOG_ROTATION_BACKUP_DAYS, build_daemon_logger
from app.automation.log_notifier import classify_log_line

# Mirrors the exact format build_daemon_logger() configures - kept as a
# self-contained expectation here rather than importing log_notifier's
# private regex, so this test is about the formatter's own output, not a
# proxy for a different module's parsing behavior.
_EXPECTED_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(\w+)\] (.*)$")


@pytest.fixture(autouse=True)
def _reset_mizan_daemon_logger():
    """build_daemon_logger() targets the single shared "mizan.daemon"
    logger and only attaches handlers if none exist yet - without this,
    handlers from one test would leak into the next and make
    build_daemon_logger() a silent no-op everywhere but the first test."""
    logger = logging.getLogger("mizan.daemon")
    logger.handlers.clear()
    yield
    logger.handlers.clear()


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "LOGS_DIR", tmp_path)
    return tmp_path


def _file_handlers(logger: logging.Logger) -> list[TimedRotatingFileHandler]:
    return [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]


def test_build_daemon_logger_attaches_a_timed_rotating_file_handler(logs_dir):
    logger = build_daemon_logger()

    handlers = _file_handlers(logger)
    assert len(handlers) == 1
    handler = handlers[0]
    assert handler.when == "MIDNIGHT"
    assert handler.backupCount == DAEMON_LOG_ROTATION_BACKUP_DAYS
    assert handler.baseFilename == str(logs_dir / "daemon.log")


def test_build_daemon_logger_rotation_is_bounded_not_unlimited():
    """The whole point of this change: backupCount must be a real, finite
    cap - not 0 (0 means "keep every rotated file forever", i.e. unbounded
    growth again, just spread across more files)."""
    assert DAEMON_LOG_ROTATION_BACKUP_DAYS > 0


def test_build_daemon_logger_keeps_the_existing_line_format(logs_dir):
    """app.automation.log_notifier.classify_log_line() only recognizes an
    ERROR-level line when it can extract level="ERROR" via the exact
    "%(asctime)s [%(levelname)s] %(message)s" format - proving the
    formatter's output is still genuinely parseable by the downstream
    Telegram notifier, not merely that some substring happened to match."""
    logger = build_daemon_logger()

    record = logging.LogRecord(
        name="mizan.daemon",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed to fetch bars for AAPL: network timeout",
        args=(),
        exc_info=None,
    )
    handler = _file_handlers(logger)[0]
    formatted = handler.format(record)

    assert _EXPECTED_LINE_RE.match(formatted) is not None
    event = classify_log_line(formatted)
    assert event is not None
    assert event.kind == "error"


def test_build_daemon_logger_actually_writes_a_parseable_line_to_disk(logs_dir):
    logger = build_daemon_logger()
    logger.info("[meanrev-ko] signal=WAIT price=90.38")

    for handler in logger.handlers:
        handler.flush()

    contents = (logs_dir / "daemon.log").read_text(encoding="utf-8")
    lines = [line for line in contents.splitlines() if line]
    assert len(lines) == 1
    match = _EXPECTED_LINE_RE.match(lines[0])
    assert match is not None
    assert match.group(1) == "INFO"
    assert match.group(2) == "[meanrev-ko] signal=WAIT price=90.38"


def test_build_daemon_logger_is_idempotent_and_never_duplicates_handlers(logs_dir):
    first = build_daemon_logger()
    handler_count_after_first = len(first.handlers)

    second = build_daemon_logger()

    assert second is first
    assert len(second.handlers) == handler_count_after_first
    assert len(_file_handlers(second)) == 1


def test_build_daemon_logger_still_attaches_a_console_handler_when_stderr_present(logs_dir, monkeypatch):
    """Regression: the file-handler rotation change must not disturb the
    separate StreamHandler(stderr) attachment (see build_daemon_logger's
    own comment on why it's conditional on sys.stderr)."""
    import sys as sys_module

    monkeypatch.setattr(sys_module, "stderr", sys_module.__stderr__)

    logger = build_daemon_logger()

    plain_stream_handlers = [
        h for h in logger.handlers if type(h) is logging.StreamHandler
    ]
    assert len(plain_stream_handlers) == 1
