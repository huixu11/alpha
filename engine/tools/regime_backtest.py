"""Research backtest of a month-end market-regime / trend-momentum stock design.

This is research tooling that lives OUTSIDE the record. Nothing here reads or
writes call files, `record/`, or anything the spec-driven RSI mean-reversion
system depends on; it only reads the parquet bar cache through
`quantdesk.cache.BarCache` and the fee schedule through
`quantdesk.portfolio.commission`, and writes CSV/JSON outputs to a directory
given on the command line.

What is backtested (all rules fixed by the research brief):

* Calendar: SPY's sessions. Month-end T is the last SPY session of a calendar
  month. Every signal is computed on the close of a session T and every
  resulting trade fills at the open of the next SPY session T+1 (or the next
  session on which that ticker has a bar). The single-asset variants fill at
  T+1's CLOSE instead; see `Params.asset_fill` and ASSET_ASSUMPTIONS.
* Market regime at month-ends: vote A = SPY close above its 10-month-end SMA;
  vote B = SPY 12-month total return above BIL's. Two votes BULL (1.0), one
  HALF (0.5), none BEAR (0.0).
* Volatility cap daily: V_raw = clip(0.20 / SPY 20-session realized vol,
  0.25, 1); the applied V moves only when |V_raw - V_applied| > 0.10.
* Stock layer at month-ends (variant B): trend + 12-1 momentum + liquidity
  eligibility, rank by momentum, hold up to 20 with a 40-rank hold band,
  inverse-63-session-vol weights with a 10 % name cap, a 30 % bucket cap, a
  2 % minimum (a name squeezed to zero weight leaves the held set and its slot
  is refilled), residual to SPY. Cash sits in BIL.
* Daily sync with rebalance bands, whole shares, 5 bp slippage per side and
  the repo's commission schedule.

Variants: regime_stocks, regime_spy, stocks_only, spy_bh, ew_universe_bh (the
survivorship-matched benchmark). `--sweep` runs a parameter grid, `--breaker`
turns on a 12 % 20-session drawdown circuit breaker.

Pair variants (opt-in with `--with-pairs` / `--pair A,B`, so the standard
outputs stay exactly as committed): `pair_fixed` holds two named stocks 50/50
and trades them back to 50/50 at every month-end; `pair_rule` chooses the pair
at each month-end by a causal rule over the trailing `--pair-lookback` sessions
(the most volatile, least correlated pair: the largest annualized variance of
the return difference, with `--pair-keep-frac` hysteresis). `--pair-sweep`
runs the pair rule over lookback x hysteresis plus the topvol_lowcorr
selection (pair_sweep.csv). See PAIR_ASSUMPTIONS.

Tax: every variant's own trades and dividends go through `tools/regime_tax.py`
(FIFO lots, wash sales, a nonresident and a resident regime); summary.json
carries after-tax CAGR / final NAV per regime and tax_by_year.csv the per-year
table; `--tax-dividend-rate`, `--tax-st-rate`, `--tax-lt-rate` set the rates.

The universe is today's list of large names and is therefore survivorship
biased; the equal-weight buy-and-hold of the same universe is written next to
every strategy so the stock layer's marginal effect can be read net of that
bias. See `run_meta.json` for every assumption made where the brief left a
detail open.

Point-in-time universe (opt-in, defaults leave every existing output
unchanged): `--cache data/sp500_cache --universe data/sp500/universe_sp500.csv
--membership data/sp500/membership.csv` restricts the candidates at every
month-end to the S&P 500 members of that date (held names are kept until the
rules exit them), `--delist-after N` liquidates a held name N sessions after
its final cached bar at that last close (`--delist-haircut H` for a
sensitivity), and `--bucket-cap 1.0` switches the sector cap off where sectors
are unknown. See MEMBERSHIP_ASSUMPTIONS / DELIST_ASSUMPTIONS and
data/sp500/README.md; tools/sp500_run.py runs the whole set.

Single-asset variants (opt-in with `--asset TICKER --with-asset`, so every
standard output stays exactly as committed): the same month-end method run on
ONE instrument that is not in universe.csv (a cache ticker such as BTCUSD,
fetched by tools/crypto_fetch.py). `asset_regime` takes the two month-end votes
on the ASSET itself (close above the mean of its last 10 month-end closes; its
12-month return above BIL's), BULL 1.0 / HALF 0.5 / BEAR 0.0, times the daily
volatility cap computed on the asset's own 20-session realized vol;
`asset_regime_votes` is the same without the cap; `asset_bh` buys and holds;
`asset_cash_5050` holds half asset, half cash and trades back to 50/50 at every
month-end. Units are fractional (6 decimals), the cost is `--asset-cost-bps`
per side of traded notional (no per-share commission, no slippage), and an
order signalled at the close of T fills on T+1 at that bar's CLOSE
(`--asset-fill`, default `next_close`): for a UTC-day series the OPEN of T+1 is
the same 00:00 UTC print as the signal close, so filling there would model no
delay at all. A session on which the asset has no bar votes nothing, re-caps
nothing and issues no order. `--asset-only` runs just these four,
`--calendar-start` prepends NYSE sessions to SPY's calendar for an asset whose
history is older than SPY's cached one, `--asset-sweep` writes asset_sweep.csv.
See ASSET_ASSUMPTIONS for the timing of a 24/7 market on NYSE session days.

Universe-freshness guard: every stock-layer evaluation (the month-end ranking
of regime_stocks / stocks_only, the ew_universe_bh admission, the pair_rule
selection including its start-session evaluation) first checks that the
universe's bars are there. At the evaluation session t the LIVE names are the
universe tickers (index members only with `--membership`) with a bar in
sessions [t-63, t], the FRESH ones those with a bar at t (and no missing bar
in the 20-session liquidity window behind it, which the rules need just as
much); with at least 5 live names and fresh / live below `--freshness-min`
(0.8) the evaluation is STALE: nothing is re-chosen (the held set, the sleeve,
the pair stay as they are; a pair_rule account without a pair stays in SPY)
and the evaluation is retried at every following session until one is fresh,
recorded with its own date. A cache that was refreshed for a few names only
can therefore never "choose" those names by default. See
FRESHNESS_ASSUMPTIONS; 0 switches the guard off.

Usage (from the repo root):

    python tools/regime_backtest.py --out OUTDIR [--sweep] [--breaker]
                                    [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                                    [--with-pairs] [--pair MSTR,SMCI] [--pair-sweep]
                                    [--tax-dividend-rate 0.10 --tax-st-rate 0.24 --tax-lt-rate 0.15]
                                    [--cache ROOT --universe CSV --membership CSV]
                                    [--delist-after 10 --delist-haircut 0.0 --bucket-cap 1.0]
                                    [--freshness-min 0.8]
                                    [--asset BTCUSD --with-asset [--asset-only] [--asset-cost-bps 10]
                                     [--asset-fill next_close|open]
                                     [--calendar-start 2011-08-01] [--asset-sweep]]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run as `python tools/regime_backtest.py`
from quantdesk.cache import BarCache  # noqa: E402
from quantdesk.portfolio import commission  # noqa: E402
from tools import regime_tax  # noqa: E402

TRADING_DAYS = 252
SLIPPAGE = 0.0005
MIN_TRADE_USD = 500.0
START_NAV = 100_000.0
STATE_NAMES = {2: "BULL", 1: "HALF", 0: "BEAR"}
STATE_EXPOSURE = {2: 1.0, 1: 0.5, 0: 0.0}
# single-asset variants (Params.asset): fractional units on a 6-decimal grid
UNIT_DECIMALS = 6
UNIT_SCALE = 10 ** UNIT_DECIMALS
ASSET_5050_WEIGHT = 0.5


def floor_units(x: float) -> float:
    """x floored onto the 6-decimal unit grid of the asset variants (never above the target)."""
    return math.floor(x * UNIT_SCALE) / UNIT_SCALE


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Params:
    variant: str = "regime_stocks"
    sma_months: int = 10           # market SMA length in month-ends
    stock_sma_months: int = 10     # per-stock trend filter (fixed at 10)
    tr_months: int = 12            # vote B lookback
    mom_months: int = 12           # 12-1 momentum lookback (skip most recent month)
    vol_months: int = 12           # lowvol_stocks: daily volatility look-back in month-ends
    top_n: int = 20
    hold_rank: int = 40
    min_month_ends: int = 13
    name_cap: float = 0.10
    bucket_cap: float = 0.30
    min_weight: float = 0.02
    vol_window: int = 63
    liq_window: int = 20
    liq_min_usd: float = 20_000_000.0
    vol_target: float = 0.20
    vol_floor: float = 0.25
    vol_hyst: float = 0.10
    votes: str = "both"            # both | a_only | b_only
    breaker: bool = False
    breaker_dd: float = -0.12
    breaker_window: int = 20
    band_abs: float = 0.01
    band_rel: float = 0.25
    # pair variants (pair_fixed, pair_rule): two names 50/50, fully invested,
    # rebalanced to exactly 50/50 at every month-end, no intra-month trades
    pair_a: str = ""
    pair_b: str = ""
    pair_lookback: int = 252       # sessions of daily log returns the pair rule scores
    pair_keep_frac: float = 0.8    # keep the current pair while score >= keep_frac x best score
    pair_select: str = "variance_of_difference"   # | topvol_lowcorr
    pair_top_vol: int = 10         # topvol_lowcorr: the pair is drawn from the pair_top_vol most volatile names
                                   # (the leader plus the least correlated of the other pair_top_vol - 1)
    # point-in-time universes (--delist-after / --delist-haircut): a held name whose
    # cached history has ENDED and that has had no bar for delist_after sessions is
    # liquidated at its last close x (1 - delist_haircut); 0 = off (the default, so
    # every existing output is unchanged). See MEMBERSHIP_ASSUMPTIONS.
    delist_after: int = 0
    delist_haircut: float = 0.0
    # universe-freshness guard of every stock-layer evaluation: with at least
    # FRESHNESS_MIN_LIVE live names, an evaluation at which fewer than
    # freshness_min of them have a bar is STALE and re-chooses nothing (see
    # `universe_freshness`); 0 switches the guard off
    freshness_min: float = 0.8
    # single-asset variants (asset_regime, asset_regime_votes, asset_bh, asset_cash_5050): a cache ticker
    # OUTSIDE universe.csv (loaded through load_data's extra_tickers, e.g. BTCUSD) that is the whole sleeve;
    # "" = none (the default, and run_meta.json then omits both keys so every existing output is unchanged)
    asset: str = ""
    # their trading cost per side, in basis points of traded notional; it REPLACES the per-share commission
    # and the 5 bp slippage for these variants only
    asset_cost_bps: float = 10.0
    # WHICH print of the fill session T+1 an asset order fills at. For a UTC-day series the OPEN of the bar
    # dated T+1 is the same 00:00 UTC print as the CLOSE of the bar dated T, i.e. the signal price itself:
    # "open" would give the asset variants a zero-latency fill the stock variants never get, and would price
    # the paper book's fill at a print that exists BEFORE the sheet that orders it. The default "next_close"
    # fills at the close of the bar dated T+1 (for BTCUSD: 20:00 New York on T+1), which every reader of a
    # 21:15 sheet can still reach; "open" is kept for the sensitivity. See ASSET_ASSUMPTIONS.
    asset_fill: str = "next_close"


ASSET_FILLS = ("next_close", "open")
ASSET_PARAM_KEYS = ("asset", "asset_cost_bps", "asset_fill")
ASSET_REGIME_VARIANTS = ("asset_regime", "asset_regime_votes")      # the two month-end votes on the asset itself
ASSET_VARIANTS = ASSET_REGIME_VARIANTS + ("asset_bh", "asset_cash_5050")
ASSET_SWEEP_VOL_TARGETS = (None, 0.20, 0.40)                          # None = the volatility cap off


def params_dict(p: "Params") -> dict:
    """Params as run_meta.json records them: the asset keys appear only when an asset is set."""
    out = dict(p.__dict__)
    if not p.asset:
        for key in ASSET_PARAM_KEYS:
            out.pop(key, None)
    return out


PAIR_VARIANTS = ("pair_fixed", "pair_rule")
PAIR_SELECTS = ("variance_of_difference", "topvol_lowcorr")
# the variants whose stock layer DECIDES something from the universe's bars at an
# evaluation session, and is therefore guarded (pair_fixed only reports statistics)
GUARDED_VARIANTS = ("regime_stocks", "stocks_only", "lowvol_stocks", "ew_universe_bh", "pair_rule")
# sessions: a name is LIVE at t when it has a bar in [t - 63, t]. A quarter, not a fortnight: the window is how
# long a cache may go unrefreshed before its silent names stop counting, and once they stop counting the guard
# judges the few names that ARE refreshed against themselves and passes them (with [t-10, t] the 2026-09-19
# incident, 90 names ending 2026-09-04, would have stopped being flagged from 2026-09-22 on). Every evaluation
# a shorter window flags is flagged by this one too; a delisted name counts as live-but-not-fresh for a quarter,
# which needs a fifth of the universe to die within one quarter before it matters.
FRESHNESS_WINDOW = 63
FRESHNESS_MIN_LIVE = 5     # with fewer live names the guard does not judge (tiny universes, early history)


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


@dataclass
class Data:
    dates: pd.DatetimeIndex
    tickers: list[str]                 # stock universe (no SPY / BIL)
    all_tickers: list[str]             # universe + SPY
    col: dict[str, int]                # ticker -> column in the arrays
    bucket: dict[str, str]
    close: np.ndarray                  # forward-filled adjusted close (NaN before first bar)
    open_: np.ndarray                  # adjusted open, NaN where no bar
    has_bar: np.ndarray                # bool
    div: np.ndarray                    # adjusted cash dividend per share on ex-date
    tr: np.ndarray                     # total-return index, forward-filled
    dvol_med: np.ndarray               # rolling median close*volume, liq_window
    vol63: np.ndarray                  # 63-session realized vol (annualized)
    first_idx: np.ndarray              # first session index with a bar
    spy_vol20: np.ndarray
    bil_tr: np.ndarray                 # BIL total return index on the calendar
    bil_present: bool
    me_idx: np.ndarray                 # session indices of month-ends
    notes: list[str] = field(default_factory=list)
    # raw split factor of every session (1 where there is none or no bar). The
    # backtest never reads it: its prices are already adjusted to the present.
    # The paper-trading job does, because its book holds a fixed share count
    # that must be rescaled on the split date the way a broker account is.
    splits: np.ndarray | None = None
    # last session index with a bar; len(dates) when the cached history runs
    # past the calendar's end (the name is still trading). A held name is
    # "delisted" only when its last cached bar is its final bar (see --delist-after).
    last_idx: np.ndarray | None = None
    # point-in-time membership (--membership): member[s, j] is True when the
    # ticker in column j belongs to the index on session s (start_date <= s <
    # end_date of some interval). None = no restriction (today's list, the default).
    member: np.ndarray | None = None
    membership_csv: str | None = None
    # --delist-after only: which histories load_data treated as ended (and which
    # lag SPY by too little to count), for run_meta.json
    ended_note: dict | None = None
    # single-asset variants: cache tickers outside the universe that were loaded as extra columns
    # (after SPY, so every existing column index is unchanged) and their own 20-session realized vol
    extra_tickers: list[str] = field(default_factory=list)
    vol20: dict[str, np.ndarray] = field(default_factory=dict)
    # --calendar-start: NYSE sessions prepended to SPY's calendar (SPY has no bar there); None = SPY's own
    calendar_start: str | None = None


def _cum_split_factor(split_factor: pd.Series) -> np.ndarray:
    """f[s] = product of split_factor of all LATER sessions; 1 at the last."""
    sf = split_factor.to_numpy(dtype=float)
    f = np.ones(len(sf))
    if len(sf) > 1:
        f[:-1] = np.cumprod(sf[::-1])[::-1][1:]
    return f


def load_membership(path: Path, dates: pd.DatetimeIndex, tickers: list[str], col: dict[str, int],
                    notes: list[str]) -> np.ndarray:
    """member[s, j] from an intervals file (ticker_hist, start_date, end_date[, ticker]).

    Membership on session s is start_date <= s < end_date (end_date empty = still
    a member). The `ticker` column (the engine's symbol; a rename is one
    continuous membership under the new symbol) is used when present, else
    `ticker_hist`. SPY is always a member. Only start_date <= s is knowledge of
    the past; end_date > s says the name has not left YET, which is known at s.
    """
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    key = "ticker" if "ticker" in frame.columns else "ticker_hist"
    n, m = len(dates), len(col)
    member = np.zeros((n, m), dtype=bool)
    member[:, col["SPY"]] = True
    used = 0
    covered: set[str] = set()
    unknown: set[str] = set()
    for tkr, start, end in zip(frame[key], frame["start_date"], frame["end_date"]):
        tkr = str(tkr).strip().upper()
        if tkr not in col or tkr == "SPY":
            unknown.add(tkr)
            continue
        mask = np.asarray(dates >= pd.Timestamp(start))
        if str(end).strip():
            mask &= np.asarray(dates < pd.Timestamp(end))
        member[mask, col[tkr]] = True
        covered.add(tkr)
        used += 1
    never = sorted(t for t in tickers if t not in covered)
    notes.append(f"membership from {path.name}: {used} intervals over {len(covered)} universe tickers; "
                 f"{len(unknown)} intervals for symbols not in the universe were ignored")
    if never:
        notes.append(f"universe tickers with no membership interval (never candidates): {never}")
    return member


def load_data(
    cache_root: Path | None,
    universe_csv: Path,
    end: pd.Timestamp | None,
    params: Params,
    membership_csv: Path | None = None,
    extra_tickers: list[str] | None = None,
    calendar_start: pd.Timestamp | None = None,
) -> Data:
    """The engine's arrays on SPY's session calendar.

    `extra_tickers` (the single-asset variants): cache tickers OUTSIDE the
    universe file, loaded as extra columns after SPY. They are never
    candidates of the stock layer (`data.tickers` does not list them); a
    series with bars on non-session days (a 24/7 market) is aligned to the
    sessions on read, the other days are simply not looked at. `calendar_start`
    prepends the NYSE sessions from that date to SPY's own calendar when SPY's
    cached history starts later (SPY has no bar on those sessions, so only the
    asset variants can be run from there). Both default to off, which leaves
    every array exactly as it was.
    """
    cache = BarCache(cache_root) if cache_root else BarCache()
    uni = pd.read_csv(universe_csv)
    uni = uni[uni["included"].astype(str).str.lower() == "true"]
    tickers = sorted(uni["ticker"].tolist())
    bucket = dict(zip(uni["ticker"], uni["bucket"]))
    notes: list[str] = []

    spy_raw = cache.read_raw("SPY")
    if spy_raw.empty:
        raise SystemExit("SPY is not in the cache; the calendar cannot be built")
    full_dates = spy_raw.index
    calendar_note: str | None = None
    if calendar_start is not None and pd.Timestamp(calendar_start) < full_dates[0]:
        from quantdesk.calendar import sessions_between   # the XNYS calendar the rest of the repo uses

        first_spy = full_dates[0]
        earlier = [pd.Timestamp(d) for d in sessions_between(pd.Timestamp(calendar_start).date(),
                                                             (first_spy - pd.Timedelta(days=1)).date())]
        full_dates = pd.DatetimeIndex(earlier, name=full_dates.name).append(full_dates)
        calendar_note = str(pd.Timestamp(calendar_start).date())
        notes.append(f"calendar: {len(earlier)} NYSE sessions from {calendar_note} were prepended to SPY's own calendar "
                     f"(SPY's cached history starts {first_spy.date()}; it has no bar before that)")
    dates = full_dates
    if end is not None:
        dates = dates[dates <= end]
    n = len(dates)

    extras = [t for t in dict.fromkeys(str(x).strip().upper() for x in (extra_tickers or []))
              if t and t != "SPY" and t not in tickers]
    for t in extras:
        if not cache.path_for(t).exists():
            raise SystemExit(f"{t} is not in the cache {cache.root} (fetch it first, e.g. python tools/crypto_fetch.py)")
    all_tickers = tickers + ["SPY"] + extras
    m = len(all_tickers)
    col = {t: i for i, t in enumerate(all_tickers)}
    close = np.full((n, m), np.nan)
    open_ = np.full((n, m), np.nan)
    has_bar = np.zeros((n, m), dtype=bool)
    div = np.zeros((n, m))
    volume = np.full((n, m), np.nan)
    splits = np.ones((n, m))

    def aligned(ticker: str):
        adj = cache.read(ticker)
        raw = cache.read_raw(ticker)
        if adj.empty:
            return None
        f = _cum_split_factor(raw["split_factor"])
        div_adj = pd.Series(raw["div_cash"].to_numpy() / f, index=raw.index)
        last_bar = adj.index[-1]              # of the whole cached history, before the calendar cut
        adj = adj.reindex(dates)
        d = div_adj.reindex(dates).fillna(0.0)
        sf = raw["split_factor"].reindex(dates).fillna(1.0)
        return adj, d, sf, last_bar

    missing: list[str] = []
    last_idx = np.full(m, n, dtype=int)
    # "ended" is judged from the cache alone: a history counts as ended when its
    # last cached bar lies at least max(1, delist_after) SPY sessions before
    # SPY's last cached bar (the untruncated calendar), so a file that merely
    # lags SPY by a few refreshes is never treated as a delisting. tiingo_end
    # from data/sp500/tickers.csv is NOT consulted: the cache must be refreshed
    # in one pass (SPY and the stocks together) for the judgement to be right.
    spy_last = full_dates[-1]
    min_lag = max(1, int(params.delist_after))
    ended: list[tuple[str, str, int]] = []       # (ticker, last bar, lag in sessions) treated as ended
    lagging: list[tuple[str, str, int]] = []     # last bar before SPY's but too recent to count as ended
    for t in all_tickers:
        got = aligned(t)
        if got is None:
            missing.append(t)
            continue
        adj, d, sf, last_bar = got
        j = col[t]
        hb = adj["close"].notna().to_numpy()
        has_bar[:, j] = hb
        # the close is carried forward through every session after a bar, the
        # last one included: a position in a name whose history has ended must
        # still be marked (and, with --delist-after, liquidated at that close).
        # No signal reads a carried-forward close after the last bar: eligibility,
        # the pair rule and the benchmark's admission all require has_bar at T,
        # and the liquidity median is built on has_bar-masked values only.
        close[:, j] = adj["close"].ffill().to_numpy()
        open_[:, j] = adj["open"].to_numpy()
        volume[:, j] = adj["volume"].to_numpy()
        div[:, j] = d.to_numpy()
        splits[:, j] = sf.to_numpy()
        if last_bar < spy_last and hb.any():
            lag = int((full_dates > last_bar).sum())
            if lag >= min_lag and last_bar <= dates[-1]:
                last_idx[j] = int(np.flatnonzero(hb)[-1])
                ended.append((t, str(last_bar.date()), lag))
            elif lag < min_lag:
                lagging.append((t, str(last_bar.date()), lag))
    if missing:
        notes.append(f"tickers in universe.csv but not in the cache, skipped: {missing}")
        tickers = [t for t in tickers if t not in missing]
    ended_note: dict | None = None
    if params.delist_after > 0:
        ended_note = {"rule": f"last cached bar at least {min_lag} sessions before SPY's last cached bar {spy_last.date()} "
                              f"(judged from the cache; tiingo_end is not consulted)",
                      "n_ended": len(ended), "ended": [{"ticker": a, "last_bar": b, "lag_sessions": c} for a, b, c in ended],
                      "n_lagging_not_ended": len(lagging),
                      "lagging_not_ended": [{"ticker": a, "last_bar": b, "lag_sessions": c} for a, b, c in lagging]}
        shown = ", ".join(f"{a} {b} (lag {c})" for a, b, c in ended[:40]) + (" ..." if len(ended) > 40 else "")
        notes.append(f"histories treated as ended for --delist-after {params.delist_after} ({len(ended)}; last bar and "
                     f"lag behind SPY's {spy_last.date()} in sessions): {shown or 'none'}")
        if lagging:
            notes.append(f"WARNING: {len(lagging)} histories end before SPY's last bar but by fewer than {min_lag} sessions, "
                         f"so they are NOT treated as ended (a stale cache, not a delisting; refresh SPY and the stocks "
                         f"together): {[a for a, _, _ in lagging[:40]]}")

    # total-return index on the forward-filled close: on a gap day the
    # close is unchanged and there is no dividend, so the index is flat.
    prev = np.vstack([np.full((1, m), np.nan), close[:-1]])
    ratio = (close + div) / prev
    ratio = np.where(np.isnan(ratio), 1.0, ratio)
    tr = np.cumprod(ratio, axis=0)
    tr[np.isnan(close)] = np.nan

    first_idx = np.full(m, n, dtype=int)
    for j in range(m):
        idx = np.flatnonzero(has_bar[:, j])
        if len(idx):
            first_idx[j] = idx[0]

    # liquidity: median close*volume over the last liq_window sessions on
    # which the ticker has a bar (NaN elsewhere -> ignored by the median).
    dv = pd.DataFrame(np.where(has_bar, close * volume, np.nan))
    dvol_med = dv.rolling(params.liq_window, min_periods=params.liq_window).median().to_numpy()

    logret = np.log(close / prev)
    logret = np.where(np.isnan(logret), 0.0, logret)
    logret[np.isnan(close)] = np.nan
    lr = pd.DataFrame(logret)
    vol63 = (lr.rolling(params.vol_window, min_periods=params.vol_window).std(ddof=1)
             * math.sqrt(TRADING_DAYS)).to_numpy()
    spy_vol20 = (lr[col["SPY"]].rolling(20, min_periods=20).std(ddof=1)
                 * math.sqrt(TRADING_DAYS)).to_numpy()
    # the asset variants' volatility cap reads the ASSET's own 20-session realized vol, built exactly like SPY's
    vol20 = {t: (lr[col[t]].rolling(20, min_periods=20).std(ddof=1) * math.sqrt(TRADING_DAYS)).to_numpy()
             for t in extras}

    # BIL as the cash instrument
    bil_present = cache.path_for("BIL").exists()
    if bil_present:
        bil_adj = cache.read("BIL")
        bil_raw = cache.read_raw("BIL")
        f = _cum_split_factor(bil_raw["split_factor"])
        bdiv = pd.Series(bil_raw["div_cash"].to_numpy() / f, index=bil_raw.index)
        bc = bil_adj["close"].reindex(dates).ffill()
        bd = bdiv.reindex(dates).fillna(0.0)
        r = ((bc + bd) / bc.shift(1)).fillna(1.0).to_numpy()
        bil_tr = np.cumprod(r)
    else:
        bil_tr = np.ones(n)
        notes.append("BIL.parquet missing: cash return set to 0 percent")

    me_idx = month_ends(dates, full_dates)
    member = None
    if membership_csv is not None:
        member = load_membership(Path(membership_csv), dates, tickers, col, notes)
    return Data(
        dates=dates, tickers=tickers, all_tickers=all_tickers, col=col,
        bucket=bucket, close=close, open_=open_, has_bar=has_bar, div=div,
        tr=tr, dvol_med=dvol_med, vol63=vol63, first_idx=first_idx,
        spy_vol20=spy_vol20, bil_tr=bil_tr, bil_present=bil_present,
        me_idx=me_idx, notes=notes, splits=splits, last_idx=last_idx,
        member=member, membership_csv=str(membership_csv) if membership_csv is not None else None,
        ended_note=ended_note, extra_tickers=extras, vol20=vol20, calendar_start=calendar_note,
    )


def month_ends(dates: pd.DatetimeIndex, full_dates: pd.DatetimeIndex | None = None) -> np.ndarray:
    """Session index of the last SPY session of every calendar month.

    The last session in `dates` counts only if the month is complete. When the
    untruncated SPY calendar `full_dates` is given and extends beyond the tail,
    completeness is decided against it (the tail is a month-end iff the real
    calendar has no later session in that month, which is exact and knows
    exchange holidays). For the genuinely last session of the data the only
    information available is the weekday calendar: the tail counts if no
    weekday of its month remains after it (a partial month is never a signal
    date).
    """
    period = dates.to_period("M")
    last = pd.Series(np.arange(len(dates)), index=period).groupby(level=0).max()
    idx = last.to_numpy()
    tail = dates[-1]
    if full_dates is not None and full_dates[-1] > tail:
        later_same_month = full_dates[(full_dates > tail) & (full_dates.to_period("M") == tail.to_period("M"))]
        complete = len(later_same_month) == 0
    else:
        complete = (tail + pd.offsets.BDay(1)).month != tail.month
    if not complete:
        idx = idx[idx != len(dates) - 1]
    return idx


# --------------------------------------------------------------------------
# signals (all evaluated with data <= T)
# --------------------------------------------------------------------------


def market_state(data: Data, k: int, params: Params) -> dict:
    """Regime at month-end number k (data.me_idx[k]) using data <= that session."""
    me = data.me_idx
    t = me[k]
    j = data.col["SPY"]
    window = me[k - params.sma_months + 1: k + 1]
    assert window.max() <= t and len(window) == params.sma_months
    closes = data.close[window, j]
    sma = float(closes.mean())
    spy_close = float(data.close[t, j])
    vote_a = spy_close > sma
    t0 = me[k - params.tr_months]
    assert t0 < t
    spy_tr = float(data.tr[t, j] / data.tr[t0, j] - 1.0)
    bil_tr = float(data.bil_tr[t] / data.bil_tr[t0] - 1.0)
    vote_b = (spy_tr - bil_tr) > 0.0
    if params.votes == "a_only":
        votes = 2 if vote_a else 0
    elif params.votes == "b_only":
        votes = 2 if vote_b else 0
    else:
        votes = int(vote_a) + int(vote_b)
    return {
        "date": data.dates[t], "spy_close": spy_close, "sma10": sma,
        "vote_a": bool(vote_a), "spy_12m_tr": spy_tr, "bil_12m_tr": bil_tr,
        "vote_b": bool(vote_b), "votes": votes, "state": STATE_NAMES[votes],
        "base_exposure": STATE_EXPOSURE[votes],
    }


def asset_history_need(params: Params) -> int:
    """Month-end closes of the asset the two votes need: the SMA window and the 12-month look-back."""
    return max(params.min_month_ends, params.tr_months + 1, params.sma_months)


def asset_state_ready(data: Data, k: int, params: Params) -> bool:
    """True when month-end k has the asset's whole vote history behind it (its first bar included)."""
    need = asset_history_need(params)
    return k + 1 >= need and int(data.first_idx[data.col[params.asset]]) <= int(data.me_idx[k - need + 1])


