from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.automation import control
from app.core.formatting import format_uptime

AUTO_REFRESH_INTERVAL_MS = 4000
POLL_INTERVAL_MS = 500
POLL_MAX_ATTEMPTS = 20  # ~10s at POLL_INTERVAL_MS

NOTE_TEXT = (
    "Демон работает как отдельный процесс — закрытие Mizan не останавливает его. "
    "Для работы после перезагрузки нужен зарегистрированный автозапуск (см. README)."
)

MISSING_EXE_TEXT = (
    "MizanDaemon.exe не найден рядом с приложением. "
    "В режиме разработки запусти python daemon.py вручную."
)


class AutomationPanel(QWidget):
    """The "Автоматизация" sidebar section: live status of the automation
    daemon (MizanDaemon.exe, a separate process) plus manual start/stop
    control over it. This widget never becomes the daemon's parent process
    - see control.launch_daemon() - and never stops it any way other than
    the existing data/daemon.stop flag file from Stage 8b.
    """

    def __init__(self, session: Session, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_transition)
        self._poll_attempts_left = 0
        self._poll_target_running: bool | None = None

        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(AUTO_REFRESH_INTERVAL_MS)
        self._auto_refresh_timer.timeout.connect(self.refresh_status)

        self._build_ui()
        self.refresh_status()

    # --- construction --------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(18)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QLabel("Автоматизация")
        header.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(header)

        note = QLabel(NOTE_TEXT)
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 12px; color: #888;")
        layout.addWidget(note)

        status_box = QGroupBox("Статус демона")
        status_layout = QVBoxLayout()
        status_layout.setSpacing(8)
        status_layout.setContentsMargins(12, 16, 12, 12)

        self.status_label = QLabel()
        self.status_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        status_layout.addWidget(self.status_label)

        self.detail_label = QLabel()
        self.detail_label.setStyleSheet("font-size: 12px; color: #888;")
        status_layout.addWidget(self.detail_label)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(10)
        self.start_button = QPushButton("Запустить демон")
        self.start_button.setMinimumHeight(34)
        self.start_button.clicked.connect(self.start_daemon)
        buttons_row.addWidget(self.start_button)

        self.stop_button = QPushButton("Остановить демон")
        self.stop_button.setMinimumHeight(34)
        self.stop_button.clicked.connect(self.stop_daemon)
        buttons_row.addWidget(self.stop_button)
        buttons_row.addStretch(1)
        status_layout.addLayout(buttons_row)

        status_box.setLayout(status_layout)
        layout.addWidget(status_box)

        bots_box = QGroupBox("Боты")
        bots_layout = QVBoxLayout()
        bots_layout.setSpacing(8)
        self.bots_table = QTableWidget(0, 6)
        self.bots_table.setMinimumHeight(160)
        self.bots_table.setHorizontalHeaderLabels(
            ["Имя", "Символ", "Риск-профиль", "Статус", "Последний сигнал", "Последняя сделка"]
        )
        self.bots_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.bots_table.setEditTriggers(QTableWidget.NoEditTriggers)
        bots_layout.addWidget(self.bots_table)
        bots_box.setLayout(bots_layout)
        layout.addWidget(bots_box)

        layout.addStretch(1)
        self.setLayout(layout)

    # --- visibility-driven auto-refresh ---------------------------------------

    def start_auto_refresh(self) -> None:
        self.refresh_status()
        self._auto_refresh_timer.start()

    def stop_auto_refresh(self) -> None:
        self._auto_refresh_timer.stop()

    # --- status rendering ------------------------------------------------------

    def refresh_status(self) -> None:
        self._render_status(control.get_daemon_status())
        self._render_bots(control.bot_status_rows(self.session))

    def _render_status(self, status: control.DaemonStatus) -> None:
        if status.running:
            self.status_label.setText("● Работает")
            self.status_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #3fb950;")
            uptime_text = ""
            if status.started_at is not None:
                uptime = datetime.now(timezone.utc) - status.started_at
                uptime_text = f", в работе {format_uptime(uptime)}"
            self.detail_label.setText(f"PID {status.pid}{uptime_text}")
        else:
            self.status_label.setText("● Остановлен")
            self.status_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #888;")
            self.detail_label.setText("")

        self.start_button.setEnabled(not status.running)
        self.stop_button.setEnabled(status.running)

    def _render_bots(self, rows: list[control.BotStatusRow]) -> None:
        self.bots_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            status_text = f"Halted: {row.halted_reason}" if row.halted else "Активен"

            signal_text = "—"
            if row.last_signal_at is not None:
                signal_text = f"{row.last_signal_at.strftime('%Y-%m-%d %H:%M:%S')} ({row.last_signal_action})"

            trade_text = "—"
            if row.last_trade_at is not None:
                trade_text = f"{row.last_trade_at.strftime('%Y-%m-%d %H:%M:%S')} {row.last_trade_summary}"

            self.bots_table.setItem(row_index, 0, QTableWidgetItem(row.name))
            self.bots_table.setItem(row_index, 1, QTableWidgetItem(row.symbol))
            self.bots_table.setItem(row_index, 2, QTableWidgetItem(row.risk_profile_name))
            self.bots_table.setItem(row_index, 3, QTableWidgetItem(status_text))
            self.bots_table.setItem(row_index, 4, QTableWidgetItem(signal_text))
            self.bots_table.setItem(row_index, 5, QTableWidgetItem(trade_text))

    # --- start/stop actions ------------------------------------------------------

    def start_daemon(self) -> None:
        exe_path = control.resolve_daemon_exe_path()
        if not exe_path.exists():
            QMessageBox.warning(self, "MizanDaemon.exe не найден", MISSING_EXE_TEXT)
            return

        control.launch_daemon(exe_path)
        self.start_button.setEnabled(False)
        self.status_label.setText("Запускается…")
        self._begin_poll(target_running=True)

    def stop_daemon(self) -> None:
        control.request_stop()
        self.stop_button.setEnabled(False)
        self.status_label.setText("Останавливается…")
        self._begin_poll(target_running=False)

    def _begin_poll(self, target_running: bool) -> None:
        self._poll_target_running = target_running
        self._poll_attempts_left = POLL_MAX_ATTEMPTS
        self._poll_timer.start()

    def _poll_transition(self) -> None:
        status = control.get_daemon_status()
        self._poll_attempts_left -= 1
        if status.running == self._poll_target_running or self._poll_attempts_left <= 0:
            self._poll_timer.stop()
            self.refresh_status()
