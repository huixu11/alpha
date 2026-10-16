"""Sizing, model fills, commission, the ledger, equity, benchmark, replay.

This module owns the session state machine of strategy_spec.md section 11.
`daily.py` drives it one session at a time from fetched bars; `backtest.py`
drives it over history; `nulltest.py` reuses its sizing and cost model. They
must not diverge, which is why there is exactly one implementation here.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from quantdesk import config
from quantdesk.calendar import next_session, session_index
from quantdesk.strategy import (
    Candidate,
    Declined,
    MarketData,
    Order,
    Position,
    bucket_counts,
    collect_candidates,
    compute_slots,
    exit_conditions,
    rank_candidates,
    sessions_held,
)
from quantdesk.universe import BUCKET_OF


# --------------------------------------------------------------------------
# Rounding, section 8
# --------------------------------------------------------------------------


def round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def round_cash(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_rsi(value: float | None) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def round_shares(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# Section 9: model fills and commission
# --------------------------------------------------------------------------


def commission(shares: int, side: str = "BUY", fill_price: float = 0.0) -> float:
    """Section 9, charged on buys and on sells.

    Both sides:
        `max(MIN_COMMISSION, COMMISSION_PER_SHARE * shares)`
      + `max(MIN_PLATFORM_FEE, PLATFORM_FEE_PER_SHARE * shares)`
      + `SETTLEMENT_FEE_PER_SHARE * shares`

    Sells only, added to the above:
        `REG_FEE_RATE * proceeds + TAF_PER_SHARE * shares`,
    where `proceeds = shares * exit fill price` before any fee.

    The components are summed unrounded and the total is rounded once to 2
    decimals (section 9, `Commission booking`). Rounding each component would
    change the answer, which is why the sum happens first.
    """
    total = (
        max(config.MIN_COMMISSION, config.COMMISSION_PER_SHARE * shares)
        + max(config.MIN_PLATFORM_FEE, config.PLATFORM_FEE_PER_SHARE * shares)
        + config.SETTLEMENT_FEE_PER_SHARE * shares
    )
    if side == "SELL":
        proceeds = shares * fill_price
        total += config.REG_FEE_RATE * proceeds + config.TAF_PER_SHARE * shares
    return round_cash(total)


def entry_fill(open_price: float, slippage_bps: float = config.SLIPPAGE_BPS) -> float:
    return round_price(open_price * (1.0 + slippage_bps / 10_000.0))


def exit_fill(open_price: float, slippage_bps: float = config.SLIPPAGE_BPS) -> float:
    return round_price(open_price * (1.0 - slippage_bps / 10_000.0))


def slippage_bps(side: str, model_price: float, actual_price: float) -> float:
    """Basis points of an actual fill against the model fill of section 9.

    Positive is **worse for the strategy**: a buy that filled above the model
    fill, or a sell that filled below it. `execution_spec.md` section 7 gates
    scaling on "median buy slippage vs model fill" and section 4 step 7 asks
    for "slippage in basis points versus the model" in `fills/T+1.json`;
    neither states a sign, so this is the sign the whole system uses and it is
    stated here, in `docs/PAPER_SETUP.md`, and in `docs/spec_issues.md` under
    Phase B.
    """
    if not model_price:
        return 0.0
    difference = (actual_price - model_price) if side == "BUY" else (model_price - actual_price)
    return float(
        Decimal(str(difference / model_price * 10_000.0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def trade_return(
    shares: int,
    entry_price: float,
    entry_commission: float,
    exit_price: float,
    exit_commission: float,
) -> float:
    """Section 9: `(exit_proceeds - entry_cost) / entry_cost`, net both sides."""
    entry_cost = shares * entry_price + entry_commission
    exit_proceeds = shares * exit_price - exit_commission
    if entry_cost == 0:
        return 0.0
    return (exit_proceeds - entry_cost) / entry_cost


# --------------------------------------------------------------------------
# Session inputs and outputs
# --------------------------------------------------------------------------


@dataclass
class SessionInput:
    """Everything one session needs that the ledger cannot know by itself."""

    session: dt.date
    prev_session: dt.date
    market: MarketData
    included: list[str]
    data_status: str = "OK"                      # OK, STALE, DISPUTED, CROSSCHECK_UNAVAILABLE, ERROR
    fresh_fraction: float = 1.0
    crosscheck: str = "PASS"                     # PASS, WARN, DISPUTED, UNAVAILABLE, SKIPPED
    crosscheck_warn: list[str] = field(default_factory=list)
    revisions: dict[str, list] = field(default_factory=dict)
    carry_in: list[dict] = field(default_factory=list)   # record/carry.json, Phase B
    broker_fills: dict[str, dict] = field(default_factory=dict)  # fills/T.json, Phase B
    restart_recorded: bool = False               # operator restart from corrections.md
    trend_filter: bool = True                    # section 16.2 ablation
    floors: bool = True                          # section 16.2 ablation


@dataclass
class SessionResult:
    session: dt.date
    status: str
    halted: bool
    call: dict
    corrections: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


class Ledger:
    """The strategy's books, advanced one session at a time.

    The ledger is derived: `record/trades.csv` and `record/equity.csv` are
    regenerated by replay from recorded fills and published marks (section 14).
    Nothing here re-fetches or re-reads the bar cache to reconstruct history.
    """

    def __init__(
        self,
        starting_cash: float = config.STARTING_CASH,
        slippage_bps: float | None = None,
        auto_restart_sessions: int | None = None,
    ):
        # `auto_restart_sessions` is the backtest's operator (section 16.2).
        # In the record a halt is cleared only by a human writing a restart
        # into `record/corrections.md`; a backtest has no such human, so the
        # 2016 halt of the no-trend-filter ablation stood for ten years and
        # every later session of that run measured nothing. `None` is the
        # record's behaviour and is the default; `backtest.py` and
        # `nulltest.py` pass `config.BACKTEST_HALT_RESTART_SESSIONS`.
        self.auto_restart_sessions = auto_restart_sessions
        self.halt_session: dt.date | None = None
        self.halts: list[str] = []
        self.restarts: list[str] = []
        self.cash: float = starting_cash
        self.starting_cash: float = starting_cash
        self.slippage_bps: float = config.SLIPPAGE_BPS if slippage_bps is None else slippage_bps
        self.positions: dict[str, Position] = {}
        self.pending: list[Order] = []
        self.cooldown: dict[str, dt.date] = {}
        self.missing_ok_streak: dict[str, int] = {}
        self.hwm: float = starting_cash
        self.halted: bool = False
        self.equity_rows: list[dict] = []
        self.trades: list[dict] = []
        self.corrections: list[str] = []
        self.marks: dict[str, float] = {}
        self.mark_dates: dict[str, dt.date] = {}
        self.benchmark_shares: int = 0
        self.benchmark_cash: float = 0.0
        self.benchmark_started: dt.date | None = None
        self.benchmark_mark: float | None = None
        self.first_execution_session: dt.date | None = None
        self.session_flags: list[str] = []
        self._entry_cost_basis: dict[str, tuple[float, float]] = {}
        self._recent_losses: dict[str, dt.date] = {}
        self._order_seq: int = 0

    # -- helpers ------------------------------------------------------------

    def held_tickers(self) -> set[str]:
        return set(self.positions)

    def pending_tickers(self) -> set[str]:
        return {o.ticker for o in self.pending}

    def _next_order_seq(self) -> int:
        self._order_seq += 1
        return self._order_seq

    # ------------------------------------------------------------------
    # The session
    # ------------------------------------------------------------------

    def process_session(self, inp: SessionInput) -> SessionResult:
        session = inp.session
        self.session_flags = []
        corrections: list[str] = []
        fills: list[dict] = []
        reconciles = inp.data_status in ("OK", "DISPUTED", "CROSSCHECK_UNAVAILABLE")

        # -- step 3: reconcile published orders ---------------------------
        if reconciles:
            fills = self._reconcile(inp)
        else:
            # Section 11 step 3, last sentence: "In PAPER and LIVE, a fill
            # recorded in `fills/T.json` by the execution runner is the fill,
            # whatever the session status." A recorded fill therefore books
            # even on a STALE or ERROR session, on which no reconciliation from
            # bars runs. Section 11 step 6 says the same from the other side:
            # only an entry order *with no fill recorded* expires.
            for order in list(self.pending):
                row = self._actual_fill(inp, order)
                if row is not None:
                    fill = self._book_actual(order, inp, row, flag_crosscheck=False)
                    if fill is not None:
                        fills.append(fill)
                    continue
                if order.side == "BUY":
                    self._cancel_entry(order, "ORDER_EXPIRED")
                else:
                    order.flags.append("NEEDS_CARRY")

        # -- section 6.1 corporate actions --------------------------------
        # Applied before marking, because equity computed from unadjusted
        # shares against adjusted closes is arithmetically wrong. See
        # docs/spec_issues.md, Phase A, item 3.
        corporate_action, ca_exits, ca_corrections = self._corporate_actions(inp)
        corrections.extend(ca_corrections)

        # -- write-off of a stale DATA exit (section 6.1) ------------------
        written_off = self._write_offs(session)
        fills.extend(written_off)

        # -- marks and equity ---------------------------------------------
        self._update_marks(inp)
        equity = self.equity()

        # -- section 6.1 halt ---------------------------------------------
        # Section 6.1 `Halt restart` (v1.2.0): a restart recorded in
        # record/corrections.md re-bases `hwm` to `equity` at the restart
        # session and clears the halt, and from that session onward `hwm` is
        # the maximum over sessions from the restart only (section 4). Without
        # the re-basing the halt would be permanent: it liquidates every
        # position, cash earns 0, so equity can never climb back above 0.85 of
        # the pre-restart peak. See docs/spec_issues.md, Phase A, item 2.
        #
        # Section 16.2 (v1.2.0 amendment A2): in a *backtest* a halt is
        # followed by an automatic restart `auto_restart_sessions` sessions
        # after the halt session, with `hwm` re-based exactly as section 6.1
        # re-bases it for an operator's restart. Only a backtest sets it; in
        # the record `auto_restart_sessions` is `None` and only a human
        # clears a halt.
        auto_restart = (
            self.auto_restart_sessions is not None
            and self.halted
            and not inp.restart_recorded
            and self.halt_session is not None
            and session_index(session) - session_index(self.halt_session)
            >= self.auto_restart_sessions
        )
        if inp.restart_recorded or auto_restart:
            if self.halted:
                self.restarts.append(session.isoformat())
            self.halted = False
            self.halt_session = None
            self.hwm = equity
        else:
            self.hwm = max(self.hwm, equity)
        if equity < config.DRAWDOWN_HALT_FRACTION * self.hwm:
            if not self.halted:
                self.halts.append(session.isoformat())
                self.halt_session = session
            self.halted = True
        halted = self.halted

        # -- missing-bar bookkeeping (DATA counts only OK sessions) --------
        if inp.data_status == "OK":
            self._update_missing_streaks(inp)

        # -- publication ---------------------------------------------------
        orders: list[Order] = []
        declined: list[Declined] = []
        execute_on = next_session(session)

        carry_orders = self._carry_orders(inp, execute_on)
        orders.extend(carry_orders)
        carried = {o.ticker for o in carry_orders}

        # Section 6.4, one rule whatever the session status: a held position
        # with no bar at T has its exit check recorded `SKIPPED_NO_DATA`. The
        # flag follows bar presence alone, so it is set on a HALTED session too.
        for ticker, position in self.positions.items():
            series = inp.market.get(ticker)
            no_bar = series is None or not series.has_bar(session)
            if no_bar and "SKIPPED_NO_DATA" not in position.flags:
                position.flags.append("SKIPPED_NO_DATA")
            elif not no_bar and "SKIPPED_NO_DATA" in position.flags:
                position.flags.remove("SKIPPED_NO_DATA")

        if halted:
            for ticker in sorted(self.positions):
                if ticker in carried:
                    continue
                orders.append(
                    self._make_exit(ticker, "HALTED", ["HALTED"], inp, execute_on)
                )
        else:
            for order in ca_exits:
                order.execute_on = execute_on
                order.session = session
                orders.append(order)
            ca_exit_tickers = {o.ticker for o in ca_exits}
            for ticker in sorted(self.positions):
                if ticker in carried or ticker in ca_exit_tickers:
                    continue
                if ticker in corporate_action:
                    continue
                if any(o.ticker == ticker and o.side == "SELL" for o in self.pending):
                    continue
                series = inp.market.get(ticker)
                close_at_t, rsi_at_t = self._exit_inputs(series, inp)
                codes = exit_conditions(
                    self.positions[ticker],
                    session,
                    close_at_t,
                    rsi_at_t,
                    self.missing_ok_streak.get(ticker, 0),
                )
                if codes:
                    orders.append(
                        self._make_exit(ticker, codes[0], codes, inp, execute_on)
                    )

        exiting = {o.ticker for o in orders if o.side == "SELL"}

        status = config.worst_status(
            inp.data_status, "HALTED" if halted else "OK"
        )

        entry_orders: list[Order] = []
        if status == "OK":
            entry_orders, declined = self._entries(
                inp, equity, exiting, corporate_action, execute_on
            )
            orders.extend(entry_orders)

        # publish
        for order in orders:
            order.session = session
            order.execute_on = execute_on
        self.pending.extend(orders)

        # -- ledger rows ----------------------------------------------------
        self._update_benchmark_mark(inp)
        self.equity_rows.append(
            {
                "session": session.isoformat(),
                "status": status,
                "cash": round_cash(self.cash),
                "positions_value": round_cash(equity - round_cash(self.cash)),
                "equity": equity,
                "hwm": self.hwm,
                "spy_shares": self.benchmark_shares,
                "spy_cash": round_cash(self.benchmark_cash),
                "spy_equity": self.benchmark_equity(),
            }
        )

        call = self._build_call(
            inp, status, halted, equity, fills, orders, declined
        )
        return SessionResult(session, status, halted, call, corrections)

    # ------------------------------------------------------------------
    # Reconciliation, section 11 step 3
    # ------------------------------------------------------------------

    def _reconcile(self, inp: SessionInput) -> list[dict]:
        session = inp.session
        flag_crosscheck = inp.data_status in ("DISPUTED", "CROSSCHECK_UNAVAILABLE")
        fills: list[dict] = []

        due = [o for o in self.pending if o.execute_on is not None and o.execute_on <= session]
        sells = [o for o in due if o.side == "SELL"]
        buys = [o for o in due if o.side == "BUY"]

        # Sells first: their proceeds are what makes the buys affordable.
        for order in sells:
            row = self._actual_fill(inp, order)
            if row is not None:
                fill = self._book_actual(order, inp, row, flag_crosscheck)
            else:
                fill = self._fill_sell(order, inp, flag_crosscheck)
            if fill is not None:
                fills.append(fill)

        # An entry order the broker actually filled is a position, and the
        # ledger follows the broker (section 11 step 6, execution_spec.md
        # section 5). It is booked before the model-filled orders and is not
        # subject to the `CANCELLED_CASH` pass below, because a fill that has
        # already happened cannot be cancelled.
        for order in list(buys):
            row = self._actual_fill(inp, order)
            if row is None:
                continue
            buys.remove(order)
            fill = self._book_actual(order, inp, row, flag_crosscheck)
            if fill is not None:
                fills.append(fill)

        # Buys: cancel in reverse rank order until cash is not negative.
        priced: list[tuple[Order, float, float, int]] = []
        for order in buys:
            series = inp.market.get(order.ticker)
            open_price = series.open_at(session) if series else None
            if open_price is None:
                # section 9: an entry with no bar at T+1 is never delayed
                self._cancel_entry(order, "ORDER_EXPIRED")
                continue
            price = entry_fill(open_price, self.slippage_bps)
            fee = commission(order.shares, "BUY", price)
            priced.append((order, open_price, price, fee))

        priced.sort(key=lambda item: (item[0].rank if item[0].rank is not None else 0))
        while priced:
            total = sum(o.shares * p + f for o, _, p, f in priced)
            if self.cash - total >= 0:
                break
            order, _, _, _ = priced.pop()          # highest rank number first
            self._cancel_entry(order, "CANCELLED_CASH")

        for order, open_price, price, fee in priced:
            # Cash is money and money is two decimals (section 8). Applying the
            # rounded delta, rather than rounding a running unrounded total, is
            # what lets replay reproduce the ledger from the recorded fills.
            self.cash = round_cash(self.cash + round_cash(-(order.shares * price + fee)))
            self.positions[order.ticker] = Position(
                ticker=order.ticker,
                shares=order.shares,
                fill_session=session,
                fill_price=price,
                entry_commission=fee,
            )
            self._entry_cost_basis[order.ticker] = (price, fee)
            flags = ["CROSSCHECK_FLAGGED"] if flag_crosscheck else []
            fills.append(
                {
                    "ticker": order.ticker,
                    "side": "BUY",
                    "shares": order.shares,
                    "open": round_price(open_price),
                    "fill_price": price,
                    "commission": fee,
                    "flags": flags,
                }
            )
            self._record_trade(session, order, open_price, price, fee, flags, None)
            self.pending.remove(order)
            self._start_benchmark(inp, open_price)

        if self.cash < 0 and "CASH_NEGATIVE" not in self.session_flags:
            self.session_flags.append("CASH_NEGATIVE")
        return fills

    def _fill_sell(self, order: Order, inp: SessionInput, flag_crosscheck: bool) -> dict | None:
        session = inp.session
        series = inp.market.get(order.ticker)
        open_price = series.open_at(session) if series else None
        if open_price is None:
            # section 6.4: the exit executes at the first later session with a bar
            return None
        price = exit_fill(open_price, self.slippage_bps)
        fee = commission(order.shares, "SELL", price)
        flags: list[str] = []
        if order.execute_on is not None and session > order.execute_on:
            flags.append("DELAYED")
        if flag_crosscheck:
            flags.append("CROSSCHECK_FLAGGED")
        self.cash = round_cash(self.cash + round_cash(order.shares * price - fee))
        position = self.positions.pop(order.ticker, None)
        self.cooldown[order.ticker] = session
        self.marks.pop(order.ticker, None)
        self.mark_dates.pop(order.ticker, None)
        self.pending.remove(order)
        self._record_trade(session, order, open_price, price, fee, flags, position)
        self._start_benchmark(inp, open_price)
        return {
            "ticker": order.ticker,
            "side": "SELL",
            "shares": order.shares,
            "open": round_price(open_price),
            "fill_price": price,
            "commission": fee,
            "flags": flags,
        }

    # -- actual fills, strategy_spec.md section 11 steps 3 and 6 -----------
    #
    # "In PAPER and LIVE, a fill recorded in `fills/T.json` by the execution
    # runner is the fill, whatever the session status." The model fill of
    # section 9 stays as a reference column in `record/trades.csv`
    # (`model_fill_price`) together with the slippage in basis points
    # (`slippage_bps`) and which of the two was booked (`fill_source`).
    #
    # In `DRY_RUN` there is no `fills/` file, `inp.broker_fills` is empty, and
    # every path below is skipped, so nothing about Phase A's behaviour or its
    # committed call-file fixtures changes.

    def _actual_fill(self, inp: SessionInput, order: Order) -> dict | None:
        """The row of `fills/T.json` for this order, if the runner recorded one."""
        if not inp.broker_fills:
            return None
        row = inp.broker_fills.get(order.ticker)
        if row is None:
            return None
        side = str(row.get("side", order.side)).upper()
        if side != order.side:
            return None
        return row

    def _book_actual(
        self, order: Order, inp: SessionInput, row: dict, flag_crosscheck: bool
    ) -> dict | None:
        """Book one order from the broker's actual fill rather than from `open[T]`."""
        session = inp.session
        filled = int(row.get("filled_shares", row.get("shares", 0)) or 0)
        raw_price = row.get("avg_fill_price", row.get("fill_price"))
        price = round_price(float(raw_price)) if raw_price is not None else 0.0
        fee = round_cash(float(row.get("commission", 0.0) or 0.0))

        series = inp.market.get(order.ticker)
        open_price = series.open_at(session) if series is not None else None

        model = row.get("model_fill_price")
        if model is None and open_price is not None:
            model = (
                entry_fill(open_price, self.slippage_bps)
                if order.side == "BUY"
                else exit_fill(open_price, self.slippage_bps)
            )
        model = round_price(float(model)) if model is not None else None
        slip = row.get("slippage_bps")
        if slip is None and model:
            slip = slippage_bps(order.side, model, price)
        reference = {
            "model_fill_price": "" if model is None else model,
            "slippage_bps": "" if slip is None else slip,
            "fill_source": "BROKER",
        }

        flags = [f for f in (row.get("flags") or []) if isinstance(f, str)]
        if order.execute_on is not None and session > order.execute_on and "DELAYED" not in flags:
            flags.append("DELAYED")
        if flag_crosscheck and "CROSSCHECK_FLAGGED" not in flags:
            flags.append("CROSSCHECK_FLAGGED")

        if filled <= 0:
            if order.side == "BUY":
                # Section 4 `Cooldown`: a zero-fill cancellation starts cooldown
                # as if the intended execution session were an exit fill session.
                #
                # Which code it is recorded under depends on whether the order
                # ever reached the market. `execution_spec.md` section 5 says a
                # refused cycle writes a zero-fill row carrying the refusal, and
                # such an order is `ORDER_EXPIRED`, exactly as a pending entry on
                # a STALE session is (`strategy_spec.md` section 11 step 6). A
                # zero fill with no refusal was placed and did not fill, which is
                # `UNFILLED` (section 4 step 6). Both enter cooldown.
                self._cancel_entry(
                    order, "ORDER_EXPIRED" if row.get("refusal") else "UNFILLED"
                )
            else:
                # Section 4 step 7 and section 6.3: a sell is never dropped; the
                # position stays open and the exit is carried with rule CARRY.
                if "NEEDS_CARRY" not in order.flags:
                    order.flags.append("NEEDS_CARRY")
                if "UNFILLED_SELL" not in self.session_flags:
                    self.session_flags.append("UNFILLED_SELL")
            return None

        shares = min(filled, order.shares)
        booked_open = round_price(open_price) if open_price is not None else price

        if order.side == "BUY":
            self.cash = round_cash(self.cash + round_cash(-(shares * price + fee)))
            self.positions[order.ticker] = Position(
                ticker=order.ticker,
                shares=shares,
                fill_session=session,
                fill_price=price,
                entry_commission=fee,
            )
            self._entry_cost_basis[order.ticker] = (price, fee)
            self._record_trade(
                session, order, booked_open, price, fee, flags, None, reference, shares
            )
            self.pending.remove(order)
            if shares < order.shares and "UNFILLED" not in self.session_flags:
                # Section 4 `Partial entry fills`: the position is the filled
                # quantity, the remainder is cancelled and never retried.
                self.session_flags.append("UNFILLED")
            self._start_benchmark(inp, open_price if open_price is not None else price)
        else:
            self.cash = round_cash(self.cash + round_cash(shares * price - fee))
            position = self.positions.get(order.ticker)
            self._record_trade(
                session, order, booked_open, price, fee, flags, position, reference, shares
            )
            if shares >= order.shares:
                self.positions.pop(order.ticker, None)
                self.cooldown[order.ticker] = session
                self.marks.pop(order.ticker, None)
                self.mark_dates.pop(order.ticker, None)
                self.pending.remove(order)
            else:
                # A partially filled sell books the filled quantity and leaves
                # the remainder outstanding, which section 6.3 re-publishes as
                # `CARRY`. See docs/spec_issues.md, Phase B.
                if position is not None:
                    position.shares = max(0, position.shares - shares)
                order.shares = order.shares - shares
                if "NEEDS_CARRY" not in order.flags:
                    order.flags.append("NEEDS_CARRY")
                if "UNFILLED_SELL" not in self.session_flags:
                    self.session_flags.append("UNFILLED_SELL")
            self._start_benchmark(inp, open_price if open_price is not None else price)

        return {
            "ticker": order.ticker,
            "side": order.side,
            "shares": shares,
            "open": booked_open,
            "fill_price": price,
            "commission": fee,
            "flags": flags,
        }

    def _cancel_entry(self, order: Order, reason: str) -> None:
        """Section 4: a zero-fill cancellation starts cooldown from T+1."""
        if order in self.pending:
            self.pending.remove(order)
        started = order.execute_on or order.session
        if started is not None:
            self.cooldown[order.ticker] = started
        if reason not in self.session_flags:
            self.session_flags.append(reason)

    # ------------------------------------------------------------------
    # Section 6.1: corporate actions
    # ------------------------------------------------------------------

    def _corporate_actions(self, inp: SessionInput):
        marked: set[str] = set()
        exits: list[Order] = []
        corrections: list[str] = []
        for ticker, revisions in sorted(inp.revisions.items()):
            if not revisions:
                continue
            close_rev = [
                r for r in revisions
                if r.field == "close" and r.date == inp.prev_session
            ]
            if close_rev:
                rev = close_rev[0]
                if rev.old == 0:
                    continue
                r = rev.new / rev.old
            else:
                r = 1.0
            if abs(r - 1.0) <= config.CORPORATE_ACTION_THRESHOLD:
                # "the revision is logged and nothing else happens": the log is
                # data/revisions.log, written by the cache before the update.
                continue
            marked.add(ticker)
            close_revisions = [rr for rr in revisions if rr.field == "close" and rr.old != 0]
            ratios = [rr.new / rr.old for rr in close_revisions]
            single_ratio = all(
                abs(x - r) <= config.CORPORATE_ACTION_RATIO_TOLERANCE for x in ratios
            )
            position = self.positions.get(ticker)
            if single_ratio:
                if position is not None:
                    # strategy_spec.md v1.2.0 section 6.1: `shares_X` is
                    # divided by `r` and `fill_price_X` is multiplied by `r`,
                    # with `r = close_X[T-1]_new / close_X[T-1]_old` on the
                    # split-adjusted close. A 2-for-1 split gives r = 0.5, so
                    # 100 shares at a fill of 200.00 become 200 shares at a
                    # fill of 100.00: position value is preserved and the share
                    # count matches what the broker reports. v1.1.0 stated the
                    # inverse and the code deviated from it; v1.2.0 states the
                    # direction implemented here, so spec and code now agree.
                    # See docs/spec_issues.md, Phase A, item 1.
                    old_shares, old_price = position.shares, position.fill_price
                    position.shares = round_shares(position.shares / r)
                    position.fill_price = round_price(position.fill_price * r)
                    corrections.append(
                        f"{inp.session.isoformat()} {ticker}: single-ratio corporate action "
                        f"r={r:.6f}; shares {old_shares} -> {position.shares}, "
                        f"fill price {old_price} -> {position.fill_price}."
                    )
                else:
                    corrections.append(
                        f"{inp.session.isoformat()} {ticker}: single-ratio corporate action "
                        f"r={r:.6f}; no position held, no adjustment."
                    )
            else:
                corrections.append(
                    f"{inp.session.isoformat()} {ticker}: corporate action without a single "
                    f"ratio (r={r:.6f}); CORPORATE_ACTION_EXIT published."
                )
                if position is not None:
                    exits.append(
                        Order(
                            side="SELL",
                            ticker=ticker,
                            shares=position.shares,
                            rule="CORPORATE_ACTION_EXIT",
                            rules=["CORPORATE_ACTION_EXIT"],
                            rsi=None,
                        )
                    )
        self.corrections.extend(corrections)
        return marked, exits, corrections

    # ------------------------------------------------------------------
    # Section 6.1: write-off
    # ------------------------------------------------------------------

    def _write_offs(self, session: dt.date) -> list[dict]:
        """Section 6.1 `Write-off`, at 10 sessions **of any status**.

        The gap is measured in XNYS calendar positions, which counts every
        session between the intended execution session and T whatever that
        session's status was. The `DATA` count of section 6.2 is the one that
        advances on OK sessions only; this one does not.
        """
        fills: list[dict] = []
        for order in list(self.pending):
            if order.side != "SELL" or order.rule not in ("DATA", "CARRY"):
                continue
            if "DATA" not in order.rules:
                continue
            if order.execute_on is None:
                continue
            gap = session_index(session) - session_index(order.execute_on)
            if gap < config.DELISTING_WRITEOFF_SESSIONS:
                continue
            position = self.positions.pop(order.ticker, None)
            self.pending.remove(order)
            self.cooldown[order.ticker] = session
            self.marks.pop(order.ticker, None)
            self.mark_dates.pop(order.ticker, None)
            flags = ["WRITTEN_OFF"]
            self._record_trade(session, order, 0.0, 0.0, 0.0, flags, position)
            fills.append(
                {
                    "ticker": order.ticker,
                    "side": "SELL",
                    "shares": order.shares,
                    "open": 0.0,
                    "fill_price": 0.0,
                    "commission": 0.0,
                    "flags": flags,
                }
            )
        return fills

    # ------------------------------------------------------------------
    # Marks and equity, section 4
    # ------------------------------------------------------------------

    def _update_marks(self, inp: SessionInput) -> None:
        session = inp.session
        for ticker, position in self.positions.items():
            series = inp.market.get(ticker)
            if series is None:
                continue
            found = series.last_close_on_or_before(session)
            if found is None:
                continue
            bar_date, close = found
            gap = session_index(session) - session_index(bar_date)
            if gap >= config.DELISTING_WRITEOFF_SESSIONS:
                # section 6.1: equity is never marked from bars older than 10 sessions
                self.marks[ticker] = 0.0
                self.mark_dates[ticker] = bar_date
                continue
            self.marks[ticker] = close
            self.mark_dates[ticker] = bar_date

    def _update_missing_streaks(self, inp: SessionInput) -> None:
        for ticker in inp.included:
            series = inp.market.get(ticker)
            if series is not None and series.has_bar(inp.session):
                self.missing_ok_streak[ticker] = 0
            else:
                self.missing_ok_streak[ticker] = self.missing_ok_streak.get(ticker, 0) + 1

    def equity(self) -> float:
        """Section 4: `round(cash,2) + sum(round(shares*close,2))` alphabetically.

        The mark is rounded to the published price precision (four decimals,
        section 8) before it is multiplied, so a stranger recomputing equity
        from the numbers in the call file gets the number in the call file. See
        docs/spec_issues.md, Phase A, item 4.
        """
        total = Decimal(str(round_cash(self.cash)))
        for ticker in sorted(self.positions):
            position = self.positions[ticker]
            mark = round_price(self.marks.get(ticker, position.fill_price))
            total += Decimal(str(round_cash(position.shares * mark)))
        return float(total)

    # ------------------------------------------------------------------
    # Section 6.3: carry
    # ------------------------------------------------------------------

    def _carry_orders(self, inp: SessionInput, execute_on: dt.date) -> list[Order]:
        """Re-publish an exit order whose execution session ended unfilled.

        Section 6.3 (v1.2.0): a `CARRY` **replaces** the pending exit order it
        re-publishes rather than sitting beside it, so at most one exit order
        exists for a ticker at any time and exactly one sell is ever
        outstanding and ever executed for a position. See
        docs/spec_issues.md, Phase A, item 7.
        """
        out: list[Order] = []
        for order in list(self.pending):
            if order.side != "SELL":
                continue
            if "NEEDS_CARRY" not in order.flags:
                continue
            self.pending.remove(order)
            original = [r for r in order.rules if r != "CARRY"] or list(order.rules)
            out.append(
                Order(
                    side="SELL",
                    ticker=order.ticker,
                    shares=order.shares,
                    rule="CARRY",
                    rules=original,
                    rsi=order.rsi,
                )
            )
        # Phase B leaves record/carry.json for an unfilled sell at the broker.
        for entry in inp.carry_in:
            ticker = entry["ticker"]
            if any(o.ticker == ticker for o in out):
                continue
            position = self.positions.get(ticker)
            if position is None:
                continue
            for order in list(self.pending):
                if order.ticker == ticker and order.side == "SELL":
                    self.pending.remove(order)
            out.append(
                Order(
                    side="SELL",
                    ticker=ticker,
                    shares=int(entry.get("shares", position.shares)),
                    rule="CARRY",
                    rules=list(entry.get("rules", ["CARRY"])),
                    rsi=entry.get("rsi"),
                )
            )
        return sorted(out, key=lambda o: o.ticker)

    def _exit_inputs(self, series, inp: SessionInput) -> tuple[float | None, float | None]:
        """The close and the RSI the exit table is evaluated on (section 6.4).

        v1.2.0 states one rule, whatever the session status: a held position
        with a bar at T is evaluated against the whole table; a held position
        with no bar at T is marked at its last available close, its exit check
        is recorded `SKIPPED_NO_DATA`, and no price or RSI condition is
        evaluated for it. The v1.1.0 split by session status is gone.

        The one condition that still runs with no bar is `DATA`, which is
        evaluated in `strategy.exit_conditions` from the missing-bar counter
        alone. `DATA` fires only when the ticker has no bar, so reading
        "no condition of the section 6.2 table is evaluated" to include `DATA`
        would make a `DATA` exit unreachable and the write-off of section 6.1
        dead code. See docs/spec_issues.md, Phase A, item 14.
        """
        if series is None or not series.has_bar(inp.session):
            return None, None
        return series.close_at(inp.session), series.rsi_at(inp.session)

    def _make_exit(
        self, ticker: str, rule: str, rules: list[str], inp: SessionInput, execute_on: dt.date
    ) -> Order:
        series = inp.market.get(ticker)
        _, rsi = self._exit_inputs(series, inp)
        return Order(
            side="SELL",
            ticker=ticker,
            shares=self.positions[ticker].shares,
            rule=rule,
            rules=list(rules),
            rsi=rsi,
        )

    # ------------------------------------------------------------------
    # Sections 5, 7, 8: entries
    # ------------------------------------------------------------------

    def _entries(
        self,
        inp: SessionInput,
        equity: float,
        exiting: set[str],
        corporate_action: set[str],
        execute_on: dt.date,
    ) -> tuple[list[Order], list[Declined]]:
        pending_entries = {o.ticker for o in self.pending if o.side == "BUY"}
        candidates = collect_candidates(
            inp.market,
            inp.included,
            inp.session,
            inp.prev_session,
            held=self.held_tickers(),
            cooldown=self.cooldown,
            pending=self.pending_tickers() | exiting,
            warned=set(inp.crosscheck_warn),
            corporate_action=corporate_action,
            trend_filter=inp.trend_filter,
            floors=inp.floors,
        )
        ranked = rank_candidates(candidates)

        slots = compute_slots(self.positions, exiting, pending_entries)
        counts = bucket_counts(self.positions, exiting, pending_entries)

        alloc = round_cash(config.ALLOCATION_FRACTION * equity)
        min_alloc = config.MIN_ALLOCATION_FRACTION * equity
        avail = self.cash + config.SALE_PROCEEDS_HAIRCUT * sum(
            self.positions[t].shares * (inp.market.get(t).close_at(inp.session) or 0.0)
            for t in exiting
            if t in self.positions
            and inp.market.get(t) is not None
            and inp.market.get(t).has_bar(inp.session)
        )

        orders: list[Order] = []
        declined: list[Declined] = []
        used = 0
        rank = 0
        for candidate in ranked:
            bucket = candidate.bucket
            if counts.get(bucket, 0) + 1 > config.MAX_PER_BUCKET:
                declined.append(
                    Declined(candidate.ticker, "DECLINED_SECTOR", bucket,
                             candidate.rsi_prev, candidate.rsi)
                )
                continue
            if used >= slots:
                declined.append(
                    Declined(candidate.ticker, "DECLINED_CAPACITY", bucket,
                             candidate.rsi_prev, candidate.rsi)
                )
                continue
            a = min(alloc, avail)
            if a < min_alloc:
                declined.append(
                    Declined(candidate.ticker, "DECLINED_CASH", bucket,
                             candidate.rsi_prev, candidate.rsi)
                )
                continue
            shares = math.floor(a / candidate.close)
            if shares == 0:
                declined.append(
                    Declined(candidate.ticker, "DECLINED_PRICE", bucket,
                             candidate.rsi_prev, candidate.rsi)
                )
                continue
            rank += 1
            orders.append(
                Order(
                    side="BUY",
                    ticker=candidate.ticker,
                    shares=shares,
                    rule="ENTRY",
                    rules=["ENTRY"],
                    rsi=candidate.rsi,
                    rsi_prev=candidate.rsi_prev,
                    rank=rank,
                    close_used=candidate.close,
                    alloc=alloc,
                    avail_before=avail,
                )
            )
            avail -= shares * candidate.close
            used += 1
            counts[bucket] = counts.get(bucket, 0) + 1
        return orders, declined

    # ------------------------------------------------------------------
    # Section 10: benchmark
    # ------------------------------------------------------------------

    def _start_benchmark(self, inp: SessionInput, _open_price: float) -> None:
        if self.benchmark_started is not None:
            return
        series = inp.market.get(config.BENCHMARK_TICKER)
        if series is None:
            return
        spy_open = series.open_at(inp.session)
        if spy_open is None:
            return
        shares = math.floor(self.starting_cash / (spy_open * (1.0 + self.slippage_bps / 10_000.0)))
        price = entry_fill(spy_open, self.slippage_bps)
        fee = commission(shares, "BUY", price)
        self.benchmark_shares = shares
        self.benchmark_cash = round_cash(self.starting_cash - shares * price - fee)
        self.benchmark_started = inp.session
        self.first_execution_session = inp.session

    def _update_benchmark_mark(self, inp: SessionInput) -> None:
        series = inp.market.get(config.BENCHMARK_TICKER)
        if series is None:
            return
        found = series.last_close_on_or_before(inp.session)
        if found is not None:
            self.benchmark_mark = found[1]

    def benchmark_equity(self) -> float:
        if self.benchmark_started is None:
            return round_cash(self.starting_cash)
        mark = self.benchmark_mark or 0.0
        return float(
            Decimal(str(round_cash(self.benchmark_cash)))
            + Decimal(str(round_cash(self.benchmark_shares * mark)))
        )

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def _record_trade(
        self,
        session: dt.date,
        order: Order,
        open_price: float,
        price: float,
        fee: float,
        flags: list[str],
        position: Position | None,
        reference: dict | None = None,
        shares: int | None = None,
    ) -> None:
        """One row of `record/trades.csv`.

        `reference` carries the Phase B columns: the model fill of section 9,
        the slippage against it in basis points, and whether the booked fill
        came from the broker or from the model. On a `DRY_RUN` session it is
        `None` and the three columns are the empty string, exactly as they were
        before Phase B existed.
        """
        shares = order.shares if shares is None else int(shares)
        reference = reference or {}
        row = {
            "session": session.isoformat(),
            "ticker": order.ticker,
            "side": order.side,
            "shares": shares,
            "open": round_price(open_price),
            "fill_price": price,
            "commission": fee,
            "rule": order.rule,
            "rules": "|".join(order.rules),
            "flags": "|".join(flags),
            "entry_session": "",
            "entry_price": "",
            "net_return": "",
            "wash_sale_candidate": "false",
            "model_fill_price": reference.get("model_fill_price", ""),
            "slippage_bps": reference.get("slippage_bps", ""),
            "fill_source": reference.get("fill_source", "MODEL"),
        }
        if order.side == "BUY":
            row["cash_delta"] = round_cash(-(shares * price + fee))
            prior_loss = self._recent_losses.get(order.ticker)
            if prior_loss is not None and (session - prior_loss).days <= 30:
                row["wash_sale_candidate"] = "true"
        else:
            row["cash_delta"] = round_cash(shares * price - fee)
            if position is not None:
                ret = trade_return(
                    shares,
                    position.fill_price,
                    position.entry_commission,
                    price,
                    fee,
                )
                row["entry_session"] = position.fill_session.isoformat()
                row["entry_price"] = position.fill_price
                row["net_return"] = f"{ret:.10f}"
                if ret < 0:
                    self._recent_losses[order.ticker] = session
        self.trades.append(row)

    # ------------------------------------------------------------------
    # The call file, section 13
    # ------------------------------------------------------------------

    def _build_call(
        self,
        inp: SessionInput,
        status: str,
        halted: bool,
        equity: float,
        fills: list[dict],
        orders: list[Order],
        declined: list[Declined],
    ) -> dict:
        positions = []
        for ticker in sorted(self.positions):
            position = self.positions[ticker]
            positions.append(
                {
                    "ticker": ticker,
                    "shares": position.shares,
                    "fill_session": position.fill_session.isoformat(),
                    "fill_price": round_price(position.fill_price),
                    "mark": round_price(self.marks.get(ticker, position.fill_price)),
                    "sessions_held": sessions_held(position.fill_session, inp.session),
                    "flags": sorted(position.flags),
                }
            )

        sells = [o for o in orders if o.side == "SELL"]
        buys = [o for o in orders if o.side == "BUY"]
        sells.sort(key=lambda o: o.ticker)
        buys.sort(key=lambda o: (o.rank if o.rank is not None else 0))
        order_rows = [_order_row(o) for o in sells] + [_order_row(o) for o in buys]

        call = {
            "spec_version": config.SPEC_VERSION,
            "session": inp.session.isoformat(),
            "generated_at": "",
            "status": status,
            "halted": halted,
            "flags": sorted(set(self.session_flags)),
            "data": {
                "provider": config.PRIMARY_PROVIDER,
                "crosscheck_provider": config.CROSSCHECK_PROVIDER,
                "crosscheck": inp.crosscheck,
                "crosscheck_warn": sorted(inp.crosscheck_warn),
                "fresh_fraction": round(inp.fresh_fraction, 6),
            },
            "equity_close": round_cash(equity),
            "cash_close": round_cash(self.cash),
            "benchmark": {
                "ticker": config.BENCHMARK_TICKER,
                "shares": self.benchmark_shares,
                "cash": round_cash(self.benchmark_cash),
                "mark": round_price(self.benchmark_mark) if self.benchmark_mark is not None else None,
                "equity": self.benchmark_equity(),
            },
            "fills": fills,
            "positions": positions,
            "orders": order_rows,
            "declined": [
                {
                    "ticker": d.ticker,
                    "reason": d.reason,
                    "bucket": d.bucket,
                    "rsi_prev": round_rsi(d.rsi_prev),
                    "rsi": round_rsi(d.rsi),
                }
                for d in declined
            ],
            "sha256": "",
        }
        return call