def asset_state(data: Data, k: int, params: Params) -> dict:
    """The two month-end votes on the ASSET itself at month-end k, with data <= that session.

    Vote A: the asset's close above the mean of its last sma_months month-end
    closes. Vote B: its tr_months return above BIL's. The month-ends are SPY's
    (the last NYSE session of the month); the asset's close of a session is
    the close of its bar dated that day. A missing history makes a vote fail
    (NaN compares False), never pass.
    """
    me = data.me_idx
    t = me[k]
    j = data.col[params.asset]
    window = me[k - params.sma_months + 1: k + 1]
    assert window.max() <= t and len(window) == params.sma_months
    sma = float(data.close[window, j].mean())
    close = float(data.close[t, j])
    vote_a = close > sma
    t0 = me[k - params.tr_months]
    assert t0 < t
    asset_tr = float(data.tr[t, j] / data.tr[t0, j] - 1.0)
    bil_tr = float(data.bil_tr[t] / data.bil_tr[t0] - 1.0)
    vote_b = (asset_tr - bil_tr) > 0.0
    if params.votes == "a_only":
        votes = 2 if vote_a else 0
    elif params.votes == "b_only":
        votes = 2 if vote_b else 0
    else:
        votes = int(vote_a) + int(vote_b)
    return {
        "date": data.dates[t], "asset": params.asset, "asset_close": close, "sma": sma,
        "sma_months": int(params.sma_months), "vote_a": bool(vote_a), "asset_12m_tr": asset_tr,
        "bil_12m_tr": bil_tr, "vote_b": bool(vote_b), "votes": votes, "state": STATE_NAMES[votes],
        "base_exposure": STATE_EXPOSURE[votes],
    }


def vol_cap(data: Data, i: int, v_applied: float | None, params: Params) -> tuple[float, float]:
    """(V_raw, V_applied) at session i with hysteresis (on SPY's vol; on the asset's own for the asset variants)."""
    vol = data.vol20[params.asset][i] if params.variant in ASSET_VARIANTS else data.spy_vol20[i]
    if not np.isfinite(vol) or vol <= 0:
        v_raw = 1.0
    else:
        v_raw = max(params.vol_floor, min(1.0, params.vol_target / vol))
    if v_applied is None or abs(v_raw - v_applied) > params.vol_hyst:
        v_applied = v_raw
    return v_raw, v_applied


