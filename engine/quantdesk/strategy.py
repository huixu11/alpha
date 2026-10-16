"""The rule: entries, exits, precedence, ranking, capacity, bucket cap.

Pure functions and immutable value objects. Nothing in this module reads a
file, writes a file, or knows what time it is. strategy_spec.md sections 4 to 8.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
import pandas as pd

from quantdesk import config
from quantdesk.calendar import session_index
from quantdesk.indicators import (
    rolling_median_dollar_volume,
    rsi_wilder,
    trend_mean,
)
from quantdesk.universe import BUCKET_OF


# --------------------------------------------------------------------------
# Per-ticker derived series
# --------------------------------------------------------------------------


class TickerSeries:
    """Bars of one ticker from `rsi_start` forward, with derived series.

    Bar indices are one-based in the spec; this class stores them zero-based
    and exposes `bar_number` where the distinction matters. Bar 1 is the
    ticker's first cached bar on or after `rsi_start` (section 4), so leading
    cache bars before `rsi_start` cannot change any value here.
    """

    __slots__ = (
        "ticker", "dates", "open", "high", "low", "close", "volume",
        "rsi", "trend", "dollar_volume", "_pos",
    )

    def __init__(self, ticker: str, bars: pd.DataFrame, rsi_start: dt.date):
        usable = bars[bars.index >= pd.Timestamp(rsi_start)]
        self.ticker = ticker
        self.dates = [d.date() for d in usable.index]
        self.open = usable["open"].to_numpy(dtype=np.float64)
        self.high = usable["high"].to_numpy(dtype=np.float64)
        self.low = usable["low"].to_numpy(dtype=np.float64)
        self.close = usable["close"].to_numpy(dtype=np.float64)
        self.volume = usable["volume"].to_numpy(dtype=np.float64)
        self.rsi = rsi_wilder(self.close)
        self.trend = trend_mean(self.close)
        self.dollar_volume = rolling_median_dollar_volume(self.close, self.volume)
        self._pos = {d: i for i, d in enumerate(self.dates)}

    def __len__(self) -> int:
        return len(self.dates)

    def has_bar(self, session: dt.date) -> bool:
        return session in self._pos

    def pos(self, session: dt.date) -> int | None:
        return self._pos.get(session)

    def bar_number(self, session: dt.date) -> int | None:
        """One-based bar index of `session`, or None when there is no bar."""
        p = self._pos.get(session)
        return None if p is None else p + 1

    def last_pos_on_or_before(self, session: dt.date) -> int | None:
        """Position of the most recent bar with date `<= session`."""
        lo, hi = 0, len(self.dates) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.dates[mid] <= session:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def last_close_on_or_before(self, session: dt.date) -> tuple[dt.date, float] | None:
        p = self.last_pos_on_or_before(session)
        if p is None:
            return None
        return self.dates[p], float(self.close[p])

    # -- section 4: RSI is defined only with 250 bars from rsi_start --------

    def rsi_defined(self, session: dt.date) -> bool:
        p = self._pos.get(session)
        if p is None:
            return False
        if p + 1 < config.MIN_HISTORY_BARS:
            return False
        return not np.isnan(self.rsi[p])

    def rsi_at(self, session: dt.date) -> float | None:
        if not self.rsi_defined(session):
            return None
        return float(self.rsi[self._pos[session]])

    def close_at(self, session: dt.date) -> float | None:
        p = self._pos.get(session)
        return None if p is None else float(self.close[p])

    def open_at(self, session: dt.date) -> float | None:
        p = self._pos.get(session)
        return None if p is None else float(self.open[p])


class MarketData:
    """Every ticker's series, keyed by ticker."""

    def __init__(self, series: dict[str, TickerSeries]):
        self.series = series

    def __contains__(self, ticker: str) -> bool:
        return ticker in self.series

    def get(self, ticker: str) -> TickerSeries | None:
        return self.series.get(ticker)

    @classmethod
    def from_frames(
        cls,
        frames: dict[str, pd.DataFrame],
        rsi_start: dict[str, dt.date] | dt.date,
    ) -> "MarketData":
        if isinstance(rsi_start, dt.date):
            starts = {t: rsi_start for t in frames}
        else:
            starts = rsi_start
        return cls(
            {
                ticker: TickerSeries(ticker, frame, starts[ticker])
                for ticker, frame in frames.items()
                if ticker in starts
            }
        )


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    ticker: str
    bucket: str
    rsi_prev: float
    rsi: float
    close: float