def _order_row(order: Order) -> dict:
    row = {
        "side": order.side,
        "ticker": order.ticker,
        "shares": order.shares,
        "rule": order.rule,
        "rules": list(order.rules),
        "rsi": round_rsi(order.rsi),
    }
    if order.side == "BUY":
        row["rsi_prev"] = round_rsi(order.rsi_prev)
        row["rank"] = order.rank
        row["close_used"] = round_price(order.close_used)
        row["alloc"] = round_cash(order.alloc)
        row["avail_before"] = round_cash(order.avail_before)
    return row


# --------------------------------------------------------------------------
# Ledger files
# --------------------------------------------------------------------------

# The last three columns are Phase B's: the model fill of section 9 kept as a
# reference beside the actual fill, the slippage between them in basis points,
# and which of the two was booked. On a DRY_RUN session `fill_source` is
# `MODEL` and the other two are empty.
TRADE_COLUMNS = (
    "session", "ticker", "side", "shares", "open", "fill_price", "commission",
    "cash_delta", "rule", "rules", "flags", "entry_session", "entry_price",
    "net_return", "wash_sale_candidate",
    "model_fill_price", "slippage_bps", "fill_source",
)

EQUITY_COLUMNS = (
    "session", "status", "cash", "positions_value", "equity", "hwm",
    "spy_shares", "spy_cash", "spy_equity",
)