def candidates_at(data: Data, t: int, keep: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Universe names the rules may look at on session t.

    Without a membership file: every universe ticker (today's list). With one:
    the index members on session t (start_date <= t only) plus the names in
    `keep`, which are the names currently held: a held name that has left the
    index is still scored by the strategy's own rules until they exit it, but
    a non-member is never admitted anew.
    """
    if data.member is None:
        return list(data.tickers)
    keep_set = set(keep or ())
    return [tkr for tkr in data.tickers if data.member[t, data.col[tkr]] or tkr in keep_set]


def universe_freshness(data: Data, t: int, params: Params) -> dict:
    """Is the universe's data there at session t? {stale, n_fresh, n_live, n_bar}, with data <= t only.

    LIVE names are the universe tickers (the index members on t with a
    membership file) that have at least one bar in sessions [t - FRESHNESS_WINDOW,
    t]. A name whose history ended (or has not begun) is not live, so
    delistings and late listings never count against freshness.

    FRESH names are the live ones the rules can actually judge at t: a bar at t
    (n_bar counts those) AND no missing bar in the liquidity window behind it.
    The second half matters after a hole: the liquidity median is NaN for
    liq_window sessions after a missing bar, which makes the name ineligible
    for the ranking and for the pair rule exactly as a missing bar at t does,
    so a session on which most names have their bar back but not yet their
    window would again "choose" the few names that never had a hole. A name
    listed fewer than liq_window sessions ago has no median yet for a reason
    that is not missing data; it counts as fresh.

    With at least FRESHNESS_MIN_LIVE live names and n_fresh / n_live <
    params.freshness_min the evaluation is STALE: what the rules would "see"
    is a partially refreshed cache, not the market, and the caller must not
    re-choose anything from it. freshness_min 0 never flags.
    """
    names, live, bar, judged = _freshness_masks(data, t, params)
    if not names:
        return {"stale": False, "n_fresh": 0, "n_live": 0, "n_bar": 0}
    n_live = int(live.sum())
    n_fresh = int(judged.sum())
    stale = n_live >= FRESHNESS_MIN_LIVE and n_fresh / n_live < params.freshness_min
    return {"stale": bool(stale), "n_fresh": n_fresh, "n_live": n_live, "n_bar": int(bar.sum())}


def _freshness_masks(data: Data, t: int, params: Params):
    """(names, live, bar, fresh) of `universe_freshness`: boolean arrays over the candidate names at t."""
    names = candidates_at(data, t)
    if not names:
        empty = np.zeros(0, dtype=bool)
        return names, empty, empty, empty
    cols = np.fromiter((data.col[n] for n in names), dtype=int, count=len(names))
    lo = max(0, t - FRESHNESS_WINDOW)
    live = data.has_bar[lo: t + 1][:, cols].any(axis=0)
    bar = data.has_bar[t, cols]                          # a bar at t is a bar in the window: fresh implies live
    young = data.first_idx[cols] > t - (params.liq_window - 1)
    judged = bar & (np.isfinite(data.dvol_med[t, cols]) | young)
    return names, live, bar, judged


def universe_missing(data: Data, t: int, params: Params) -> dict:
    """The LIVE names that are not FRESH at t (see `universe_freshness`), for the paper job's wording and refetch.

    {"no_bar": live names without a bar at t, "window": names with the bar of t
    but a hole in the liquidity window behind it}. An evaluation that is not
    stale still SKIPS these names, so a choice made without them can change
    once their data arrives.
    """
    names, live, bar, judged = _freshness_masks(data, t, params)
    no_bar = [n for n, lv, b in zip(names, live, bar) if lv and not b]
    window = [n for n, b, j in zip(names, bar, judged) if b and not j]
    return {"no_bar": no_bar, "window": window}


def eligible_and_rank(data: Data, k: int, params: Params, held: list[str] | None = None,
                      t: int | None = None) -> pd.DataFrame:
    """Eligible names at month-end k with 12-1 momentum, ranked descending.

    `held` matters only with a membership file: those names stay in the
    candidate set after leaving the index (see `candidates_at`).

    `t` is the session the evaluation is made on: month-end k itself by default;
    a later session (before the next month-end) when a STALE month-end
    evaluation is retried. The retry reads the bar, the close and the liquidity
    of t and the same month-end grid through k (the trend average and the 12-1
    momentum), so it still uses data <= t only.
    """
    me = data.me_idx
    if t is None:
        t = me[k]
    assert me[k] <= t and (k + 1 >= len(me) or t < me[k + 1]), "t must lie between month-end k and the next one"
    if params.variant == "lowvol_stocks":
        return lowvol_rank(data, k, params, held, t)
    need = max(params.min_month_ends, params.mom_months + 1)
    rows = []
    for tkr in candidates_at(data, t, held):
        j = data.col[tkr]
        if not data.has_bar[t, j]:
            continue
        # month-end closes available: month-ends at or after the first bar
        if k + 1 < need or data.first_idx[j] > me[k - need + 1]:
            continue
        window = me[k - params.stock_sma_months + 1: k + 1]
        assert window.max() <= t
        if not data.close[t, j] > np.nanmean(data.close[window, j]):
            continue
        t1, t12 = me[k - 1], me[k - params.mom_months]
        assert t12 < t1 < t
        mom = data.tr[t1, j] / data.tr[t12, j] - 1.0
        if not mom > 0:
            continue
        liq = data.dvol_med[t, j]
        if not (np.isfinite(liq) and liq >= params.liq_min_usd):
            continue
        rows.append((tkr, float(mom)))
    frame = pd.DataFrame(rows, columns=["ticker", "momentum"])
    frame = frame.sort_values(["momentum", "ticker"], ascending=[False, True]).reset_index(drop=True)
    frame["rank"] = np.arange(1, len(frame) + 1)
    return frame


def lowvol_rank(data: Data, k: int, params: Params, held: list[str] | None, t: int) -> pd.DataFrame:
    """lowvol_stocks: eligible names at month-end k ranked by daily volatility, lowest first.

    Blitz and van Vliet (2007): no trend and no momentum test, only a bar at t,
    the month-end history and the liquidity floor. The score is the annualized
    standard deviation of the daily total returns from month-end k - vol_months
    to session t (data <= t only); a name with fewer than 80 percent of those
    returns finite is skipped. `momentum` is NaN: the holdings report reads it.
    """
    me = data.me_idx
    need = max(params.min_month_ends, params.vol_months + 1)
    rows = []
    if k + 1 >= need:
        t0 = me[k - params.vol_months]
        assert t0 < t
        for tkr in candidates_at(data, t, held):
            j = data.col[tkr]
            if not data.has_bar[t, j]:
                continue
            if data.first_idx[j] > me[k - need + 1]:
                continue
            liq = data.dvol_med[t, j]
            if not (np.isfinite(liq) and liq >= params.liq_min_usd):
                continue
            px = data.tr[t0: t + 1, j]
            rets = px[1:] / px[:-1] - 1.0
            ok = np.isfinite(rets)
            if ok.sum() < 2 or ok.sum() < 0.8 * len(rets):
                continue
            rows.append((tkr, np.nan, float(np.std(rets[ok], ddof=1) * math.sqrt(252))))
    frame = pd.DataFrame(rows, columns=["ticker", "momentum", "vol"])
    frame = frame.sort_values(["vol", "ticker"], ascending=[True, True]).reset_index(drop=True)
    frame["rank"] = np.arange(1, len(frame) + 1)
    return frame


def update_held(held: list[str], ranked: pd.DataFrame, params: Params,
                exclude: set[str] | None = None) -> list[str]:
    """Keep held names still ranked <= hold_rank, then fill to top_n from rank <= top_n.

    `exclude` holds names that were squeezed to zero weight at this month-end
    and may not be re-admitted in the same evaluation.
    """
    exclude = exclude or set()
    rank = dict(zip(ranked["ticker"], ranked["rank"]))
    keep = [t for t in held if t in rank and rank[t] <= params.hold_rank and t not in exclude]
    keep.sort(key=lambda t: rank[t])
    for t in ranked["ticker"]:
        if len(keep) >= params.top_n:
            break
        if rank[t] > params.top_n:
            break
        if t not in keep and t not in exclude:
            keep.append(t)
    return keep


def held_and_sleeve(data: Data, t: int, held: list[str], ranked: pd.DataFrame,
                    params: Params) -> tuple[list[str], pd.Series, list[str]]:
    """Held-set update and weighting converged at one month-end.

    A name the 2 percent minimum (or the cap squeeze) leaves with zero weight is
    not held: it leaves the held set and its slot is refilled from the rank <=
    top_n candidates, then the weights are recomputed. Names dropped this way
    cannot re-enter in the same evaluation, so the loop terminates. Returns
    (held, sleeve, dropped_names).
    """
    excluded: set[str] = set()
    held = update_held(held, ranked, params)
    for _ in range(len(data.tickers) + 2):
        sleeve = sleeve_weights(data, t, held, params)
        kept = [h for h in held if float(sleeve.get(h, 0.0)) > 0]
        if len(kept) == len(held):
            return held, sleeve, sorted(excluded)
        excluded |= set(held) - set(kept)   # strictly grows every pass
        held = update_held(kept, ranked, params, excluded)
    raise AssertionError("held-set / weight convergence did not terminate")


def apply_caps(w: pd.Series, groups: pd.Series, name_cap: float, bucket_cap: float,
               total: float = 1.0) -> pd.Series:
    """Cap names at `name_cap` and buckets at `bucket_cap` jointly.

    Names (or whole buckets) that hit a cap are frozen and the shortfall
    against `total` is handed pro rata to names that are still free. The
    frozen set only grows, so the loop terminates; whatever cannot be placed
    because everything is frozen is left unallocated (the residual to SPY).
    """
    eps = 1e-12
    w = w * (total / w.sum())
    capped = pd.Series(False, index=w.index)
    for _ in range(500):
        over = w > name_cap + eps
        if over.any():
            w[over] = name_cap
            capped |= over
        for g, members in groups.groupby(groups).groups.items():
            gw = w[members].sum()
            if gw > bucket_cap + eps:
                w[members] = w[members] * (bucket_cap / gw)
                capped[members] = True
        shortfall = total - w.sum()
        free = ~capped
        if shortfall <= eps or not free.any() or w[free].sum() <= 0:
            break
        w[free] = w[free] + shortfall * w[free] / w[free].sum()
    assert (w <= name_cap + 1e-9).all()
    assert (w.groupby(groups).sum() <= bucket_cap + 1e-9).all()
    return w


def sleeve_weights(data: Data, t: int, held: list[str], params: Params) -> pd.Series:
    """Inverse-vol weights with name/bucket caps and a minimum; residual -> SPY."""
    if not held:
        return pd.Series({"SPY": 1.0})
    vols = pd.Series({h: data.vol63[t, data.col[h]] for h in held})
    ok = np.isfinite(vols) & (vols > 0)
    vols = vols.where(ok, vols[ok].median() if ok.any() else 0.3)
    groups = pd.Series({h: data.bucket.get(h, "UNKNOWN") for h in held})
    w = apply_caps(1.0 / vols, groups, params.name_cap, params.bucket_cap, 1.0)
    before = float(w.sum())
    keep = w[w >= params.min_weight]
    if len(keep) and len(keep) < len(w):
        # renormalize once: the dropped weight is handed back to the survivors,
        # still subject to the caps, with no second minimum-weight pass
        w = apply_caps(keep, groups[keep.index], params.name_cap, params.bucket_cap, before)
    elif not len(keep):
        w = keep
    residual = max(0.0, 1.0 - float(w.sum()))
    out = w.sort_index()
    if residual > 1e-9 or out.empty:
        out["SPY"] = float(out.get("SPY", 0.0)) + residual
    return out


# --------------------------------------------------------------------------
# pair variants: a causal rule that picks two names at each month-end
# --------------------------------------------------------------------------


def pair_candidates(data: Data, t: int, params: Params, keep: tuple[str, str] | None = None) -> list[str]:
    """Universe names usable by the pair rule at session t, with data <= t only.

    A bar at t, a first bar at or before t - pair_lookback (so the lookback
    window of daily returns tr[t-L..t] exists in full) and the engine's
    liquidity filter (median dollar volume over liq_window sessions >= liq_min_usd).
    With a membership file only index members on t qualify, plus the current
    pair `keep` (held names are scored until the rule itself drops them).
    """
    out = []
    for tkr in candidates_at(data, t, keep):
        j = data.col[tkr]
        if not data.has_bar[t, j]:
            continue
        if data.first_idx[j] > t - params.pair_lookback:
            continue
        liq = data.dvol_med[t, j]
        if not (np.isfinite(liq) and liq >= params.liq_min_usd):
            continue
        out.append(tkr)
    return out


def pair_statistics(data: Data, t: int, names: list[str], params: Params) -> dict:
    """Annualized vols, correlations and the pair score over the trailing window ending at t.

    Daily log returns of the total-return index over the pair_lookback
    sessions t-L+1..t (each return uses tr[s] / tr[s-1], so tr[t-L] is the
    earliest value read). score(A, B) = annualized variance of (rA - rB) =
    volA^2 + volB^2 - 2 rho volA volB, which is eight times the theoretical
    50/50 rebalancing bonus (1/2 x 1/2 x 1/2 x var of the difference): the
    largest score is the most volatile, least correlated pair.
    """
    L = params.pair_lookback
    cols = [data.col[n] for n in names]
    tr = data.tr[t - L: t + 1][:, cols]
    R = np.log(tr[1:] / tr[:-1])
    R = np.where(np.isfinite(R), R, 0.0)
    Z = R - R.mean(axis=0)
    cov = Z.T @ Z / (L - 1)
    var = np.diag(cov).copy()
    vol = np.sqrt(var * TRADING_DAYS)
    sd = np.sqrt(var)
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = cov / (sd[:, None] * sd[None, :])
    corr = np.where(np.isfinite(corr), corr, 0.0)
    score = (var[:, None] + var[None, :] - 2.0 * cov) * TRADING_DAYS
    return {"names": list(names), "vol": vol, "corr": corr, "score": score}


def select_pair(data: Data, t: int, params: Params, current: tuple[str, str] | None,
                force_keep: bool = False) -> dict:
    """The pair rule at session t (data <= t): the chosen pair with its statistics.

    variance_of_difference: the pair with the largest score. topvol_lowcorr:
    the most volatile name, then the least correlated partner among the other
    names of the pair_top_vol most volatile (the leader included in the ten).
    Hysteresis: the current pair is kept
    while both names are still candidates and its score >= pair_keep_frac x
    the selected pair's score (pair_keep_frac 1.0 = no hysteresis). Ties break
    alphabetically. Fewer than two candidates: no pair (the caller holds SPY).
    `force_keep` (pair_fixed's reporting) keeps `current` unconditionally and
    only fills in its statistics next to what the rule would have picked.

    With a membership file the NEW pair is drawn from index members on t only;
    the current pair's legs are added to the statistics so the hysteresis test
    can keep them, but a leg that has left the index never enters a fresh pair.
    When fewer than two members qualify and the current pair is still fully
    scoreable it is kept (there is no admissible competitor). Without the file
    members and candidates coincide, so the choice is unchanged.
    """
    if params.pair_top_vol < 2:
        raise ValueError("pair_top_vol must be at least 2 (the leader and one partner)")
    # one canonical (alphabetical) order for every pair the rows report, whatever order the caller typed
    current = tuple(sorted(current)) if current else None
    members = pair_candidates(data, t, params)                    # admissible for a NEW pair
    names = pair_candidates(data, t, params, keep=current)         # members plus the current legs (scored)
    row = {"date": data.dates[t], "n_candidates": len(names), "pair_a": None, "pair_b": None, "score": np.nan,
           "vol_a": np.nan, "vol_b": np.nan, "corr": np.nan, "best_a": None, "best_b": None, "best_score": np.nan,
           "kept_current": False, "switched": False, "prev_a": current[0] if current else None,
           "prev_b": current[1] if current else None}
    if force_keep:
        row.update(pair_a=current[0], pair_b=current[1], kept_current=True)
    if len(names) < 2:
        row["switched"] = current is not None and not force_keep
        return dict(row, pair=current if force_keep else None)
    st = pair_statistics(data, t, names, params)
    pos = {n: k for k, n in enumerate(names)}
    score, vol, corr = st["score"], st["vol"], st["corr"]
    member_set = set(members)
    best: tuple[str, str] | None = None
    if params.pair_select == "variance_of_difference":
        iu, ju = np.triu_indices(len(names), 1)
        admissible = [(a, b) for a, b in zip(iu, ju) if names[a] in member_set and names[b] in member_set]
        if admissible:
            vals = np.array([score[a, b] for a, b in admissible])
            top = float(vals.max())
            best = min(tuple(sorted((names[a], names[b]))) for (a, b), v in zip(admissible, vals) if v >= top - 1e-15)
    elif params.pair_select == "topvol_lowcorr":
        order = sorted((k for k in range(len(names)) if names[k] in member_set), key=lambda k: (-float(vol[k]), names[k]))
        if len(order) >= 2:
            a_i = order[0]
            partners = order[1: params.pair_top_vol] or order[1:2]   # the other members of the top pair_top_vol
            b_i = min(partners, key=lambda k: (float(corr[a_i, k]), names[k]))
            best = tuple(sorted((names[a_i], names[b_i])))
    else:
        raise ValueError(f"unknown pair_select {params.pair_select!r}; expected one of {PAIR_SELECTS}")
    if best is None:
        # fewer than two admissible members (only with --membership): keep the current
        # pair while it is still fully scoreable, since nothing may replace it
        if current is not None and current[0] in pos and current[1] in pos:
            a, b = current
            row.update(pair_a=a, pair_b=b, score=float(score[pos[a], pos[b]]), vol_a=float(vol[pos[a]]),
                       vol_b=float(vol[pos[b]]), corr=float(corr[pos[a], pos[b]]), kept_current=True, switched=False)
            return dict(row, pair=current)
        row["switched"] = current is not None and not force_keep
        return dict(row, pair=current if force_keep else None)
    best_val = float(score[pos[best[0]], pos[best[1]]])
    row.update(best_a=best[0], best_b=best[1], best_score=best_val)
    chosen = best
    kept = False
    if current is not None and current[0] in pos and current[1] in pos:
        cur = float(score[pos[current[0]], pos[current[1]]])
        if force_keep or cur >= params.pair_keep_frac * best_val:
            chosen = current
            kept = True
    elif force_keep:
        return dict(row, pair=current)          # the fixed pair is not (fully) a candidate today: no statistics
    a, b = chosen
    row.update(pair_a=a, pair_b=b, score=float(score[pos[a], pos[b]]), vol_a=float(vol[pos[a]]),
               vol_b=float(vol[pos[b]]), corr=float(corr[pos[a], pos[b]]), kept_current=kept,
               switched=current is not None and set(chosen) != set(current))
    return dict(row, pair=(a, b))


PAIR_ROW_COLUMNS = ["date", "n_candidates", "pair_a", "pair_b", "score", "vol_a", "vol_b", "corr",
                    "best_a", "best_b", "best_score", "kept_current", "switched", "prev_a", "prev_b",
                    # the universe-freshness guard (appended, so the earlier columns read as before):
                    # stale = the evaluation was skipped and the pair (or SPY) kept; n_fresh of n_live names had a bar
                    "stale", "n_fresh", "n_live"]
# result["freshness"]: every guarded evaluation; n_bar = live names with a bar at the session, n_fresh = those of
# them whose liquidity window is complete too (see `universe_freshness`)
# n_live_ref = the live count the evaluation was JUDGED against: n_live itself, or, inside a run of stale
# evaluations, the larger live count the run started with (a shrinking denominator must not end a run)
FRESHNESS_COLUMNS = ["date", "kind", "stale", "n_fresh", "n_live", "n_bar", "n_live_ref"]
DELISTED_COLUMNS = ["date", "variant", "ticker", "shares", "last_close", "liquidation_price", "proceeds_usd",
                    "last_bar_date"]
CANCELLED_COLUMNS = ["date", "variant", "ticker", "side", "shares", "last_bar_date"]


def stale_pair_row(data: Data, t: int, params: Params, current: tuple[str, str] | None) -> dict:
    """The pairs_chosen row of a STALE pair_rule evaluation: nothing is scored, the current pair (or none) stays.

    n_candidates is what the rule would have seen (the names with a bar at t
    that pass the lookback and liquidity tests), reported so the row shows how
    thin the data was; no statistics are computed from a universe that is not there.
    """
    current = tuple(sorted(current)) if current else None
    return {"date": data.dates[t], "n_candidates": len(pair_candidates(data, t, params, keep=current)),
            "pair_a": current[0] if current else None, "pair_b": current[1] if current else None,
            "score": np.nan, "vol_a": np.nan, "vol_b": np.nan, "corr": np.nan,
            "best_a": None, "best_b": None, "best_score": np.nan,
            "kept_current": current is not None, "switched": False,
            "prev_a": current[0] if current else None, "prev_b": current[1] if current else None,
            "pair": current}


def fixed_pair(params: Params) -> tuple[str, str]:
    """pair_fixed's two tickers in canonical (alphabetical) order, so reports do not depend on typing order."""
    a, b = sorted((str(params.pair_a).upper(), str(params.pair_b).upper()))
    return a, b


def pair_sleeve(pair: tuple[str, str] | None) -> pd.Series:
    """50/50 on the pair; SPY when the rule found no pair."""
    if pair is None:
        return pd.Series({"SPY": 1.0})
    return pd.Series({pair[0]: 0.5, pair[1]: 0.5}).sort_index()


# --------------------------------------------------------------------------
# the daily engine
# --------------------------------------------------------------------------


@dataclass
class Order:
    ticker: str
    side: str
    shares: int                        # whole shares; fractional units (a float on the 6-decimal grid) for the asset variants
    target_w: float
    reason: str
    signal_idx: int
    exit: bool


@dataclass
class FillResult:
    """What `fill_orders` did at one open: the new book and the rows to log."""
    cash: float
    shares: dict[str, int]             # (floats on the 6-decimal grid for the asset variants)
    still: list[Order]                 # orders whose ticker had no bar; still working
    trades: list[tuple]
    dropped: list[tuple]
    carry_reason: dict[str, str]       # reason of every buy dropped for cash
    # one entry per fill, in fill order: (commission + slippage, buy notional,
    # sell notional). The backtest adds them to its running totals one by one,
    # in this order, so its unrounded aggregates are bit-identical to the
    # pre-refactor loop that accumulated inside the fill block.
    steps: list[tuple[float, float, float]] = field(default_factory=list)

    @property
    def total_costs(self) -> float:
        return sum(s[0] for s in self.steps)

    @property
    def buy_notional(self) -> float:
        return sum(s[1] for s in self.steps)

    @property
    def sell_notional(self) -> float:
        return sum(s[2] for s in self.steps)


def mark_nav(data: Data, i: int, cash: float, shares: dict[str, int]) -> float:
    """Cash plus every position at the (forward-filled) close of session i."""
    val = cash
    for t, s in shares.items():
        if s:
            val += s * data.close[i, data.col[t]]
    return val


def fill_orders_fractional(data: Data, i: int, cash: float, shares: dict[str, float],
                           pending: list[Order], variant: str, cost_rate: float,
                           fill_at: str = "next_close") -> FillResult:
    """`fill_orders` for the single-asset variants: fractional units, a cost in basis points of notional.

    Fills are on session i (the session AFTER the signal), at that session's
    CLOSE by default and at its open with `fill_at="open"`. The close is the
    default because a UTC-day bar's open IS the previous bar's close: filling
    at the open would execute at the signal price itself, a zero-latency fill
    the stock variants never get and a price that, in the paper job, prints
    before the sheet that orders it exists. No slippage either way; the cost is
    cost_rate x traded notional per side, booked in the trade row's commission
    column (so the tax model puts it into the basis / takes it off the
    proceeds) with slippage 0. Sells first, then buys; a buy that does not fit
    the cash (it was sized on close[T] with no cost reserve) is trimmed to what
    the cash buys, cost included, on the 6-decimal grid, and dropped only below
    the minimum trade. Positions are kept on the 6-decimal grid after every
    fill, so a book written to JSON and read back is the same book.
    """
    dates = data.dates
    prices = data.open_ if fill_at == "open" else data.close
    shares = dict(shares)
    still: list[Order] = []
    trades: list[tuple] = []
    dropped: list[tuple] = []
    carry: dict[str, str] = {}
    steps: list[tuple[float, float, float]] = []
    for o in (x for x in pending if x.side == "SELL"):
        j = data.col[o.ticker]
        assert o.signal_idx < i, "look-ahead: fill before signal"
        if not data.has_bar[i, j]:
            still.append(o)
            continue
        fill = float(prices[i, j])
        qty = min(float(o.shares), float(shares.get(o.ticker, 0.0)))
        if qty <= 0:
            continue
        fee = qty * fill * cost_rate
        cash += qty * fill - fee
        shares[o.ticker] = round(shares.get(o.ticker, 0.0) - qty, UNIT_DECIMALS)
        steps.append((fee, 0.0, qty * fill))
        trades.append((dates[i], variant, o.ticker, "SELL", round(qty, UNIT_DECIMALS), round(fill, 4),
                       round(fee, 6), 0.0, o.reason))
    fillable: list[Order] = []
    for o in (x for x in pending if x.side == "BUY"):
        assert o.signal_idx < i, "look-ahead: fill before signal"
        if not data.has_bar[i, data.col[o.ticker]]:
            still.append(o)
            continue
        fillable.append(o)
    fillable.sort(key=lambda o: (o.target_w, o.ticker))

    def fill_px(o: Order) -> float:
        return float(prices[i, data.col[o.ticker]])

    def cost(o: Order) -> float:
        return float(o.shares) * fill_px(o) * (1.0 + cost_rate)

    remaining = list(fillable)
    fillable = []
    while remaining:
        if sum(cost(o) for o in remaining) <= cash:
            fillable.extend(remaining)
            break
        o = remaining.pop(0)
        avail = cash - sum(cost(x) for x in remaining)
        fp = fill_px(o)
        q = min(float(o.shares), floor_units(avail / (fp * (1.0 + cost_rate)))) if avail > 0 else 0.0
        while q > 0 and q * fp * (1.0 + cost_rate) > avail:
            q = round(q - 1.0 / UNIT_SCALE, UNIT_DECIMALS)
        if q <= 0 or q * fp < MIN_TRADE_USD:
            dropped.append((dates[i], variant, o.ticker, o.shares, 0, o.target_w,
                            round(cost(o), 2), round(max(avail, 0.0), 2),
                            round(cost(o) - max(avail, 0.0), 2), "DROPPED_CASH", o.reason))
            carry[o.ticker] = o.reason
        else:
            dropped.append((dates[i], variant, o.ticker, o.shares, q, o.target_w,
                            round(cost(o), 2), round(avail, 2),
                            round(cost(o) - avail, 2), "TRIMMED_CASH", o.reason))
            fillable.append(replace(o, shares=q))
    for o in fillable:
        fill = fill_px(o)
        qty = float(o.shares)
        fee = qty * fill * cost_rate
        cash -= qty * fill + fee
        shares[o.ticker] = round(shares.get(o.ticker, 0.0) + qty, UNIT_DECIMALS)
        steps.append((fee, qty * fill, 0.0))
        trades.append((dates[i], variant, o.ticker, "BUY", round(qty, UNIT_DECIMALS), round(fill, 4),
                       round(fee, 6), 0.0, o.reason))
    return FillResult(cash, shares, still, trades, dropped, carry, steps)


def fill_orders(data: Data, i: int, cash: float, shares: dict[str, int],
                pending: list[Order], variant: str, params: Params | None = None) -> FillResult:
    """Fill `pending` at the open of session i (step 3 of the daily engine).

    `variant` only labels the rows (the paper job passes its account name);
    `params` says WHICH rules fill: with the parameters of a single-asset
    variant the fractional path runs (`fill_orders_fractional`, which fills at
    session i's CLOSE by default — see there), otherwise, and always without
    `params`, the whole-share path below, unchanged.

    Sells first, then buys; 5 bp slippage per side and the repo's commission
    schedule. An order for a ticker without a bar at i stays pending. Buys
    were sized on close[T]; if the opens gap up (or costs intrude) the set may
    not fit the cash. The buys are walked in ascending target-weight order:
    the smallest one that does not fit is trimmed to the cash that remains
    after the others, and dropped only when even the trimmed lot is below the
    minimum trade. Every trim/drop is logged and a dropped buy keeps its
    reason for the re-signal at the next close. Pure: the inputs are not
    mutated; the paper-trading job and the backtest both call this.
    """
    if params is not None and params.variant in ASSET_VARIANTS:
        return fill_orders_fractional(data, i, cash, shares, pending, variant,
                                      params.asset_cost_bps / 10_000.0, fill_at=params.asset_fill)
    dates = data.dates
    shares = dict(shares)
    still: list[Order] = []
    trades: list[tuple] = []
    dropped: list[tuple] = []
    carry: dict[str, str] = {}
    steps: list[tuple[float, float, float]] = []
    sells = [o for o in pending if o.side == "SELL"]
    buys = [o for o in pending if o.side == "BUY"]
    for o in sells:
        j = data.col[o.ticker]
        assert o.signal_idx < i, "look-ahead: fill before signal"
        if not data.has_bar[i, j]:
            still.append(o)
            continue
        fill = data.open_[i, j] * (1 - SLIPPAGE)
        qty = min(o.shares, shares.get(o.ticker, 0))
        if qty <= 0:
            continue
        comm = commission(qty, "SELL", fill)
        cash += qty * fill - comm
        shares[o.ticker] = shares.get(o.ticker, 0) - qty
        slip = qty * data.open_[i, j] * SLIPPAGE
        steps.append((comm + slip, 0.0, qty * fill))
        trades.append((dates[i], variant, o.ticker, "SELL", qty, round(fill, 4),
                       comm, round(slip, 2), o.reason))
    fillable = []
    for o in buys:
        j = data.col[o.ticker]
        assert o.signal_idx < i, "look-ahead: fill before signal"
        if not data.has_bar[i, j]:
            still.append(o)
            continue
        fillable.append(o)
    fillable.sort(key=lambda o: (o.target_w, o.ticker))
    def fill_px(o: Order) -> float:
        return data.open_[i, data.col[o.ticker]] * (1 + SLIPPAGE)
    def cost(o: Order) -> float:
        fp = fill_px(o)
        return o.shares * fp + commission(o.shares, "BUY", fp)
    remaining = list(fillable)
    fillable = []
    while remaining:
        total = sum(cost(o) for o in remaining)
        if total <= cash:
            fillable.extend(remaining)
            break
        o = remaining.pop(0)
        avail = cash - sum(cost(x) for x in remaining)
        fp = fill_px(o)
        q = min(o.shares, int(math.floor(avail / fp))) if avail > 0 else 0
        while q > 0 and q * fp + commission(q, "BUY", fp) > avail:
            q -= 1
        if q <= 0 or q * fp < MIN_TRADE_USD:
            dropped.append((dates[i], variant, o.ticker, o.shares, 0, o.target_w,
                            round(cost(o), 2), round(max(avail, 0.0), 2),
                            round(cost(o) - max(avail, 0.0), 2), "DROPPED_CASH", o.reason))
            carry[o.ticker] = o.reason
        else:
            dropped.append((dates[i], variant, o.ticker, o.shares, q, o.target_w,
                            round(cost(o), 2), round(avail, 2),
                            round(cost(o) - avail, 2), "TRIMMED_CASH", o.reason))
            fillable.append(replace(o, shares=q))
    for o in fillable:
        j = data.col[o.ticker]
        fill = data.open_[i, j] * (1 + SLIPPAGE)
        comm = commission(o.shares, "BUY", fill)
        cash -= o.shares * fill + comm
        shares[o.ticker] = shares.get(o.ticker, 0) + o.shares
        slip = o.shares * data.open_[i, j] * SLIPPAGE
        steps.append((comm + slip, o.shares * fill, 0.0))
        trades.append((dates[i], variant, o.ticker, "BUY", o.shares, round(fill, 4),
                       comm, round(slip, 2), o.reason))
    return FillResult(cash, shares, still, trades, dropped, carry, steps)


def build_orders(data: Data, params: Params, i: int, nav: float, shares: dict[str, int],
                 targets: pd.Series, pending_tickers: set[str], carry_reason: dict[str, str],
                 exposure: float, base: float, prev_base: float, base_changed_today: bool,
                 v_changed_today: bool, breaker_on: bool,
                 pair_switch_today: bool = False,
                 dead: set[str] | None = None) -> tuple[list[Order], dict[str, str]]:
    """Orders for the next open from targets against the actual book (step 8).

    `targets` are portfolio weights (sleeve x exposure) at the close of session
    i, `shares` and `nav` the book they are compared with. Rebalance bands,
    whole shares, the minimum trade and the reason codes are all here. Returns
    the orders and the carried reasons that survive (a carried reason only
    lives while the name is still a target). Pure: `carry_reason` is copied,
    so the paper-trading job can call this on its real book with the replay's
    targets and get exactly the replay's orders when the books agree.

    Pair variants (pair_fixed, pair_rule): trades only at month-ends, where
    every held name is brought back to exactly its target regardless of the
    band (PAIR_REBALANCE), plus first entries (PAIR_ENTER) and the sells and
    buys of a switch (PAIR_SWITCH, flagged by `pair_switch_today`); no
    intra-month band trades. A SPY leg (the rule found no pair) is RESIDUAL_SPY.

    `dead` (--delist-after) holds names liquidated because their history
    ended; no order is ever built for them again, whatever the targets say.

    Single-asset variants: the same rules on fractional units (`floor_units`,
    6 decimals, instead of the whole-share floor). asset_regime(_votes) syncs
    daily with the bands like regime_spy (REGIME_ENTER / REGIME_EXIT / VOL_CAP /
    REBALANCE_BAND, the regime being the asset's own); asset_bh enters once
    (ASSET_ENTER); asset_cash_5050 enters (ASSET_ENTER) and is traded back to
    its half at every month-end regardless of the band (ASSET_REBALANCE), with
    no trades inside the month.
    """
    variant = params.variant
    is_asset = variant in ASSET_VARIANTS
    use_regime = variant in ("regime_stocks", "regime_spy") or variant in ASSET_REGIME_VARIANTS
    use_stocks = variant in ("regime_stocks", "stocks_only", "lowvol_stocks")
    is_pair = variant in PAIR_VARIANTS
    monthly_only = is_pair or variant == "asset_cash_5050"   # trades at month-ends (and the first entry) only
    dead = dead or set()

    def units(x: float):
        """target value / price as an order size: whole shares, or the 6-decimal grid of the asset variants."""
        return floor_units(x) if is_asset else int(math.floor(x))

    carry_reason = dict(carry_reason)
    actual = pd.Series({t: s * data.close[i, data.col[t]] / nav for t, s in shares.items()})
    orders: list[Order] = []
    # the equal-weight benchmark is rebalanced to 1/N at every month-end
    # regardless of the band (a 1 percent absolute band is wider than a
    # 1/N target, so the band alone would never top a name up); the pair
    # variants are rebalanced to 50/50 at every month-end the same way
    is_month_end = i in set(int(x) for x in data.me_idx)
    ew_rebalance_today = variant == "ew_universe_bh" and is_month_end
    pair_rebalance_today = monthly_only and is_month_end
    for t in sorted(set(targets.index) | set(actual.index)):
        if t in pending_tickers:
            continue  # an unfilled order (no bar yet) is still working
        if t in dead:
            continue  # liquidated as delisted; its history has ended
        tw = float(targets.get(t, 0.0))
        aw = float(actual.get(t, 0.0))
        j = data.col[t]
        px = data.close[i, j]
        if not np.isfinite(px) or px <= 0:
            continue
        is_spy_res = (t == "SPY" and use_stocks)
        if tw == 0 and aw > 0:
            if is_pair:
                reason = "REGIME_EXIT" if exposure == 0 else ("RESIDUAL_SPY" if t == "SPY" else "PAIR_SWITCH")
            elif is_spy_res and exposure > 0:
                reason = "RESIDUAL_SPY"
            elif exposure == 0:
                reason = "REGIME_EXIT"
            else:
                reason = "RANK_EXIT"
            orders.append(Order(t, "SELL", shares[t], tw, reason, i, True))
        elif tw > 0 and aw == 0:
            qty = units(tw * nav / px)
            if qty <= 0 or qty * px < MIN_TRADE_USD:
                continue
            if t in carry_reason:
                reason = carry_reason.pop(t)   # re-signal of a buy dropped for cash
            elif is_asset and not use_regime:
                reason = "ASSET_ENTER"         # asset_bh, asset_cash_5050
            elif is_pair:
                reason = "RESIDUAL_SPY" if t == "SPY" else ("PAIR_SWITCH" if pair_switch_today else "PAIR_ENTER")
            elif is_spy_res:
                reason = "RESIDUAL_SPY"
            elif use_regime and (prev_base == 0.0 and base > 0 and base_changed_today):
                reason = "REGIME_ENTER"
            elif use_stocks or not use_regime:
                reason = "RANK_ENTER"      # stock entries, and benchmark entries
            else:
                reason = "REGIME_ENTER"    # regime_spy's SPY sleeve
            orders.append(Order(t, "BUY", qty, tw, reason, i, False))
        elif tw > 0 and aw > 0:
            if monthly_only and not pair_rebalance_today:
                continue                   # pairs (and the 50/50 asset account) never trade inside the month
            if not (ew_rebalance_today or pair_rebalance_today) and abs(tw - aw) <= max(params.band_abs, params.band_rel * tw):
                continue
            target_shares = units(tw * nav / px)
            diff = target_shares - shares[t]
            if is_asset:
                diff = round(diff, UNIT_DECIMALS)
            if diff == 0 or abs(diff) * px < MIN_TRADE_USD:
                continue
            if diff > 0 and t in carry_reason:
                reason = carry_reason.pop(t)
            elif variant == "asset_cash_5050":
                reason = "ASSET_REBALANCE"
            elif is_pair:
                reason = "RESIDUAL_SPY" if t == "SPY" else "PAIR_REBALANCE"
            elif is_spy_res:
                reason = "RESIDUAL_SPY"
            elif base_changed_today:
                reason = "REGIME_ENTER" if base > prev_base else "REGIME_EXIT"
            elif v_changed_today or (breaker_on and exposure == 0):
                reason = "VOL_CAP"
            else:
                reason = "REBALANCE_BAND"
            side = "BUY" if diff > 0 else "SELL"
            orders.append(Order(t, side, abs(diff), tw, reason, i, False))
    # a carried reason only survives while the name is still a target
    carry_reason = {t: r for t, r in carry_reason.items() if t in targets.index}
    return orders, carry_reason


def run_variant(data: Data, params: Params, start_idx: int, end_idx: int,
                start_nav: float = START_NAV) -> dict:
    """Simulate one variant from the close of session start_idx to end_idx."""
    t0 = time.perf_counter()
    dates = data.dates
    n = end_idx + 1
    n_all = len(dates)                  # last_idx == n_all marks a history that runs past the calendar
    me_set = {int(i): k for k, i in enumerate(data.me_idx)}
    variant = params.variant
    is_asset = variant in ASSET_VARIANTS
    use_regime = variant in ("regime_stocks", "regime_spy") or variant in ASSET_REGIME_VARIANTS
    use_stocks = variant in ("regime_stocks", "stocks_only", "lowvol_stocks")
    use_volcap = use_regime and variant != "asset_regime_votes"
    is_pair = variant in PAIR_VARIANTS
    if is_asset and (not params.asset or params.asset not in data.col or params.asset not in data.vol20):
        raise ValueError(f"{variant} needs Params.asset to be a ticker loaded through load_data(extra_tickers=...); "
                         f"got {params.asset!r}")
    pair: tuple[str, str] | None = None
    pair_rows: list[dict] = []          # pair_rule: the choice at every evaluation
    n_switches = 0
    pair_switch_today = False
    div_rows: list[tuple] = []          # (date, ticker, kind, shares, per_share, amount) dividend and interest credits
    if variant == "pair_fixed":
        pair = fixed_pair(params)          # alphabetical, whatever order the caller typed
        if not pair[0] or not pair[1] or pair[0] == pair[1]:
            raise ValueError("pair_fixed needs two different tickers (Params.pair_a / pair_b)")
        for t in pair:
            if t not in data.col:
                raise ValueError(f"pair ticker {t} is not in the universe / cache")
    if variant == "pair_rule" and params.pair_select not in PAIR_SELECTS:
        raise ValueError(f"unknown pair_select {params.pair_select!r}; expected one of {PAIR_SELECTS}")

    cash = start_nav
    shares: dict[str, int] = {}
    pending: list[Order] = []
    held: list[str] = []
    sleeve = pd.Series({"SPY": 1.0}) if variant != "pair_fixed" else pair_sleeve(pair)
    base = 1.0
    prev_base = 1.0
    if is_asset:
        sleeve = pd.Series({params.asset: 1.0})          # the asset is the whole sleeve; cash is the rest
        base = prev_base = ASSET_5050_WEIGHT if variant == "asset_cash_5050" else 1.0
    v_applied: float | None = None
    v_raw = 1.0
    v_changed_today = False
    base_changed_today = False
    breaker_on = False
    state_row: dict | None = None

    nav_series = np.full(len(dates), np.nan)
    exposure_rows = []
    regime_rows = []
    holdings_rows = []
    trades = []
    dropped = []                       # buys trimmed or dropped for want of cash at the open
    carry_reason: dict[str, str] = {}  # reason of a dropped buy, reused when it is re-signalled
    names_held = np.zeros(len(dates))
    invested = np.zeros(len(dates))
    buy_notional = sell_notional = 0.0
    total_costs = 0.0
    last_orders: list[Order] = []
    last_targets: pd.Series = pd.Series(dtype=float)
    state_hist = []
    dead: set[str] = set()              # names liquidated as delisted (--delist-after)
    delisted_rows: list[tuple] = []     # (date, variant, ticker, shares, last_close, price, proceeds, last_bar)
    cancelled_rows: list[tuple] = []    # (date, variant, ticker, side, shares, last_bar): dead orders of ended names
    # the universe-freshness guard of the stock layer (see `universe_freshness`)
    guarded = variant in GUARDED_VARIANTS
    retry_pending = False               # the last stock-layer evaluation was STALE: evaluate again at the next session
    last_k: int | None = None           # the latest month-end number passed (a retry ranks on its month-end grid)
    fresh_rows: list[dict] = []         # every guarded evaluation: date, kind (month_end | start | retry), stale, n_fresh, n_live
    last_fresh: dict | None = None      # the latest of them
    last_stale_idx: int | None = None   # session of the last STALE evaluation
    stale_since_idx: int | None = None  # first session of the unbroken run of stale evaluations still in force
    stale_ref_live: int | None = None   # the largest live count seen in that run: the denominator its retries are judged against
    # the ASSET-freshness guard, the single-asset variants' equivalent of the universe one: the whole
    # strategy is one instrument, so a session on which it has no bar decides nothing. `close[i]` is
    # carried forward from the last bar, and a vote, a volatility factor or an order size built on it
    # would be taken from an earlier session's price without anyone being told. On such a session the
    # vote is not taken (it is retried at every session until one has a bar), the volatility factor is
    # kept where it was, and no order is built; the position is still marked at the carried close.
    j_asset = data.col[params.asset] if is_asset else -1
    asset_vote_pending = False          # a month-end vote the missing bar skipped: retake it when a bar is there
    asset_vote_k: int | None = None     # the month-end number that vote belongs to
    asset_stale_idx: int | None = None  # the last session on which the asset had no bar
    asset_stale_rows: list[dict] = []   # every such session: date, the date its close really comes from, kind
    asset_bar_today = True
    asset_close_from: int | None = None  # index of the session the close in force was last printed on

    for i in range(start_idx, n):
        if is_asset:
            asset_bar_today = bool(data.has_bar[i, j_asset])
            if asset_bar_today:
                asset_close_from = i
            else:
                prior = np.flatnonzero(data.has_bar[: i + 1, j_asset])
                asset_close_from = int(prior[-1]) if len(prior) else None
                asset_stale_idx = i
                asset_stale_rows.append({"date": dates[i], "close_from": dates[asset_close_from]
                                         if asset_close_from is not None else None,
                                         "month_end": bool(i in me_set)})
        # 1. cash earns BIL's daily total return (booked as interest-type income for the tax model)
        if i > start_idx:
            cash_before = cash
            cash *= data.bil_tr[i] / data.bil_tr[i - 1]
            if cash != cash_before:
                div_rows.append((dates[i], "BIL", "interest", 0, 0.0, cash - cash_before))
        # 2. dividends: holders at the previous close receive the ex-date cash
        for t, s in list(shares.items()):
            if s:
                d = data.div[i, data.col[t]]
                if d:
                    cash += s * d
                    div_rows.append((dates[i], t, "stock", int(s), float(d), s * d))
        # 3. fills at today's open for orders signalled at an earlier close
        if pending:
            filled = fill_orders(data, i, cash, shares, pending, variant, params=params)
            cash, shares, pending = filled.cash, filled.shares, filled.still
            trades.extend(filled.trades)
            dropped.extend(filled.dropped)
            carry_reason.update(filled.carry_reason)
            for cost_step, bought, sold in filled.steps:   # per fill, in fill order
                total_costs += cost_step
                buy_notional += bought
                sell_notional += sold
        shares = {t: s for t, s in shares.items() if s > 0}
        # 3b. delisting (--delist-after): a held name with no bar today whose
        # cached history has ENDED (its last bar is its final bar) and that has
        # been silent for delist_after sessions is liquidated at that last
        # close x (1 - delist_haircut), with no commission or slippage. Its
        # pending orders are cancelled and it never gets an order again. A name
        # that was signalled on its final bar but never filled (no bar to fill
        # at) has no shares: its dead order is cancelled the same way.
        if params.delist_after > 0 and data.last_idx is not None and (shares or pending):
            sweep = list(shares) + [o.ticker for o in pending if o.ticker not in shares]
            for t in dict.fromkeys(sweep):
                j = data.col[t]
                if data.has_bar[i, j] or data.last_idx[j] >= n_all:
                    continue
                if i - int(data.last_idx[j]) < params.delist_after:
                    continue
                s = shares.get(t, 0)
                if s > 0:
                    last_close = float(data.close[i, j])           # carried forward from the final bar
                    px = last_close * (1.0 - params.delist_haircut)
                    proceeds = s * px
                    cash += proceeds
                    sell_notional += proceeds
                    # the liquidation is booked on the same unit grid as every other fill: with int(s)
                    # a fractional position credited the cash in full but logged a truncated size, so
                    # trades.csv / delisted.csv stopped reconciling with the cash and a sub-1-unit sale
                    # (shares 0) never realized its gain in the tax match at all
                    trades.append((dates[i], variant, t, "SELL", round(s, UNIT_DECIMALS), round(px, 4),
                                   0.0, 0.0, "DELISTED"))
                    delisted_rows.append((dates[i], variant, t, round(s, UNIT_DECIMALS), round(last_close, 4),
                                          round(px, 4), round(proceeds, 2), dates[int(data.last_idx[j])]))
                    shares.pop(t)
                for o in pending:
                    if o.ticker == t:
                        cancelled_rows.append((dates[i], variant, t, o.side, round(o.shares, UNIT_DECIMALS),
                                               dates[int(data.last_idx[j])]))
                dead.add(t)
                pending = [o for o in pending if o.ticker != t]
                carry_reason.pop(t, None)
                held = [h for h in held if h != t]
                if t in sleeve.index:
                    sleeve = sleeve.drop(t)

        # 4. mark at the close
        nav = mark_nav(data, i, cash, shares)
        nav_series[i] = nav
        names_held[i] = len(shares)
        invested[i] = (nav - cash) / nav if nav > 0 else 0.0

        # 5. month-end evaluation: the market vote first (it reads SPY and BIL only) ...
        base_changed_today = False
        pair_switch_today = False
        me_ranked: pd.DataFrame | None = None
        is_me = i in me_set
        voted_today = False
        if is_me:
            k = me_set[i]
            last_k = k
            breaker_on = False
            if is_asset and use_regime and not asset_bar_today:
                # the asset has no bar on its own vote day: close[i] is an earlier session's price, so
                # there is no vote to take. The state in force is kept and the skipped month-end is
                # recorded; a cache that later gets the bar makes the same replay vote normally.
                asset_vote_pending, asset_vote_k = True, k
            elif use_regime and (not is_asset or asset_state_ready(data, k, params)):
                state_row = asset_state(data, k, params) if is_asset else market_state(data, k, params)
                prev_base, base = base, state_row["base_exposure"]
                base_changed_today = base != prev_base
                voted_today = True
                asset_vote_pending, asset_vote_k = False, None
            elif not use_regime:
                base = ASSET_5050_WEIGHT if variant == "asset_cash_5050" else 1.0
        elif i == start_idx and variant in ASSET_REGIME_VARIANTS:
            # an asset account started INSIDE a month (the paper job) takes the vote in force, i.e. the
            # one of the latest month-end before the start (data < start, so causal), instead of sitting
            # fully invested until the first month-end the way the SPY accounts do
            k0 = int(np.searchsorted(data.me_idx, i, side="right")) - 1
            if k0 >= 0 and asset_state_ready(data, k0, params):
                if bool(data.has_bar[int(data.me_idx[k0]), j_asset]):
                    state_row = asset_state(data, k0, params)
                    base = prev_base = state_row["base_exposure"]
                else:
                    asset_vote_pending, asset_vote_k = True, k0
        # 5b. ... then the stock layer, behind the universe-freshness guard. It is
        # evaluated at every month-end, at pair_rule's start close when the start
        # falls inside a month (the paper job), and at every session after a STALE
        # evaluation until one is fresh. A stale evaluation re-chooses nothing: the
        # held set, the sleeve and the pair stay as they are (a pair_rule account
        # without a pair stays in the SPY sleeve), so no entry, exit or switch can
        # come from a cache that was refreshed for a few names only. A retry uses
        # the data of ITS session (causal) and is recorded with its own date.
        start_eval = variant == "pair_rule" and i == start_idx
        fresh_today: dict | None = None
        if guarded and (is_me or start_eval or retry_pending):
            fresh_today = universe_freshness(data, i, params)
            # Inside a run of stale evaluations the denominator is the live count the run started with:
            # names that stay silent long enough drop out of the live window, and judging the few
            # refreshed names against themselves (2 of 2, or "fewer than 5 live, no verdict") would end
            # the run with no data having arrived and choose those names by default. A run therefore
            # ends only when enough names are fresh again, never because the universe looks smaller.
            n_live_ref = fresh_today["n_live"]
            if retry_pending and stale_ref_live is not None:
                n_live_ref = max(n_live_ref, stale_ref_live)
                if (params.freshness_min > 0 and n_live_ref >= FRESHNESS_MIN_LIVE
                        and fresh_today["n_fresh"] / n_live_ref < params.freshness_min):
                    fresh_today["stale"] = True
            fresh_today["n_live_ref"] = n_live_ref
            kind = "month_end" if is_me else ("start" if start_eval else "retry")
            last_fresh = {"date": dates[i], "kind": kind, **fresh_today}
            fresh_rows.append(last_fresh)
            if fresh_today["stale"]:
                last_stale_idx = i
                if not retry_pending:
                    stale_since_idx = i
                retry_pending = True
                stale_ref_live = n_live_ref
            else:
                retry_pending = False
                stale_since_idx = None
                stale_ref_live = None
        stale_today = bool(fresh_today and fresh_today["stale"])
        if fresh_today is not None and use_stocks:
            # a retry ranks at session i on the month-end grid through last_k (see `eligible_and_rank`)
            ranked = eligible_and_rank(data, last_k, params, held=held, t=i) if (is_me or not stale_today) else None
            dropped_names: list[str] = []
            if not stale_today:
                held, sleeve, dropped_names = held_and_sleeve(data, i, held, ranked, params)
                assert all(float(sleeve.get(h, 0.0)) > 0 for h in held)
            # a stale month-end still reports what the ranking saw (n_eligible), but keeps the held set
            if use_regime and ranked is not None:
                state_row = dict(state_row, n_eligible=len(ranked), n_held=len(held),
                                 n_dropped_min_weight=len(dropped_names), universe_stale=stale_today,
                                 n_fresh=fresh_today["n_fresh"], n_live=fresh_today["n_live"])
            me_ranked = ranked   # holdings rows are written once today's exposure is known
        elif fresh_today is not None and variant == "ew_universe_bh":
            # the benchmark is reconstituted monthly: with a membership file
            # only index members on T are admitted, and a name that has left
            # drops out of the 1/N sleeve at the next month-end (that IS its rule)
            if not stale_today:
                elig = []
                need = params.min_month_ends
                for t in candidates_at(data, i):
                    j = data.col[t]
                    if data.has_bar[i, j] and last_k + 1 >= need and data.first_idx[j] <= data.me_idx[last_k - need + 1]:
                        elig.append(t)
                sleeve = pd.Series({t: 1.0 / len(elig) for t in elig}) if elig else pd.Series({"SPY": 1.0})
        elif fresh_today is not None and variant == "pair_rule":
            # a start inside a month (the paper job) picks the pair at the start close, with data <= start
            sel = stale_pair_row(data, i, params, pair) if stale_today else select_pair(data, i, params, pair)
            sel.update(stale=stale_today, n_fresh=fresh_today["n_fresh"], n_live=fresh_today["n_live"])
            pair_rows.append(sel)
            if not stale_today:
                pair_switch_today = bool(sel["switched"])
                n_switches += int(pair_switch_today)
                pair = sel["pair"]
                sleeve = pair_sleeve(pair)
        elif is_me and variant == "pair_fixed":
            sleeve = pair_sleeve(pair)
            sel = select_pair(data, i, params, pair, force_keep=True)     # the fixed pair's own statistics
            info = universe_freshness(data, i, params)                    # reported only: a fixed pair decides nothing
            sel.update(stale=info["stale"], n_fresh=info["n_fresh"], n_live=info["n_live"])
            pair_rows.append(sel)
        elif is_me and not guarded:
            sleeve = pd.Series({params.asset: 1.0}) if is_asset else pd.Series({"SPY": 1.0})
        if voted_today:
            regime_rows.append(state_row)

        # 6. volatility cap (never recomputed from a session on which the asset has no bar: its
        #    20-session vol would be read off a carried-forward close, i.e. a zero return it never had)
        v_changed_today = False
        if use_volcap and not (is_asset and not asset_bar_today):
            v_raw, v_new = vol_cap(data, i, v_applied, params)
            v_changed_today = v_applied is not None and v_new != v_applied
            v_applied = v_new
        elif use_volcap:
            if v_applied is None:               # no bar yet on the first session: nothing to cap
                v_raw = v_applied = 1.0
        else:
            v_raw = v_applied = 1.0
        exposure = base * v_applied
        # 7. circuit breaker
        if params.breaker and i - start_idx >= 1:
            lo = max(start_idx, i - params.breaker_window + 1)
            peak = np.nanmax(nav_series[lo: i + 1])
            if nav / peak - 1.0 < params.breaker_dd:
                breaker_on = True
        if breaker_on:
            exposure = 0.0
        exposure_rows.append((dates[i], base, v_applied, exposure))
        state_hist.append(base)
        if me_ranked is not None:
            # month-end holdings with the same exposure that builds today's targets
            mom = dict(zip(me_ranked["ticker"], me_ranked["momentum"]))
            rk = dict(zip(me_ranked["ticker"], me_ranked["rank"]))
            for t, w in sleeve.items():
                pw = w * exposure
                holdings_rows.append((dates[i], t, mom.get(t, np.nan), rk.get(t, np.nan),
                                      w, pw, int(math.floor(pw * nav / data.close[i, data.col[t]]))))

        # 8. targets and orders for the next open. An asset session without a bar issues NONE: every
        #    size would be (target weight x NAV) / a carried-forward price, and the NAV itself is marked
        #    at that same stale close. The book is left where it is and the next session with a bar
        #    re-signals whatever the rule wants.
        targets = (sleeve * exposure)
        targets = targets[targets > 0]
        if is_asset and not asset_bar_today:
            orders = []
        else:
            orders, carry_reason = build_orders(
                data, params, i, nav, shares, targets, {o.ticker for o in pending}, carry_reason,
                exposure, base, prev_base, base_changed_today, v_changed_today, breaker_on,
                pair_switch_today=pair_switch_today, dead=dead)
        pending.extend(orders)
        last_orders = orders
        last_targets = targets

    # final bookkeeping
    idx = np.arange(start_idx, n)
    nav = pd.Series(nav_series[idx], index=dates[idx], name=variant)
    trades_df = pd.DataFrame(trades, columns=["date", "variant", "ticker", "side", "shares",
                                              "fill_price", "commission", "slippage_usd", "reason"])
    exposure_df = pd.DataFrame(exposure_rows, columns=["date", "base_exposure", "v_applied", "exposure"])
    dropped_df = pd.DataFrame(dropped, columns=["date", "variant", "ticker", "shares_requested", "shares_filled",
                                                "target_w", "order_cost_usd", "cash_available_usd",
                                                "shortfall_usd", "outcome", "original_reason"])
    years = (dates[n - 1] - dates[start_idx]).days / 365.25
    dividends_df = pd.DataFrame(div_rows, columns=["date", "ticker", "kind", "shares", "per_share", "amount"])
    pairs_df = pd.DataFrame([{c: r.get(c) for c in PAIR_ROW_COLUMNS} for r in pair_rows], columns=PAIR_ROW_COLUMNS)
    delisted_df = pd.DataFrame(delisted_rows, columns=DELISTED_COLUMNS)
    cancelled_df = pd.DataFrame(cancelled_rows, columns=CANCELLED_COLUMNS)
    freshness_df = pd.DataFrame(fresh_rows, columns=FRESHNESS_COLUMNS)
    result = {
        "variant": variant, "params": params, "nav": nav, "trades": trades_df, "dropped": dropped_df,
        "delisted": delisted_df,            # --delist-after liquidations (empty when off)
        "delisted_pending": cancelled_df,   # --delist-after: pending orders cancelled on ended names (empty when off)
        "exposure": exposure_df, "regime": pd.DataFrame(regime_rows),
        "holdings": pd.DataFrame(holdings_rows, columns=["month_end", "ticker", "momentum", "rank",
                                                         "sleeve_weight", "portfolio_weight", "shares"]),
        "names_held": pd.Series(names_held[idx], index=dates[idx]),
        "invested": pd.Series(invested[idx], index=dates[idx]),
        "buy_notional": buy_notional, "sell_notional": sell_notional,
        "total_costs": total_costs, "years": years,
        "dividends": dividends_df,          # every dividend / cash-interest credit, for the tax model
        "pairs": pairs_df,                  # pair variants: the pair (and the rule's choice) at every evaluation
        "n_pair_switches": n_switches,
        # the universe-freshness guard: every guarded stock-layer evaluation (month_end | start | retry)
        "freshness": freshness_df,
        "n_stale_evaluations": int(sum(1 for r in fresh_rows if r["stale"])),
        # the asset-freshness guard: every session of the window on which the asset had no bar
        "asset_stale_sessions": asset_stale_rows,
        "final": {
            "state": state_row, "v_applied": v_applied, "exposure": exposure_rows[-1][3],
            "nav": float(nav.iloc[-1]), "cash": cash, "shares": dict(shares),
            "targets": last_targets, "orders": last_orders, "breaker_on": breaker_on,
            # the rest of the loop state at the last close, for the paper-trading
            # job that rebuilds the day's orders against its real book
            "base": base, "prev_base": prev_base, "base_changed_today": base_changed_today,
            "v_raw": v_raw, "v_changed_today": v_changed_today, "carry_reason": dict(carry_reason),
            "still_pending": pending[:len(pending) - len(last_orders)],
            "held": list(held), "sleeve": sleeve,
            "pair": pair, "pair_switch_today": pair_switch_today,
            "pair_row": pair_rows[-1] if pair_rows else None,
            # the universe-freshness guard, so the paper job can word it: universe_stale says the
            # stock-layer evaluation IN FORCE is stale (nothing was re-chosen and a retry is due at the
            # next session); n_fresh / n_live / evaluated are those of the latest guarded evaluation
            "universe_guarded": guarded,
            "universe_stale": bool(retry_pending),
            "n_fresh": last_fresh["n_fresh"] if last_fresh else None,
            "n_live": last_fresh["n_live"] if last_fresh else None,
            "n_live_ref": last_fresh["n_live_ref"] if last_fresh else None,
            "n_bar": last_fresh["n_bar"] if last_fresh else None,
            "universe_evaluated": last_fresh["date"] if last_fresh else None,
            "universe_evaluated_kind": last_fresh["kind"] if last_fresh else None,
            "last_stale_session": dates[last_stale_idx] if last_stale_idx is not None else None,
            "stale_since": dates[stale_since_idx] if stale_since_idx is not None else None,
            # single-asset variants: the asset and its own 20-session realized vol at the last close
            "asset": params.asset if is_asset else None,
            "asset_vol20": float(data.vol20[params.asset][n - 1]) if is_asset else None,
            # the asset-freshness guard (see the loop): `asset_stale` says the asset has NO bar on the
            # last session, so nothing was voted, re-capped or ordered there and every price on this
            # sheet comes from `asset_close_from`; `asset_vote_skipped` names a month-end whose vote the
            # missing bar prevented and that is still not taken.
            "asset_stale": bool(is_asset and not asset_bar_today),
            "asset_close_from": dates[asset_close_from] if (is_asset and asset_close_from is not None) else None,
            "asset_bar_today": bool(asset_bar_today) if is_asset else None,
            "asset_vote_skipped": (dates[int(data.me_idx[asset_vote_k])]
                                   if (is_asset and asset_vote_pending and asset_vote_k is not None) else None),
            "asset_last_stale_session": dates[asset_stale_idx] if (is_asset and asset_stale_idx is not None) else None,
        },
        "elapsed": time.perf_counter() - t0,
    }
    return result


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def stats(result: dict, data: Data) -> dict:
    nav: pd.Series = result["nav"]
    rets = nav.pct_change().dropna()
    idx = nav.index
    pos = data.dates.get_indexer(idx)
    bil = pd.Series(data.bil_tr[pos], index=idx).pct_change().dropna()
    years = result["years"]
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan
    ann_vol = rets.std(ddof=1) * math.sqrt(TRADING_DAYS)
    excess = rets - bil.reindex(rets.index).fillna(0.0)
    sharpe = excess.mean() / excess.std(ddof=1) * math.sqrt(TRADING_DAYS) if excess.std() > 0 else np.nan
    peak = nav.cummax()
    dd = nav / peak - 1.0
    trough = dd.idxmin()
    mdd = float(dd.min())
    start = nav.loc[:trough].idxmax()
    after = nav.loc[trough:]
    rec = after[after >= nav.loc[start]]
    recovery = rec.index[0] if len(rec) else None
    me_nav = nav.groupby(nav.index.to_period("M")).last()
    monthly = me_nav.pct_change().dropna()
    me_set = set(int(i) for i in data.me_idx)
    last_pos = int(pos[-1])
    trailing_partial_month = last_pos not in me_set
    if trailing_partial_month and len(monthly) > 1:
        monthly = monthly.iloc[:-1]   # a few sessions of an unfinished month are not a month
    yearly = nav.groupby(nav.index.year).last()
    prev = yearly.shift(1)
    prev.iloc[0] = nav.iloc[0]
    yearly_ret = (yearly / prev - 1.0)
    yearly_sessions = nav.groupby(nav.index.year).size()
    partial_years = []
    first_year, last_year = int(idx[0].year), int(idx[-1].year)
    first_session_of_year = data.dates[data.dates.year == first_year][0]
    if idx[0] != first_session_of_year:
        partial_years.append(first_year)
    if not (idx[-1].month == 12 and last_pos in me_set):
        partial_years.append(last_year)
    exp = result["exposure"]
    dropped = result["dropped"]
    out = {
        "cagr": float(cagr), "ann_vol": float(ann_vol), "sharpe": float(sharpe),
        "max_drawdown": mdd, "max_dd_start": str(start.date()), "max_dd_trough": str(trough.date()),
        "max_dd_recovery": str(recovery.date()) if recovery is not None else None,
        "calmar": float(cagr / abs(mdd)) if mdd < 0 else None,
        "worst_month": float(monthly.min()), "best_month": float(monthly.max()),
        "worst_month_date": str(monthly.idxmin()), "best_month_date": str(monthly.idxmax()),
        "avg_exposure": float(result["invested"].mean()),
        "avg_target_exposure": float(exp["exposure"].mean()),
        "avg_names_held": float(result["names_held"].mean()),
        "one_way_turnover_per_year": float((result["buy_notional"] + result["sell_notional"]) / 2
                                           / nav.mean() / years),
        "total_costs_usd": round(float(result["total_costs"]), 2),
        "costs_pct_nav_per_year": float(result["total_costs"] / nav.mean() / years),
        "n_trades": int(len(result["trades"])),
        "n_dropped_orders": int((dropped["outcome"] == "DROPPED_CASH").sum()) if len(dropped) else 0,
        "n_trimmed_orders": int((dropped["outcome"] == "TRIMMED_CASH").sum()) if len(dropped) else 0,
        "final_nav": round(float(nav.iloc[-1]), 2),
        "yearly_returns": {int(y): float(r) for y, r in yearly_ret.items()},
        "yearly_sessions": {int(y): int(c) for y, c in yearly_sessions.items()},
        "partial_years": sorted(set(partial_years)),
        "trailing_partial_month_excluded_from_monthly_stats": bool(trailing_partial_month),
    }
    if result["variant"] in ("regime_stocks", "regime_spy") or result["variant"] in ASSET_REGIME_VARIANTS:
        b = exp["base_exposure"]
        out["pct_time_bull"] = float((b == 1.0).mean())
        out["pct_time_half"] = float((b == 0.5).mean())
        out["pct_time_bear"] = float((b == 0.0).mean())
    if result["params"].delist_after > 0:
        # keys exist only when the delisting rule is on, so default outputs are unchanged
        dl = result["delisted"]
        out["n_delisted"] = int(len(dl))
        out["delisted_proceeds_usd"] = round(float(dl["proceeds_usd"].sum()), 2) if len(dl) else 0.0
        out["delisted_names"] = sorted(set(dl["ticker"])) if len(dl) else []
    if result["variant"] in PAIR_VARIANTS:
        pr = result["pairs"]
        p = result["params"]
        out["pair_select"] = p.pair_select if result["variant"] == "pair_rule" else "fixed"
        out["pair_lookback"] = int(p.pair_lookback)
        out["pair_keep_frac"] = float(p.pair_keep_frac) if result["variant"] == "pair_rule" else None
        out["n_pair_evaluations"] = int(len(pr))
        out["n_pair_switches"] = int(result["n_pair_switches"])
        with_pair = pr.dropna(subset=["pair_a"]) if len(pr) else pr
        out["n_evaluations_without_pair"] = int(len(pr) - len(with_pair))
        if result["variant"] == "pair_fixed":
            fa, fb = fixed_pair(p)
            out["pair"] = [fa, fb]
            out["months_fixed_pair_was_the_rules_best"] = int(
                ((with_pair["best_a"] == fa) & (with_pair["best_b"] == fb)).sum()) if len(with_pair) else 0
        else:
            pairs = [f"{a}/{b}" for a, b in zip(with_pair["pair_a"], with_pair["pair_b"])]
            counts = pd.Series(pairs).value_counts() if pairs else pd.Series(dtype=int)
            out["n_distinct_pairs"] = int(len(counts))
            out["most_held_pair"] = str(counts.index[0]) if len(counts) else None
            out["most_held_pair_months"] = int(counts.iloc[0]) if len(counts) else 0
            out["final_pair"] = [result["final"]["pair"][0], result["final"]["pair"][1]] if result["final"]["pair"] else None
    if result["variant"] in ASSET_VARIANTS:
        # keys of the single-asset variants only, so every other variant's summary reads as before
        p = result["params"]
        out["asset"] = p.asset
        out["asset_cost_bps"] = float(p.asset_cost_bps)
        out["vol_cap"] = float(p.vol_target) if result["variant"] == "asset_regime" else None
        out["trades_per_year"] = float(len(result["trades"]) / years) if years > 0 else None
    # the universe-freshness guard: stock-layer evaluations skipped because the universe's bars were not
    # there (0 for a variant without a stock-layer decision); the key comes last, after every earlier one
    out["n_stale_evaluations"] = int(result.get("n_stale_evaluations", 0))
    return out


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return str(o.date())
    if isinstance(o, Params):
        return params_dict(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(str(type(o)))


def variant_tax(result: dict, tax_params: regime_tax.TaxParams) -> dict:
    """The tax model on one variant's own trades, dividends and NAV, under nra and resident.

    Returns the summary keys added to summary.json, the per-year table (both
    regimes stacked), the realized lot rows and the per-year withdrawal rows.
    """
    p = result["params"]
    # the single-asset variants trade fractional units: the lots are matched on the 6-decimal grid
    fractional = (p.asset,) if p.variant in ASSET_VARIANTS and p.asset else ()
    rep = regime_tax.tax_report(result["trades"], result["dividends"], result["nav"], tax_params.with_regime("nra"),
                                fractional=fractional)
    match = rep["match"]
    keys: dict = {}
    frames = []
    paths = {}
    for regime in ("nra", "resident"):
        blk = rep["regimes"][regime]
        path = blk["path"]
        frames.append(blk["by_year"])
        paths[regime] = path["rows"]
        # basis of each key: cagr_after_tax_* / final_nav_after_tax_* / total_tax_withdrawn_* exclude the
        # last year's still-owed tax (no following session to withdraw it on); total_tax_* and the
        # *_incl_accrued keys include it; accrued_unpaid_tax_last_year_* is the difference
        keys[f"cagr_after_tax_{regime}"] = path["cagr_after_tax"]
        keys[f"final_nav_after_tax_{regime}"] = round(path["final_nav_after_tax"], 2)
        keys[f"total_tax_withdrawn_{regime}"] = round(path["total_tax_withdrawn"], 2)
        keys[f"total_tax_{regime}"] = round(path["total_tax_incl_accrued"], 2)
        keys[f"tax_pct_of_profit_{regime}"] = path["tax_pct_of_pre_tax_profit"]
        keys[f"accrued_unpaid_tax_last_year_{regime}"] = round(path["accrued_unpaid_tax_last_year"], 2)
        keys[f"cagr_after_tax_incl_accrued_{regime}"] = path["cagr_after_tax_incl_accrued"]
        keys[f"final_nav_after_tax_incl_accrued_{regime}"] = round(path["final_nav_after_tax_incl_accrued"], 2)
    realized = match.realized
    divs = match.dividends
    keys["wash_sale_disallowed_usd"] = round(match.wash_sale_disallowed, 2)
    # the part of the disallowed losses still sitting in open lots' basis at the end of the window:
    # a deferral not yet recovered, so the after-tax path is conservative by this amount
    keys["wash_sale_deferred_open_usd"] = round(match.wash_sale_deferred_open, 2)
    keys["n_realized_lots"] = int(len(realized))
    keys["realized_gain_usd"] = round(float(realized["gain"].sum()), 2) if len(realized) else 0.0
    keys["realized_gain_long_term_usd"] = round(float(realized.loc[realized["long_term"], "gain"].sum()), 2) if len(realized) else 0.0
    keys["dividends_stock_usd"] = round(float(divs.loc[divs["kind"] == "stock", "amount"].sum()), 2) if len(divs) else 0.0
    keys["dividends_interest_usd"] = round(float(divs.loc[divs["kind"] == "interest", "amount"].sum()), 2) if len(divs) else 0.0
    return {"keys": keys, "by_year": pd.concat(frames, ignore_index=True), "realized": realized, "paths": paths}


def add_tax_summaries(results: dict[str, dict], summaries: dict, tax_params: regime_tax.TaxParams) -> dict[str, dict]:
    """Add the after-tax keys to every variant's summary; returns the per-variant tax frames."""
    frames = {}
    for key, r in results.items():
        t = variant_tax(r, tax_params)
        summaries[key].update(t["keys"])
        frames[key] = t
    return frames


ASSET_CLI_KEYS = ("asset", "with_asset", "asset_only", "asset_cost_bps", "asset_fill", "asset_sweep",
                  "calendar_start")


def cli_dict(args) -> dict:
    """vars(args) as run_meta.json records them: the asset flags appear only in a run that uses them."""
    out = dict(vars(args))
    if not (out.get("with_asset") or out.get("asset_sweep")):
        for key in ASSET_CLI_KEYS:
            out.pop(key, None)
    return out


def units_column(col: pd.Series) -> pd.Series:
    """A share-count column that mixes whole shares and fractional units, written without a spurious '.0'.

    The result is built as an object Series row by row: `Series.map` over a
    float column would coerce the ints back to floats and write `178.0` where
    the stock variants of the same run wrote `178`.
    """
    def one(x):
        if x is None or (isinstance(x, float) and not np.isfinite(x)):
            return x
        xf = float(x)
        return int(xf) if xf.is_integer() else round(xf, UNIT_DECIMALS)
    return pd.Series([one(x) for x in col], index=col.index, dtype=object)


def asset_target_block(exposure: float, px: float, account: float) -> dict:
    """What `exposure` of an `account` is in units of the asset and in USD at the last close `px`."""
    units = floor_units(exposure * account / px) if px > 0 else 0.0
    return {"account_usd": account, "exposure": float(exposure), "asset_units": units,
            "asset_usd": round(units * px, 2), "cash_usd": round(account - units * px, 2), "last_close": px}


ASSET_REGIME_COLUMNS = ["date", "asset", "asset_close", "sma", "sma_months", "vote_a", "asset_12m_tr", "bil_12m_tr",
                        "vote_b", "state", "base_exposure"]


def write_asset_outputs(out: Path, results: dict[str, dict], data: Data, end_idx: int) -> None:
    """asset_regime.csv (the month-end votes on the asset), asset_exposure.csv, asset_instruction_latest.json.

    Written next to the standard files under their own names, so a run with
    the standard variants keeps its regime.csv / instruction_latest.json.
    """
    capped, votes_only = results.get("asset_regime"), results.get("asset_regime_votes")
    src = capped or votes_only
    if src is None:
        return
    reg = src["regime"]
    reg = reg[ASSET_REGIME_COLUMNS] if len(reg) else pd.DataFrame(columns=ASSET_REGIME_COLUMNS)
    reg.to_csv(out / "asset_regime.csv", index=False)
    ex = src["exposure"].copy()
    for key, r in (("asset_regime", capped), ("asset_regime_votes", votes_only)):
        if r is not None:
            ex[f"target_{key}"] = r["exposure"]["exposure"].to_numpy()
            ex[f"invested_{key}"] = r["invested"].to_numpy()
    ex.to_csv(out / "asset_exposure.csv", index=False)

    fin = src["final"]
    p: Params = src["params"]
    st = fin["state"]
    i = end_idx
    px = float(data.close[i, data.col[p.asset]])
    nav = fin["nav"]
    exposure_votes = float(votes_only["final"]["exposure"]) if votes_only is not None else float(fin["base"])
    orders = [{"ticker": o.ticker, "side": o.side, "units": float(o.shares), "reference_price": px,
               "approx_usd": round(float(o.shares) * px, 2), "reason": o.reason} for o in fin["orders"]]
    instruction = {
        "as_of": str(data.dates[i].date()), "asset": p.asset, "variant": src["variant"],
        "fill": p.asset_fill,
        "fill_moment": ("the CLOSE of the asset's bar dated the next SPY session after as_of "
                        "(for a UTC-day series: 20:00 New York on that date, 19:00 in winter)"
                        if p.asset_fill == "next_close" else
                        "the open of the asset's bar dated the next SPY session after as_of "
                        "(for a UTC-day series: the same instant as the signal close)"),
        "fill_moment_zh": ASSET_FILL_ZH[p.asset_fill],
        # the asset had no bar on as_of: nothing was voted, re-capped or ordered there and every price
        # below comes from `asset_close_from` (see the asset-freshness guard in run_variant)
        "asset_stale": bool(fin.get("asset_stale")),
        "asset_close_from": (str(pd.Timestamp(fin["asset_close_from"]).date())
                             if fin.get("asset_close_from") is not None else None),
        "asset_vote_skipped": (str(pd.Timestamp(fin["asset_vote_skipped"]).date())
                               if fin.get("asset_vote_skipped") is not None else None),
        "state": st["state"] if st else None,
        "votes": {
            "vote_a": {"passed": st["vote_a"], "asset_close": st["asset_close"], "sma": st["sma"],
                       "sma_months": st["sma_months"]},
            "vote_b": {"passed": st["vote_b"], "asset_12m_tr": st["asset_12m_tr"], "bil_12m_tr": st["bil_12m_tr"],
                       "difference": st["asset_12m_tr"] - st["bil_12m_tr"]},
            "evaluated_at": str(st["date"].date()),
        } if st else None,
        "base_exposure": st["base_exposure"] if st else None,
        "asset_vol20": fin["asset_vol20"], "vol_target": p.vol_target, "vol_floor": p.vol_floor,
        "vol_hysteresis": p.vol_hyst, "v_raw": fin["v_raw"], "v_applied": fin["v_applied"],
        "exposure": fin["exposure"],                         # votes x volatility cap (asset_regime)
        "exposure_votes_only": exposure_votes,               # asset_regime_votes
        "last_close": px,
        "target_100k": asset_target_block(float(fin["exposure"]), px, 100_000.0),
        "target_100k_votes_only": asset_target_block(exposure_votes, px, 100_000.0),
        "backtest_account": {"nav": round(nav, 2), "cash": round(fin["cash"], 2),
                             "units": {t: float(s) for t, s in sorted(fin["shares"].items())},
                             "drawdown_from_peak": nav / float(src["nav"].max()) - 1.0},
        "orders_next_open": orders,
    }
    (out / "asset_instruction_latest.json").write_text(json.dumps(instruction, indent=2, default=_json_default))


def write_outputs(out: Path, results: dict[str, dict], data: Data, summaries: dict,
                  start_idx: int, end_idx: int, args, base_params: Params,
                  tax: dict[str, dict] | None = None, tax_params: regime_tax.TaxParams | None = None,
                  asset_sensitivity: dict | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # single-asset variants in the run: their share counts are fractional, so the mixed columns are written
    # through `units_column`, and the asset's own files are added; a run without them writes exactly what it did
    has_asset = any(r["variant"] in ASSET_VARIANTS for r in results.values())
    period = {"start": str(data.dates[start_idx].date()), "end": str(data.dates[end_idx].date()),
              "sessions": int(end_idx - start_idx + 1)}
    summary = {"period": period, "variants": summaries}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=_json_default))

    equity = pd.concat([r["nav"] for r in results.values()], axis=1)
    equity.index.name = "date"
    equity.round(2).to_csv(out / "equity.csv")
    (equity / equity.cummax() - 1.0).round(6).to_csv(out / "drawdown.csv")

    rs = results.get("regime_stocks")
    if rs is not None and "regime_spy" in results:
        ex = rs["exposure"].copy()
        ex["regime_spy_exposure"] = results["regime_spy"]["exposure"]["exposure"].to_numpy()
        ex.to_csv(out / "exposure.csv", index=False)

    if rs is not None:
        reg = rs["regime"].copy()
        cols = ["date", "spy_close", "sma10", "vote_a", "spy_12m_tr", "bil_12m_tr", "vote_b",
                "state", "n_eligible", "n_held", "n_dropped_min_weight"]
        # the freshness columns are written only when a month-end ranking WAS stale, so a run
        # without one (every historical run so far) keeps regime.csv byte for byte as it was
        if "universe_stale" in reg.columns and bool(reg["universe_stale"].any()):
            cols += ["universe_stale", "n_fresh", "n_live"]
        reg = reg[cols]
        reg.to_csv(out / "regime.csv", index=False)
        rs["holdings"].to_csv(out / "holdings_monthly.csv", index=False)

    trades = pd.concat([r["trades"] for r in results.values()], ignore_index=True)
    if has_asset:
        trades["shares"] = units_column(trades["shares"])
    trades.sort_values(["date", "variant", "side", "ticker"]).to_csv(out / "trades.csv", index=False)
    frames = [r["dropped"] for r in results.values() if len(r["dropped"])]
    dropped = pd.concat(frames, ignore_index=True) if frames else next(iter(results.values()))["dropped"]
    if has_asset:
        dropped = dropped.copy()
        for c in ("shares_requested", "shares_filled"):
            dropped[c] = units_column(dropped[c])
    dropped.sort_values(["date", "variant", "ticker"]).to_csv(out / "dropped_orders.csv", index=False)

    # --delist-after: every liquidation of a name whose history ended
    if base_params.delist_after > 0:
        dl_frames = [r["delisted"] for r in results.values() if len(r["delisted"])]
        delisted = pd.concat(dl_frames, ignore_index=True) if dl_frames else pd.DataFrame(columns=DELISTED_COLUMNS)
        # as for trades.csv: a mixed run's shares column holds whole shares and fractional units, and
        # without this the stock rows would be written as "1328.0" because the column infers float64
        if has_asset and len(delisted):
            delisted = delisted.copy()
            delisted["shares"] = units_column(delisted["shares"])
        delisted.sort_values(["date", "variant", "ticker"]).to_csv(out / "delisted.csv", index=False)
        cp_frames = [r["delisted_pending"] for r in results.values() if len(r["delisted_pending"])]
        cancelled = pd.concat(cp_frames, ignore_index=True) if cp_frames else pd.DataFrame(columns=CANCELLED_COLUMNS)
        if has_asset and len(cancelled):
            cancelled = cancelled.copy()
            cancelled["shares"] = units_column(cancelled["shares"])
        cancelled.sort_values(["date", "variant", "ticker"]).to_csv(out / "delisted_pending.csv", index=False)

    # pair variants: the pair (and the rule's choice) at every evaluation
    pair_frames = [r["pairs"].assign(variant=key) for key, r in results.items()
                   if r["variant"] in PAIR_VARIANTS and len(r["pairs"])]
    if pair_frames:
        pairs = pd.concat(pair_frames, ignore_index=True)
        pairs = pairs[["variant"] + PAIR_ROW_COLUMNS]
        pairs["date"] = pd.to_datetime(pairs["date"]).dt.date
        pairs.to_csv(out / "pairs_chosen.csv", index=False)

    # the tax model on every variant's own trades and dividends
    if tax:
        by_year = pd.concat([t["by_year"].assign(variant=key) for key, t in tax.items()], ignore_index=True)
        by_year = by_year[["variant"] + regime_tax.TAX_COLUMNS]
        by_year.sort_values(["variant", "regime", "year"]).round(2).to_csv(out / "tax_by_year.csv", index=False)
        lots = [t["realized"].assign(variant=key) for key, t in tax.items() if len(t["realized"])]
        realized = (pd.concat(lots, ignore_index=True) if lots
                    else pd.DataFrame(columns=["variant"] + regime_tax.REALIZED_COLUMNS))
        realized = realized[["variant"] + regime_tax.REALIZED_COLUMNS]
        if has_asset:
            realized = realized.copy()
            realized["shares"] = units_column(realized["shares"])
        realized.to_csv(out / "realized_lots.csv", index=False)

    # instruction for the next open
    if rs is not None:
        fin = rs["final"]
        i = end_idx
        nav = fin["nav"]
        peak = float(rs["nav"].max())
        st = fin["state"]
        targets = fin["targets"]
        def target_list(scale_nav: float):
            rows = []
            for t, w in targets.sort_values(ascending=False).items():
                px = float(data.close[i, data.col[t]])
                rows.append({"ticker": t, "target_weight": round(float(w), 6),
                             "target_shares": int(math.floor(w * scale_nav / px)), "last_close": px})
            return rows
        orders = []
        for o in fin["orders"]:
            px = float(data.close[i, data.col[o.ticker]])
            orders.append({"ticker": o.ticker, "side": o.side, "shares": int(o.shares),
                           "reference_price": px,
                           "limit_hint": round(px * (1.003 if o.side == "BUY" else 0.997), 2),
                           "reason": o.reason})
        instruction = {
            "as_of": str(data.dates[i].date()), "variant": "regime_stocks",
            "next_open": "next SPY session after as_of",
            "state": st["state"] if st else None,
            "votes": {
                "vote_a": {"passed": st["vote_a"], "spy_close": st["spy_close"], "sma10": st["sma10"]},
                "vote_b": {"passed": st["vote_b"], "spy_12m_tr": st["spy_12m_tr"], "bil_12m_tr": st["bil_12m_tr"],
                           "difference": st["spy_12m_tr"] - st["bil_12m_tr"]},
                "evaluated_at": str(st["date"].date()),
            } if st else None,
            "base_exposure": st["base_exposure"] if st else None,
            "v_applied": fin["v_applied"], "exposure": fin["exposure"],
            "breaker_on": fin["breaker_on"],
            "nav": round(nav, 2), "cash": round(fin["cash"], 2),
            "drawdown_from_peak": nav / peak - 1.0,
            "current_holdings": {t: int(s) for t, s in sorted(fin["shares"].items())},
            "target_holdings": target_list(nav),
            "orders_next_open": orders,
            "target_holdings_100k": target_list(100_000.0),
        }
        if fin.get("universe_stale"):
            # only when the ranking in force is stale (the file is unchanged otherwise): the list
            # below is the PREVIOUS month's, kept because the universe's bars were not there
            instruction["universe_stale"] = {
                "n_fresh": fin["n_fresh"], "n_live": fin["n_live"], "evaluated": fin["universe_evaluated"],
                "stale_since": fin["stale_since"],
                "note": "the month-end ranking was skipped (too few universe names had a bar); the held list is the "
                        "previous one and the ranking is retried at every session until the data is there",
            }
        (out / "instruction_latest.json").write_text(json.dumps(instruction, indent=2, default=_json_default))

    if has_asset:
        write_asset_outputs(out, results, data, end_idx)

    point_in_time = data.member is not None
    assumptions = list(ASSUMPTIONS)
    warnings = list(WARNINGS)
    if point_in_time:
        assumptions += MEMBERSHIP_ASSUMPTIONS
        warnings[0] = POINT_IN_TIME_WARNING
    if base_params.delist_after > 0:
        assumptions += DELIST_ASSUMPTIONS
    meta = {
        "tool": "tools/regime_backtest.py (research tooling, outside the record)",
        "parameters": params_dict(base_params),
        "period": period,
        "universe_size": len(data.tickers),
        "bil_present": data.bil_present,
        "start_nav": START_NAV, "slippage_per_side": SLIPPAGE, "min_trade_usd": MIN_TRADE_USD,
        "cli": cli_dict(args),
        "assumptions": assumptions + data.notes,
        "warnings": warnings,
        "cash_short_buys": {
            v: {"n_trimmed": s["n_trimmed_orders"], "n_dropped": s["n_dropped_orders"],
                "session_days_with_a_drop": int(results[v]["dropped"].loc[
                    results[v]["dropped"]["outcome"] == "DROPPED_CASH", "date"].nunique())}
            for v, s in summaries.items()
        },
        "timings_sec": {k: round(r["elapsed"], 2) for k, r in results.items()},
        "pair_variants": {
            "variants": {key: params_dict(r["params"]) for key, r in results.items() if r["variant"] in PAIR_VARIANTS},
            "assumptions": PAIR_ASSUMPTIONS,
        },
        "tax": {
            "regimes": {
                "nra": (tax_params or regime_tax.TaxParams()).with_regime("nra").__dict__,
                "resident": (tax_params or regime_tax.TaxParams()).with_regime("resident").__dict__,
            },
            "assumptions": regime_tax.ASSUMPTIONS,
            "withdrawals": {key: t["paths"] for key, t in (tax or {}).items()},
        },
        # a new key (everything above reads as before): the guard's rule and what it did in every variant
        "universe_freshness": {
            "freshness_min": base_params.freshness_min, "window_sessions": FRESHNESS_WINDOW,
            "min_live": FRESHNESS_MIN_LIVE, "guarded_variants": list(GUARDED_VARIANTS),
            "assumptions": FRESHNESS_ASSUMPTIONS,
            "by_variant": {
                key: {"n_evaluations": int(len(r["freshness"])),
                      "n_stale_evaluations": int(r["n_stale_evaluations"]),
                      "n_retries": int((r["freshness"]["kind"] == "retry").sum()) if len(r["freshness"]) else 0,
                      "stale_evaluations": [{"date": row["date"], "kind": row["kind"], "n_fresh": int(row["n_fresh"]),
                                             "n_live": int(row["n_live"])}
                                            for row in r["freshness"].to_dict(orient="records") if row["stale"]][:200],
                      "stale_at_end": bool(r["final"]["universe_stale"])}
                for key, r in results.items()
            },
        },
    }
    if point_in_time:
        member_counts = data.member[data.me_idx][:, [data.col[t] for t in data.tickers]].sum(axis=1) if len(data.tickers) else np.zeros(0)
        meta["membership"] = {
            "file": data.membership_csv,
            "members_at_month_ends": {str(data.dates[i].date()): int(c) for i, c in zip(data.me_idx, member_counts)},
            "rule": "candidates at T = index members on T (start_date <= T) plus the names currently held; "
                    "a name that left the index is never admitted anew but is kept until the strategy's own rules exit it",
        }
    if has_asset:
        j = data.col[base_params.asset]
        bars = np.flatnonzero(data.has_bar[:, j])
        no_bar = sorted({str(pd.Timestamp(row["date"]).date()) for r in results.values()
                         for row in r.get("asset_stale_sessions") or []})
        meta["asset"] = {
            "ticker": base_params.asset, "cost_bps_per_side": base_params.asset_cost_bps,
            "unit_decimals": UNIT_DECIMALS,
            "fill": base_params.asset_fill,
            "fill_zh": ASSET_FILL_ZH[base_params.asset_fill],
            "fill_timing_sensitivity": asset_sensitivity,
            "variants": [key for key, r in results.items() if r["variant"] in ASSET_VARIANTS],
            "calendar_start": data.calendar_start,
            "first_session_with_a_bar": str(data.dates[bars[0]].date()) if len(bars) else None,
            "last_session_with_a_bar": str(data.dates[bars[-1]].date()) if len(bars) else None,
            "sessions_without_a_bar_in_window": int((~data.has_bar[start_idx: end_idx + 1, j]).sum()),
            # the asset-freshness guard: sessions of the window on which the asset had no bar, so
            # nothing was voted, re-capped or ordered on them
            "sessions_without_a_bar": no_bar[:200],
            "assumptions": ASSET_ASSUMPTIONS + (BTCUSD_DATA_NOTES if base_params.asset == "BTCUSD" else []),
            "tax_assumptions": regime_tax.CRYPTO_ASSUMPTIONS,
            "warnings": ASSET_WARNINGS,
        }
    if base_params.delist_after > 0:
        meta["delisting"] = {
            "delist_after_sessions": base_params.delist_after, "haircut": base_params.delist_haircut,
            "by_variant": {
                key: {"n_liquidations": int(len(r["delisted"])),
                      "proceeds_usd": round(float(r["delisted"]["proceeds_usd"].sum()), 2) if len(r["delisted"]) else 0.0,
                      "names": sorted(set(r["delisted"]["ticker"])) if len(r["delisted"]) else [],
                      "n_pending_cancelled": int(len(r["delisted_pending"])),
                      "pending_cancelled_names": sorted(set(r["delisted_pending"]["ticker"])) if len(r["delisted_pending"]) else []}
                for key, r in results.items()
            },
            "ended_histories": data.ended_note,
        }
    (out / "run_meta.json").write_text(json.dumps(meta, indent=2, default=_json_default))


ASSUMPTIONS = [
    "Cash is not held as BIL shares; the cash balance earns BIL's daily total return (adjusted close plus split-adjusted div_cash), so no BIL trades or BIL commissions appear.",
    "Dividends: div_cash is divided by the same cumulative split factor as prices (f[s] = product of split_factor of later sessions). Holders at the previous close receive the ex-date cash; a name sold at the open of its ex-date still receives it. No off-calendar ex-dates exist in the cache.",
    "Total-return indices are built on the forward-filled close aligned to the SPY calendar; a day without a bar contributes a zero return.",
    "63-session and 20-session realized vol use log returns of the forward-filled close with ddof=1, annualized by sqrt(252). A gap day contributes a zero log return.",
    "Liquidity filter: median of adjusted close x adjusted volume (equal to raw close x raw volume) over the last 20 sessions on which the ticker has a bar.",
    "'At least 13 month-end closes' is read as: the ticker's first bar is on or before the 13th-most-recent month-end (so the 12-1 momentum window exists). In the sweep with momentum lookback L the requirement is max(13, L+1) month-ends.",
    "V_applied is initialized to V_raw at the start month-end (the first session of the backtest) and the hysteresis test uses strict > 0.10.",
    "Name and bucket caps are applied jointly and iteratively: names above 10 percent and buckets above 30 percent are frozen at the cap and the shortfall is redistributed pro rata to names that are still free; when everything is frozen (fewer than 10 names, or fewer than 4 buckets, or one bucket holding most of the held set) the unplaceable remainder is the residual that goes to SPY. After dropping names below 2 percent the survivors are renormalized once to the pre-drop total, again subject to the caps and with no second minimum-weight pass, so no name ever exceeds 10 percent and no bucket 30 percent; the weight that cannot be placed joins the SPY residual.",
    "A name that the 2 percent minimum (or the cap squeeze) leaves with zero weight is not 'currently held': it leaves the held set at that month-end, its slot is refilled from the rank <= 20 candidates and the weights are recomputed, iterating until every held name carries weight (a name dropped this way cannot re-enter in the same evaluation, so the loop terminates). n_held in regime.csv therefore counts positions with weight; n_dropped_min_weight counts the names squeezed out at that month-end. When the candidates run out the remainder is still the SPY residual.",
    "Month-ends are the last SPY session of each calendar month. With --end before the last cached session, whether the truncated tail is a completed month-end is decided against the untruncated SPY calendar (exact, holiday-aware); for the genuinely last session of the data the tail counts only when no weekday of its month remains after it, so a trailing partial month (2026-09-04) is not a signal date and the latest instruction uses the 2026-08-31 evaluation.",
    "Sleeve weights are fixed at the month-end and held until the next month-end; the daily sync only reacts to V_applied changes and to drift beyond the bands.",
    "Whole-share targets are floor(target_weight x NAV[T] / close[T]) computed at the signal close with no cost reserve, so when the opens gap up the set of buys can exceed the cash at the open (fill plus commission). Buys are then walked in ascending target-weight order: the smallest that does not fit is trimmed to the cash remaining after the others (whole shares, commission included) and dropped only when the trimmed lot is below the 500 USD minimum; every trim and drop is written to dropped_orders.csv and counted in summary.json (n_trimmed_orders, n_dropped_orders). A dropped buy is re-signalled at the next close with its original reason code. Without the trim a 100 percent single-instrument entry (spy_bh, regime_spy) would only fill on the first down-gap day. The drops that remain are band top-ups (and occasionally the smallest month-end entries) signalled while the account is fully invested and holds only a few hundred USD of cash; with no sell to fund them they are re-signalled and dropped daily until the next month-end changes the targets (run_meta.json 'cash_short_buys' gives the counts).",
    "An order for a ticker that has no bar at T+1 stays pending and fills at the ticker's next bar; no new order for that ticker is generated while one is pending.",
    "Trade-to-target inside the band uses the same whole-share floor; a resulting difference of zero shares or below 500 USD is skipped (exits are never skipped).",
    "Reason codes: exits with exposure 0 are REGIME_EXIT, other full exits RANK_EXIT (SPY residual: RESIDUAL_SPY); full entries after the base exposure rises from 0 are REGIME_ENTER (regime variants only), other stock entries RANK_ENTER, regime_spy's SPY-sleeve entry REGIME_ENTER, and the entries of the non-regime benchmarks spy_bh and ew_universe_bh RANK_ENTER (the fixed code list has no benchmark code); partial trades are REGIME_ENTER/EXIT when the base exposure changed at this month-end, VOL_CAP when V_applied changed today (or the breaker fired), otherwise REBALANCE_BAND. A buy dropped for cash carries its original reason when re-signalled.",
    "stocks_only forces base exposure 1.0 and V_applied 1.0 but keeps the SPY residual and all costs/bands. ew_universe_bh sets equal 1/N targets at each month-end and on month-end sessions trades every name back to target regardless of the band (whole shares, 500 USD minimum, same slippage and commission); between month-ends the same daily band sync applies. The band is bypassed at month-ends because the 1 percent absolute band is wider than a 1/N target of ~1.1-1.3 percent: with the band alone a name is trimmed after roughly doubling but never topped up, and trim proceeds plus dividends accumulated in cash (in the unpatched run the benchmark averaged 82.6 percent invested and ended with 398.5k of 1,107k NAV in cash), which is neither equal-weight nor monthly rebalanced.",
    "spy_bh targets SPY at 100 percent; with a 25 percent relative band the position is never rebalanced, dividends accumulate in cash earning BIL (as the brief specifies), so spy_bh trails SPY's total return and its avg_exposure is below 1.",
    "avg_exposure is the mean invested fraction (non-cash market value / NAV) over sessions; avg_target_exposure is the mean of base x V_applied. avg_names_held counts every non-cash position, SPY included.",
    "one_way_turnover_per_year = (buy notional + sell notional) / 2 / mean NAV / years. costs_pct_nav_per_year = (commissions + slippage) / mean NAV / years. Sharpe is on daily returns in excess of BIL's daily total return.",
    "Yearly returns are calendar-year, the first year measured from the start NAV; summary.json lists partial_years (here 2015 from 2015-06-30 and 2026 to 2026-09-04) and yearly_sessions next to yearly_returns. Monthly returns are between month-end NAVs; a trailing partial month (2026-09, four sessions) is excluded from worst_month/best_month.",
    "In the sweep, sma_months changes the market regime SMA only; the per-stock 10-month trend filter is unchanged. hold_rank = 2 x top_n. votes a_only / b_only map a single passing vote to BULL and a failing vote to BEAR.",
    "The circuit breaker compares NAV to the max NAV over the last 20 sessions (including today), zeroes all targets, and resets at the next month-end evaluation.",
    "holdings_monthly.csv 'shares' are the target shares at the month-end close (floor(portfolio_weight x NAV / close)), not the shares filled at the next open; portfolio_weight uses the same exposure (base x V_applied of that session, breaker included) that builds the orders for the next open.",
]

PAIR_ASSUMPTIONS = [
    "pair_fixed holds two tickers 50/50, fully invested (base exposure 1.0, no volatility cap, no regime vote), and at every month-end trades both back to exactly 50/50 regardless of the band (whole shares, 500 USD minimum, the same slippage and commission); there are no intra-month trades. Reason codes: PAIR_ENTER for the first buys, PAIR_REBALANCE for month-end trades.",
    "pair_rule uses the same portfolio mechanics but chooses the pair at each month-end with data through that close only: candidates are universe names with a bar at T, a first bar at or before T - pair_lookback sessions (so the full window of daily returns exists) and the engine's liquidity filter (median dollar volume over 20 sessions >= 20M USD); daily log returns come from the total-return index; score(A, B) = annualized variance of (rA - rB) = volA^2 + volB^2 - 2 rho volA volB, eight times the theoretical 50/50 rebalancing bonus (the most volatile, least correlated pair scores highest); the pair with the largest score is chosen. Hysteresis: the current pair is kept while both names remain candidates and its score >= pair_keep_frac x the best score (1.0 = no hysteresis). A switch sells the old names and buys the new ones (PAIR_SWITCH); a name common to both pairs is only rebalanced (PAIR_REBALANCE).",
    "pair_select = topvol_lowcorr is the alternative rule of the sweep: the most volatile candidate, then the least correlated partner among the other nine of the ten most volatile (pair_top_vol = 10 names in play, the leader included); the hysteresis test compares variance-of-difference scores exactly as above.",
    "When fewer than two candidates qualify (the 504-session lookback has none before mid-2016) the pair variant holds SPY (RESIDUAL_SPY legs) until a pair qualifies; pairs_chosen.csv records every evaluation with the chosen pair, its score, vols and correlation, the rule's best pair and whether the current pair was kept.",
    "A pair account started inside a month (the paper job) buys the pair at the next open (PAIR_ENTER); pair_rule picks its first pair at the start close with data through that close, then re-evaluates at every month-end.",
    "Pair results are stock-level results on the survivorship-biased universe: MSTR/SMCI was the best pair IN HINDSIGHT of the 2,485 early-listed pairs (tools/rebalance_test.py E4) and its buy-and-hold multiples are inflated by the same selection; read pair_fixed as an illustration of a known outcome, never as an achievable return, and read pair_rule (the causal rule) against the two names' own buy-and-hold and against ew_universe_bh.",
]

MEMBERSHIP_ASSUMPTIONS = [
    "POINT-IN-TIME UNIVERSE (--membership): at every month-end T the candidate set of eligible_and_rank (regime_stocks, stocks_only), of the ew_universe_bh admission and of pair_candidates (pair_rule) is the index members on T, read from the membership file as start_date <= T and (end_date empty or end_date > T). Only start_date <= T is knowledge of the past; whether a name will later leave the index is never used.",
    "A name that leaves the index is never admitted anew, but a HELD name is not sold for leaving: it stays in the candidate set while it is held, so the strategy's own rules (trend, momentum, rank <= hold_rank, liquidity, a bar at T) decide when it exits, exactly as for a member; it occupies a rank slot meanwhile. pair_rule draws every NEW pair from index members on T only; the current pair's legs are scored alongside so the hysteresis test can keep the pair, which is the only way a leg that has left the index survives (it never enters a fresh pair; when fewer than two members qualify a still-scoreable current pair is kept because nothing may replace it). n_candidates in pairs_chosen.csv counts the members plus the kept legs. ew_universe_bh, whose rule IS the monthly 1/N reconstitution, drops a name that left at the next month-end.",
    "Membership intervals are keyed by the engine symbol (the `ticker` column): a rename (FB -> META) is one continuous membership of META, so no exit and re-entry is booked at the rename. Tickers in the universe file without any interval are never candidates; intervals for symbols not in the universe are ignored (both are listed in the notes).",
    "Sectors are known for current members only (bucket UNKNOWN otherwise), so the point-in-time runs are made with --bucket-cap 1.0: the 30 percent bucket cap would otherwise squeeze the whole UNKNOWN bucket to 30 percent and hand the rest to SPY.",
]

DELIST_ASSUMPTIONS = [
    "DELISTING (--delist-after N): a held name with no bar on session i whose cached history has ended and whose last bar is at least N sessions before i is liquidated at i at its last close x (1 - delist_haircut), with no commission or slippage; proceeds go to cash (they count in sell_notional), the trade is booked with reason DELISTED, the name's pending orders are cancelled, it leaves the held set and the sleeve, and no order is ever built for it again. A name signalled on what turns out to be its final bar has no shares to liquidate: its never-filled order is cancelled after the same N sessions (delisted_pending.csv). The default 0 turns the rule off and leaves every existing output unchanged.",
    "'Ended' is judged from the cache alone: a history counts as ended when its last cached bar lies at least N sessions before SPY's last cached bar on the untruncated calendar; tiingo_end from data/sp500/tickers.csv is not consulted. The cache must therefore be refreshed in one pass (SPY and the stocks together), or --end must not run past the stocks' last cached session, otherwise a still-trading name whose file lags SPY by N or more sessions would be sold as DELISTED at a stale close. run_meta.json (delisting.ended_histories) lists every history treated as ended with its last bar and its lag, and warns about files that lag SPY by fewer than N sessions.",
    "Whether a silence is final is read from the cache's LATER bars, i.e. with hindsight: a gap of N or more sessions that is followed by later bars (a halt, or two listings under one symbol) is never liquidated, the position is marked at the carried-forward close until bars resume. This is the treatment a holder gets in a real halt (no sale is possible either); the look-ahead is only in knowing that bars will return.",
    "The last close of an acquired name is close to the deal price (so the liquidation is realistic, if a few days early for cash deals), that of a bankrupt name close to zero; --delist-haircut H is the sensitivity to the liquidation price. Names whose Tiingo history continues on the OTC market after failing (SIVBQ, FRCB) are not delisted by this rule; the strategy's own rules exit them at the OTC price.",
]

FRESHNESS_ASSUMPTIONS = [
    "UNIVERSE-FRESHNESS GUARD: every stock-layer evaluation (the month-end ranking of regime_stocks / stocks_only, the ew_universe_bh admission, the pair_rule selection including the start-session evaluation of an account started inside a month) first counts, at its session t and with data <= t only, the LIVE names (universe tickers, index members only with --membership, with at least one bar in sessions [t-63, t]) and the FRESH ones (a bar at t itself and no missing bar in the liq_window sessions ending at t: the liquidity median is NaN for liq_window sessions after a hole, which makes a name ineligible exactly as a missing bar at t does, so without this half a session on which the bars are back but the windows are not would 'choose' the few names that never had a hole; a name listed fewer than liq_window sessions ago counts as fresh). With at least 5 live names and fresh / live below freshness_min (0.8) the evaluation is STALE: the eligibility tests all require a bar at t, so ranking or pairing on such a session would 'choose' whatever few names happen to have been refreshed, which is an artifact of the cache and not a decision of the rule.",
    "A stale evaluation re-chooses nothing: regime_stocks / stocks_only keep the held set and the sleeve weights, ew_universe_bh keeps its sleeve, pair_rule keeps its pair (a pair_rule account without a pair stays in the SPY sleeve) and does not switch; no RANK_ENTER / RANK_EXIT / PAIR_ENTER / PAIR_SWITCH order can come from it. Everything that does not read the universe still happens on that session: the market vote, the volatility cap, the band sync and the month-end 50/50 rebalance of a pair already held.",
    "A stale evaluation is retried at the NEXT session and at every session after it until one is fresh (not only at the next month-end), so late data heals within a day: a late fetch fills the missing sessions in, and the replay of the paper job then finds the ORIGINAL evaluation fresh. Bars that stay missing (a true hole in a static cache) heal only once the liquidity window is complete again, liq_window sessions later. The retry reads the bar, close and liquidity of its own session and, for the ranking, the month-end grid through the latest month-end (trend average, 12-1 momentum), so it is causal; it is recorded with its own date (pairs_chosen.csv, holdings_monthly.csv). regime.csv keeps one row per month-end vote: a stale month-end row carries universe_stale / n_fresh / n_live (the columns are written only when a run has a stale month-end) and n_eligible there is what the skipped ranking saw.",
    "A name whose history ended more than a quarter ago, or has not begun, has no bar in [t-63, t] and is not live, so old delistings and late listings never count against freshness; a name that died within the quarter counts as live but not fresh, which matters only if a fifth of the universe dies in one quarter. The window is a quarter rather than a fortnight because it is also how long a cache may go unrefreshed before its silent names stop counting toward a FIRST verdict: an evaluation made with no stale run in force on a cache whose other names have been silent for more than a quarter judges the few refreshed names against themselves and passes them, and with fewer than 5 live names it does not judge at all. INSIDE a run of stale evaluations the denominator cannot shrink: every retry is judged against the largest live count the run has seen (n_live_ref in result['freshness'] and result['final']; n_live stays as measured), so a run ends only when enough names are fresh again, and in a cache that never gets them it never ends (the pair or the held set is kept: fail-closed). The paper job keeps the cache inside that window from its side by fetching the whole universe at every month-end, at a pair_rule start session and after every stale evaluation.",
    "pair_fixed decides nothing from the universe; its pairs_chosen rows carry stale / n_fresh / n_live as information only (the rule's 'best pair' next to a fixed pair is meaningless on a stale session) and they are not counted in n_stale_evaluations. freshness_min 0 switches the guard off.",
]

ASSET_ASSUMPTIONS = [
    "SINGLE-ASSET VARIANTS (--asset T --with-asset): the month-end regime method run on ONE cache ticker that is not in the universe file (T is loaded through load_data's extra_tickers and is never a candidate of the stock layer). asset_regime = the two month-end votes on the asset itself times the daily volatility cap on the asset's own vol; asset_regime_votes = the votes without the cap; asset_bh = buy and hold; asset_cash_5050 = half asset, half cash, traded back to one half at every month-end. The asset is the whole sleeve; whatever is not in it is cash earning BIL's daily total return.",
    "Votes: vote A = the asset's close above the mean of its last sma_months (10) month-end closes; vote B = its tr_months (12) return above BIL's over the same month-ends; two votes 1.0, one 0.5, none 0.0 (votes a_only / b_only in the sweep map the single vote to 1.0 / 0.0). The asset pays no dividend, so its total-return index is its price. A month-end is the last SPY session of a calendar month, as everywhere in this engine.",
    "Volatility cap (asset_regime only): V_raw = clip(vol_target / the asset's own 20-session realized vol, vol_floor 0.25, 1) with the 0.10 hysteresis, the vol computed on session-to-session log returns of the asset's close and annualized by sqrt(252) exactly like SPY's (a Friday-to-Monday return spans three calendar days of a 24/7 market, so this is a per-NYSE-session figure, not a calendar-day one). An asset whose vol is 50 to 80 percent keeps the cap between the 0.25 floor and about 0.40 nearly all the time: asset_regime is, by construction, mostly a quarter-to-a-third position even with both votes passing. vol_target 0.40 in the sweep is the same rule with a looser target.",
    "TIMING of a 24/7 market on NYSE session days: signals are computed on SPY's sessions only and the asset's bar of a session is its bar DATED that day. BTCUSD is stored as UTC days (tools/crypto_fetch.py), so the bar of date D covers 00:00 to 24:00 UTC: its close prints at 20:00 New York time on D in summer (19:00 in winter), four hours AFTER the NYSE close of D, and the OPEN of the next session's bar is the 00:00 UTC print of that date, which after a weekday is the SAME instant as the signal close. Filling there would give the asset variants a zero-latency fill the stock variants never get (16:00 to 09:30 ET), and would let the paper book record a fill at a price that printed BEFORE the 21:15 sheet ordering it existed. So asset_fill defaults to 'next_close': an order signalled at the close of T fills at the close of the bar dated T+1, i.e. 20:00 New York on T+1, about 23 hours after the sheet is written and reachable by anyone who reads it. This is a conservative bound for spot (a 24/7 buyer could act within the hour) and about right for a spot ETF, which can only trade at 09:30 ET on T+1. asset_fill 'open' is kept as the sensitivity; run_meta records both. Weekend and holiday bars are never looked at: the move across them arrives as one session-to-session return.",
    "ASSET FRESHNESS: a session on which the asset has NO bar decides nothing. close[i] is carried forward from the last bar, so a vote, a volatility factor or an order size taken from it would silently be an earlier session's price. On such a session no month-end vote is taken (the state in force is kept and the skipped month-end is named in the result; a cache that later gets the bar makes the same replay vote normally), the volatility factor is held where it was, and NO order is built; the position is still marked at the carried close. The window's no-bar sessions are listed in run_meta (asset.sessions_without_a_bar). The vote's own SMA window and 12-month reference are read off the month-end closes as stored: a month-end inside the window that has no bar contributes its carried close, which is one point of ten rather than the decision itself.",
    "Units and costs: order sizes are floored onto a 6-decimal grid (floor_units) and positions are kept on it; the whole-share floor does not apply. The cost is asset_cost_bps (default 10) basis points of traded notional per side, booked in the trade's commission column; it REPLACES the per-share commission schedule and the 5 bp slippage for these variants only (the fill price is the bar print itself, see the timing note). The 500 USD minimum trade, the rebalance bands (1 percent absolute, 25 percent relative), sells before buys and the trim-to-cash rule (a buy sized on close[T] that does not fit the cash at the fill is trimmed to what the cash buys, cost included) are the engine's own.",
    "asset_bh buys the asset with the whole account at the first open and never trades again (there is no dividend, so unlike spy_bh no cash builds up). asset_cash_5050 targets one half of the account in the asset, the other half in cash earning BIL, and at every month-end trades back to exactly one half regardless of the band (ASSET_REBALANCE, 500 USD minimum); there are no trades inside the month. Reason codes: ASSET_ENTER for the first buy of these two; asset_regime(_votes) use the regime codes (REGIME_ENTER / REGIME_EXIT / VOL_CAP / REBALANCE_BAND), the regime being the asset's own.",
    "The window starts at the first month-end at which both the calendar and the asset have 13 month-end closes (find_asset_start); --calendar-start prepends NYSE sessions (quantdesk.calendar, XNYS) to SPY's calendar when the asset's history is older than SPY's cached one, and such a run is made with --asset-only because SPY has no bar on the prepended sessions. An asset account started INSIDE a month (the paper job) takes the vote in force at its start, i.e. the evaluation at the latest month-end before the start (data before the start only), instead of sitting fully invested until the first month-end.",
]

BTCUSD_DATA_NOTES = [
    "BTCUSD bars come from Tiingo's crypto endpoint (an aggregate over the exchanges Tiingo covers), one bar per UTC day, the still-forming day dropped. 2011-2012 bars rest on a few coins to a few thousand coins of volume a day, and 33 calendar days have no bar at all (30 of them in 2011, 3 in January 2015); a session without a bar keeps the previous close for marking and leaves orders pending until the next bar.",
    "In 2015-2018 the day's open differs from the previous day's close, which is the same instant in a 24/7 market, by a median 1 to 3 percent in absolute value (the two prints come from different exchange mixes; mean +0.1 to +1.0 percent). A fill at the open therefore carries that much noise in those years: buys slightly dear and sells slightly rich, which roughly cancels over a round trip but is not a price anyone could be sure of getting. From 2019 on the two prints agree to a few basis points.",
]

ASSET_WARNINGS = [
    "SURVIVORSHIP: Bitcoin is itself the survivor among thousands of coins; choosing it today and testing it on its own past is the strongest selection bias in this whole project. Every figure of the asset variants, buy-and-hold first of all, is an outcome known in advance, not an expectation.",
    "The history holds three or four boom-and-bust cycles, all of them in a young asset whose early prices rest on tiny volumes; a month-end rule fitted to so few cycles proves little, and the sweep shows how much the result depends on the rule's numbers.",
    "Not modelled: exchange and custody risk (a failed exchange, a lost key), withdrawal and funding costs, and the tax treatment of spot coins for a nonresident (the tax figures treat the asset like a security). The lag between the sheet and a real order IS modelled, as a bound: the fill is the next session's UTC-day close, about 23 hours after the sheet (asset.fill_timing_sensitivity gives the same runs filled at the open, i.e. at the signal instant).",
]

ASSET_FILL_ZH = {
    "next_close": "下一个交易日那根 UTC 日线的收盘价（纽约时间晚上 8 点，冬令时 7 点）",
    "open": "下一个交易日那根 UTC 日线的开盘价（和信号收盘是同一个时刻）",
}


def asset_fill_sensitivity(data: Data, results: dict[str, dict], start_idx: int, end_idx: int) -> dict | None:
    """The same asset variants under the other fill convention, so the page can say what the timing is worth.

    The shipped convention is `next_close` (the fill session's own UTC-day
    close, ~23 h after the sheet); `open` is that session's 00:00 UTC print,
    which after a weekday IS the signal close. Reported as CAGR and final NAV
    per variant, plus the difference, and nothing else in the run changes.
    """
    rows: dict[str, dict] = {}
    for key, r in results.items():
        p: Params = r["params"]
        if p.variant not in ASSET_VARIANTS:
            continue
        base_stats = stats(r, data)
        row = {"fill": p.asset_fill, "cagr": round(base_stats["cagr"], 6), "final_nav": base_stats["final_nav"]}
        for other in ASSET_FILLS:
            if other == p.asset_fill:
                continue
            alt = run_variant(data, replace(p, asset_fill=other), start_idx, end_idx)
            alt_stats = stats(alt, data)
            row[other] = {"cagr": round(alt_stats["cagr"], 6), "final_nav": alt_stats["final_nav"],
                          "cagr_difference": round(alt_stats["cagr"] - base_stats["cagr"], 6),
                          "final_nav_ratio": round(alt_stats["final_nav"] / base_stats["final_nav"], 6)
                          if base_stats["final_nav"] else None}
        rows[key] = row
    if not rows:
        return None
    return {"applied": next(iter(rows.values()))["fill"], "conventions": list(ASSET_FILLS),
            "note": "next_close = the fill session's own UTC-day close (~23 h after the sheet, the shipped "
                    "convention); open = that session's 00:00 UTC print, which after a weekday is the same "
                    "instant as the signal close (no delay at all)",
            "by_variant": rows}

POINT_IN_TIME_WARNING = (
    "POINT-IN-TIME UNIVERSE: the candidates at every month-end are the S&P 500 members of that date (data/sp500/membership.csv), "
    "including names later acquired, bankrupt or dropped, so the stock layer is no longer read on today's winners. What remains: "
    "the membership dates come from a volunteer-maintained dataset (a day or two of slack around changes), Tiingo's history of a few "
    "delisted names starts late or is missing (data/sp500/tickers.csv, fetch_status.csv), sectors are unknown for former members "
    "(the bucket cap is off), and the S&P 500 itself is a large-cap list chosen by a committee."
)

WARNINGS = [
    "SURVIVORSHIP BIAS: the universe is today's list of 92 large, successful names (data/universe.csv). Names that failed, were acquired or shrank are absent, so every stock-layer result is inflated. Read regime_stocks and stocks_only against ew_universe_bh (equal-weight buy-and-hold of the same universe), not against SPY, to judge the stock layer's marginal effect. The market-regime layer (regime_spy vs spy_bh) is not affected by this bias.",
    "Many tickers list late (ABNB 2020-12, ARM 2023-09, PLTR 2020-09, CRCL 2025-06 ...); they enter the universe only once they have 13 month-end closes. Their inclusion in today's list is itself a form of look-ahead in universe selection.",
    "Whole shares from a 100,000 USD account leave a cash residual and make small weights lumpy; results at another account size differ slightly.",
    "spy_bh keeps its dividends in cash (BIL) as the brief specifies and therefore trails SPY's total return; read the printed avg_exposure column for the invested fraction of every benchmark.",
    "This is research tooling outside the record; nothing here is a call, a fill or a spec-governed artifact.",
]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def format_table(summaries: dict) -> str:
    cols = ["cagr", "ann_vol", "sharpe", "max_drawdown", "calmar", "avg_exposure",
            "avg_names_held", "one_way_turnover_per_year", "costs_pct_nav_per_year", "n_trades", "final_nav"]
    head = f"{'variant':16s}" + "".join(f"{c[:12]:>13s}" for c in cols)
    lines = [head]
    for v, s in summaries.items():
        cells = []
        for c in cols:
            x = s.get(c)
            if x is None:
                cells.append(f"{'-':>13s}")
            elif c in ("n_trades",):
                cells.append(f"{int(x):>13d}")
            elif c == "final_nav":
                cells.append(f"{x:>13,.0f}")
            elif c in ("cagr", "ann_vol", "max_drawdown", "avg_exposure", "one_way_turnover_per_year",
                       "costs_pct_nav_per_year"):
                cells.append(f"{x:>12.2%} ")
            else:
                cells.append(f"{x:>13.2f}")
        lines.append(f"{v:16s}" + "".join(cells))
    return "\n".join(lines)


TAX_TABLE_NOTE = ("cagr_at_* / final_at_* / tax_paid_* are on one basis: each year's tax withdrawn the next January, "
                  "the last year's tax NOT yet withdrawn; accrued_* is that still-owed last-year tax "
                  "(summary.json: total_tax_* = tax_paid + accrued; *_incl_accrued keys net it out); "
                  "wash_disallow is the lifetime disallowed loss, wash_deferred the part still in open lots' basis")


def format_tax_table(summaries: dict) -> str:
    """Pre-tax and after-tax numbers per variant; every after-tax column of a row is on the same basis (see TAX_TABLE_NOTE)."""
    cols = [("cagr", "cagr", "pct"), ("max_drawdown", "max_dd", "pct"),
            ("cagr_after_tax_nra", "cagr_at_nra", "pct"), ("cagr_after_tax_resident", "cagr_at_res", "pct"),
            ("final_nav", "final_nav", "usd"), ("final_nav_after_tax_nra", "final_at_nra", "usd"),
            ("final_nav_after_tax_resident", "final_at_res", "usd"),
            ("total_tax_withdrawn_nra", "tax_paid_nra", "usd"), ("total_tax_withdrawn_resident", "tax_paid_res", "usd"),
            ("accrued_unpaid_tax_last_year_nra", "accrued_nra", "usd"),
            ("accrued_unpaid_tax_last_year_resident", "accrued_res", "usd"),
            ("n_trades", "n_trades", "int"),
            ("wash_sale_disallowed_usd", "wash_disallow", "usd"), ("wash_sale_deferred_open_usd", "wash_deferred", "usd")]
    head = f"{'variant':16s}" + "".join(f"{label:>14s}" for _, label, _ in cols)
    lines = [head]
    for v, s in summaries.items():
        cells = []
        for key, _, kind in cols:
            x = s.get(key)
            if x is None or (isinstance(x, float) and not np.isfinite(x)):
                cells.append(f"{'-':>14s}")
            elif kind == "pct":
                cells.append(f"{x:>13.2%} ")
            elif kind == "usd":
                cells.append(f"{x:>14,.0f}")
            else:
                cells.append(f"{int(x):>14d}")
        lines.append(f"{v:16s}" + "".join(cells))
    return "\n".join(lines)


def find_start(data: Data, params: Params, start: pd.Timestamp | None) -> int:
    need = params.min_month_ends
    for k, i in enumerate(data.me_idx):
        if k + 1 >= need and (start is None or data.dates[i] >= start):
            return int(i)
    raise SystemExit("no month-end with enough history")


def find_asset_start(data: Data, params: Params, start: pd.Timestamp | None) -> int:
    """The first month-end (on or after `start`) at which the calendar AND the asset have their vote history."""
    for k, i in enumerate(data.me_idx):
        if (start is None or data.dates[i] >= start) and asset_state_ready(data, k, params):
            return int(i)
    raise SystemExit(f"no month-end at which {params.asset} has {asset_history_need(params)} month-end closes")


def run_asset_sweep(data: Data, base: Params, start_idx: int, end_idx: int, out: Path,
                    tax_params: regime_tax.TaxParams | None = None) -> pd.DataFrame:
    """The asset's votes over sma_months x votes x volatility cap (off / 0.20 / 0.40); writes asset_sweep.csv.

    `main` marks the two runs the report calls the method: asset_regime (10 months, both votes, cap 0.20)
    and asset_regime_votes (the same without the cap).
    """
    grid = []
    for sma in (6, 8, 10, 12):
        for votes in ("a_only", "b_only", "both"):
            for target in ASSET_SWEEP_VOL_TARGETS:
                if target is None:
                    grid.append(replace(base, variant="asset_regime_votes", sma_months=sma, votes=votes))
                else:
                    grid.append(replace(base, variant="asset_regime", sma_months=sma, votes=votes, vol_target=target))
    tax_params = tax_params or regime_tax.TaxParams()
    rows = []
    for p in grid:
        r = run_variant(data, p, start_idx, end_idx)
        s = stats(r, data)
        t = variant_tax(r, tax_params)["keys"]
        capped = p.variant == "asset_regime"
        is_main = p.sma_months == Params.sma_months and p.votes == "both" and (not capped or p.vol_target == Params.vol_target)
        rows.append({
            "variant": p.variant, "asset": p.asset, "sma_months": p.sma_months, "votes": p.votes,
            "vol_cap": p.vol_target if capped else "off", "main": p.variant if is_main else "",
            "cagr": round(s["cagr"], 6), "max_drawdown": round(s["max_drawdown"], 6), "ann_vol": round(s["ann_vol"], 6),
            "sharpe": round(s["sharpe"], 4), "final_nav": s["final_nav"], "n_trades": s["n_trades"],
            "trades_per_year": round(s["trades_per_year"], 2), "avg_exposure": round(s["avg_exposure"], 4),
            "pct_time_bull": round(s["pct_time_bull"], 4), "pct_time_half": round(s["pct_time_half"], 4),
            "pct_time_bear": round(s["pct_time_bear"], 4),
            "cagr_after_tax_nra": round(t["cagr_after_tax_nra"], 6),
            "final_nav_after_tax_nra": t["final_nav_after_tax_nra"],
            "cagr_after_tax_resident": round(t["cagr_after_tax_resident"], 6),
            "elapsed_sec": round(r["elapsed"], 2),
        })
        print(f"asset sweep {p.asset} sma={p.sma_months:2d} votes={p.votes:6s} cap={str(rows[-1]['vol_cap']):4s} "
              f"cagr={s['cagr']:.2%} mdd={s['max_drawdown']:.2%} sharpe={s['sharpe']:.2f} trades={s['n_trades']} "
              f"({r['elapsed']:.1f}s)", flush=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(out / "asset_sweep.csv", index=False)
    return frame


def run_sweep(data: Data, base: Params, start_idx: int, end_idx: int, out: Path) -> pd.DataFrame:
    rows = []
    grid = []
    for sma in (8, 10, 12):
        for mom in (9, 12, 15):
            for top in (15, 20, 30):
                grid.append(replace(base, variant="regime_stocks", sma_months=sma, mom_months=mom,
                                    top_n=top, hold_rank=2 * top))
    for sma in (6, 8, 10, 12):
        for votes in ("a_only", "b_only", "both"):
            grid.append(replace(base, variant="regime_spy", sma_months=sma, votes=votes))
    grid.append(replace(base, variant="regime_stocks", breaker=True))
    for p in grid:
        r = run_variant(data, p, start_idx, end_idx)
        s = stats(r, data)
        rows.append({
            "variant": p.variant, "sma_months": p.sma_months, "momentum_lookback_months": p.mom_months,
            "top_n": p.top_n, "hold_rank": p.hold_rank, "votes": p.votes, "breaker": p.breaker,
            "cagr": round(s["cagr"], 6), "max_drawdown": round(s["max_drawdown"], 6),
            "sharpe": round(s["sharpe"], 4), "n_trades": s["n_trades"], "final_nav": s["final_nav"],
            "elapsed_sec": round(r["elapsed"], 2),
        })
        print(f"sweep {p.variant:14s} sma={p.sma_months:2d} mom={p.mom_months:2d} top={p.top_n:2d} "
              f"votes={p.votes:6s} brk={int(p.breaker)} cagr={s['cagr']:.2%} mdd={s['max_drawdown']:.2%} "
              f"sharpe={s['sharpe']:.2f} trades={s['n_trades']} ({r['elapsed']:.1f}s)", flush=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(out / "sweep.csv", index=False)
    run_pair_sweep(data, base, start_idx, end_idx, out)
    return frame


def run_pair_sweep(data: Data, base: Params, start_idx: int, end_idx: int, out: Path) -> pd.DataFrame:
    """pair_rule over lookback x hysteresis, plus the topvol_lowcorr selection; writes pair_sweep.csv."""
    grid = []
    for lb in (126, 252, 504):
        for keep in (1.0, 0.8, 0.6):
            grid.append(replace(base, variant="pair_rule", pair_lookback=lb, pair_keep_frac=keep,
                                pair_select="variance_of_difference"))
    grid.append(replace(base, variant="pair_rule", pair_select="topvol_lowcorr"))
    rows = []
    tax_params = regime_tax.TaxParams()
    for p in grid:
        r = run_variant(data, p, start_idx, end_idx)
        s = stats(r, data)
        t = variant_tax(r, tax_params)["keys"]
        pr = r["pairs"].dropna(subset=["pair_a"])
        rows.append({
            "variant": p.variant, "pair_select": p.pair_select, "pair_lookback": p.pair_lookback,
            "pair_keep_frac": p.pair_keep_frac,
            "pair_top_vol": p.pair_top_vol if p.pair_select == "topvol_lowcorr" else None,
            "cagr": round(s["cagr"], 6), "max_drawdown": round(s["max_drawdown"], 6), "ann_vol": round(s["ann_vol"], 6),
            "sharpe": round(s["sharpe"], 4), "n_trades": s["n_trades"], "final_nav": s["final_nav"],
            "n_switches": s["n_pair_switches"], "n_evaluations": s["n_pair_evaluations"],
            "n_evaluations_without_pair": s["n_evaluations_without_pair"],
            "n_distinct_pairs": s["n_distinct_pairs"], "most_held_pair": s["most_held_pair"],
            "most_held_pair_months": s["most_held_pair_months"],
            "months_mstr_smci_chosen": int(((pr["pair_a"] == "MSTR") & (pr["pair_b"] == "SMCI")).sum()) if len(pr) else 0,
            "one_way_turnover_per_year": round(s["one_way_turnover_per_year"], 4),
            "costs_pct_nav_per_year": round(s["costs_pct_nav_per_year"], 6),
            "cagr_after_tax_nra": round(t["cagr_after_tax_nra"], 6),
            "cagr_after_tax_resident": round(t["cagr_after_tax_resident"], 6),
            "wash_sale_disallowed_usd": t["wash_sale_disallowed_usd"],
            "elapsed_sec": round(r["elapsed"], 2),
        })
        print(f"pair sweep {p.pair_select:22s} lookback={p.pair_lookback:3d} keep={p.pair_keep_frac:.1f} "
              f"cagr={s['cagr']:.2%} mdd={s['max_drawdown']:.2%} sharpe={s['sharpe']:.2f} trades={s['n_trades']} "
              f"switches={s['n_pair_switches']} ({r['elapsed']:.1f}s)", flush=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(out / "pair_sweep.csv", index=False)
    return frame


DEFAULT_PAIR = "MSTR,SMCI"


def parse_pair(text: str) -> tuple[str, str]:
    """Two different tickers from 'A,B', returned in canonical (alphabetical) order."""
    parts = [p.strip().upper() for p in str(text).split(",") if p.strip()]
    if len(parts) != 2 or parts[0] == parts[1]:
        raise SystemExit(f"--pair needs two different tickers, e.g. MSTR,SMCI (got {text!r})")
    a, b = sorted(parts)
    return a, b


def resolve_pair_flags(args) -> None:
    """--pair (in either `--pair A,B` or `--pair=A,B` form) implies --with-pairs; --with-pairs alone uses DEFAULT_PAIR."""
    if args.pair is not None:
        args.with_pairs = True
    else:
        args.pair = DEFAULT_PAIR


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sweep", action="store_true", help="the parameter grid (sweep.csv) plus the pair sweep")
    ap.add_argument("--pair-sweep", action="store_true", help="only the pair sweep (pair_sweep.csv)")
    ap.add_argument("--breaker", action="store_true")
    ap.add_argument("--with-pairs", action="store_true",
                    help="also run pair_fixed (--pair) and pair_rule next to the five standard variants")
    ap.add_argument("--pair", default=None,
                    help=f"the two tickers of pair_fixed (implies --with-pairs when given; default {DEFAULT_PAIR})")
    ap.add_argument("--pair-lookback", type=int, default=Params.pair_lookback)
    ap.add_argument("--pair-keep-frac", type=float, default=Params.pair_keep_frac)
    ap.add_argument("--pair-select", default=Params.pair_select, choices=PAIR_SELECTS)
    ap.add_argument("--tax-dividend-rate", type=float, default=regime_tax.TaxParams.dividend_rate,
                    help="nra: treaty rate on stock dividends (0.10 US-China, 0.30 without a treaty)")
    ap.add_argument("--tax-st-rate", type=float, default=regime_tax.TaxParams.st_rate, help="resident short-term rate")
    ap.add_argument("--tax-lt-rate", type=float, default=regime_tax.TaxParams.lt_rate, help="resident long-term rate")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default="2026-09-04")
    ap.add_argument("--cache", default=None, help="cache root (default data/cache)")
    ap.add_argument("--universe", default=Path(__file__).resolve().parents[1] / "data" / "universe.csv", type=Path)
    ap.add_argument("--membership", default=None, type=Path,
                    help="point-in-time membership intervals (data/sp500/membership.csv); default: no restriction")
    ap.add_argument("--delist-after", type=int, default=Params.delist_after,
                    help="liquidate a held name N sessions after its last cached bar when its history has ended (0 = off)")
    ap.add_argument("--delist-haircut", type=float, default=Params.delist_haircut,
                    help="fraction taken off the delisting liquidation price (sensitivity; default 0)")
    ap.add_argument("--bucket-cap", type=float, default=Params.bucket_cap,
                    help=f"sector (bucket) cap of the sleeve weights (default {Params.bucket_cap}; 1.0 disables it)")
    ap.add_argument("--freshness-min", type=float, default=Params.freshness_min,
                    help="universe-freshness guard: a stock-layer evaluation at which fewer than this fraction of the "
                         f"live universe names has a bar is skipped and retried (default {Params.freshness_min}; 0 = off)")
    ap.add_argument("--asset", default=None,
                    help="single-asset variants: a cache ticker outside universe.csv, e.g. BTCUSD (tools/crypto_fetch.py)")
    ap.add_argument("--with-asset", action="store_true",
                    help=f"also run {', '.join(ASSET_VARIANTS)} on --asset")
    ap.add_argument("--asset-only", action="store_true", help="with --with-asset: run only the asset variants")
    ap.add_argument("--asset-cost-bps", type=float, default=Params.asset_cost_bps,
                    help="asset variants: cost per side in basis points of traded notional (replaces commission and slippage)")
    ap.add_argument("--asset-fill", default=Params.asset_fill, choices=list(ASSET_FILLS),
                    help="asset variants: which print of the fill session T+1 an order fills at "
                         f"(default {Params.asset_fill}; 'open' is the same instant as the signal close "
                         "for a UTC-day series and is kept for the sensitivity)")
    ap.add_argument("--asset-sweep", action="store_true",
                    help="only the asset sweep: sma_months x votes x volatility cap (asset_sweep.csv)")
    ap.add_argument("--calendar-start", default=None,
                    help="prepend NYSE sessions from this date to SPY's calendar (an asset older than SPY's cached "
                         "history; needs --asset-only or --asset-sweep)")
    args = ap.parse_args()
    resolve_pair_flags(args)
    asset_mode = bool(args.with_asset or args.asset_sweep)
    if asset_mode and not args.asset:
        raise SystemExit("--with-asset / --asset-sweep need --asset TICKER (e.g. --asset BTCUSD)")
    if not asset_mode and (args.asset or args.asset_only or args.calendar_start):
        raise SystemExit("--asset, --asset-only and --calendar-start only mean something with --with-asset or --asset-sweep")
    if args.asset_only and not args.with_asset:
        raise SystemExit("--asset-only needs --with-asset")
    if args.calendar_start and not (args.asset_only or args.asset_sweep):
        raise SystemExit("--calendar-start needs --asset-only (SPY has no bar on the prepended sessions)")
    if args.asset_cost_bps < 0:
        raise SystemExit("--asset-cost-bps must be >= 0")
    if args.delist_after < 0 or not 0.0 <= args.delist_haircut < 1.0 or not 0.0 < args.bucket_cap <= 1.0:
        raise SystemExit("--delist-after must be >= 0, --delist-haircut in [0, 1), --bucket-cap in (0, 1]")
    if not 0.0 <= args.freshness_min <= 1.0:
        raise SystemExit("--freshness-min must be in [0, 1]")

    t0 = time.perf_counter()
    pair_a, pair_b = parse_pair(args.pair)
    base = Params(pair_a=pair_a, pair_b=pair_b, pair_lookback=args.pair_lookback,
                  pair_keep_frac=args.pair_keep_frac, pair_select=args.pair_select,
                  bucket_cap=args.bucket_cap, delist_after=args.delist_after, delist_haircut=args.delist_haircut,
                  freshness_min=args.freshness_min)
    if asset_mode:
        base = replace(base, asset=str(args.asset).strip().upper(), asset_cost_bps=args.asset_cost_bps,
                       asset_fill=args.asset_fill)
    tax_params = regime_tax.TaxParams(dividend_rate=args.tax_dividend_rate, st_rate=args.tax_st_rate,
                                      lt_rate=args.tax_lt_rate)
    end = pd.Timestamp(args.end) if args.end else None
    if asset_mode:
        data = load_data(Path(args.cache) if args.cache else None, args.universe, end, base,
                         membership_csv=args.membership, extra_tickers=[base.asset],
                         calendar_start=pd.Timestamp(args.calendar_start) if args.calendar_start else None)
        # one window for every variant of the run: the first month-end at which the asset has its history too
        start_idx = find_asset_start(data, base, pd.Timestamp(args.start) if args.start else None)
    else:
        data = load_data(Path(args.cache) if args.cache else None, args.universe, end, base,
                         membership_csv=args.membership)
        start_idx = find_start(data, base, pd.Timestamp(args.start) if args.start else None)
    end_idx = len(data.dates) - 1
    print(f"loaded {len(data.tickers)} stocks + SPY, BIL present={data.bil_present}, "
          f"period {data.dates[start_idx].date()} .. {data.dates[end_idx].date()} "
          f"({end_idx - start_idx + 1} sessions) in {time.perf_counter() - t0:.1f}s", flush=True)
    if data.member is not None:
        stock_cols = [data.col[t] for t in data.tickers]
        counts = data.member[data.me_idx][:, stock_cols].sum(axis=1) if stock_cols else np.zeros(0)
        print(f"membership {args.membership}: members at month-ends min={int(counts.min()) if len(counts) else 0} "
              f"max={int(counts.max()) if len(counts) else 0}; delist_after={base.delist_after} "
              f"haircut={base.delist_haircut} bucket_cap={base.bucket_cap}", flush=True)
        for note in data.notes:
            print(f"  note: {note[:300]}", flush=True)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    if asset_mode:
        j = data.col[base.asset]
        missing_bars = int((~data.has_bar[start_idx: end_idx + 1, j]).sum())
        print(f"asset {base.asset}: first bar {data.dates[int(data.first_idx[j])].date()}, cost {base.asset_cost_bps:g} bp per side, "
              f"fill at the {'close' if base.asset_fill == 'next_close' else 'open'} of the fill session, "
              f"{missing_bars} sessions of the window without a bar (they vote nothing and order nothing)"
              + (f", calendar extended back to {data.calendar_start}" if data.calendar_start else ""), flush=True)
    if args.asset_sweep:
        run_asset_sweep(data, base, start_idx, end_idx, out, tax_params)
        print(f"asset sweep written to {out / 'asset_sweep.csv'} in {time.perf_counter() - t0:.1f}s")
        return
    if args.pair_sweep:
        run_pair_sweep(data, base, start_idx, end_idx, out)
        print(f"pair sweep written to {out / 'pair_sweep.csv'} in {time.perf_counter() - t0:.1f}s")
        return
    if args.sweep:
        run_sweep(data, base, start_idx, end_idx, out)
        print(f"sweep written to {out / 'sweep.csv'} and {out / 'pair_sweep.csv'} in {time.perf_counter() - t0:.1f}s")
        return

    results: dict[str, dict] = {}
    summaries: dict[str, dict] = {}
    variants = ["regime_stocks", "regime_spy", "stocks_only", "spy_bh", "ew_universe_bh"]
    if args.with_pairs:
        variants += ["pair_fixed", "pair_rule"]
    if args.asset_only:
        variants = []
    if args.with_asset:
        variants += list(ASSET_VARIANTS)
    for variant in variants:
        p = replace(base, variant=variant)
        r = run_variant(data, p, start_idx, end_idx)
        results[variant] = r
        summaries[variant] = stats(r, data)
        print(f"ran {variant} in {r['elapsed']:.1f}s", flush=True)
    if args.breaker and not args.asset_only:
        # the breaker run is reported as a separate variant, never in place of the main run
        p = replace(base, variant="regime_stocks", breaker=True)
        r = run_variant(data, p, start_idx, end_idx)
        r["nav"].name = "regime_stocks_breaker"
        results["regime_stocks_breaker"] = r
        summaries["regime_stocks_breaker"] = stats(r, data)
        print(f"ran regime_stocks_breaker in {r['elapsed']:.1f}s", flush=True)
    tax = add_tax_summaries(results, summaries, tax_params)
    sensitivity = asset_fill_sensitivity(data, results, start_idx, end_idx) if args.with_asset else None
    write_outputs(out, results, data, summaries, start_idx, end_idx, args, base, tax=tax, tax_params=tax_params,
                  asset_sensitivity=sensitivity)
    print(format_table(summaries))
    print()
    print("after tax: " + TAX_TABLE_NOTE)
    print(format_tax_table(summaries))
    stale = {key: r["n_stale_evaluations"] for key, r in results.items() if r["n_stale_evaluations"]}
    if stale:
        print()
        print(f"universe-freshness guard (freshness_min {base.freshness_min}): stock-layer evaluations skipped as STALE "
              f"and retried: {stale}; see run_meta.json universe_freshness for the sessions")
    print(f"outputs written to {out} in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
