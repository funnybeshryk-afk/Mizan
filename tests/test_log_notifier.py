from __future__ import annotations

import asyncio

from app.automation.log_notifier import classify_log_line, tail_log_and_notify


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


# --- classify_log_line: pure classification -----------------------------------


def test_classify_daemon_starting():
    line = "2026-08-15 12:00:00 [INFO] Mizan automation daemon starting - PAPER TRADING ONLY"
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "daemon_starting"
    assert "запускается" in event.message.lower()


def test_classify_daemon_stopped():
    line = "2026-08-15 12:00:00 [INFO] Daemon stopped."
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "daemon_stopped"


def test_classify_single_instance_rejection():
    line = (
        "2026-08-15 12:00:00 [ERROR] Another daemon instance is already running "
        "(pid=1234, started_at=2026-08-15T12:00:00) - refusing to start a second "
        "one against the same accounts. Exiting."
    )
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "daemon_start_rejected"
    assert "pid=1234" in event.message


def test_classify_risk_stop_loss():
    line = (
        "2026-08-15 12:00:00 [WARNING] [conservative-aapl] RISK_STOP_LOSS triggered: "
        "closed 10.000000 @ 80 (entry was 100.000000) order_id=2"
    )
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "risk_stop_loss"
    assert "conservative-aapl" in event.message


def test_classify_risk_take_profit():
    line = (
        "2026-08-15 12:00:00 [WARNING] [conservative-aapl] RISK_TAKE_PROFIT triggered: "
        "closed 10.000000 @ 120 (entry was 100.000000) order_id=3"
    )
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "risk_take_profit"


def test_classify_risk_session_close():
    line = (
        "2026-08-15 15:55:00 [WARNING] [intraday-orb-spy] RISK_SESSION_CLOSE triggered "
        "(Eastern time >= 15:55:00): closed 5.000000 @ 450 order_id=7"
    )
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "risk_session_close"
    assert "intraday-orb-spy" in event.message


def test_classify_bot_halted_on_circuit_breaker_trip():
    line = (
        "2026-08-15 12:00:00 [WARNING] [conservative-aapl] drawdown circuit breaker "
        "tripped - halted until manually cleared (persisted)"
    )
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "bot_halted"


def test_classify_bot_halted_on_startup_skip():
    line = (
        "2026-08-15 12:00:00 [WARNING] [aggressive-tsla] skipping halted bot "
        "(reason=Max drawdown circuit breaker tripped, halted_at=2026-08-15T12:00:00) - "
        "clear the halt with app.automation.manage_bots.clear_halt to resume"
    )
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "bot_halted"


def test_classify_generic_error():
    line = "2026-08-15 12:00:00 [ERROR] something unexpected went wrong"
    event = classify_log_line(line)
    assert event is not None
    assert event.kind == "error"


def test_classify_ignores_routine_info_lines():
    assert classify_log_line("2026-08-15 12:00:00 [INFO] [conservative-aapl] signal=WAIT price=100") is None
    assert classify_log_line("2026-08-15 12:00:00 [INFO] Loaded 2 bot(s): conservative-aapl, aggressive-tsla") is None
    assert classify_log_line("") is None
    assert classify_log_line("\n") is None


# --- tail_log_and_notify: async tailing behavior --------------------------------


def _run(coro):
    asyncio.run(coro)


def test_tailer_does_not_replay_pre_existing_lines(tmp_path):
    log_path = tmp_path / "daemon.log"
    log_path.write_text("2026-08-15 11:00:00 [ERROR] pre-existing error nobody should be notified about\n")
    bot = FakeBot()

    async def scenario():
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            tail_log_and_notify(bot, 12345, log_path, poll_interval=0.02, stop_event=stop_event)
        )
        await asyncio.sleep(0.1)
        assert bot.sent == []  # nothing replayed from before startup

        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

    _run(scenario())
    assert bot.sent == []


def test_tailer_notifies_new_notify_worthy_lines_as_they_are_appended(tmp_path):
    log_path = tmp_path / "daemon.log"
    log_path.write_text("2026-08-15 11:00:00 [INFO] old line, ignored\n")
    bot = FakeBot()

    async def scenario():
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            tail_log_and_notify(bot, 999, log_path, poll_interval=0.02, stop_event=stop_event)
        )
        await asyncio.sleep(0.1)

        with log_path.open("a", encoding="utf-8") as f:
            f.write("2026-08-15 11:00:01 [INFO] Mizan automation daemon starting - PAPER TRADING ONLY\n")
            f.write("2026-08-15 11:00:02 [INFO] [conservative-aapl] signal=WAIT price=100\n")  # not notify-worthy

        await asyncio.sleep(0.15)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

    _run(scenario())
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 999
    assert "запускается" in text.lower()


def test_tailer_creates_the_log_file_if_missing(tmp_path):
    log_path = tmp_path / "nested" / "daemon.log"
    bot = FakeBot()

    async def scenario():
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            tail_log_and_notify(bot, 1, log_path, poll_interval=0.02, stop_event=stop_event)
        )
        await asyncio.sleep(0.1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

    _run(scenario())
    assert log_path.exists()
    assert bot.sent == []
