"""What the agent may propose: a strategy family from the engine and its knobs, inside fixed ranges.

An idea is data, never code. Every idea is checked here before anything runs,
and two ideas that would run the same backtest get the same hash, so the
ledger counts each distinct backtest once.

No family takes a ticker. The model has read about the years the holdout
covers, so letting it name stocks would leak the holdout into the search.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Knob:
    kind: str                      # int | float | bool | choice
    default: Any
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    def check(self, name: str, value: Any) -> Any:
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
            return value
        if self.kind == "choice":
            if value not in self.choices:
                raise ValueError(f"{name} must be one of {', '.join(self.choices)}")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if self.kind == "int":
            if float(value) != int(value):
                raise ValueError(f"{name} must be a whole number")
            value = int(value)
        else:
            value = round(float(value), 6)
        if not (self.lo <= value <= self.hi):
            raise ValueError(f"{name}={value} is outside [{self.lo:g}, {self.hi:g}]")
        return value


KNOBS: dict[str, Knob] = {
    "sma_months": Knob("int", 10, 3, 12, help="market trend vote: SPY above its N-month-end average"),
    "tr_months": Knob("int", 12, 3, 12, help="market momentum vote: SPY's N-month total return above T-bills"),
    "votes": Knob("choice", "both", choices=("both", "a_only", "b_only"),
                  help="which market votes decide exposure (a = trend, b = momentum)"),
    "vol_target": Knob("float", 0.20, 0.10, 0.40, help="scale exposure down when SPY's 20-day vol exceeds this"),
    "mom_months": Knob("int", 12, 3, 12, help="stock ranking: N-month return skipping the latest month"),
    "vol_months": Knob("int", 12, 3, 12, help="stock ranking: daily volatility over the last N months, lowest first"),
    "top_n": Knob("int", 20, 5, 50, help="stocks bought at each month-end"),
    "hold_rank": Knob("int", 40, 5, 100, help="a held stock is kept while it ranks inside this"),
    "name_cap": Knob("float", 0.10, 0.03, 0.25, help="largest weight of one stock"),
    "liq_min_usd": Knob("float", 20e6, 5e6, 100e6, help="minimum 20-day median dollar volume to be eligible"),
    "band_abs": Knob("float", 0.01, 0.0, 0.05, help="skip a rebalance trade smaller than this weight"),
    "band_rel": Knob("float", 0.25, 0.0, 0.50, help="skip a rebalance trade smaller than this fraction of the target"),
    "breaker": Knob("bool", False, help="go to cash after a sharp drawdown until the next month-end"),
    "breaker_dd": Knob("float", -0.12, -0.25, -0.05, help="drawdown from the 20-session peak that trips the breaker"),
    "breaker_window": Knob("int", 20, 5, 60, help="sessions in the breaker's peak window"),
    "pair_lookback": Knob("int", 252, 63, 378, help="sessions of returns the pair rule scores"),
    "pair_keep_frac": Knob("float", 0.8, 0.5, 1.0, help="keep the current pair while its score is at least this x the best"),
    "pair_select": Knob("choice", "variance_of_difference", choices=("variance_of_difference", "topvol_lowcorr"),
                        help="how the two stocks are chosen"),
    "pair_top_vol": Knob("int", 10, 5, 30, help="topvol_lowcorr draws the pair from the N most volatile stocks"),
    "w_spy": Knob("int", 1, 0, 10, help="relative weight of a SPY buy-and-hold sleeve, 0 leaves it out"),
    "w_regime_spy": Knob("int", 1, 0, 10, help="relative weight of the regime_spy sleeve, 0 leaves it out"),
    "w_regime_stocks": Knob("int", 1, 0, 10, help="relative weight of the regime_stocks sleeve, 0 leaves it out"),
    "w_stocks_only": Knob("int", 1, 0, 10, help="relative weight of the stocks_only sleeve, 0 leaves it out"),
    "w_pair_rule": Knob("int", 1, 0, 10, help="relative weight of the pair_rule sleeve, 0 leaves it out"),
}

# blend weight knob -> the family its sleeve runs; spy_bh is the benchmark variant and reads no knob
SLEEVES = {"w_spy": "spy_bh", "w_regime_spy": "regime_spy", "w_regime_stocks": "regime_stocks",
           "w_stocks_only": "stocks_only", "w_pair_rule": "pair_rule"}

_WEIGHTS = tuple(SLEEVES)
_REGIME = ("sma_months", "tr_months", "votes", "vol_target")
_STOCKS = ("mom_months", "top_n", "hold_rank", "name_cap", "liq_min_usd")
_BANDS = ("band_abs", "band_rel")
_BREAKER = ("breaker", "breaker_dd", "breaker_window")
_PAIR = ("pair_lookback", "pair_keep_frac", "pair_select", "pair_top_vol")

FAMILIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "regime_spy": ("SPY or cash: hold SPY when the market votes say risk-on, scaled by a volatility cap",
                   _REGIME + _BANDS + _BREAKER),
    "regime_stocks": ("momentum stocks when the market votes say risk-on, cash otherwise, scaled by a volatility cap",
                      _REGIME + _STOCKS + _BANDS + _BREAKER),
    "stocks_only": ("momentum stocks, always fully invested", _STOCKS + _BANDS + _BREAKER),
    "pair_rule": ("two stocks 50/50 chosen by a rule each month-end", _PAIR + _BREAKER),
    "blend": ("sleeves of SPY and the four families above, each run as its own account and set back to the target "
              "weights at every month-end, paying the cost level on the money moved; weights are relative, so 2 and 1 "
              "mean two thirds and one third", _WEIGHTS + _REGIME + _STOCKS + _BANDS + _BREAKER + _PAIR),
    "lowvol_stocks": ("the lowest-volatility stocks, always fully invested, with no trend or momentum test "
                      "(Blitz and van Vliet 2007)",
                      ("vol_months", "top_n", "hold_rank", "name_cap", "liq_min_usd") + _BANDS + _BREAKER),
}
# ew_universe_bh is left out: at a 100k account its 1/500 targets sit under the engine's
# 500 USD minimum trade, so it holds cash and measures nothing

BENCHMARK = "spy_bh"


@dataclass(frozen=True)
class Idea:
    family: str
    params: dict[str, Any]         # every knob of the family, defaults filled in

    @property
    def key(self) -> str:
        """sha256 of the canonical idea: equal for any two ideas that run the same backtest."""
        blob = json.dumps({"family": self.family, "params": self.params}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def short(self) -> str:
        """The knobs that differ from the defaults, for people."""
        changed = {k: v for k, v in self.params.items() if v != KNOBS[k].default}
        return self.family + (" " + " ".join(f"{k}={v}" for k, v in sorted(changed.items())) if changed else " (defaults)")

    def as_dict(self) -> dict:
        return {"family": self.family, "params": dict(self.params)}


def validate(raw: Any) -> Idea:
    """Turn a proposal into an Idea or raise ValueError naming what is wrong. Nothing is clamped."""
    if not isinstance(raw, dict):
        raise ValueError("an idea must be an object")
    family = raw.get("family")
    if family not in FAMILIES:
        raise ValueError(f"unknown family {family!r}; allowed: {', '.join(FAMILIES)}")
    given = raw.get("params") or {}
    if not isinstance(given, dict):
        raise ValueError("params must be an object")
    allowed = FAMILIES[family][1]
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        raise ValueError(f"{family} has no knob {', '.join(unknown)}; its knobs are {', '.join(allowed)}")
    params = {k: KNOBS[k].check(k, given[k]) if k in given else KNOBS[k].default for k in allowed}
    if family == "blend":
        _normalize_blend(params)
    if "hold_rank" in params and params["hold_rank"] < params["top_n"]:
        raise ValueError("hold_rank must be at least top_n")
    # knobs the backtest never reads under these settings go back to their defaults, so they cannot fake a new trial
    if not params.get("breaker", True):
        params["breaker_dd"], params["breaker_window"] = KNOBS["breaker_dd"].default, KNOBS["breaker_window"].default
    if params.get("pair_select") == "variance_of_difference":
        params["pair_top_vol"] = KNOBS["pair_top_vol"].default
    if params.get("votes") == "a_only":
        params["tr_months"] = KNOBS["tr_months"].default
    elif params.get("votes") == "b_only":
        params["sma_months"] = KNOBS["sma_months"].default
    return Idea(family, params)


def _normalize_blend(params: dict[str, Any]) -> None:
    """Weights in lowest terms, and knobs no active sleeve reads at their defaults, so neither fakes a new trial."""
    active = [w for w in SLEEVES if params[w] > 0]
    if len(active) < 2:
        raise ValueError("a blend needs at least two sleeves with weight above 0")
    g = math.gcd(*(params[w] for w in active))
    for w in SLEEVES:
        params[w] //= g
    read = {k for w in active if SLEEVES[w] in FAMILIES for k in FAMILIES[SLEEVES[w]][1]}
    for k in params:
        if k not in SLEEVES and k not in read:
            params[k] = KNOBS[k].default


def describe() -> str:
    """The search space as the agent reads it."""
    lines = []
    for name, (what, knobs) in FAMILIES.items():
        lines.append(f"- {name}: {what}")
        for k in knobs:
            kn = KNOBS[k]
            rng = (" | ".join(kn.choices) if kn.kind == "choice" else "true | false" if kn.kind == "bool"
                   else f"{kn.kind} in [{kn.lo:g}, {kn.hi:g}]")
            lines.append(f"    {k} ({rng}, default {kn.default}): {kn.help}")
    return "\n".join(lines)
