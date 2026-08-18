from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Same line format app.automation.daemon.build_daemon_logger() writes -
# "%(asctime)s [%(levelname)s] %(message)s" with datefmt "%Y-%m-%d %H:%M:%S".
_LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(?P<level>\w+)\] (?P<message>.*)$")

DEFAULT_POLL_INTERVAL_SECONDS = 1.0


class NotifierBot(Protocol):
    """The one method this module needs from a python-telegram-bot Bot -
    kept narrow so tests can pass a trivial fake instead of a real Bot."""

    async def send_message(self, chat_id: int, text: str) -> object: ...


@dataclass(frozen=True)
class LogEvent:
    """A notify-worthy moment in logs/daemon.log, classified from one raw
    line - kind is machine-readable (for tests/future filtering), message
    is the ready-to-send Telegram text."""

    kind: str
    message: str


def classify_log_line(line: str) -> LogEvent | None:
    """Pure function: does one raw logs/daemon.log line deserve a
    proactive Telegram notification, and if so what should it say?

    Parses the exact structured markers app.automation.daemon already
    logs (RISK_STOP_LOSS/RISK_TAKE_PROFIT/RISK_SESSION_CLOSE, "drawdown
    circuit breaker tripped", [ERROR] lines, ...) rather than inventing a
    second, parallel event format - if the daemon's own wording changes,
    this is the one place that needs to change with it.

    Returns None for lines that aren't worth interrupting someone's phone
    for (e.g. routine per-cycle "signal=WAIT" INFO lines).
    """
    line = line.rstrip("\n").rstrip("\r")
    if not line:
        return None

    match = _LOG_LINE_RE.match(line)
    level = match.group("level") if match else None
    message = match.group("message") if match else line

    if "Mizan automation daemon starting" in message:
        return LogEvent("daemon_starting", f"\U0001F7E2 Демон запускается.\n{message}")

    if message.startswith("Daemon stopped."):
        return LogEvent("daemon_stopped", "\U0001F534 Демон остановлен.")

    if "Another daemon instance is already running" in message:
        return LogEvent(
            "daemon_start_rejected", f"⚠️ Попытка повторного запуска отклонена.\n{message}"
        )

    if "RISK_STOP_LOSS triggered" in message:
        return LogEvent("risk_stop_loss", f"\U0001F6D1 Stop-loss сработал.\n{message}")

    if "RISK_TAKE_PROFIT triggered" in message:
        return LogEvent("risk_take_profit", f"\U0001F4B0 Take-profit сработал.\n{message}")

    if "RISK_SESSION_CLOSE triggered" in message:
        return LogEvent("risk_session_close", f"⏰ Принудительное закрытие по сессии.\n{message}")

    if "drawdown circuit breaker tripped" in message:
        return LogEvent("bot_halted", f"⛔ Бот остановлен риск-менеджером.\n{message}")

    if "skipping halted bot" in message:
        return LogEvent("bot_halted", f"⛔ Пропущен halted-бот.\n{message}")

    if level == "ERROR":
        return LogEvent("error", f"❌ Ошибка демона.\n{message}")

    return None


async def tail_log_and_notify(
    bot: NotifierBot,
    chat_id: int,
    log_path: Path,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Follows logs/daemon.log from end-of-file - never replays history
    that already existed when the supervisor started, so restarting the
    Telegram bot doesn't re-send a flood of old notifications - and
    forwards a message for every notify-worthy line, as classified by
    classify_log_line().

    Runs until stop_event is set (or forever, if none is given), polling
    for new lines every poll_interval seconds - a plain tail -f style
    loop, not an OS-level file-watch API, since daemon.log's write volume
    is a handful of lines a minute at most.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.touch()

    with open(log_path, "r", encoding="utf-8") as f:
        f.seek(0, 2)  # end of file - deliberately never replay pre-existing content
        while stop_event is None or not stop_event.is_set():
            line = f.readline()
            if not line:
                await asyncio.sleep(poll_interval)
                continue
            event = classify_log_line(line)
            if event is not None:
                await bot.send_message(chat_id=chat_id, text=event.message)
