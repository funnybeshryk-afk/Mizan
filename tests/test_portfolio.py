from decimal import Decimal

import pytest

from app.database.session import get_engine, get_session_factory
from app.portfolio.portfolio import (
    InsufficientFundsError,
    InsufficientPositionError,
    Portfolio,
)


@pytest.fixture
def portfolio():
    engine = get_engine("sqlite:///:memory:")
    Session = get_session_factory(engine)
    session = Session()
    yield Portfolio.create(session, initial_cash=10000)
    session.close()


def test_buy_reduces_cash_and_opens_position(portfolio):
    portfolio.buy("AAPL", 10, 100)

    assert portfolio.cash == Decimal("9000")
    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("10")
    assert position.avg_price == Decimal("100")


def test_buy_applies_commission(portfolio):
    portfolio.buy("AAPL", 10, 100, commission=5)

    assert portfolio.cash == Decimal("8995")
    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("10")
    assert position.avg_price == Decimal("100")


def test_buy_rejects_when_cash_insufficient(portfolio):
    with pytest.raises(InsufficientFundsError):
        portfolio.buy("AAPL", 1000, 100)

    assert portfolio.cash == Decimal("10000")
    assert portfolio.get_position("AAPL") is None


def test_buy_recomputes_average_price_on_top_up(portfolio):
    portfolio.buy("AAPL", 10, 100)
    portfolio.buy("AAPL", 10, 200)

    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("20")
    assert position.avg_price == Decimal("150")
    assert portfolio.cash == Decimal("7000")


def test_sell_increases_cash_and_reduces_position(portfolio):
    portfolio.buy("AAPL", 10, 100)
    portfolio.sell("AAPL", 4, 120)

    assert portfolio.cash == Decimal("9000") + Decimal("480")
    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("6")
    assert position.avg_price == Decimal("100")


def test_sell_applies_commission(portfolio):
    portfolio.buy("AAPL", 10, 100)
    portfolio.sell("AAPL", 10, 120, commission=3)

    assert portfolio.cash == Decimal("9000") + Decimal("1200") - Decimal("3")


def test_sell_rejects_when_position_missing(portfolio):
    with pytest.raises(InsufficientPositionError):
        portfolio.sell("AAPL", 1, 100)


def test_sell_rejects_when_quantity_exceeds_position(portfolio):
    portfolio.buy("AAPL", 5, 100)

    with pytest.raises(InsufficientPositionError):
        portfolio.sell("AAPL", 6, 100)

    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("5")


def test_sell_closing_position_resets_avg_price(portfolio):
    portfolio.buy("AAPL", 5, 100)
    portfolio.sell("AAPL", 5, 120)

    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("0")
    assert position.avg_price == Decimal("0")


def test_realized_pnl_on_profitable_close(portfolio):
    portfolio.buy("AAPL", 10, 100)
    portfolio.sell("AAPL", 10, 150)

    assert portfolio.realized_pnl("AAPL") == Decimal("500")
    assert portfolio.realized_pnl() == Decimal("500")


def test_realized_pnl_uses_weighted_average_cost(portfolio):
    portfolio.buy("AAPL", 10, 100)
    portfolio.buy("AAPL", 10, 200)
    # avg_price is now 150; selling half at 180 locks in (180-150)*10 = 300
    portfolio.sell("AAPL", 10, 180)

    assert portfolio.realized_pnl("AAPL") == Decimal("300")


def test_realized_pnl_nets_commission(portfolio):
    portfolio.buy("AAPL", 10, 100)
    portfolio.sell("AAPL", 10, 150, commission=10)

    assert portfolio.realized_pnl("AAPL") == Decimal("490")


def test_unrealized_pnl_reflects_current_price(portfolio):
    portfolio.buy("AAPL", 10, 100)

    assert portfolio.unrealized_pnl("AAPL", 130) == Decimal("300")
    assert portfolio.unrealized_pnl("AAPL", 90) == Decimal("-100")


def test_unrealized_pnl_zero_without_position(portfolio):
    assert portfolio.unrealized_pnl("AAPL", 100) == Decimal("0")


def test_market_value_sums_cash_and_positions(portfolio):
    portfolio.buy("AAPL", 10, 100)
    portfolio.buy("MSFT", 5, 200)

    value = portfolio.market_value({"AAPL": 120, "MSFT": 210})

    # cash left: 10000 - 1000 - 1000 = 8000
    # positions: 10*120 + 5*210 = 1200 + 1050 = 2250
    assert value == Decimal("8000") + Decimal("2250")


def test_market_value_ignores_symbols_without_a_quote(portfolio):
    portfolio.buy("AAPL", 10, 100)

    value = portfolio.market_value({})

    assert value == portfolio.cash


def test_trade_log_is_append_only_history(portfolio):
    portfolio.buy("AAPL", 10, 100)
    portfolio.sell("AAPL", 4, 120)

    trades = portfolio.trades()
    assert len(trades) == 2
    assert trades[0].side.value == "SELL"
    assert trades[1].side.value == "BUY"


def test_symbols_are_case_insensitive(portfolio):
    portfolio.buy("aapl", 10, 100)

    position = portfolio.get_position("AAPL")
    assert position is not None
    assert position.quantity == Decimal("10")


def test_buy_rejects_non_positive_quantity(portfolio):
    with pytest.raises(ValueError):
        portfolio.buy("AAPL", 0, 100)


def test_buy_rejects_non_positive_price(portfolio):
    with pytest.raises(ValueError):
        portfolio.buy("AAPL", 1, 0)


# --- fractional quantities (no whole-share rounding anywhere in Portfolio) --


def test_fractional_buy_recomputes_average_price_and_full_sale_realizes_pnl(portfolio):
    portfolio.buy("AAPL", Decimal("0.4"), Decimal("100"))
    portfolio.buy("AAPL", Decimal("0.6"), Decimal("150"))

    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("1.0")
    # weighted avg: (0.4*100 + 0.6*150) / 1.0 = (40 + 90) / 1.0 = 130
    assert position.avg_price == Decimal("130")
    assert portfolio.cash == Decimal("10000") - Decimal("40") - Decimal("90")
    assert portfolio.unrealized_pnl("AAPL", Decimal("140")) == Decimal("10")

    portfolio.sell("AAPL", Decimal("1.0"), Decimal("140"))

    assert portfolio.cash == Decimal("10000") - Decimal("40") - Decimal("90") + Decimal("140")
    assert portfolio.realized_pnl("AAPL") == Decimal("10")
    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("0")


def test_partial_fractional_sell_leaves_the_remainder_at_the_same_avg_price(portfolio):
    portfolio.buy("AAPL", Decimal("1"), Decimal("100"))
    portfolio.sell("AAPL", Decimal("0.5"), Decimal("120"))

    assert portfolio.cash == Decimal("10000") - Decimal("100") + Decimal("60")
    position = portfolio.get_position("AAPL")
    assert position.quantity == Decimal("0.5")
    assert position.avg_price == Decimal("100")
    assert portfolio.realized_pnl("AAPL") == Decimal("10")


def test_sub_share_quantity_like_0_000001_is_not_rounded_away(portfolio):
    tiny = Decimal("0.000001")
    portfolio.buy("AAPL", tiny, Decimal("100"))

    position = portfolio.get_position("AAPL")
    assert position.quantity == tiny
    assert portfolio.cash == Decimal("10000") - tiny * Decimal("100")
