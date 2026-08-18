from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.automation import control
from app.database.models import Account, BotConfig, Instrument, SignalAction
from app.database.models import Signal as SignalRecord
from app.database.models import Trade, TradeSide
from app.database.session import get_engine, get_session_factory


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    session = session_factory()
    yield session
    session.close()


# --- exe path resolution -----------------------------------------------------


def test_resolve_daemon_exe_path_joins_base_dir_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    resolved = control.resolve_daemon_exe_path(tmp_path)
    assert resolved == tmp_path / "MizanDaemon.exe"


def test_resolve_daemon_exe_path_resolves_to_daemon_script_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    resolved = control.resolve_daemon_exe_path(tmp_path)
    assert resolved == tmp_path / "daemon.py"


def test_daemon_exe_exists_reflects_the_filesystem(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    assert control.daemon_exe_exists(tmp_path) is False
    (tmp_path / "MizanDaemon.exe").write_bytes(b"")
    assert control.daemon_exe_exists(tmp_path) is True


# --- status -------------------------------------------------------------------


def test_get_daemon_status_not_running_when_no_lock(monkeypatch):
    monkeypatch.setattr(control, "read_daemon_status", lambda: None)
    status = control.get_daemon_status()
    assert status.running is False
    assert status.pid is None


def test_get_daemon_status_running_parses_pid_and_started_at(monkeypatch):
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        control,
        "read_daemon_status",
        lambda: {"pid": 4321, "exe": "MizanDaemon.exe", "started_at": started.isoformat()},
    )
    status = control.get_daemon_status()
    assert status.running is True
    assert status.pid == 4321
    assert status.started_at == started


# --- launch/stop ----------------------------------------------------------------


def test_launch_daemon_uses_detached_creation_flags_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    exe_path = tmp_path / "MizanDaemon.exe"
    exe_path.write_bytes(b"")
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "fake-process"

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = control.launch_daemon(exe_path)

    assert result == "fake-process"
    assert captured["args"] == [str(exe_path)]
    assert captured["kwargs"]["cwd"] == str(exe_path.parent)
    expected_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    assert captured["kwargs"]["creationflags"] == expected_flags


def test_launch_daemon_asks_systemd_to_start_the_unit_on_linux(monkeypatch, tmp_path):
    """Stage 9c hotfix (ported from the VPS): on POSIX the daemon is
    systemd-managed, so /start must ask systemd to start the unit -
    never spawn daemon.py directly, which would create a process systemd
    doesn't know about."""
    monkeypatch.setattr(sys, "platform", "linux")
    script_path = tmp_path / "daemon.py"
    script_path.write_bytes(b"")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = control.launch_daemon(script_path)

    assert result is None
    assert captured["args"] == ["sudo", "-n", "/usr/bin/systemctl", "start", "mizan-daemon.service"]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_launch_daemon_raises_on_systemctl_failure_without_falling_back(monkeypatch, tmp_path):
    """No silent fallback to a raw subprocess spawn - that would silently
    reintroduce an unmanaged daemon running outside systemd's tracking."""
    monkeypatch.setattr(sys, "platform", "linux")
    script_path = tmp_path / "daemon.py"
    script_path.write_bytes(b"")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="", stderr="sudo: a password is required\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    popen_mock = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    with pytest.raises(control.DaemonLaunchError, match="password is required"):
        control.launch_daemon(script_path)

    popen_mock.assert_not_called()


def test_launch_daemon_raises_when_sudo_itself_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    script_path = tmp_path / "daemon.py"
    script_path.write_bytes(b"")

    def fake_run(args, **kwargs):
        raise FileNotFoundError("sudo not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(control.DaemonLaunchError):
        control.launch_daemon(script_path)


def test_request_stop_writes_the_flag_file(monkeypatch, tmp_path):
    stop_path = tmp_path / "data" / "daemon.stop"
    monkeypatch.setattr(control, "stop_file_path", lambda: stop_path)

    assert not stop_path.exists()
    control.request_stop()
    assert stop_path.exists()


# --- bot status rows --------------------------------------------------------------


def test_bot_status_rows_reports_latest_signal_and_trade(session):
    account = Account(cash=Decimal("10000"))
    session.add(account)
    session.commit()

    bot_config = BotConfig(
        name="conservative-aapl",
        strategy_class="TrendStrategy",
        strategy_params_json="{}",
        risk_profile_name="CONSERVATIVE",
        account_id=account.id,
        symbol="AAPL",
        poll_interval_seconds=300,
        enabled=True,
    )
    session.add(bot_config)
    session.commit()

    instrument = Instrument(symbol="AAPL", asset_class="US_EQUITY")
    session.add(instrument)
    session.commit()

    session.add(
        SignalRecord(
            strategy_name="TrendStrategy",
            strategy_params_json="{}",
            instrument_id=instrument.id,
            action=SignalAction.BUY,
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            price_at_signal=Decimal("100"),
        )
    )
    session.add(
        Trade(
            account_id=account.id,
            instrument_id=instrument.id,
            side=TradeSide.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
            executed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    session.commit()

    rows = control.bot_status_rows(session)

    assert len(rows) == 1
    row = rows[0]
    assert row.name == "conservative-aapl"
    assert row.symbol == "AAPL"
    assert row.risk_profile_name == "CONSERVATIVE"
    assert row.halted is False
    assert row.last_signal_action == "BUY"
    assert row.last_trade_summary == "BUY 10 @ 100.00"


def test_bot_status_rows_reports_halt_reason(session):
    account = Account(cash=Decimal("10000"))
    session.add(account)
    session.commit()

    bot_config = BotConfig(
        name="aggressive-tsla",
        strategy_class="TrendStrategy",
        strategy_params_json="{}",
        risk_profile_name="AGGRESSIVE",
        account_id=account.id,
        symbol="TSLA",
        poll_interval_seconds=300,
        enabled=True,
        halted=True,
        halted_reason="Max drawdown circuit breaker tripped",
        halted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    session.add(bot_config)
    session.commit()

    rows = control.bot_status_rows(session)

    assert len(rows) == 1
    assert rows[0].halted is True
    assert rows[0].halted_reason == "Max drawdown circuit breaker tripped"
    assert rows[0].last_signal_at is None
    assert rows[0].last_trade_at is None