@dataclass(frozen=True)
class Declined:
    ticker: str
    reason: str
    bucket: str
    rsi_prev: float | None
    rsi: float | None


@dataclass
class Order:
    side: str                     # BUY or SELL
    ticker: str
    shares: int
    rule: str
    rules: list[str]
    rsi: float | None = None
    rsi_prev: float | None = None
    rank: int | None = None
    close_used: float | None = None
    alloc: float | None = None
    avail_before: float | None = None
    session: dt.date | None = None          # session whose call file published it
    execute_on: dt.date | None = None       # intended execution session
    flags: list[str] = field(default_factory=list)


@dataclass
class Position:
    ticker: str
    shares: int
    fill_session: dt.date
    fill_price: float
    entry_commission: float = 0.0
    flags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Section 5: entry candidacy
# --------------------------------------------------------------------------


def is_entry_candidate(
    series: TickerSeries | None,
    session: dt.date,
    prev_session: dt.date,
    held: bool,
    in_cooldown: bool,
    has_pending_order: bool,
    crosscheck_warned: bool,
    corporate_action: bool,
    trend_filter: bool = True,
    floors: bool = True,
) -> tuple[bool, str | None]:
    """Section 5 conditions 1 to 12, in the spec's order.

    Returns `(is_candidate, first_failing_condition)`. The failure name is for
    logging only; a ticker that is not a candidate produces no `declined` row,
    because `declined` records candidates that lost, not tickers that never
    qualified.
    """
    if series is None:
        return False, "NO_SERIES"
    # 1. RSI[T] and RSI[T-1] both defined
    if not series.rsi_defined(session) or not series.rsi_defined(prev_session):
        return False, "RSI_UNDEFINED"
    rsi_prev = series.rsi_at(prev_session)
    rsi_now = series.rsi_at(session)
    # 2 and 3. the cross-up
    if not rsi_prev < config.OVERSOLD_THRESHOLD:
        return False, "RSI_PREV_NOT_OVERSOLD"
    if not rsi_now >= config.OVERSOLD_THRESHOLD:
        return False, "RSI_NOT_RECOVERED"
    p = series.pos(session)
    # 4. trend filter (`trend_filter=False` is the section 16.2 ablation)
    if trend_filter:
        trend = series.trend[p]
        if np.isnan(trend) or not series.close[p] >= trend:
            return False, "TREND_FILTER"
    # 5 and 6. price and liquidity floors (`floors=False` is the other ablation)
    if floors:
        if not series.close[p] >= config.MIN_PRICE:
            return False, "PRICE_FLOOR"
        dvol = series.dollar_volume[p]
        if np.isnan(dvol) or not dvol >= config.MIN_MEDIAN_DOLLAR_VOLUME:
            return False, "LIQUIDITY_FLOOR"
    # 7. not held
    if held:
        return False, "HELD"
    # 8. not in cooldown
    if in_cooldown:
        return False, "COOLDOWN"
    # 9. bar for T and T-1 (implied by conditions 1 and 3, restated)
    if not series.has_bar(session) or not series.has_bar(prev_session):
        return False, "NO_BAR"
    # 10. no published order still open
    if has_pending_order:
        return False, "PENDING_ORDER"
    # 11. not CROSSCHECK_WARN for T
    if crosscheck_warned:
        return False, "CROSSCHECK_WARN"
    # 12. not CORPORATE_ACTION for T
    if corporate_action:
        return False, "CORPORATE_ACTION"
    return True, None


