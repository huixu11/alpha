"""The provider interface (strategy_spec.md section 3).

The primary provider returns *raw* daily OHLCV bars plus `split_factor` and
`div_cash`. The system builds the split-adjusted-only bars itself, in
`split_adjust` below, exactly as section 3 `Bar construction` states; no
provider's own adjusted column is ever used for a price. Dividends are not
reflected in the bars; they are booked as cash (section 8).
"""

from __future__ import annotations

import abc
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP, localcontext

import pandas as pd

BARS_COLUMNS = ("open", "high", "low", "close", "volume")

# What the cache stores, per section 3 `Bar construction`: the raw fields and
# `splitFactor`, never the adjusted values, so the construction can be redone
# from the cache alone after an incremental fetch. `div_cash` rides along
# because section 8 books it as a cash event and section 3 forbids it touching
# a price.
RAW_COLUMNS = (*BARS_COLUMNS, "split_factor", "div_cash")

# Section 3 `Bar construction`: 28 significant digits.
DECIMAL_PRECISION = 28


class ProviderError(RuntimeError):
    """The provider failed to answer, or answered with something that is not data.

    A response whose shape is wrong is a provider error, never missing data
    (BUILD_PROMPT.md, Phase A). Three or more provider errors in one fetch
    means the session status is ERROR.
    """

    def __init__(self, provider: str, ticker: str, detail: str):
        self.provider = provider
        self.ticker = ticker
        self.detail = detail
        super().__init__(f"{provider}:{ticker}: {detail}")


def empty_bars() -> pd.DataFrame:
    frame = pd.DataFrame(columns=list(BARS_COLUMNS), dtype="float64")
    frame.index = pd.DatetimeIndex([], name="date")
    return frame


def normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Put a provider frame into the canonical shape.

    Index is a naive `DatetimeIndex` of session dates named `date`, sorted
    ascending and free of duplicates; columns are exactly `BARS_COLUMNS` as
    float64. Session dates carry no time component: a bar belongs to a session,
    not to an instant.
    """
    if frame is None or len(frame) == 0:
        return empty_bars()
    out = frame.copy()
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index)).tz_localize(None).normalize()
    out.index.name = "date"
    missing = [c for c in BARS_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"provider frame is missing columns {missing}")
    out = out[list(BARS_COLUMNS)].astype("float64")
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def empty_raw() -> pd.DataFrame:
    frame = pd.DataFrame(columns=list(RAW_COLUMNS), dtype="float64")
    frame.index = pd.DatetimeIndex([], name="date")
    return frame


def normalize_raw(frame: pd.DataFrame) -> pd.DataFrame:
    """Put a raw provider frame into the canonical raw shape.

    A frame that carries no `split_factor` is read as a history with no split
    (every factor 1.0) and no dividend, which is what a source that publishes
    only OHLCV means. That keeps `split_adjust` an identity on such a frame
    rather than a failure.
    """
    if frame is None or len(frame) == 0:
        return empty_raw()
    out = frame.copy()
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index)).tz_localize(None).normalize()
    out.index.name = "date"
    missing = [c for c in BARS_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"provider frame is missing columns {missing}")
    if "split_factor" not in out.columns:
        out["split_factor"] = 1.0
    if "div_cash" not in out.columns:
        out["div_cash"] = 0.0
    out = out[list(RAW_COLUMNS)].astype("float64")
    out["split_factor"] = out["split_factor"].fillna(1.0)
    out["div_cash"] = out["div_cash"].fillna(0.0)
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def split_adjust(raw: pd.DataFrame) -> pd.DataFrame:
    """Section 3 `Bar construction`: split-adjusted-only bars from raw fields.

    For sessions `s_1 < ... < s_L`, `f[s_L] = 1` and, for `i < L`,
    `f[s_i] = splitFactor[s_{i+1}] * ... * splitFactor[s_L]`, the product taken
    in ascending session order over `decimal.Decimal` at 28 significant digits
    and converted once to double. Prices are divided by `f`, volume is
    multiplied by it and rounded to the nearest integer half-up. A 2-for-1
    split at `s_k` therefore halves every price before `s_k` and doubles their
    volume, so position value across the split is unchanged.

    `div_cash` never touches a price (section 3, `Fields not used`).

    Section 3 says the factors are "parsed from the provider's decimal
    strings". They reach here as float64, because that is what the parquet
    cache stores, and `Decimal(str(value))` is what recovers them: `str` of a
    float is the shortest decimal that round-trips, so the Decimal is the exact
    value the double holds. A split factor is a small ratio of whole
    numbers — 2, 4, 10, 1.5, 0.1 for a one-for-ten reverse — and every one of
    those survives the round trip unchanged.
    """
    raw = normalize_raw(raw)
    if raw.empty:
        return empty_bars()
    factors_raw = raw["split_factor"].tolist()
    n = len(factors_raw)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        one = Decimal(1)
        # A session with no split carries a factor of exactly 1, which is an
        # exact identity under Decimal multiplication at any precision. Only
        # the sessions that actually split enter a product, so `f` can be taken
        # in ascending session order, as section 3 requires, without an O(n^2)
        # walk over a history in which splits are rare.
        splits = [
            (i, Decimal(str(v)))
            for i, v in enumerate(factors_raw)
            if Decimal(str(v)) != one
        ]
        memo: dict[int, Decimal] = {}

        def suffix_product(first: int) -> Decimal:
            """Product of `splits[first:]` taken in ascending session order."""
            if first not in memo:
                acc = one
                for _, value in splits[first:]:
                    acc = acc * value
                memo[first] = acc
            return memo[first]

        suffix: list[Decimal] = [one] * n
        # `first` is the position in `splits` of the earliest split that falls
        # *after* session i, so `suffix_product(first)` is exactly
        # `splitFactor[s_{i+1}] * ... * splitFactor[s_L]`.
        first = len(splits)
        for i in range(n - 1, -1, -1):
            while first > 0 and splits[first - 1][0] > i:
                first -= 1
            suffix[i] = suffix_product(first)
        factors = [float(f) for f in suffix]
        raw_volume = raw["volume"].tolist()
        volumes: list[float] = []
        for i, value in enumerate(raw_volume):
            if suffix[i] == one and float(value).is_integer():
                # f is exactly 1 and the volume is already whole, so the
                # multiplication and the half-up rounding are both identities.
                # Most sessions of most tickers land here, and the Decimal work
                # below is what a full-history reconstruction spends its time
                # on, so the shortcut is worth stating.
                volumes.append(float(value))
                continue
            volumes.append(
                float(
                    (Decimal(str(value)) * suffix[i]).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
            )
    out = pd.DataFrame(index=raw.index.copy())
    out.index.name = "date"
    unadjusted = not splits
    for column in ("open", "high", "low", "close"):
        values = raw[column].tolist()
        # Dividing by an exact 1.0 is the identity in IEEE-754, so a history
        # with no split is copied rather than walked.
        out[column] = values if unadjusted else [
            value / factor for value, factor in zip(values, factors)
        ]
    out["volume"] = volumes
    return normalize_bars(out)


class Provider(abc.ABC):
    """Daily bars for one ticker at a time."""

    name: str = "base"

    @abc.abstractmethod
    def fetch_daily(self, ticker: str, start: dt.date) -> pd.DataFrame:
        """Bars for `ticker` with session date `>= start`, canonical shape.

        Raises `ProviderError` when the provider answered with something that
        is not data. Returns an empty frame only when the provider genuinely
        has no bars for the ticker in the window.
        """

    def close(self) -> None:  # pragma: no cover - default is a no-op
        return None
