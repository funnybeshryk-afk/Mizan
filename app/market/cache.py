from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import CachedDay, Instrument, PriceBar
from app.market.provider import Bar, MarketDataProvider


def _to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def _uses_bar_presence_tracking(timeframe: str) -> bool:
    """"Day" is the one granularity where a single bar's own date is both
    necessary and sufficient proof the whole day is covered (1 bar IS the
    whole day) - and it's the only granularity ever requested in
    production today (daemon.py always uses "1Day"). Every other unit
    (Minute/Hour/Week/Month) can have many bars covering, or one bar
    spanning much more than, a single calendar day, so bar presence alone
    can't answer "is this day covered" - those use the CachedDay ledger
    instead (see _days_covered_by_ledger).
    """
    return timeframe.endswith("Day")


def _group_contiguous_dates(days: list[date]) -> list[tuple[date, date]]:
    if not days:
        return []
    days = sorted(days)
    ranges: list[tuple[date, date]] = []
    range_start = prev = days[0]
    for day in days[1:]:
        if (day - prev).days > 1:
            ranges.append((range_start, prev))
            range_start = day
        prev = day
    ranges.append((range_start, prev))
    return ranges


class CachedMarketDataProvider(MarketDataProvider):
    """Wraps another MarketDataProvider and caches get_bars() results in
    PriceBar, keyed by (instrument, timeframe, day). A request only reaches
    the upstream provider for calendar days not already cached; get_latest_price
    always passes straight through since "latest" is only meaningful live.

    Coverage tracking depends on the timeframe's granularity:

    - "Day": a day is considered cached once a PriceBar row exists for it,
      since one bar IS the whole day at this granularity. This is the
      original, simplest check, and is exactly what every bot in
      production uses today (daemon.py always requests "1Day") - left
      completely unchanged so daily behaviour is guaranteed identical to
      before this fix.
    - Everything else (Minute/Hour/Week/Month): a day can hold many bars,
      or a single bar can span more than a day, so "a bar exists" does not
      mean the whole day was fetched. These are tracked via a separate
      CachedDay ledger row per (instrument, timeframe, day), written only
      after that day's upstream fetch has completed - so a day that only
      got partially stored (e.g. an interrupted fetch, or a handful of
      bars left over from before this fix) is correctly re-fetched rather
      than mistaken for complete. Storing is dedupe-safe (by timestamp), so
      re-fetching a day that already has some bars tops up the missing
      ones instead of erroring on a duplicate or discarding what's there.

      Today's date is never written to the ledger, even after a fetch for
      it completes without error: an in-progress trading session keeps
      producing new bars as the day goes on, so "the fetch completed" does
      not mean "the day is done" the way it does for a past day. Every
      request that touches today re-hits upstream for it (dedupe in
      _store keeps this idempotent) until the calendar rolls over and it
      stops being today - at which point the next fetch for it is finally
      allowed to mark it covered.
    """

    def __init__(self, session: Session, upstream: MarketDataProvider):
        self.session = session
        self.upstream = upstream

    def get_latest_price(self, symbol: str) -> Decimal:
        return self.upstream.get_latest_price(symbol)

    def get_bars(self, symbol: str, start: date, end: date, timeframe: str = "1Day") -> list[Bar]:
        if start > end:
            raise ValueError("start must not be after end")

        instrument = self._get_or_create_instrument(symbol)
        bar_presence_tracking = _uses_bar_presence_tracking(timeframe)

        cached_rows: list[PriceBar] | None = None
        if bar_presence_tracking:
            cached_rows = self._load_cached_rows(instrument.id, timeframe, start, end)
            cached_days = {row.timestamp.date() for row in cached_rows}
        else:
            cached_days = self._days_covered_by_ledger(instrument.id, timeframe, start, end)

        expected_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        missing_days = [day for day in expected_days if day not in cached_days]

        for gap_start, gap_end in _group_contiguous_dates(missing_days):
            fetched = self.upstream.get_bars(symbol, gap_start, gap_end, timeframe)
            self._store(instrument.id, timeframe, fetched)
            if not bar_presence_tracking:
                self._mark_ledger_covered(instrument.id, timeframe, gap_start, gap_end)
            self.session.commit()

        if missing_days or cached_rows is None:
            cached_rows = self._load_cached_rows(instrument.id, timeframe, start, end)

        return [self._row_to_bar(row) for row in sorted(cached_rows, key=lambda r: r.timestamp)]

    def _get_or_create_instrument(self, symbol: str) -> Instrument:
        symbol = symbol.upper().strip()
        instrument = self.session.execute(
            select(Instrument).where(Instrument.symbol == symbol)
        ).scalar_one_or_none()
        if instrument is None:
            instrument = Instrument(symbol=symbol, asset_class="US_EQUITY")
            self.session.add(instrument)
            self.session.flush()
        return instrument

    def _days_covered_by_ledger(
        self, instrument_id: int, timeframe: str, start: date, end: date
    ) -> set[date]:
        rows = self.session.execute(
            select(CachedDay.day).where(
                CachedDay.instrument_id == instrument_id,
                CachedDay.timeframe == timeframe,
                CachedDay.day >= start,
                CachedDay.day <= end,
            )
        ).scalars()
        return set(rows)

    def _mark_ledger_covered(
        self, instrument_id: int, timeframe: str, gap_start: date, gap_end: date
    ) -> None:
        today = date.today()
        for i in range((gap_end - gap_start).days + 1):
            day = gap_start + timedelta(days=i)
            if day >= today:
                continue  # an in-progress (or future) day is never permanently "covered"
            self.session.add(CachedDay(instrument_id=instrument_id, timeframe=timeframe, day=day))

    def _load_cached_rows(
        self, instrument_id: int, timeframe: str, start: date, end: date
    ) -> list[PriceBar]:
        range_start = datetime.combine(start, datetime.min.time())
        range_end = datetime.combine(end, datetime.max.time())
        return list(
            self.session.execute(
                select(PriceBar).where(
                    PriceBar.instrument_id == instrument_id,
                    PriceBar.timeframe == timeframe,
                    PriceBar.timestamp >= range_start,
                    PriceBar.timestamp <= range_end,
                )
            ).scalars()
        )

    def _store(self, instrument_id: int, timeframe: str, bars: list[Bar]) -> None:
        """Inserts bars, skipping any timestamp already stored for this
        instrument+timeframe. The skip check (rather than an unconditional
        insert) matters once a day can be re-fetched while already
        partially stored - without it, re-fetching a partially cached
        intraday day would crash on the PriceBar unique constraint instead
        of topping up the missing bars.
        """
        if not bars:
            return

        normalized = [(_to_utc_naive(bar.timestamp), bar) for bar in bars]
        range_start = min(ts for ts, _ in normalized)
        range_end = max(ts for ts, _ in normalized)
        existing_timestamps = set(
            self.session.execute(
                select(PriceBar.timestamp).where(
                    PriceBar.instrument_id == instrument_id,
                    PriceBar.timeframe == timeframe,
                    PriceBar.timestamp >= range_start,
                    PriceBar.timestamp <= range_end,
                )
            ).scalars()
        )

        for ts, bar in normalized:
            if ts in existing_timestamps:
                continue
            self.session.add(
                PriceBar(
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    timestamp=ts,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
            )

    @staticmethod
    def _row_to_bar(row: PriceBar) -> Bar:
        return Bar(
            timestamp=row.timestamp.replace(tzinfo=timezone.utc),
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