def collect_candidates(
    market: MarketData,
    included: list[str],
    session: dt.date,
    prev_session: dt.date,
    held: set[str],
    cooldown: dict[str, dt.date],
    pending: set[str],
    warned: set[str],
    corporate_action: set[str],
    trend_filter: bool = True,
    floors: bool = True,
) -> list[Candidate]:
    """Every section 5 entry candidate, unranked."""
    out: list[Candidate] = []
    for ticker in sorted(included):
        series = market.get(ticker)
        ok, _ = is_entry_candidate(
            series,
            session,
            prev_session,
            held=ticker in held,
            in_cooldown=in_cooldown(ticker, session, cooldown),
            has_pending_order=ticker in pending,
            crosscheck_warned=ticker in warned,
            corporate_action=ticker in corporate_action,
            trend_filter=trend_filter,
            floors=floors,
        )
        if not ok:
            continue
        out.append(
            Candidate(
                ticker=ticker,
                bucket=BUCKET_OF[ticker],
                rsi_prev=series.rsi_at(prev_session),
                rsi=series.rsi_at(session),
                close=series.close_at(session),
            )
        )
    return out


def in_cooldown(ticker: str, session: dt.date, cooldown: dict[str, dt.date]) -> bool:
    """Section 4: `1 <= index(T) - index(exit fill session) <= 5`."""
    started = cooldown.get(ticker)
    if started is None:
        return False
    gap = session_index(session) - session_index(started)
    return 1 <= gap <= config.COOLDOWN_SESSIONS


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Section 7: by `RSI[T-1]` ascending, then ticker ascending."""
    return sorted(candidates, key=lambda c: (c.rsi_prev, c.ticker))


# --------------------------------------------------------------------------
# Section 6.2: the exit table
# --------------------------------------------------------------------------


def stop_trigger_price(fill_price: float) -> Decimal:
    """`(Decimal(str(fill_price)) * Decimal("0.93")).quantize(0.0001, HALF_UP)`."""
    from decimal import ROUND_HALF_UP

    return (Decimal(str(fill_price)) * Decimal(config.STOP_FRACTION)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def exit_conditions(
    position: Position,
    session: dt.date,
    close_at_t: float | None,
    rsi_at_t: float | None,
    missing_ok_streak: int,
) -> list[str]:
    """Which of DATA, STOP, TARGET, TIME hold, in precedence order.

    `close_at_t` is None when the ticker has no bar at T. Section 6.4 (v1.2.0)
    gives one rule for that case, whatever the session status: the position is
    marked at its last available close and its exit check is recorded
    `SKIPPED_NO_DATA`, so no price or RSI condition is evaluated. `DATA` is
    still evaluated, from the missing-bar counter alone: it can only ever hold
    on a session with no bar, so excluding it would make a `DATA` exit
    unreachable and the write-off of section 6.1 dead code. See
    docs/spec_issues.md, Phase A, item 14.
    """
    held: list[str] = []
    if missing_ok_streak >= config.MISSING_DATA_EXIT_SESSIONS:
        held.append("DATA")
    if close_at_t is not None:
        if Decimal(str(close_at_t)) <= stop_trigger_price(position.fill_price):
            held.append("STOP")
        if rsi_at_t is not None and rsi_at_t >= config.TARGET_THRESHOLD:
            held.append("TARGET")
        n = sessions_held(position.fill_session, session)
        if n >= config.TIME_STOP_SESSIONS:
            held.append("TIME")
    return [code for code in config.EXIT_PRECEDENCE if code in held]


def sessions_held(fill_session: dt.date, session: dt.date) -> int:
    """Section 4: `index(T) - index(E) + 1`."""
    return session_index(session) - session_index(fill_session) + 1


# --------------------------------------------------------------------------
# Section 7: capacity and the bucket cap
# --------------------------------------------------------------------------


def compute_slots(
    held: dict[str, Position],
    exiting: set[str],
    pending_entries: set[str],
) -> int:
    """Section 7's `slots`."""
    holding_through = sum(1 for t in held if t not in exiting)
    return config.MAX_POSITIONS - holding_through - len(pending_entries)


def bucket_counts(
    held: dict[str, Position],
    exiting: set[str],
    pending_entries: set[str],
) -> dict[str, int]:
    """Held positions not exiting, plus published entry orders still open."""
    counts: dict[str, int] = {b: 0 for b in set(BUCKET_OF.values())}
    for ticker in held:
        if ticker in exiting:
            continue
        counts[BUCKET_OF[ticker]] += 1
    for ticker in pending_entries:
        counts[BUCKET_OF[ticker]] += 1
    return counts
