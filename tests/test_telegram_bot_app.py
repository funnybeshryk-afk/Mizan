from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.automation import control
from app.automation import telegram_bot_app as bot_app

ALLOWED_CHAT_ID = 111
UNAUTHORIZED_CHAT_ID = 999


def make_update(chat_id: int):
    update = MagicMock()
    update.effective_chat = SimpleNamespace(id=chat_id)
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def make_context(allowed_chat_id: int = ALLOWED_CHAT_ID):
    context = MagicMock()
    context.bot_data = {"allowed_chat_id": allowed_chat_id}
    return context


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    def close(self) -> None:
        pass


# --- authorization -----------------------------------------------------------


def test_is_authorized_matches_chat_id():
    assert bot_app.is_authorized(make_update(ALLOWED_CHAT_ID), ALLOWED_CHAT_ID) is True
    assert bot_app.is_authorized(make_update(UNAUTHORIZED_CHAT_ID), ALLOWED_CHAT_ID) is False


@pytest.mark.parametrize("handler_name", ["status_command", "stop_command", "start_command"])
def test_unauthorized_chat_id_is_silently_ignored_by_every_command(handler_name, monkeypatch, caplog):
    handler = getattr(bot_app, handler_name)
    update = make_update(chat_id=UNAUTHORIZED_CHAT_ID)
    context = make_context(allowed_chat_id=ALLOWED_CHAT_ID)

    request_stop_mock = MagicMock()
    launch_daemon_mock = MagicMock()
    build_status_mock = MagicMock()
    monkeypatch.setattr(control, "request_stop", request_stop_mock)
    monkeypatch.setattr(control, "launch_daemon", launch_daemon_mock)
    monkeypatch.setattr(bot_app, "build_status_message", build_status_mock)

    with caplog.at_level(logging.DEBUG, logger="mizan.telegram_bot"):
        run(handler(update, context))

    update.message.reply_text.assert_not_called()
    request_stop_mock.assert_not_called()
    launch_daemon_mock.assert_not_called()
    build_status_mock.assert_not_called()
    assert "unauthorized" in caplog.text.lower()


# --- /status -------------------------------------------------------------------


def test_build_status_message_reuses_control_functions_and_formats_output(monkeypatch):
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        control, "get_daemon_status", lambda: control.DaemonStatus(running=True, pid=42, started_at=started)
    )
    fake_row = control.BotStatusRow(
        name="conservative-aapl",
        symbol="AAPL",
        risk_profile_name="CONSERVATIVE",
        halted=False,
        halted_reason=None,
        last_signal_at=started,
        last_signal_action="BUY",
        last_trade_at=started,
        last_trade_summary="BUY 10 @ 100.00",
    )
    bot_status_rows_mock = MagicMock(return_value=[fake_row])
    monkeypatch.setattr(control, "bot_status_rows", bot_status_rows_mock)
    monkeypatch.setattr(bot_app, "get_engine", lambda: object())
    monkeypatch.setattr(bot_app, "get_session_factory", lambda engine: (lambda: FakeSession()))

    text = bot_app.build_status_message()

    bot_status_rows_mock.assert_called_once()
    assert "работает" in text
    assert "PID 42" in text
    assert "conservative-aapl" in text
    assert "AAPL" in text
    assert "CONSERVATIVE" in text
    assert "BUY 10 @ 100.00" in text


def test_build_status_message_when_daemon_not_running(monkeypatch):
    monkeypatch.setattr(control, "get_daemon_status", lambda: control.DaemonStatus(running=False))
    monkeypatch.setattr(control, "bot_status_rows", lambda session: [])
    monkeypatch.setattr(bot_app, "get_engine", lambda: object())
    monkeypatch.setattr(bot_app, "get_session_factory", lambda engine: (lambda: FakeSession()))

    text = bot_app.build_status_message()

    assert "остановлен" in text
    assert "PID" not in text


def test_status_command_authorized_replies_with_status_message(monkeypatch):
    update = make_update(chat_id=ALLOWED_CHAT_ID)
    context = make_context()
    monkeypatch.setattr(bot_app, "build_status_message", MagicMock(return_value="STATUS TEXT"))

    run(bot_app.status_command(update, context))

    update.message.reply_text.assert_awaited_once_with("STATUS TEXT")


# --- /stop -----------------------------------------------------------------------


def test_stop_command_authorized_calls_request_stop(monkeypatch):
    update = make_update(chat_id=ALLOWED_CHAT_ID)
    context = make_context()
    request_stop_mock = MagicMock()
    monkeypatch.setattr(control, "request_stop", request_stop_mock)

    run(bot_app.stop_command(update, context))

    request_stop_mock.assert_called_once()
    update.message.reply_text.assert_awaited_once()


