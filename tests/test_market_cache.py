from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import PriceBar
from app.database.session import get_engine, get_session_factory
from app.market.cache import CachedMarketDataProvider, _uses_bar_presence_tracking
from app.market.provider import AuthenticationError, Bar, MarketDataProvider, NetworkError


class FakeMarketDataProvider(MarketDataProvider):
    """No-network stand-in for MarketDataProvider. Records every get_bars
    call so tests can assert exactly which range was (not) re-fetched."""

    def __init__(self, latest_price=Decimal("100"), raise_on_bars=None, raise_on_price=None):
        self.calls: list[tuple[str, date, date, str]] = []
        self.latest_price = latest_price
        self.raise_on_bars = raise_on_bars
        self.raise_on_price = raise_on_price

    def get_latest_price(self, symbol):
        if self.raise_on_price is not None:
            raise self.raise_on_price
        return self.latest_price

    def get_bars(self, symbol, start, end, timeframe="1Day"):
        self.calls.append((symbol, start, end, timeframe))
        if self.raise_on_bars is not None:
            raise self.raise_on_bars

        bars = []
        day = start
        while day <= end:
            bars.append(
                Bar(
                    timestamp=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100.5"),
                    volume=1000,
                )
            )
            day += timedelta(days=1)
        return bars


class FakeIntradayMarketDataProvider(MarketDataProvider):
    """Like FakeMarketDataProvider, but returns a fixed multi-bar trading
    session per day (09:30, 12:00, 15:59 UTC) instead of one bar per
    calendar day - so tests can tell "the whole day's bars" apart from
    "just the first one that happened to get stored"."""

    SESSION_TIMES = [(9, 30), (12, 0), (15, 59)]

    def __init__(self):
        self.calls: list[tuple[str, date, date, str]] = []

    def get_latest_price(self, symbol):
        raise NotImplementedError

    def get_bars(self, symbol, start, end, timeframe="1Day"):
        self.calls.append((symbol, start, end, timeframe))
        bars = []
        day = start
        while day <= end:
            for hour, minute in self.SESSION_TIMES:
                bars.append(
                    Bar(
                        timestamp=datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc),
                        open=Decimal("100"),
                        high=Decimal("101"),
                        low=Decimal("99"),
                        close=Decimal("100.5"),
                        volume=100,
                    )
                )
            day += timedelta(days=1)
        return bars


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    session_factory = get_session_factory(engine)
    s = session_factory()
    yield s
    s.close()


def test_get_bars_fetches_and_caches_full_range(session):
    fake = FakeMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)

    bars = provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 5))

    assert len(bars) == 5
    assert len(fake.calls) == 1
    assert fake.calls[0] == ("AAPL", date(2024, 1, 1), date(2024, 1, 5), "1Day")


def test_repeated_request_does_not_hit_upstream_again(session):
    fake = FakeMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)

    provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 5))
    bars_again = provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 5))

    assert len(bars_again) == 5
    assert len(fake.calls) == 1


def test_extended_range_only_fetches_missing_days(session):
    fake = FakeMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)

    provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 5))
    bars = provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 10))

    assert len(bars) == 10
    assert len(fake.calls) == 2
    assert fake.calls[1] == ("AAPL", date(2024, 1, 6), date(2024, 1, 10), "1Day")


def test_gap_in_the_middle_fetches_only_that_gap(session):
    fake = FakeMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)

    provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 3))
    provider.get_bars("AAPL", date(2024, 1, 8), date(2024, 1, 10))
    bars = provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 10))

    assert len(bars) == 10
    assert len(fake.calls) == 3
    assert fake.calls[2] == ("AAPL", date(2024, 1, 4), date(2024, 1, 7), "1Day")


def test_different_symbols_cached_independently(session):
    fake = FakeMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)

    provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 3))
    provider.get_bars("MSFT", date(2024, 1, 1), date(2024, 1, 3))

    assert len(fake.calls) == 2


def test_get_latest_price_passes_through_without_caching(session):
    fake = FakeMarketDataProvider(latest_price=Decimal("142.50"))
    provider = CachedMarketDataProvider(session, fake)

    assert provider.get_latest_price("AAPL") == Decimal("142.50")


def test_get_bars_propagates_authentication_error(session):
    fake = FakeMarketDataProvider(raise_on_bars=AuthenticationError("bad key"))
    provider = CachedMarketDataProvider(session, fake)

    with pytest.raises(AuthenticationError):
        provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 3))


def test_failed_fetch_caches_nothing_so_it_is_retried_later(session):
    fake = FakeMarketDataProvider(raise_on_bars=NetworkError("timeout"))
    provider = CachedMarketDataProvider(session, fake)

    with pytest.raises(NetworkError):
        provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 3))

    fake.raise_on_bars = None
    bars = provider.get_bars("AAPL", date(2024, 1, 1), date(2024, 1, 3))

    assert len(bars) == 3
    assert len(fake.calls) == 2  # the failed call plus the successful retry


def test_get_latest_price_propagates_network_error(session):
    fake = FakeMarketDataProvider(raise_on_price=NetworkError("no network"))
    provider = CachedMarketDataProvider(session, fake)

    with pytest.raises(NetworkError):
        provider.get_latest_price("AAPL")


def test_bar_presence_tracking_is_used_only_for_day_granularity():
    assert _uses_bar_presence_tracking("1Day") is True
    assert _uses_bar_presence_tracking("1Min") is False
    assert _uses_bar_presence_tracking("5Min") is False
    assert _uses_bar_presence_tracking("1Hour") is False
    assert _uses_bar_presence_tracking("1Week") is False
    assert _uses_bar_presence_tracking("1Month") is False


