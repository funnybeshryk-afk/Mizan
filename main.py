import sys
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select

from app.backtesting.engine import run_backtest
from app.backtesting.runner import save_backtest_run
from app.broker.broker import BrokerError
from app.broker.paper_broker import PaperBroker
from app.core.formatting import format_quantity
from app.database.models import Account, BacktestRun, TradeSide
from app.database.models import Signal as SignalRecord
from app.database.session import get_engine, get_session_factory
from app.market.factory import build_market_provider
from app.market.provider import MarketDataError
from app.portfolio.portfolio import (
    InsufficientFundsError,
    InsufficientPositionError,
    Portfolio,
)
from app.risk.risk_profile import AGGRESSIVE, CONSERVATIVE
from app.strategies.runner import evaluate_and_log
from app.strategies.trend import TrendStrategy
from app.ui.automation_panel import AutomationPanel

FIELD_MIN_HEIGHT = 32
FIELD_MIN_WIDTH = 180
BUTTON_MIN_HEIGHT = 34
TABLE_MIN_HEIGHT = 160

SIDEBAR_WIDTH = 220

NAV_BUTTON_STYLE = """
QPushButton {
    text-align: left;
    padding-left: 14px;
    border: none;
    border-radius: 6px;
    background-color: transparent;
    color: #e8e8e8;
}
QPushButton:hover {
    background-color: #3a3f4b;
}
QPushButton:checked {
    background-color: #4a6fa5;
    color: white;
    font-weight: bold;
}
"""

DEFAULT_INITIAL_CASH = Decimal("10000")

RISK_PROFILE_CHOICES = {
    "Без риск-профиля": None,
    "CONSERVATIVE": CONSERVATIVE,
    "AGGRESSIVE": AGGRESSIVE,
}

SECTION_NAMES = ["Портфель", "Торговля", "Стратегии", "Бэктест", "Настройки", "Автоматизация"]
AUTOMATION_SECTION_INDEX = SECTION_NAMES.index("Автоматизация")


def style_field(widget, min_width=FIELD_MIN_WIDTH, min_height=FIELD_MIN_HEIGHT):
    """Floors a widget's size so Qt can never compress it into an unreadable
    strip when several sections compete for space in the same layout."""
    widget.setMinimumHeight(min_height)
    widget.setMinimumWidth(min_width)
    return widget


def style_button(button, min_height=BUTTON_MIN_HEIGHT):
    button.setMinimumHeight(min_height)
    return button