CASH_EVENT_COLUMNS = ("date", "kind", "ticker", "amount", "note")


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="ascii") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    return path


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="ascii") as handle:
        return list(csv.DictReader(handle))


def read_cash_events(path: Path) -> list[dict]:
    """`record/cash_events.csv`, written by the operator (section 8)."""
    return read_csv(path)


# --------------------------------------------------------------------------
# Replay, section 14
# --------------------------------------------------------------------------


def replay(
    trades: list[dict],
    calls: list[dict],
    starting_cash: float = config.STARTING_CASH,
    cash_events: list[dict] | None = None,
) -> list[dict]:
    """Rebuild `record/equity.csv` from recorded fills and published marks.

    Reads nothing but the two arguments: the rows of `record/trades.csv` and
    the call files in session order. It never touches `data/cache/`, so a later
    revision of the bars cannot move a published number (section 14).
    """
    cash = starting_cash
    shares: dict[str, int] = {}
    trades_by_session: dict[str, list[dict]] = {}
    for row in trades:
        trades_by_session.setdefault(row["session"], []).append(row)
    events_by_date: dict[str, float] = {}
    for event in cash_events or []:
        events_by_date[event["date"]] = events_by_date.get(event["date"], 0.0) + float(
            event["amount"]
        )

    hwm = starting_cash
    rows: list[dict] = []
    for call in calls:
        session = call["session"]
        for row in trades_by_session.get(session, []):
            qty = int(row["shares"])
            cash = round_cash(cash + float(row["cash_delta"]))
            if row["side"] == "BUY":
                shares[row["ticker"]] = shares.get(row["ticker"], 0) + qty
            else:
                remaining = shares.get(row["ticker"], 0) - qty
                if remaining <= 0:
                    shares.pop(row["ticker"], None)
                else:
                    shares[row["ticker"]] = remaining
        if session in events_by_date:
            cash = round_cash(cash + events_by_date[session])

        marks = {p["ticker"]: float(p["mark"]) for p in call["positions"]}
        call_shares = {p["ticker"]: int(p["shares"]) for p in call["positions"]}
        # A corporate action adjusts shares without producing a fill, so where
        # the published share count differs from the count accumulated out of
        # the recorded fills, the published call file wins: it is the record.
        for ticker, qty in call_shares.items():
            if shares.get(ticker) != qty:
                shares[ticker] = qty
        for ticker in [t for t in shares if t not in call_shares]:
            shares.pop(ticker)
        total = Decimal(str(round_cash(cash)))
        for ticker in sorted(shares):
            total += Decimal(str(round_cash(shares[ticker] * marks[ticker])))
        equity = float(total)
        hwm = max(hwm, equity)
        benchmark = call.get("benchmark") or {}
        rows.append(
            {
                "session": session,
                "status": call["status"],
                "cash": round_cash(cash),
                "positions_value": round_cash(equity - round_cash(cash)),
                "equity": equity,
                "hwm": hwm,
                "spy_shares": benchmark.get("shares", 0),
                "spy_cash": benchmark.get("cash", 0.0),
                "spy_equity": benchmark.get("equity", 0.0),
            }
        )
    return rows
