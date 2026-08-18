from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from app.automation import control
from app.database.session import get_engine, get_session_factory
from app.ui import automation_panel as automation_panel_module
from app.ui.automation_panel import AutomationPanel


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def panel(session, monkeypatch):
    # Keep construction itself deterministic/no-op regardless of whatever
    # real daemon.lock state happens to exist on the dev machine running
    # the tests.
    monkeypatch.setattr(control, "read_daemon_status", lambda: None)
    return AutomationPanel(session)


def test_start_button_disabled_and_stop_enabled_when_daemon_is_running(panel, monkeypatch):
    running_status = control.DaemonStatus(running=True, pid=1234, started_at=datetime.now(timezone.utc))
    monkeypatch.setattr(control, "get_daemon_status", lambda: running_status)
    monkeypatch.setattr(control, "bot_status_rows", lambda session: [])

    panel.refresh_status()

    assert panel.start_button.isEnabled() is False
    assert panel.stop_button.isEnabled() is True
    assert "Работает" in panel.status_label.text()
    assert "1234" in panel.detail_label.text()


def test_stop_button_disabled_and_start_enabled_when_daemon_is_not_running(panel, monkeypatch):
    monkeypatch.setattr(control, "get_daemon_status", lambda: control.DaemonStatus(running=False))
    monkeypatch.setattr(control, "bot_status_rows", lambda session: [])

    panel.refresh_status()

    assert panel.start_button.isEnabled() is True
    assert panel.stop_button.isEnabled() is False
    assert "Остановлен" in panel.status_label.text()


def test_start_daemon_resolves_exe_and_launches_it_detached(panel, monkeypatch, tmp_path):
    exe_path = tmp_path / "MizanDaemon.exe"
    exe_path.write_bytes(b"")
    monkeypatch.setattr(control, "resolve_daemon_exe_path", lambda: exe_path)
    launch_mock = Mock()
    monkeypatch.setattr(control, "launch_daemon", launch_mock)
    monkeypatch.setattr(control, "get_daemon_status", lambda: control.DaemonStatus(running=False))
    monkeypatch.setattr(control, "bot_status_rows", lambda session: [])

    panel.start_daemon()

    launch_mock.assert_called_once_with(exe_path)
    assert panel.start_button.isEnabled() is False


def test_start_daemon_missing_exe_shows_message_and_does_not_crash(panel, monkeypatch, tmp_path):
    missing_path = tmp_path / "MizanDaemon.exe"  # never created
    monkeypatch.setattr(control, "resolve_daemon_exe_path", lambda: missing_path)
    launch_mock = Mock()
    monkeypatch.setattr(control, "launch_daemon", launch_mock)
    warning_mock = Mock()
    monkeypatch.setattr(automation_panel_module.QMessageBox, "warning", warning_mock)

    panel.start_daemon()  # must not raise

    launch_mock.assert_not_called()
    warning_mock.assert_called_once()
    shown_text = warning_mock.call_args[0][2]
    assert "MizanDaemon.exe" in shown_text
    assert "daemon.py" in shown_text


def test_stop_daemon_writes_the_stop_flag_via_existing_mechanism(panel, monkeypatch):
    request_stop_mock = Mock()
    monkeypatch.setattr(control, "request_stop", request_stop_mock)
    monkeypatch.setattr(control, "get_daemon_status", lambda: control.DaemonStatus(running=True, pid=1))
    monkeypatch.setattr(control, "bot_status_rows", lambda session: [])

    panel.stop_daemon()

    request_stop_mock.assert_called_once()
    assert panel.stop_button.isEnabled() is False