def wrap_in_scroll_area(widget: QWidget) -> QScrollArea:
    """Each sidebar section gets its own scroll area, so a page taller than
    the window scrolls instead of forcing every widget on it to compress."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setWidget(widget)
    return scroll


def build_alpaca_broker(portfolio):
    """Best-effort Alpaca paper-trading broker. Returns None if credentials
    are missing or invalid - never raises, so a dev machine with no .env
    still gets a working app (just without the Alpaca option).
    """
    try:
        from app.broker.alpaca_broker import AlpacaBroker

        return AlpacaBroker(portfolio)
    except BrokerError:
        return None


def get_existing_account(session) -> Account | None:
    return session.execute(select(Account).order_by(Account.id)).scalars().first()


class MizanWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        engine = get_engine()
        session_factory = get_session_factory(engine)
        self.session = session_factory()

        account = get_existing_account(self.session)
        if account is not None:
            self.portfolio = Portfolio(self.session, account)
        else:
            initial_cash = self._prompt_initial_capital()
            self.portfolio = Portfolio.create(self.session, initial_cash=initial_cash)

        self.market_provider = build_market_provider(self.session)
        self.broker = PaperBroker(self.portfolio, self.market_provider)
        self.alpaca_broker = build_alpaca_broker(self.portfolio)
        self.last_prices: dict[str, Decimal] = {}
        self.trend_strategy = TrendStrategy()

        self.setWindowTitle("Mizan — Investment Terminal")
        self.setMinimumSize(900, 600)
        self.resize(1300, 800)

        central = QWidget()
        root_layout = QHBoxLayout()
        root_layout.setSpacing(0)
        root_layout.setContentsMargins(0, 0, 0, 0)

        root_layout.addWidget(self._build_sidebar())

        self.stacked_widget = QStackedWidget()
        self.stacked_widget.addWidget(wrap_in_scroll_area(self._build_portfolio_page()))
        self.stacked_widget.addWidget(wrap_in_scroll_area(self._build_trading_page()))
        self.stacked_widget.addWidget(wrap_in_scroll_area(self._build_strategies_page()))
        self.stacked_widget.addWidget(wrap_in_scroll_area(self._build_backtest_page()))
        self.stacked_widget.addWidget(wrap_in_scroll_area(self._build_settings_page()))
        self.automation_panel = AutomationPanel(self.session)
        self.stacked_widget.addWidget(wrap_in_scroll_area(self.automation_panel))
        root_layout.addWidget(self.stacked_widget, 1)

        central.setLayout(root_layout)
        self.setCentralWidget(central)

        self.nav_buttons[0].setChecked(True)
        self.stacked_widget.setCurrentIndex(0)

        self.refresh()

    # --- sidebar / navigation ------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        sidebar.setStyleSheet("background-color: #262a33;")

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(6)

        title = QLabel("Mizan")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: white;")
        layout.addWidget(title)
        layout.addSpacing(24)

        self.nav_buttons: list[QPushButton] = []
        self.nav_button_group = QButtonGroup(self)
        self.nav_button_group.setExclusive(True)
        for index, name in enumerate(SECTION_NAMES):
            button = QPushButton(name)
            button.setCheckable(True)
            button.setMinimumHeight(40)
            button.setStyleSheet(NAV_BUTTON_STYLE)
            button.clicked.connect(lambda _checked=False, i=index: self._show_section(i))
            layout.addWidget(button)
            self.nav_button_group.addButton(button, index)
            self.nav_buttons.append(button)

        layout.addStretch(1)

        credit = QLabel("by Intelliqos")
        credit.setStyleSheet("font-size: 11px; color: #7a7f8a;")
        layout.addWidget(credit)

        sidebar.setLayout(layout)
        return sidebar

    def _show_section(self, index: int):
        self.stacked_widget.setCurrentIndex(index)
        if index == AUTOMATION_SECTION_INDEX:
            self.automation_panel.start_auto_refresh()
        else:
            self.automation_panel.stop_auto_refresh()

    # --- Портфель --------------------------------------------------------------

    def _build_portfolio_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(18)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QLabel("Портфель")
        header.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(header)

        self.cash_label = QLabel()
        self.cash_label.setStyleSheet("font-size: 18px;")
        layout.addWidget(self.cash_label)

        self.realized_label = QLabel()
        self.realized_label.setStyleSheet("font-size: 16px;")
        layout.addWidget(self.realized_label)

        positions_box = QGroupBox("Открытые позиции")
        positions_layout = QVBoxLayout()
        positions_layout.setSpacing(8)
        self.positions_table = QTableWidget(0, 5)
        self.positions_table.setMinimumHeight(TABLE_MIN_HEIGHT)
        self.positions_table.setHorizontalHeaderLabels(
            ["Символ", "Количество", "Средняя цена", "Текущая цена", "Нереализованный P&L"]
        )
        self.positions_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.positions_table.setEditTriggers(QTableWidget.NoEditTriggers)
        positions_layout.addWidget(self.positions_table)

        refresh_market_button = style_button(QPushButton("Обновить рынок"))
        refresh_market_button.clicked.connect(self.refresh_market_prices)
        positions_layout.addWidget(refresh_market_button)

        positions_box.setLayout(positions_layout)
        layout.addWidget(positions_box)

        trades_box = QGroupBox("Последние сделки")
        trades_layout = QVBoxLayout()
        trades_layout.setSpacing(8)
        self.trades_table = QTableWidget(0, 5)
        self.trades_table.setMinimumHeight(TABLE_MIN_HEIGHT)
        self.trades_table.setHorizontalHeaderLabels(
            ["Время", "Сторона", "Символ", "Количество", "Цена"]
        )
        self.trades_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.trades_table.setEditTriggers(QTableWidget.NoEditTriggers)
        trades_layout.addWidget(self.trades_table)
        trades_box.setLayout(trades_layout)
        layout.addWidget(trades_box)

        layout.addStretch(1)
        page.setLayout(layout)
        return page

    # --- Торговля ----------------------------------------------------------

    def _build_trading_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(18)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QLabel("Торговля")
        header.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(header)

        layout.addWidget(self._build_order_form())

        orders_box = QGroupBox("Ордера")
        orders_layout = QVBoxLayout()
        orders_layout.setSpacing(8)
        self.orders_table = QTableWidget(0, 6)
        self.orders_table.setMinimumHeight(TABLE_MIN_HEIGHT)
        self.orders_table.setHorizontalHeaderLabels(
            ["Время", "Сторона", "Символ", "Количество", "Статус", "Цена исполнения"]
        )
        self.orders_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.orders_table.setEditTriggers(QTableWidget.NoEditTriggers)
        orders_layout.addWidget(self.orders_table)

        sync_orders_button = style_button(QPushButton("Обновить статус ордеров"))
        sync_orders_button.clicked.connect(self.sync_broker_orders)
        orders_layout.addWidget(sync_orders_button)

        orders_box.setLayout(orders_layout)
        layout.addWidget(orders_box)

        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _build_order_form(self) -> QGroupBox:
        box = QGroupBox("Новая заявка")
        form = QFormLayout()
        form.setSpacing(10)
        form.setContentsMargins(12, 16, 12, 12)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.side_combo = style_field(QComboBox())
        self.side_combo.addItems(["BUY", "SELL"])

        self.broker_combo = style_field(QComboBox())
        self.broker_combo.addItems(["PaperBroker (симуляция)", "Alpaca (paper account)"])
        self.broker_combo.currentIndexChanged.connect(self.broker_choice_changed)

        self.symbol_edit = style_field(QLineEdit())
        self.symbol_edit.setPlaceholderText("AAPL")

        self.quantity_spin = style_field(QDoubleSpinBox())
        self.quantity_spin.setDecimals(6)
        self.quantity_spin.setRange(0.000001, 1_000_000_000)
        self.quantity_spin.setValue(1)

        self.price_spin = style_field(QDoubleSpinBox(), min_width=140)
        self.price_spin.setDecimals(2)
        self.price_spin.setRange(0.01, 1_000_000_000)
        self.price_spin.setValue(100)

        price_row = QHBoxLayout()
        price_row.setSpacing(10)
        price_row.addWidget(self.price_spin)
        refresh_price_button = style_button(QPushButton("Обновить цену"))
        refresh_price_button.clicked.connect(self.refresh_price_field)
        price_row.addWidget(refresh_price_button)

        form.addRow("Брокер", self.broker_combo)
        form.addRow("Сторона", self.side_combo)
        form.addRow("Символ", self.symbol_edit)
        form.addRow("Количество", self.quantity_spin)
        form.addRow("Цена", price_row)

        submit_button = style_button(QPushButton("Выполнить"))
        submit_button.setMinimumWidth(140)
        submit_button.clicked.connect(self.submit_order)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(submit_button)
        buttons_row.addStretch(1)

        outer = QVBoxLayout()
        outer.setSpacing(12)
        outer.addLayout(form)
        outer.addLayout(buttons_row)
        box.setLayout(outer)
        return box

    # --- Стратегии -----------------------------------------------------------

    def _build_strategies_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(18)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QLabel("Стратегии")
        header.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(header)

        layout.addWidget(self._build_strategy_panel())

        signals_box = QGroupBox("Сигналы стратегий")
        signals_layout = QVBoxLayout()
        signals_layout.setSpacing(8)
        self.signals_table = QTableWidget(0, 5)
        self.signals_table.setMinimumHeight(TABLE_MIN_HEIGHT)
        self.signals_table.setHorizontalHeaderLabels(
            ["Время", "Стратегия", "Символ", "Действие", "Цена"]
        )
        self.signals_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.signals_table.setEditTriggers(QTableWidget.NoEditTriggers)
        signals_layout.addWidget(self.signals_table)
        signals_box.setLayout(signals_layout)
        layout.addWidget(signals_box)

        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _build_strategy_panel(self) -> QGroupBox:
        box = QGroupBox("Стратегия (Trend)")
        layout = QHBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.addWidget(QLabel("Проверяет символ из формы заявки на странице «Торговля»"))
        layout.addStretch(1)
        check_button = style_button(QPushButton("Проверить сигнал"))
        check_button.setMinimumWidth(160)
        check_button.clicked.connect(self.check_signal)
        layout.addWidget(check_button)
        box.setLayout(layout)
        return box

    def check_signal(self):
        symbol = self.symbol_edit.text().strip()
        if not symbol:
            QMessageBox.warning(self, "Ошибка", "Укажите символ инструмента.")
            return
        if self.market_provider is None:
            QMessageBox.warning(
                self,
                "Рыночные данные недоступны",
                "Для проверки сигнала нужны исторические бары (настройте ключи Alpaca в .env).",
            )
            return

        end = date.today()
        start = end - timedelta(days=400)
        try:
            bars = self.market_provider.get_bars(symbol, start, end, timeframe="1Day")
        except MarketDataError as exc:
            QMessageBox.warning(self, "Не удалось получить бары", str(exc))
            return

        instrument = self.portfolio.get_or_create_instrument(symbol)
        signal = evaluate_and_log(self.session, self.trend_strategy, instrument, bars)

        price_text = f" (цена {signal.price:.2f})" if signal.price is not None else ""
        QMessageBox.information(self, "Сигнал", f"{symbol}: {signal.action.value}{price_text}")
        self.refresh()

    # --- Бэктест -------------------------------------------------------------

    def _build_backtest_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(18)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QLabel("Бэктест")
        header.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(header)

        layout.addWidget(self._build_backtest_panel())

        backtests_box = QGroupBox("Прогоны бэктеста")
        backtests_layout = QVBoxLayout()
        backtests_layout.setSpacing(8)
        self.backtests_table = QTableWidget(0, 8)
        self.backtests_table.setMinimumHeight(TABLE_MIN_HEIGHT)
        self.backtests_table.setHorizontalHeaderLabels(
            [
                "Символ",
                "Период",
                "Риск-профиль",
                "Итог. капитал",
                "Доходность %",
                "Просадка %",
                "Win rate %",
                "Сделок",
            ]
        )
        self.backtests_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.backtests_table.setEditTriggers(QTableWidget.NoEditTriggers)
        backtests_layout.addWidget(self.backtests_table)
        backtests_box.setLayout(backtests_layout)
        layout.addWidget(backtests_box)

        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _build_backtest_panel(self) -> QGroupBox:
        box = QGroupBox("Бэктест (Trend, символ из формы заявки на странице «Торговля»)")
        form = QFormLayout()
        form.setSpacing(10)
        form.setContentsMargins(12, 16, 12, 12)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        today = QDate.currentDate()
        self.backtest_start_edit = style_field(QDateEdit(today.addYears(-2)))
        self.backtest_start_edit.setCalendarPopup(True)
        self.backtest_end_edit = style_field(QDateEdit(today))
        self.backtest_end_edit.setCalendarPopup(True)

        form.addRow("Начало", self.backtest_start_edit)
        form.addRow("Конец", self.backtest_end_edit)
        form.addRow(
            QLabel(
                "Риск-профиль и начальный капитал бэктеста задаются на странице «Настройки»."
            )
        )

        run_button = style_button(QPushButton("Запустить бэктест"))
        run_button.setMinimumWidth(180)
        run_button_row = QHBoxLayout()
        run_button_row.addWidget(run_button)
        run_button_row.addStretch(1)
        run_button.clicked.connect(self.run_backtest_clicked)

        outer = QVBoxLayout()
        outer.setSpacing(12)
        outer.addLayout(form)
        outer.addLayout(run_button_row)
        box.setLayout(outer)
        return box

    def run_backtest_clicked(self):
        symbol = self.symbol_edit.text().strip()
        if not symbol:
            QMessageBox.warning(self, "Ошибка", "Укажите символ инструмента.")
            return

        start = self.backtest_start_edit.date().toPython()
        end = self.backtest_end_edit.date().toPython()
        if start >= end:
            QMessageBox.warning(self, "Ошибка", "Дата начала должна быть раньше даты конца.")
            return

        if self.market_provider is None:
            QMessageBox.warning(
                self,
                "Рыночные данные недоступны",
                "Для бэктеста нужны исторические бары (настройте ключи Alpaca в .env).",
            )
            return

        try:
            bars = self.market_provider.get_bars(symbol, start, end, timeframe="1Day")
        except MarketDataError as exc:
            QMessageBox.warning(self, "Не удалось получить бары", str(exc))
            return

        if not bars:
            QMessageBox.warning(
                self,
                "Недостаточно данных",
                f"Нет исторических баров для {symbol} за период {start}..{end}.",
            )
            return

        risk_profile = RISK_PROFILE_CHOICES[self.risk_profile_combo.currentText()]
        initial_capital = Decimal(str(self.backtest_capital_spin.value()))
        result = run_backtest(
            self.trend_strategy,
            bars,
            initial_capital=initial_capital,
            symbol=symbol,
            risk_profile=risk_profile,
        )

        instrument = self.portfolio.get_or_create_instrument(symbol)
        run = save_backtest_run(
            self.session, self.trend_strategy, instrument, start, end, result, risk_profile=risk_profile
        )

        profile_text = run.risk_profile_name or "без риск-профиля"
        QMessageBox.information(
            self,
            "Результат бэктеста",
            (
                f"{symbol} {start}..{end} ({profile_text})\n"
                f"Начальный капитал: {run.initial_capital:.2f}\n"
                f"Итоговый капитал: {run.final_capital:.2f}\n"
                f"Доходность: {run.total_return_pct:.2f}%\n"
                f"Макс. просадка: {run.max_drawdown_pct:.2f}%\n"
                f"Win rate: {run.win_rate_pct:.2f}%\n"
                f"Сделок: {run.num_trades}"
            ),
        )
        self.refresh()

    # --- Настройки -----------------------------------------------------------

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(18)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QLabel("Настройки")
        header.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(header)

        status_box = QGroupBox("Подключения")
        status_layout = QVBoxLayout()
        status_layout.setSpacing(8)
        status_layout.setContentsMargins(12, 16, 12, 12)

        self.market_status_label = QLabel()
        self.market_status_label.setStyleSheet("font-size: 13px; color: #888;")
        if self.market_provider is None:
            self.market_status_label.setText(
                "Рыночные данные недоступны (нет ключей Alpaca или сети) — ручной ввод цены"
            )
        else:
            self.market_status_label.setText("Рыночные данные: Alpaca подключена")
        status_layout.addWidget(self.market_status_label)

        self.broker_status_label = QLabel()
        self.broker_status_label.setStyleSheet("font-size: 13px; color: #888;")
        if self.alpaca_broker is None:
            self.broker_status_label.setText(
                "Alpaca-брокер недоступен (нет ключей Alpaca или сети) — доступен только PaperBroker"
            )
        else:
            self.broker_status_label.setText(
                "Alpaca-брокер: подключён (paper account) — выбирается на странице «Торговля»"
            )
        status_layout.addWidget(self.broker_status_label)

        status_box.setLayout(status_layout)
        layout.addWidget(status_box)

        backtest_settings_box = QGroupBox("Параметры бэктеста по умолчанию")
        form = QFormLayout()
        form.setSpacing(10)
        form.setContentsMargins(12, 16, 12, 12)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.risk_profile_combo = style_field(QComboBox())
        self.risk_profile_combo.addItems(list(RISK_PROFILE_CHOICES.keys()))

        self.backtest_capital_spin = style_field(QDoubleSpinBox())
        self.backtest_capital_spin.setDecimals(2)
        self.backtest_capital_spin.setRange(0.01, 1_000_000_000)
        self.backtest_capital_spin.setValue(float(DEFAULT_INITIAL_CASH))

        form.addRow("Риск-профиль", self.risk_profile_combo)
        form.addRow("Начальный капитал бэктеста", self.backtest_capital_spin)
        backtest_settings_box.setLayout(form)
        layout.addWidget(backtest_settings_box)

        layout.addStretch(1)
        page.setLayout(layout)
        return page

    def _prompt_initial_capital(self) -> Decimal:
        """Asked once, the very first time the app runs (no Account exists
        yet). 10000 is only the suggested default shown in the field - the
        field itself is editable, so the app works correctly whether someone
        starts with $100 or $10,000,000."""
        value, ok = QInputDialog.getDouble(
            self,
            "Стартовый капитал",
            "Укажите стартовый капитал счёта ($):",
            float(DEFAULT_INITIAL_CASH),
            0.01,
            1_000_000_000.0,
            2,
        )
        if not ok or value <= 0:
            value = float(DEFAULT_INITIAL_CASH)
        return Decimal(str(value))

    # --- broker / order actions ------------------------------------------------

    def broker_choice_changed(self, index):
        if index != 1:  # not Alpaca
            return
        if self.alpaca_broker is None:
            QMessageBox.warning(
                self,
                "Alpaca недоступен",
                "Ключи Alpaca не настроены или недействительны (см. .env.example). "
                "Остаёмся на PaperBroker.",
            )
            self.broker_combo.blockSignals(True)
            self.broker_combo.setCurrentIndex(0)
            self.broker_combo.blockSignals(False)
            return
        QMessageBox.warning(
            self,
            "Alpaca paper account",
            "Ордера будут отправляться на реальный paper-аккаунт Alpaca, а не в "
            "локальную симуляцию. Это по-прежнему не реальные деньги, но исполнение "
            "асинхронное: после отправки нажимайте «Обновить статус ордеров», чтобы "
            "узнать, исполнился ли ордер.",
        )

    def _active_broker(self):
        if self.broker_combo.currentIndex() == 1 and self.alpaca_broker is not None:
            return self.alpaca_broker
        return self.broker

    def sync_broker_orders(self):
        if self.alpaca_broker is None:
            QMessageBox.information(self, "Синхронизация ордеров", "Alpaca не настроена в этой сессии.")
            return

        try:
            updated = self.alpaca_broker.sync_pending_orders()
        except BrokerError as exc:
            QMessageBox.warning(self, "Не удалось синхронизировать ордера", str(exc))
            return

        self.refresh()
        if updated:
            QMessageBox.information(self, "Синхронизация ордеров", f"Обновлено ордеров: {len(updated)}")
        else:
            QMessageBox.information(self, "Синхронизация ордеров", "Изменений нет.")

    def submit_order(self):
        symbol = self.symbol_edit.text().strip()
        if not symbol:
            QMessageBox.warning(self, "Ошибка", "Укажите символ инструмента.")
            return

        try:
            quantity = Decimal(str(self.quantity_spin.value()))
            price = Decimal(str(self.price_spin.value()))
        except InvalidOperation:
            QMessageBox.warning(self, "Ошибка", "Некорректное количество или цена.")
            return

        side = TradeSide.BUY if self.side_combo.currentText() == "BUY" else TradeSide.SELL
        instrument = self.portfolio.get_or_create_instrument(symbol)
        broker = self._active_broker()

        try:
            if broker is self.broker:
                broker.submit_order(self.portfolio.account, instrument, side, quantity, price=price)
            else:
                broker.submit_order(self.portfolio.account, instrument, side, quantity)
        except InsufficientFundsError as exc:
            QMessageBox.warning(self, "Недостаточно средств", str(exc))
            return
        except InsufficientPositionError as exc:
            QMessageBox.warning(self, "Недостаточно позиции", str(exc))
            return
        except BrokerError as exc:
            QMessageBox.warning(self, "Ошибка брокера", str(exc))
            return
        except ValueError as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))
            return

        self.symbol_edit.clear()
        self.refresh()

    def refresh_price_field(self):
        symbol = self.symbol_edit.text().strip()
        if not symbol:
            QMessageBox.warning(self, "Ошибка", "Укажите символ инструмента.")
            return
        if self.market_provider is None:
            QMessageBox.warning(
                self, "Рыночные данные недоступны", "Настройте ключи Alpaca в .env или введите цену вручную."
            )
            return

        try:
            price = self.market_provider.get_latest_price(symbol)
        except MarketDataError as exc:
            QMessageBox.warning(self, "Не удалось получить цену", str(exc))
            return

        self.price_spin.setValue(float(price))

    def refresh_market_prices(self):
        if self.market_provider is None:
            QMessageBox.warning(
                self, "Рыночные данные недоступны", "Настройте ключи Alpaca в .env или введите цену вручную."
            )
            return

        failures = []
        for position in self.portfolio.positions():
            symbol = position.instrument.symbol
            try:
                self.last_prices[symbol] = self.market_provider.get_latest_price(symbol)
            except MarketDataError as exc:
                failures.append(f"{symbol}: {exc}")

        self.refresh()

        if failures:
            QMessageBox.warning(self, "Не удалось обновить часть цен", "\n".join(failures))

    # --- data refresh ------------------------------------------------------

    def refresh(self):
        self.cash_label.setText(f"Свободные средства: ${self.portfolio.cash:,.2f}")
        self.realized_label.setText(
            f"Реализованная прибыль/убыток: ${self.portfolio.realized_pnl():,.2f}"
        )

        positions = self.portfolio.positions()
        self.positions_table.setRowCount(len(positions))
        for row, position in enumerate(positions):
            symbol = position.instrument.symbol
            self.positions_table.setItem(row, 0, QTableWidgetItem(symbol))
            self.positions_table.setItem(row, 1, QTableWidgetItem(format_quantity(position.quantity)))
            self.positions_table.setItem(row, 2, QTableWidgetItem(f"{position.avg_price:.2f}"))

            current_price_text = "—"
            pnl_text = "—"
            current_price = self.last_prices.get(symbol)
            if current_price is not None:
                current_price_text = f"{current_price:.2f}"
                pnl = self.portfolio.unrealized_pnl(symbol, current_price)
                pnl_text = f"{pnl:.2f}"

            self.positions_table.setItem(row, 3, QTableWidgetItem(current_price_text))
            self.positions_table.setItem(row, 4, QTableWidgetItem(pnl_text))

        trades = self.portfolio.trades(limit=50)
        self.trades_table.setRowCount(len(trades))
        for row, trade in enumerate(trades):
            self.trades_table.setItem(
                row, 0, QTableWidgetItem(trade.executed_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.trades_table.setItem(row, 1, QTableWidgetItem(trade.side.value))
            self.trades_table.setItem(row, 2, QTableWidgetItem(trade.instrument.symbol))
            self.trades_table.setItem(row, 3, QTableWidgetItem(format_quantity(trade.quantity)))
            self.trades_table.setItem(row, 4, QTableWidgetItem(f"{trade.price:.2f}"))

        orders = self.broker.orders(limit=50)
        self.orders_table.setRowCount(len(orders))
        for row, order in enumerate(orders):
            self.orders_table.setItem(
                row, 0, QTableWidgetItem(order.requested_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.orders_table.setItem(row, 1, QTableWidgetItem(order.side.value))
            self.orders_table.setItem(row, 2, QTableWidgetItem(order.instrument.symbol))
            self.orders_table.setItem(row, 3, QTableWidgetItem(format_quantity(order.quantity)))
            self.orders_table.setItem(row, 4, QTableWidgetItem(order.status.value))
            fill_price_text = f"{order.fill_price:.2f}" if order.fill_price is not None else "—"
            self.orders_table.setItem(row, 5, QTableWidgetItem(fill_price_text))

        signals = list(
            self.session.execute(
                select(SignalRecord)
                .order_by(SignalRecord.generated_at.desc(), SignalRecord.id.desc())
                .limit(50)
            ).scalars()
        )
        self.signals_table.setRowCount(len(signals))
        for row, signal in enumerate(signals):
            self.signals_table.setItem(
                row, 0, QTableWidgetItem(signal.generated_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.signals_table.setItem(row, 1, QTableWidgetItem(signal.strategy_name))
            self.signals_table.setItem(row, 2, QTableWidgetItem(signal.instrument.symbol))
            self.signals_table.setItem(row, 3, QTableWidgetItem(signal.action.value))
            price_text = (
                f"{signal.price_at_signal:.2f}" if signal.price_at_signal is not None else "—"
            )
            self.signals_table.setItem(row, 4, QTableWidgetItem(price_text))

        backtest_runs = list(
            self.session.execute(
                select(BacktestRun).order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc()).limit(50)
            ).scalars()
        )
        self.backtests_table.setRowCount(len(backtest_runs))
        for row, run in enumerate(backtest_runs):
            self.backtests_table.setItem(row, 0, QTableWidgetItem(run.instrument.symbol))
            self.backtests_table.setItem(
                row, 1, QTableWidgetItem(f"{run.start_date} .. {run.end_date}")
            )
            self.backtests_table.setItem(row, 2, QTableWidgetItem(run.risk_profile_name or "—"))
            self.backtests_table.setItem(row, 3, QTableWidgetItem(f"{run.final_capital:.2f}"))
            self.backtests_table.setItem(row, 4, QTableWidgetItem(f"{run.total_return_pct:.2f}"))
            self.backtests_table.setItem(row, 5, QTableWidgetItem(f"{run.max_drawdown_pct:.2f}"))
            self.backtests_table.setItem(row, 6, QTableWidgetItem(f"{run.win_rate_pct:.2f}"))
            self.backtests_table.setItem(row, 7, QTableWidgetItem(str(run.num_trades)))

    def closeEvent(self, event):
        self.session.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MizanWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