# --- /start ----------------------------------------------------------------------


def test_start_command_missing_exe_replies_without_launching(monkeypatch, tmp_path):
    update = make_update(chat_id=ALLOWED_CHAT_ID)
    context = make_context()
    missing_path = tmp_path / "daemon.py"  # deliberately never created
    monkeypatch.setattr(control, "resolve_daemon_exe_path", lambda: missing_path)
    launch_mock = MagicMock()
    monkeypatch.setattr(control, "launch_daemon", launch_mock)

    run(bot_app.start_command(update, context))

    launch_mock.assert_not_called()
    update.message.reply_text.assert_awaited_once()
    assert str(missing_path) in update.message.reply_text.call_args[0][0]


@pytest.mark.parametrize(
    "platform, exe_name", [("win32", "MizanDaemon.exe"), ("linux", "daemon.py")]
)
def test_start_command_launches_the_platform_appropriate_target(monkeypatch, tmp_path, platform, exe_name):
    """/start doesn't re-decide the platform itself - it delegates entirely
    to control.resolve_daemon_exe_path()/launch_daemon(), which are unit-
    tested for the actual win32-vs-POSIX branching in
    tests/test_automation_control.py. This just confirms the handler wires
    to whatever those return, for either platform."""
    update = make_update(chat_id=ALLOWED_CHAT_ID)
    context = make_context()
    exe_path = tmp_path / exe_name
    exe_path.write_bytes(b"")
    monkeypatch.setattr(control, "resolve_daemon_exe_path", lambda: exe_path)
    launch_mock = MagicMock()
    monkeypatch.setattr(control, "launch_daemon", launch_mock)

    run(bot_app.start_command(update, context))

    launch_mock.assert_called_once_with(exe_path)
    update.message.reply_text.assert_awaited_once()


def test_start_command_surfaces_a_clear_error_when_systemctl_fails(monkeypatch, tmp_path):
    """No silent fallback: a DaemonLaunchError from control.launch_daemon()
    (systemd/sudoers misconfigured on the VPS) must produce a clear
    Telegram reply naming systemd/sudoers - not a generic failure, and
    definitely not a second, different attempt to start the daemon."""
    update = make_update(chat_id=ALLOWED_CHAT_ID)
    context = make_context()
    exe_path = tmp_path / "daemon.py"
    exe_path.write_bytes(b"")
    monkeypatch.setattr(control, "resolve_daemon_exe_path", lambda: exe_path)
    monkeypatch.setattr(
        control,
        "launch_daemon",
        MagicMock(side_effect=control.DaemonLaunchError("sudo: a password is required")),
    )

    run(bot_app.start_command(update, context))

    update.message.reply_text.assert_awaited_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "systemd" in reply_text.lower()
    assert "sudoers" in reply_text.lower()
    assert "password is required" in reply_text


# --- config --------------------------------------------------------------------


def test_load_config_raises_when_env_vars_missing(monkeypatch):
    monkeypatch.setattr(bot_app, "load_dotenv", lambda path: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(bot_app.ConfigError):
        bot_app.load_config()


def test_load_config_raises_when_chat_id_is_not_an_integer(monkeypatch):
    monkeypatch.setattr(bot_app, "load_dotenv", lambda path: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "not-a-number")

    with pytest.raises(bot_app.ConfigError):
        bot_app.load_config()


def test_load_config_parses_chat_id_as_int(monkeypatch):
    monkeypatch.setattr(bot_app, "load_dotenv", lambda path: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555666777")

    token, chat_id = bot_app.load_config()

    assert token == "123:ABC"
    assert chat_id == 555666777
    assert isinstance(chat_id, int)


# --- logging safety ------------------------------------------------------------


def test_configure_logging_silences_httpx_info_to_avoid_leaking_the_bot_token():
    """httpx logs full request URLs at INFO, and the Telegram Bot API embeds
    the live token in that URL (.../bot<TOKEN>/...) - regression test for
    an actual token leak observed during Stage 9b manual verification."""
    logging.getLogger("httpx").setLevel(logging.NOTSET)

    bot_app.configure_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# --- application wiring -----------------------------------------------------------


def test_build_application_registers_handlers_and_chat_id():
    application = bot_app.build_application("123456:fake-token-for-tests", 42)

    assert application.bot_data["allowed_chat_id"] == 42
    registered_commands: set[str] = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            registered_commands |= getattr(handler, "commands", frozenset())
    assert {"status", "stop", "start"} <= registered_commands
