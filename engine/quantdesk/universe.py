"""The frozen universe and bucket map (strategy_spec.md section 2).

The ticker list and the bucket map are frozen at record start. Changing either
one is a new spec version (section 14). Nothing in this module reads the
network; `build_universe` in this module takes an already-fetched frame per
ticker and decides inclusion.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

from quantdesk import config

# strategy_spec.md section 2, verbatim.
TICKERS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD",
    "INTC", "MU", "QCOM", "TXN", "ARM", "TSM", "ASML", "AMAT", "LRCX", "KLAC",
    "MRVL", "ANET", "DELL", "HPQ", "SMCI", "WDC", "STX", "SNDK", "IBM", "CSCO",
    "ORCL", "CRM", "ADBE", "NOW", "PLTR", "SNOW", "NET", "CRWD", "PANW", "DDOG",
    "SHOP", "UBER", "ABNB", "NFLX", "DIS", "COST", "WMT", "HD", "MCD", "NKE",
    "SBUX", "KO", "PEP", "PG", "JNJ", "LLY", "UNH", "PFE", "MRK", "ABBV",
    "AMGN", "GILD", "JPM", "BAC", "GS", "MS", "V", "MA", "AXP", "BRK-B", "XOM",
    "CVX", "CAT", "DE", "BA", "GE", "LMT", "RTX", "HON", "UPS", "FDX", "COIN",
    "HOOD", "MSTR", "ASTS", "IONQ", "RGTI", "QBTS", "RKLB", "CRCL", "BE", "VST",
    "CEG",
)

# strategy_spec.md section 2, the frozen bucket table.
BUCKET_MEMBERS: dict[str, tuple[str, ...]] = {
    "SEMIS_HARDWARE": (
        "NVDA", "AMD", "INTC", "MU", "QCOM", "TXN", "ARM", "TSM", "ASML",
        "AMAT", "LRCX", "KLAC", "MRVL", "AVGO", "ANET", "SMCI", "DELL", "HPQ",
        "WDC", "STX", "SNDK",
    ),
    "SOFTWARE_INTERNET": (
        "AAPL", "MSFT", "AMZN", "GOOGL", "META", "ORCL", "CRM", "ADBE", "NOW",
        "IBM", "CSCO", "PLTR", "SNOW", "NET", "CRWD", "PANW", "DDOG", "SHOP",
        "UBER", "ABNB", "NFLX",
    ),
    "CONSUMER": (
        "TSLA", "DIS", "COST", "WMT", "HD", "MCD", "NKE", "SBUX", "KO", "PEP",
        "PG",
    ),
    "HEALTHCARE": ("JNJ", "LLY", "UNH", "PFE", "MRK", "ABBV", "AMGN", "GILD"),
    "FINANCIALS": (
        "JPM", "BAC", "GS", "MS", "V", "MA", "AXP", "BRK-B", "COIN", "HOOD",
    ),
    "ENERGY_INDUSTRIALS": (
        "XOM", "CVX", "CAT", "DE", "BA", "GE", "LMT", "RTX", "HON", "UPS",
        "FDX", "VST", "CEG",
    ),
    "SPECULATIVE": (
        "MSTR", "ASTS", "IONQ", "RGTI", "QBTS", "RKLB", "CRCL", "BE",
    ),
}

BUCKET_OF: dict[str, str] = {
    ticker: bucket
    for bucket, members in BUCKET_MEMBERS.items()
    for ticker in members
}

BENCHMARK = config.BENCHMARK_TICKER

UNIVERSE_COLUMNS = (
    "ticker",
    "bucket",
    "included",
    "reason",
    "rsi_start",
    "first_cached",
    "last_cached",
    "tiingo_symbol",
    "alphavantage_symbol",
)


def _validate_bucket_map() -> None:
    """Every ticker belongs to exactly one bucket (section 2)."""
    seen: dict[str, str] = {}
    for bucket, members in BUCKET_MEMBERS.items():
        for ticker in members:
            if ticker in seen:
                raise ValueError(
                    f"{ticker} is in two buckets: {seen[ticker]} and {bucket}"
                )
            seen[ticker] = bucket
    missing = set(TICKERS) - set(seen)
    if missing:
        raise ValueError(f"tickers with no bucket: {sorted(missing)}")
    extra = set(seen) - set(TICKERS)
    if extra:
        raise ValueError(f"bucketed tickers not in the universe: {sorted(extra)}")
    if len(TICKERS) != len(set(TICKERS)):
        raise ValueError("the universe list contains a duplicate")


_validate_bucket_map()


def tiingo_symbol(ticker: str) -> str:
    """Tiingo's symbol for a universe ticker, the primary provider.

    Tiingo's ticker paths are lower case and use a hyphen for class shares, so
    `BRK-B` becomes `brk-b`.
    """
    return ticker.lower()


def alphavantage_symbol(ticker: str) -> str:
    """Alpha Vantage's symbol for a universe ticker, the cross-check provider.

    Alpha Vantage uses the upper-case US listing symbol with a hyphen for class
    shares, so `BRK-B` is unchanged.
    """
    return ticker.upper()


@dataclass(frozen=True)
class UniverseRow:
    ticker: str
    bucket: str
    included: bool
    reason: str
    rsi_start: str
    first_cached: str
    last_cached: str
    tiingo_symbol: str
    alphavantage_symbol: str

    def as_csv_row(self) -> dict[str, str]:
        row = asdict(self)
        row["included"] = "true" if self.included else "false"
        return row


def build_universe(
    frames: dict[str, pd.DataFrame],
    rsi_start: dt.date,
    listing_dates: dict[str, dt.date] | None = None,
) -> list[UniverseRow]:
    """Decide inclusion for every universe ticker.

    `frames` maps ticker to a daily bar frame indexed by session date (the
    output of `providers.base.Provider.fetch_daily`). A ticker is included only
    if the provider returns at least 250 bars of history at build time
    (section 2). A ticker whose first cached bar precedes its current listing
    date is excluded; `listing_dates` supplies those dates when the operator has
    them, and absent entries are not checked.
    """
    listing_dates = listing_dates or {}
    rows: list[UniverseRow] = []
    for ticker in sorted(TICKERS):
        frame = frames.get(ticker)
        tiingo = tiingo_symbol(ticker)
        alphavantage = alphavantage_symbol(ticker)
        if frame is None or frame.empty:
            rows.append(
                UniverseRow(
                    ticker, BUCKET_OF[ticker], False, "NO_DATA",
                    rsi_start.isoformat(), "", "", tiingo, alphavantage,
                )
            )
            continue
        usable = frame[frame.index >= pd.Timestamp(rsi_start)]
        first = frame.index[0].date()
        last = frame.index[-1].date()
        reason = ""
        included = True
        if len(usable) < config.MIN_HISTORY_BARS:
            included = False
            reason = f"INSUFFICIENT_HISTORY:{len(usable)}<{config.MIN_HISTORY_BARS}"
        listed = listing_dates.get(ticker)
        if included and listed is not None and first < listed:
            included = False
            reason = f"FIRST_BAR_{first.isoformat()}_BEFORE_LISTING_{listed.isoformat()}"
        rows.append(
            UniverseRow(
                ticker,
                BUCKET_OF[ticker],
                included,
                reason,
                rsi_start.isoformat(),
                first.isoformat(),
                last.isoformat(),
                tiingo,
                alphavantage,
            )
        )
    return rows


def write_universe_csv(rows: list[UniverseRow], path: Path | None = None) -> Path:
    path = Path(path) if path is not None else config.UNIVERSE_CSV
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="ascii") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(UNIVERSE_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv_row())
    return path


def read_universe_csv(path: Path | None = None) -> list[UniverseRow]:
    path = Path(path) if path is not None else config.UNIVERSE_CSV
    rows: list[UniverseRow] = []
    with path.open("r", newline="", encoding="ascii") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                UniverseRow(
                    ticker=raw["ticker"],
                    bucket=raw["bucket"],
                    included=raw["included"] == "true",
                    reason=raw["reason"],
                    rsi_start=raw["rsi_start"],
                    first_cached=raw["first_cached"],
                    last_cached=raw["last_cached"],
                    tiingo_symbol=raw["tiingo_symbol"],
                    alphavantage_symbol=raw["alphavantage_symbol"],
                )
            )
    return rows


def included_tickers(rows: list[UniverseRow]) -> list[str]:
    return sorted(row.ticker for row in rows if row.included)


# --------------------------------------------------------------------------
# Build-time validation
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Fetch every ticker once, decide inclusion, and write `data/universe.csv`.

    Run once, before record start. After record start the file is frozen: no
    ticker is added or removed within v1 (section 2).
    """
    import argparse
    from pathlib import Path

    from quantdesk.cache import BarCache
    from quantdesk.providers.base import ProviderError
    from quantdesk.providers.tiingo import TiingoProvider
    from quantdesk.workspace import Workspace

    parser = argparse.ArgumentParser(prog="python -m quantdesk.universe")
    parser.add_argument("--rsi-start", default="2014-06-01")
    parser.add_argument("--root")
    parser.add_argument("--cache-only", action="store_true",
                        help="decide inclusion from the cache without fetching")
    args = parser.parse_args(argv)
    config.load_env(args.root)                       # execution_spec.md section 1; docs/spec_issues.md D1

    workspace = Workspace(Path(args.root)) if args.root else Workspace.default()
    workspace.ensure()
    rsi_start = dt.date.fromisoformat(args.rsi_start)
    cache = BarCache(workspace.cache_dir, workspace.revisions_log)
    provider = None if args.cache_only else TiingoProvider()

    frames: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    for ticker in list(TICKERS) + [config.BENCHMARK_TICKER]:
        if provider is None:
            frames[ticker] = cache.read(ticker)
            continue
        try:
            incoming = provider.fetch_daily(ticker, rsi_start)
        except ProviderError as exc:
            failures.append(str(exc))
            frames[ticker] = cache.read(ticker)
            continue
        cache.update(ticker, incoming)
        frames[ticker] = cache.read(ticker)

    rows = build_universe(frames, rsi_start)
    path = write_universe_csv(rows, workspace.universe_csv)
    dropped = [r for r in rows if not r.included]
    print(f"wrote {path}: {len(rows) - len(dropped)} included, {len(dropped)} dropped")
    for row in dropped:
        print(f"  dropped {row.ticker}: {row.reason or 'NO_DATA'}")
    for failure in failures:
        print(f"  provider error: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
