"""US tax model for the regime research tooling (OUTSIDE the record).

Pure functions over a trade list and a dividend list; no I/O, no network. Used
by ``tools/regime_backtest.py`` (after-tax columns per variant) and by
``tools/regime_paper.py`` (the 税务 block on every sheet). Nothing here reads or
writes anything ``strategy_spec.md`` governs.

The model, and where it simplifies (every simplification is listed in
``ASSUMPTIONS`` so the run metadata and the sheets can repeat it):

* Lot matching: FIFO per ticker. A buy opens a lot whose cost basis is the
  fill price times shares plus the buy commission; a sell's proceeds are the
  fill price times shares minus the sell commission (allocated pro rata when a
  sell closes several lots). Slippage is already inside the fill price.
* Holding period: ``long_term`` when ``holding_days > 365`` (the brief's rule;
  the statute says "more than one year", which differs from 365 days only in a
  leap year).
* Wash sales (IRC 1091): a realized loss is disallowed when the same ticker
  was bought within 30 calendar days before or after the sale, excluding the
  shares sold. The disallowed loss is added to the basis of the replacement
  shares and their holding period includes the sold lot's. Each replacement
  share serves once, so nothing is counted twice; a loss with fewer replacement
  shares than shares sold is disallowed pro rata.
* Dividends: the engine's dividend credits. Stock dividends are one class;
  the cash balance's daily accrual at BIL's total return is booked as
  interest-type dividend income ("BIL interest"). A day on which BIL's price
  dips books a negative accrual; a calendar year whose accrual nets negative
  is taxed as zero interest income (interest income cannot be negative and the
  cash proxy's small price loss is not deducted).
* Regime ``nra`` (nonresident alien student present 183 days or more, so US
  source capital gains are taxable): the calendar year's net capital gain
  (gains minus losses, after wash-sale adjustments, no long-term preference)
  is taxed at a flat 30 percent when positive; a net loss is NOT carried
  forward (simplification); stock dividends at ``dividend_rate`` (0.10, the
  US-China treaty rate; 0.30 without a treaty); BIL's interest-type dividends
  at 0 percent (portfolio-interest / interest-related dividend exemption,
  assumed to apply to the whole accrual).
* Regime ``resident``: short-term net gains at ``st_rate`` (0.24), long-term
  at ``lt_rate`` (0.15); a net loss carries forward indefinitely against later
  gains, keeping its short/long character (the 3,000 USD ordinary-income offset
  is ignored, which is conservative); qualified stock dividends at ``lt_rate``;
  BIL dividends at ``st_rate``; state tax and NIIT ignored.
* Regime ``none``: no tax (the pre-tax path).
* After-tax path: each year's tax is withdrawn on the first session of the
  following year and every later NAV is scaled by (1 - tax_y / NAV at that
  year's end). This ignores the trading cost of raising the cash and the tax
  on that sale. The last year's tax has no following session inside the
  series and is reported separately as an accrued, unpaid liability.
* Fractional units (`fractional=(ticker, ...)`, the single-asset variants of
  the engine, e.g. BTCUSD): the named tickers' share counts are matched on a
  6-decimal grid, internally as whole micro-units so the lot arithmetic stays
  exact; the realized rows report them back as decimals. Nothing else changes:
  the asset is taxed like any other capital asset, and the wash-sale rule stays
  on although it does not reach spot crypto today (see `CRYPTO_ASSUMPTIONS`).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

WASH_SALE_DAYS = 30
LONG_TERM_DAYS = 365          # long_term when holding_days > LONG_TERM_DAYS
INTEREST_TICKERS = ("BIL",)
UNIT_DECIMALS = 6             # fractional tickers: share counts live on a 6-decimal grid ...
UNIT_SCALE = 10 ** UNIT_DECIMALS   # ... and are matched as whole micro-units
REGIMES = ("nra", "resident", "none")
OTHER_REGIME = {"nra": "resident", "resident": "nra", "none": "nra"}

ASSUMPTIONS = [
    "Lots are matched FIFO per ticker; a lot's basis is fill price x shares plus the buy commission, a sale's proceeds are fill price x shares minus the sell commission (allocated pro rata across the lots it closes); slippage is inside the fill price.",
    "long_term means holding_days > 365 (the brief's rule; the statute's 'more than one year' differs only across a leap day).",
    "Wash sale (IRC 1091): a realized loss is disallowed when the same ticker was bought within 30 calendar days before or after the sale, excluding the shares sold; the disallowed loss is added to the replacement shares' basis and their holding period includes the sold lot's; each replacement share serves once (no double counting) and a loss with fewer replacement shares than shares sold is disallowed pro rata.",
    "Dividends: stock dividends are the engine's ex-date credits; the cash balance's daily accrual at BIL's total return is treated as interest-type dividend income for the year (BIL interest). BIL's accrual can be negative on a day when its price dips; a year whose accrual nets negative is treated as zero interest income (the small price loss of the cash proxy is not deducted). The dividends_interest column keeps the signed sum so it reconciles with the recorded credits.",
    "The wash-sale disallowance is a deferral, not a permanent loss: the amount moves into the replacement lot's basis and is recovered when that lot is sold. wash_sale_deferred_open is the part still sitting in lots open at the end of the window, so the after-tax figures are conservative by that amount.",
    "nra regime: nonresident alien student present >= 183 days, so the year's net capital gain (all realized gains minus losses after wash-sale adjustments, short and long together, no long-term preference) is taxed at a flat 30 percent when positive; a net loss is not carried forward (simplifying assumption); stock dividends at the treaty rate dividend_rate (0.10 US-China; 0.30 without a treaty); BIL interest-type dividends at 0 percent (portfolio-interest / interest-related dividend exemption, assumed to cover the whole accrual).",
    "resident regime: short-term net gains at st_rate, long-term at lt_rate; net losses carry forward indefinitely keeping their short/long character (a carried short-term loss offsets short-term gains first, a carried long-term loss long-term gains first); the 3,000 USD ordinary-income offset is ignored (conservative); qualified stock dividends at lt_rate; BIL dividends at st_rate; state tax and NIIT ignored.",
    "After-tax path: each year's tax is withdrawn on the first session of the following year and every later NAV is scaled by (1 - tax_y / pre-tax NAV at that year's end), the proportional-withdrawal approximation; it ignores the trading cost of raising the cash and the tax on that sale. The last year's tax is not withdrawn inside the series (no following session) and is reported as an accrued, unpaid liability next to the path's final NAV.",
    "Tax is what a filer owes for the calendar year; nothing is deducted inside the account during the year.",
]

# Said next to the ASSUMPTIONS whenever a fractional (crypto) ticker goes through the model; kept apart so that
# the ASSUMPTIONS list, which every standard run copies into run_meta.json, reads exactly as before.
CRYPTO_ASSUMPTIONS = [
    "A directly held coin is property for US tax purposes: every sale is a capital gain or loss, matched here FIFO on a 6-decimal unit grid exactly like shares; the trading cost (basis points of notional) is in the buy's basis and off the sale's proceeds.",
    "The wash-sale rule (IRC 1091) is LEFT ON for the asset although the statute covers 'stock or securities' and, as of this writing, not spot crypto held directly. The after-tax figures are therefore conservative for spot coins (a loss the law would allow is deferred here) and right for a spot Bitcoin ETF held in a brokerage account, whose shares are securities.",
    "nra regime as for stocks: a nonresident alien present 183 days or more pays a flat 30 percent on the year's positive net US-source capital gain, losses not carried forward. Whether a nonresident's gain on a coin is US-source at all depends on facts this model does not look at; the 30 percent is the cautious reading.",
]
CRYPTO_ASSUMPTIONS_ZH = ("比特币在税上按「财产」处理：每次卖出都算一次盈亏，和股票一样按先买先卖配对。"
                         "「卖亏后 30 天内买回、亏损暂不能抵」这条规则现在其实管不到直接持有的币，这里照样按它算，所以估出来的税偏保守；"
                         "如果你在券商账户里买的是现货比特币 ETF，那是证券，这条规则就真的适用。")

ASSUMPTIONS_ZH = ("估算口径：按先买先卖配对成本；同一只股票卖亏后 30 天内又买回的，亏损暂时不能抵扣，推迟到卖出新买那批时再抵（已加到那批的成本里）；"
                  "现金按 BIL 总回报计息，某年加起来为负时按 0 计税（利息收入不能为负，也不当作亏损抵扣）；"
                  "非居民按全年净收益的 30% 计税、亏损不结转、股票分红按协定税率、短债利息免税；"
                  "居民按短期/长期税率、亏损可结转、分红按长期税率、短债利息按短期税率；不含州税和 NIIT。税在次年报税时缴，账户里没有扣。")


@dataclass(frozen=True)
class TaxParams:
    regime: str = "nra"
    dividend_rate: float = 0.10      # nra: treaty rate on stock dividends (0.30 without a treaty)
    st_rate: float = 0.24            # resident: short-term gains, BIL dividends
    lt_rate: float = 0.15            # resident: long-term gains, qualified stock dividends
    nra_gain_rate: float = 0.30      # nra: flat rate on the year's positive net gain
    nra_interest_rate: float = 0.0   # nra: BIL interest-type dividends

    def with_regime(self, regime: str) -> "TaxParams":
        return TaxParams(regime, self.dividend_rate, self.st_rate, self.lt_rate,
                         self.nra_gain_rate, self.nra_interest_rate)


def tax_params_from_dict(payload: dict | None) -> TaxParams:
    payload = dict(payload or {})
    base = TaxParams()
    return TaxParams(
        regime=str(payload.get("regime", base.regime)),
        dividend_rate=float(payload.get("dividend_rate", base.dividend_rate)),
        st_rate=float(payload.get("st_rate", base.st_rate)),
        lt_rate=float(payload.get("lt_rate", base.lt_rate)),
        nra_gain_rate=float(payload.get("nra_gain_rate", base.nra_gain_rate)),
        nra_interest_rate=float(payload.get("nra_interest_rate", base.nra_interest_rate)),
    )


# --------------------------------------------------------------------------
# input normalization
# --------------------------------------------------------------------------


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()


REALIZED_COLUMNS = ["ticker", "buy_date", "sell_date", "shares", "proceeds", "basis", "gain",
                    "holding_days", "long_term", "disallowed", "taxable_gain", "replacement_buy_dates"]
DIVIDEND_COLUMNS = ["date", "ticker", "kind", "amount"]


def normalize_trades(trades, fractional=()) -> pd.DataFrame:
    """Trades as a frame with date (date), ticker, side, shares (int), fill_price, commission (float).

    Accepts the engine's trades frame, the paper job's fills.csv rows (strings)
    or a list of dicts. Sorted by date, sells before buys on the same day (the
    engine fills in that order), then ticker; the sort is stable so equal keys
    keep their input order.

    `fractional`: tickers whose share counts are decimals on the 6-decimal grid
    (the engine's single-asset variants). Their rows come back in whole
    MICRO-units (shares x 1e6, fill_price / 1e6, so shares x fill_price is
    unchanged) with a `scale` column of 1e6; every other row has scale 1 and,
    without `fractional`, the frame is exactly what it always was (no column).
    """
    frame = pd.DataFrame(trades) if not isinstance(trades, pd.DataFrame) else trades.copy()
    if frame.empty:
        return pd.DataFrame(columns=["date", "ticker", "side", "shares", "fill_price", "commission"])
    frac = {str(t) for t in (fractional or ())}
    if frac:
        scale = np.where(np.isin(frame["ticker"].astype(str).to_numpy(), sorted(frac)), UNIT_SCALE, 1).astype("int64")
        frame = frame.assign(shares=frame["shares"].astype(float).to_numpy() * scale,
                             fill_price=frame["fill_price"].astype(float).to_numpy() / scale)
    out = pd.DataFrame({
        "date": [_as_date(d) for d in frame["date"]],
        "ticker": frame["ticker"].astype(str).to_numpy(),
        "side": frame["side"].astype(str).str.upper().to_numpy(),
        "shares": frame["shares"].astype(float).round().astype(int).to_numpy(),
        "fill_price": frame["fill_price"].astype(float).to_numpy(),
        "commission": frame["commission"].astype(float).fillna(0.0).to_numpy() if "commission" in frame
        else np.zeros(len(frame)),
    })
    if frac:
        out["scale"] = scale
    bad = set(out["side"]) - {"BUY", "SELL"}
    if bad:
        raise ValueError(f"unknown trade side(s): {sorted(bad)}")
    out["_side_order"] = np.where(out["side"] == "SELL", 0, 1)
    out = out.sort_values(["date", "_side_order", "ticker"], kind="stable").drop(columns="_side_order")
    return out.reset_index(drop=True)


def normalize_dividends(dividends) -> pd.DataFrame:
    """Dividend credits as a frame with date, ticker, kind (stock | interest), amount.

    `kind` is derived from the ticker (BIL -> interest) when the input has no
    kind column.
    """
    if dividends is None:
        return pd.DataFrame(columns=DIVIDEND_COLUMNS)
    frame = pd.DataFrame(dividends) if not isinstance(dividends, pd.DataFrame) else dividends.copy()
    if frame.empty:
        return pd.DataFrame(columns=DIVIDEND_COLUMNS)
    tickers = frame["ticker"].astype(str).to_numpy()
    if "kind" in frame and frame["kind"].notna().all() and (frame["kind"].astype(str) != "").all():
        kind = frame["kind"].astype(str).to_numpy()
    else:
        kind = np.where(np.isin(tickers, INTEREST_TICKERS), "interest", "stock")
    out = pd.DataFrame({
        "date": [_as_date(d) for d in frame["date"]],
        "ticker": tickers, "kind": kind,
        "amount": frame["amount"].astype(float).to_numpy(),
    })
    bad = set(out["kind"]) - {"stock", "interest"}
    if bad:
        raise ValueError(f"unknown dividend kind(s): {sorted(bad)}")
    return out.sort_values(["date", "ticker"], kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------
# lot matching with wash sales
# --------------------------------------------------------------------------


@dataclass(eq=False)                  # identity, never field equality: two same-day lots may look alike
class Lot:
    ticker: str
    buy_date: dt.date
    shares: int
    basis: float                      # total basis of the lot (commission and wash-sale additions included)
    holding_start: dt.date            # earlier than buy_date once a wash sale tacked on the sold lot's period
    is_replacement: bool = False      # already served as replacement shares (a share serves once)
    wash_added: float = 0.0
    scale: int = 1                    # 1e6 for a fractional ticker (`shares` are micro-units): units = shares / scale


@dataclass(eq=False)
class _PendingLoss:
    """A realized loss still looking for replacement shares bought within 30 days AFTER the sale."""
    ticker: str
    sale_date: dt.date
    row: dict
    shares_remaining: int
    loss_per_share: float
    holding_start: dt.date


@dataclass
class LotMatch:
    """The result of `match_lots`."""
    realized: pd.DataFrame                       # REALIZED_COLUMNS
    dividends: pd.DataFrame                      # DIVIDEND_COLUMNS
    open_lots: list[Lot] = field(default_factory=list)
    disallowed_by_year: dict[int, float] = field(default_factory=dict)

    @property
    def wash_sale_disallowed(self) -> float:
        return float(sum(self.disallowed_by_year.values()))

    @property
    def wash_sale_deferred_open(self) -> float:
        """Disallowed losses still parked in the basis of lots that are open: deferred, not yet recovered."""
        return float(sum(lot.wash_added for lot in self.open_lots))


def _split_off(lots: list[Lot], lot: Lot, n: int) -> Lot:
    """Split `n` shares off `lot` (in place in `lots`); returns the new n-share piece, placed before the rest."""
    if n >= lot.shares:
        return lot
    piece_basis = lot.basis * n / lot.shares
    piece = Lot(lot.ticker, lot.buy_date, n, piece_basis, lot.holding_start, lot.is_replacement, 0.0, lot.scale)
    lot.shares -= n
    lot.basis -= piece_basis
    lots.insert(lots.index(lot), piece)
    return piece


def _apply_replacement(lots: list[Lot], lot: Lot, row: dict, n: int, loss_per_share: float,
                       sale_date: dt.date, old_holding_start: dt.date) -> None:
    """Designate n shares of `lot` as replacement shares for the loss in `row`."""
    piece = _split_off(lots, lot, n)
    disallowed = loss_per_share * n
    piece.basis += disallowed
    piece.wash_added += disallowed
    piece.is_replacement = True
    old_days = (sale_date - old_holding_start).days
    tacked = piece.buy_date - dt.timedelta(days=old_days)
    piece.holding_start = min(piece.holding_start, tacked)
    row["disallowed"] += disallowed
    row["taxable_gain"] = row["gain"] + row["disallowed"]
    row["replacement_buy_dates"].append(piece.buy_date.isoformat())


def match_lots(trades, dividends=None, fractional=()) -> LotMatch:
    """FIFO lot matching with the wash-sale rule; dividends classified stock / interest.

    `fractional` names the tickers traded in decimal units (see
    `normalize_trades`); they are matched as whole micro-units and their
    realized rows report `shares` as a decimal again.

    Returns realized rows (one per closed lot piece) with ticker, buy_date,
    sell_date, shares, proceeds, basis, gain, holding_days, long_term,
    disallowed (wash-sale amount, positive), taxable_gain (= gain + disallowed)
    and the replacement lots' buy dates; the disallowed amounts per sale year;
    the lots still open; and the dividends frame.
    """
    frame = normalize_trades(trades, fractional)
    divs = normalize_dividends(dividends)
    has_scale = "scale" in frame.columns
    lots: dict[str, list[Lot]] = {}
    pending: list[_PendingLoss] = []
    realized: list[dict] = []
    window = dt.timedelta(days=WASH_SALE_DAYS)

    for trade in frame.itertuples(index=False):
        ticker, day, q = trade.ticker, trade.date, int(trade.shares)
        if q <= 0:
            continue
        scale = int(trade.scale) if has_scale else 1
        book = lots.setdefault(ticker, [])
        if trade.side == "BUY":
            lot = Lot(ticker, day, q, q * float(trade.fill_price) + float(trade.commission), day, scale=scale)
            book.append(lot)
            # replacement shares for losses realized within the 30 days before this
            # buy; `rest` is the part of the new lot not yet designated (the
            # split keeps the remainder in the original object)
            rest: Lot | None = lot
            for p in sorted((p for p in pending if p.ticker == ticker and p.shares_remaining > 0
                             and day - p.sale_date <= window), key=lambda p: p.sale_date):
                if rest is None or rest.shares <= 0:
                    break
                n = min(p.shares_remaining, rest.shares)
                whole = n == rest.shares
                _apply_replacement(book, rest, p.row, n, p.loss_per_share, p.sale_date, p.holding_start)
                p.shares_remaining -= n
                if whole:
                    rest = None
            pending = [p for p in pending if p.shares_remaining > 0 and day - p.sale_date <= window]
            continue

        # SELL: close lots FIFO
        held = sum(x.shares for x in book)
        if q > held:
            raise ValueError(f"{ticker}: selling {q / scale:g} shares on {day} with only {held / scale:g} held "
                             "(short sales are not modelled)" if scale != 1 else
                             f"{ticker}: selling {q} shares on {day} with only {held} held (short sales are not modelled)")
        comm_per_share = float(trade.commission) / q
        pieces: list[tuple[dict, dt.date]] = []
        remaining = q
        while remaining > 0:
            lot = book[0]
            take = min(remaining, lot.shares)
            basis = lot.basis * take / lot.shares
            proceeds = take * float(trade.fill_price) - comm_per_share * take
            holding_days = (day - lot.holding_start).days
            row = {
                "ticker": ticker, "buy_date": lot.buy_date.isoformat(), "sell_date": day.isoformat(),
                "shares": int(take) if scale == 1 else round(take / scale, UNIT_DECIMALS),
                "proceeds": proceeds, "basis": basis, "gain": proceeds - basis,
                "holding_days": int(holding_days), "long_term": bool(holding_days > LONG_TERM_DAYS),
                "disallowed": 0.0, "taxable_gain": proceeds - basis, "replacement_buy_dates": [],
                "_units": int(take),          # the matched count (micro-units for a fractional ticker); not a column
            }
            realized.append(row)
            pieces.append((row, lot.holding_start))
            lot.shares -= take
            lot.basis -= basis
            if lot.shares == 0:
                book.pop(0)
            remaining -= take
        # wash sale against lots bought within the 30 days BEFORE the sale that are still open
        for row, old_start in pieces:
            if row["gain"] >= 0:
                continue
            loss_per_share = -row["gain"] / row["_units"]
            need = row["_units"]
            for lot in list(book):
                if need == 0:
                    break
                if lot.is_replacement or not (dt.timedelta(0) <= day - lot.buy_date <= window):
                    continue
                n = min(need, lot.shares)
                _apply_replacement(book, lot, row, n, loss_per_share, day, old_start)
                need -= n
            if need > 0:
                pending.append(_PendingLoss(ticker, day, row, need, loss_per_share, old_start))

    realized_df = pd.DataFrame(realized, columns=REALIZED_COLUMNS)
    if len(realized_df):
        realized_df["replacement_buy_dates"] = realized_df["replacement_buy_dates"].map(lambda v: ";".join(v))
    by_year: dict[int, float] = {}
    for row in realized:
        if row["disallowed"]:
            y = int(row["sell_date"][:4])
            by_year[y] = by_year.get(y, 0.0) + row["disallowed"]
    open_lots = [x for book in lots.values() for x in book if x.shares > 0]
    return LotMatch(realized_df, divs, open_lots, by_year)


# --------------------------------------------------------------------------
# tax by calendar year
# --------------------------------------------------------------------------


TAX_COLUMNS = ["year", "regime", "n_sales", "st_gain", "lt_gain", "net_gain", "wash_sale_disallowed",
               "dividends_stock", "dividends_interest", "loss_carried_in_st", "loss_carried_in_lt",
               "st_taxable", "lt_taxable", "loss_carried_out_st", "loss_carried_out_lt",
               "tax_gains", "tax_dividends_stock", "tax_dividends_interest", "tax_total"]


def _year_range(realized: pd.DataFrame, dividends: pd.DataFrame, years=None) -> list[int]:
    found: set[int] = set()
    if len(realized):
        found |= {int(str(d)[:4]) for d in realized["sell_date"]}
    if len(dividends):
        found |= {_as_date(d).year for d in dividends["date"]}
    if years is not None:
        found |= {int(y) for y in years}
    if not found:
        return []
    return list(range(min(found), max(found) + 1))


def tax_by_year(realized: pd.DataFrame, dividends, regime: str | TaxParams, years=None, **rates) -> pd.DataFrame:
    """Per calendar year: realized gains by character, dividends by kind, and the tax under `regime`.

    `regime` is "nra", "resident" or "none", or a `TaxParams`; rate overrides
    go in `rates` (dividend_rate, st_rate, lt_rate, nra_gain_rate,
    nra_interest_rate). `years` extends the row range (e.g. the years a NAV
    series spans) so a year without a sale still gets a zero row and the
    resident carryforward flows through it.
    """
    params = regime if isinstance(regime, TaxParams) else TaxParams(regime=regime)
    if rates:
        params = TaxParams(**{**params.__dict__, **rates})
    if params.regime not in REGIMES:
        raise ValueError(f"unknown tax regime {params.regime!r}; expected one of {REGIMES}")
    realized = realized if realized is not None else pd.DataFrame(columns=REALIZED_COLUMNS)
    divs = normalize_dividends(dividends)
    rows = []
    carry_st = carry_lt = 0.0
    sell_year = realized["sell_date"].astype(str).str[:4].astype(int) if len(realized) else pd.Series(dtype=int)
    div_year = pd.Series([_as_date(d).year for d in divs["date"]], dtype=int) if len(divs) else pd.Series(dtype=int)
    for year in _year_range(realized, divs, years):
        r = realized[sell_year == year] if len(realized) else realized
        d = divs[div_year.to_numpy() == year] if len(divs) else divs
        st_gain = float(r.loc[~r["long_term"].astype(bool), "taxable_gain"].sum()) if len(r) else 0.0
        lt_gain = float(r.loc[r["long_term"].astype(bool), "taxable_gain"].sum()) if len(r) else 0.0
        disallowed = float(r["disallowed"].sum()) if len(r) else 0.0
        div_stock = float(d.loc[d["kind"] == "stock", "amount"].sum()) if len(d) else 0.0
        div_int = float(d.loc[d["kind"] == "interest", "amount"].sum()) if len(d) else 0.0
        # interest income cannot be negative: a year whose BIL accrual nets below zero is taxed as zero
        # interest (the signed sum stays in the dividends_interest column so it reconciles with the credits)
        div_int_taxable = max(div_int, 0.0)
        row = {"year": year, "regime": params.regime, "n_sales": int(len(r)), "st_gain": st_gain, "lt_gain": lt_gain,
               "net_gain": st_gain + lt_gain, "wash_sale_disallowed": disallowed,
               "dividends_stock": div_stock, "dividends_interest": div_int,
               "loss_carried_in_st": carry_st, "loss_carried_in_lt": carry_lt}
        if params.regime == "nra":
            net = st_gain + lt_gain
            row.update(st_taxable=max(net, 0.0), lt_taxable=0.0, loss_carried_out_st=0.0, loss_carried_out_lt=0.0,
                       tax_gains=params.nra_gain_rate * max(net, 0.0),
                       tax_dividends_stock=params.dividend_rate * div_stock,
                       tax_dividends_interest=params.nra_interest_rate * div_int_taxable)
        elif params.regime == "resident":
            st = st_gain - carry_st
            lt = lt_gain - carry_lt
            if st < 0 < lt:
                use = min(-st, lt)
                st += use
                lt -= use
            elif lt < 0 < st:
                use = min(-lt, st)
                lt += use
                st -= use
            st_tax, lt_tax = max(0.0, st), max(0.0, lt)
            carry_st, carry_lt = max(0.0, -st), max(0.0, -lt)
            row.update(st_taxable=st_tax, lt_taxable=lt_tax, loss_carried_out_st=carry_st, loss_carried_out_lt=carry_lt,
                       tax_gains=params.st_rate * st_tax + params.lt_rate * lt_tax,
                       tax_dividends_stock=params.lt_rate * div_stock,
                       tax_dividends_interest=params.st_rate * div_int_taxable)
        else:
            row.update(st_taxable=0.0, lt_taxable=0.0, loss_carried_out_st=0.0, loss_carried_out_lt=0.0,
                       tax_gains=0.0, tax_dividends_stock=0.0, tax_dividends_interest=0.0)
        row["tax_total"] = row["tax_gains"] + row["tax_dividends_stock"] + row["tax_dividends_interest"]
        rows.append(row)
    return pd.DataFrame(rows, columns=TAX_COLUMNS)


# --------------------------------------------------------------------------
# after-tax path
# --------------------------------------------------------------------------


def after_tax_path(nav: pd.Series, taxes: pd.DataFrame) -> dict:
    """After-tax NAV by the proportional-withdrawal approximation (see the module docstring).

    `nav` is a date-indexed pre-tax NAV series; `taxes` a `tax_by_year` frame.
    Returns the after-tax series, its CAGR, the total tax withdrawn inside the
    series, tax as a percent of pre-tax profit, per-year withdrawal rows and
    the last year's accrued (not yet withdrawn) tax with the final NAV net of it.
    """
    nav = nav.dropna().sort_index()
    if nav.empty:
        raise ValueError("empty NAV series")
    idx = pd.DatetimeIndex(nav.index)
    tax_of = {int(y): float(t) for y, t in zip(taxes["year"], taxes["tax_total"])} if len(taxes) else {}
    factor = np.ones(len(nav))
    years_in = sorted(set(idx.year))
    rows = []
    accrued = 0.0
    accrued_frac = 0.0
    for year in years_in:
        tax = tax_of.get(int(year), 0.0)
        in_year = np.flatnonzero(idx.year == year)
        end_nav = float(nav.iloc[in_year[-1]])
        later = np.flatnonzero(idx.year > year)
        frac = tax / end_nav if end_nav > 0 else 0.0
        if len(later) == 0:
            accrued, accrued_frac = tax, frac
            rows.append({"year": int(year), "tax": tax, "nav_pre_tax_year_end": end_nav, "fraction": frac,
                         "withdrawn_on": None})
            continue
        factor[later[0]:] *= (1.0 - frac)
        rows.append({"year": int(year), "tax": tax, "nav_pre_tax_year_end": end_nav, "fraction": frac,
                     "withdrawn_on": str(idx[later[0]].date())})
    after = pd.Series(nav.to_numpy() * factor, index=idx, name=f"{nav.name}_after_tax" if nav.name else "after_tax")
    years = (idx[-1] - idx[0]).days / 365.25
    start = float(nav.iloc[0])
    final_pre = float(nav.iloc[-1])
    final_after = float(after.iloc[-1])
    final_incl = final_after * (1.0 - accrued_frac)

    def cagr(final: float) -> float:
        return (final / start) ** (1.0 / years) - 1.0 if years > 0 and start > 0 and final > 0 else float("nan")

    total_withdrawn = float(sum(r["tax"] for r in rows if r["withdrawn_on"] is not None))
    total_incl = total_withdrawn + accrued
    profit = final_pre - start
    return {
        "nav_after_tax": after,
        "cagr_pre_tax": cagr(final_pre),
        "cagr_after_tax": cagr(final_after),
        "final_nav_pre_tax": final_pre,
        "final_nav_after_tax": final_after,
        "total_tax_withdrawn": total_withdrawn,
        "accrued_unpaid_tax_last_year": accrued,
        "final_nav_after_tax_incl_accrued": final_incl,
        "cagr_after_tax_incl_accrued": cagr(final_incl),
        "total_tax_incl_accrued": total_incl,
        "tax_pct_of_pre_tax_profit": (total_incl / profit) if profit > 0 else float("nan"),
        "years": years,
        "rows": rows,
    }


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------


def tax_report(trades, dividends, nav: pd.Series | None, params: TaxParams, years=None, fractional=()) -> dict:
    """Everything one account needs: lots, the per-year table under `params.regime`
    and under the other regime, and (when `nav` is given) both after-tax paths.
    `fractional`: tickers traded in decimal units (see `normalize_trades`)."""
    match = match_lots(trades, dividends, fractional)
    span = list(years or [])
    if nav is not None and len(nav):
        span += sorted(set(pd.DatetimeIndex(nav.dropna().index).year))
    out = {"match": match, "regimes": {}}
    for regime in (params.regime, OTHER_REGIME[params.regime]):
        if regime in out["regimes"]:
            continue
        p = params.with_regime(regime)
        table = tax_by_year(match.realized, match.dividends, p, years=span or None)
        block = {"params": p, "by_year": table}
        if nav is not None and len(nav):
            block["path"] = after_tax_path(nav, table)
        out["regimes"][regime] = block
    return out


def year_summary(table: pd.DataFrame, year: int) -> dict:
    """The row of `year` as plain floats (zeros when the year has no row)."""
    row = table[table["year"] == int(year)]
    if row.empty:
        return {c: 0.0 for c in TAX_COLUMNS if c not in ("year", "regime")} | {"year": int(year), "regime": None}
    out = row.iloc[0].to_dict()
    return {k: (float(v) if isinstance(v, (int, float, np.integer, np.floating)) and k not in ("year", "n_sales") else v)
            for k, v in out.items()}


def _finite(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)