def test_minute_bars_top_up_a_day_that_only_has_its_first_bar_stored(session):
    # Reproduces the bug this fix addresses: under the old bar-presence
    # check, one stored minute bar was enough to make the whole day look
    # cached, so the other ~389 minute bars of the day were never fetched.
    fake = FakeIntradayMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)
    day = date(2024, 3, 4)

    instrument = provider._get_or_create_instrument("AAPL")
    session.add(
        PriceBar(
            instrument_id=instrument.id,
            timeframe="1Min",
            timestamp=datetime(2024, 3, 4, 9, 30),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=100,
        )
    )
    session.commit()

    bars = provider.get_bars("AAPL", day, day, "1Min")

    assert len(fake.calls) == 1  # not in the ledger yet, so upstream was hit
    assert len(bars) == 3  # all three session bars, not just the pre-existing one

    bars_again = provider.get_bars("AAPL", day, day, "1Min")

    assert len(fake.calls) == 1  # now covered by the ledger, no repeat fetch
    assert len(bars_again) == 3


def test_partially_cached_intraday_day_is_topped_up_without_duplicating_existing_bars(session):
    # Boundary case: a day with bars for part of the session (09:30-12:00)
    # but not the rest. The missing tail must be fetched and the existing
    # bars must survive unduplicated - not a whole-day re-fetch that
    # errors on the pre-existing rows, and not a no-op that leaves the
    # afternoon missing.
    fake = FakeIntradayMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)
    day = date(2024, 3, 4)

    instrument = provider._get_or_create_instrument("AAPL")
    for hour, minute in [(9, 30), (12, 0)]:
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                timeframe="1Min",
                timestamp=datetime(2024, 3, 4, hour, minute),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=100,
            )
        )
    session.commit()

    bars = provider.get_bars("AAPL", day, day, "1Min")

    assert len(fake.calls) == 1
    timestamps = sorted(b.timestamp.replace(tzinfo=None) for b in bars)
    assert timestamps == [
        datetime(2024, 3, 4, 9, 30),
        datetime(2024, 3, 4, 12, 0),
        datetime(2024, 3, 4, 15, 59),
    ]

    stored = list(
        session.execute(
            select(PriceBar).where(
                PriceBar.instrument_id == instrument.id, PriceBar.timeframe == "1Min"
            )
        ).scalars()
    )
    assert len(stored) == 3  # the two pre-existing bars were kept, not duplicated


def test_hourly_bars_use_the_ledger_so_repeated_requests_do_not_refetch(session):
    fake = FakeIntradayMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)

    provider.get_bars("AAPL", date(2024, 3, 4), date(2024, 3, 5), "1Hour")
    bars_again = provider.get_bars("AAPL", date(2024, 3, 4), date(2024, 3, 5), "1Hour")

    assert len(fake.calls) == 1
    assert len(bars_again) == 6  # 3 session bars/day * 2 days


def test_extended_intraday_range_only_fetches_missing_days(session):
    fake = FakeIntradayMarketDataProvider()
    provider = CachedMarketDataProvider(session, fake)

    provider.get_bars("AAPL", date(2024, 3, 4), date(2024, 3, 5), "1Min")
    bars = provider.get_bars("AAPL", date(2024, 3, 4), date(2024, 3, 6), "1Min")

    assert len(bars) == 9  # 3 days * 3 session bars
    assert len(fake.calls) == 2
    assert fake.calls[1] == ("AAPL", date(2024, 3, 6), date(2024, 3, 6), "1Min")


class GrowingSessionMarketDataProvider(MarketDataProvider):
    """Simulates an intraday session still in progress: each call to
    get_bars returns the next (larger) snapshot of bars for the day, as if
    more of the trading session had elapsed between polls."""

    def __init__(self, snapshots: list[list[tuple[int, int]]]):
        self.calls: list[tuple[str, date, date, str]] = []
        self._snapshots = list(snapshots)

    def get_latest_price(self, symbol):
        raise NotImplementedError

    def get_bars(self, symbol, start, end, timeframe="1Day"):
        self.calls.append((symbol, start, end, timeframe))
        times = self._snapshots.pop(0)
        return [
            Bar(
                timestamp=datetime(start.year, start.month, start.day, hour, minute, tzinfo=timezone.utc),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=100,
            )
            for hour, minute in times
        ]


def test_todays_intraday_bars_are_never_marked_permanently_covered(session):
    # The scenario this guards against: a daemon polling an intraday bot
    # once a minute. The first poll of the day fetches whatever bars exist
    # so far (session still running); if that fetch got recorded in the
    # CachedDay ledger just because it completed without error, every
    # later poll that same day would wrongly believe today was already
    # fully downloaded and would stop seeing new bars as the session
    # continues.
    today = date.today()
    fake = GrowingSessionMarketDataProvider(
        [
            [(9, 30), (9, 31)],  # first poll: session just started
            [(9, 30), (9, 31), (9, 32), (9, 33)],  # second poll: session progressed
        ]
    )
    provider = CachedMarketDataProvider(session, fake)

    first = provider.get_bars("AAPL", today, today, "1Min")
    second = provider.get_bars("AAPL", today, today, "1Min")

    assert len(fake.calls) == 2  # today is never in the ledger, so both polls hit upstream
    assert len(first) == 2
    assert len(second) == 4  # the newly-appeared bars are picked up, not hidden by a stale "covered" day
