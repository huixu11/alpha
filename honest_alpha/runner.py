"""Backtest ideas on a pickled dataset. Every search sandbox runs this file and nothing else.

    python -m honest_alpha.runner JOB.json RESULTS.json

JOB.json is {"start": "2015-06-30", "ideas": [{"trial": "T-0001", "family": ..., "params": {...}}, ...]}
and may name another dataset with "data". Each idea runs once per cost level;
the cost is slippage per side, with the broker's per-share commission on top.
"""
from __future__ import annotations

import json
import os
import pickle
import platform
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honest_alpha import settings, space  # noqa: E402

settings.use_engine()
from tools import regime_backtest as rb  # noqa: E402

COSTS_BPS = (5, 15, 30)


def load(path: Path = settings.SEARCH_DATA) -> "rb.Data":
    with open(path, "rb") as h:
        return pickle.load(h)


def _summary(result: dict, data: "rb.Data") -> dict:
    s = rb.stats(result, data)
    nav = result["nav"]
    rets = nav.pct_change().dropna()
    pos = data.dates.get_indexer(nav.index)
    bil = pd.Series(data.bil_tr[pos], index=nav.index).pct_change().dropna()
    x = (rets - bil.reindex(rets.index).fillna(0.0)).to_numpy()      # the same daily excess returns as stats()
    z = (x - x.mean()) / x.std(ddof=0)
    return {
        "sharpe": round(s["sharpe"], 4), "cagr": round(s["cagr"], 4), "ann_vol": round(s["ann_vol"], 4),
        "max_drawdown": round(s["max_drawdown"], 4), "avg_exposure": round(s["avg_exposure"], 3),
        "turnover": round(s["one_way_turnover_per_year"], 3),
        "costs_pct_nav_per_year": round(s["costs_pct_nav_per_year"], 5),
        "n_trades": s["n_trades"], "n_delisted": s.get("n_delisted", 0),
        "yearly": {str(y): round(r, 4) for y, r in s["yearly_returns"].items()},
        "sr_daily": float(x.mean() / x.std(ddof=1)), "skew": float((z ** 3).mean()), "kurt": float((z ** 4).mean()),
        "n_obs": int(len(x)),
    }


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    full = [f for f in frames if len(f)]
    return pd.concat(full, ignore_index=True) if full else frames[0]


def _blend(runs: list[tuple[int, dict]], data: "rb.Data") -> dict:
    """Sleeves as separate accounts, combined in return space and set back to their target weights after every
    month-end close; the money moved between sleeves pays rb.SLIPPAGE, deducted from the blend's NAV."""
    tw = np.array([w for w, _ in runs], dtype=float)
    tw /= tw.sum()
    index = runs[0][1]["nav"].index
    navs = np.column_stack([r["nav"].to_numpy() for _, r in runs])
    me = set(int(i) for i in data.me_idx)
    pos = data.dates.get_indexer(index)
    value = tw * navs[0]
    nav = np.empty(len(index))
    nav[0] = value.sum()
    moved = paid = 0.0
    for j in range(1, len(index)):
        value = value * (navs[j] / navs[j - 1])
        total = value.sum()
        if int(pos[j]) in me:
            m = float(np.abs(tw * total - value).sum())
            cost = rb.SLIPPAGE * m
            total -= cost
            value = tw * total
            moved += m
            paid += cost
        nav[j] = total

    def avg(key: str) -> pd.Series:
        return sum(t * r[key] for t, (_, r) in zip(tw, runs))

    exposure = sum(t * r["exposure"].groupby("date")["exposure"].last().reindex(index).ffill().fillna(0.0)
                   for t, (_, r) in zip(tw, runs))
    return {
        "variant": "blend", "params": runs[0][1]["params"], "nav": pd.Series(nav, index=index, name="blend"),
        "years": runs[0][1]["years"],
        "exposure": pd.DataFrame({"date": index, "exposure": exposure.to_numpy()}),
        "invested": avg("invested"), "names_held": sum(r["names_held"] for _, r in runs),
        "buy_notional": float(sum(t * r["buy_notional"] for t, (_, r) in zip(tw, runs))) + moved / 2,
        "sell_notional": float(sum(t * r["sell_notional"] for t, (_, r) in zip(tw, runs))) + moved / 2,
        "total_costs": float(sum(t * r["total_costs"] for t, (_, r) in zip(tw, runs))) + paid,
        "trades": _concat([r["trades"] for _, r in runs]), "dropped": _concat([r["dropped"] for _, r in runs]),
        "delisted": _concat([r["delisted"] for _, r in runs]),
        "n_stale_evaluations": int(sum(r.get("n_stale_evaluations", 0) for _, r in runs)),
    }


def run(data: "rb.Data", family: str, params: dict, start: str, costs=COSTS_BPS) -> dict:
    """One backtest per cost level, from the first month-end on or after `start` to the data's last session."""
    if family == "blend":
        sleeves = [(params[w], rb.Params(variant=f, **settings.FIXED_PARAMS,
                                         **{k: params[k] for k in space.FAMILIES.get(f, ("", ()))[1]}))
                   for w, f in space.SLEEVES.items() if params[w] > 0]
    else:
        sleeves = [(1, rb.Params(variant=family, **settings.FIXED_PARAMS, **params))]
    start_idx = max(rb.find_start(data, p, pd.Timestamp(start)) for _, p in sleeves)
    end_idx = len(data.dates) - 1
    out, curve, saved = {}, None, rb.SLIPPAGE
    try:
        for bps in costs:
            rb.SLIPPAGE = bps / 10_000
            if family == "blend":
                r = _blend([(w, rb.run_variant(data, p, start_idx, end_idx)) for w, p in sleeves], data)
            else:
                r = rb.run_variant(data, sleeves[0][1], start_idx, end_idx)
            out[str(bps)] = _summary(r, data)
            if bps == 15 or curve is None:
                nav = r["nav"]
                me = nav.groupby(nav.index.to_period("M")).last()
                curve = [[str(d), round(float(v / nav.iloc[0]), 4)] for d, v in me.items()]
    finally:
        rb.SLIPPAGE = saved
    return {"costs": out, "curve": curve,
            "first": str(data.dates[start_idx].date()), "last": str(data.dates[end_idx].date())}


def run_job(job: dict, data: "rb.Data | None" = None) -> dict:
    t0 = time.perf_counter()
    data = data if data is not None else load(Path(job["data"]) if job.get("data") else settings.SEARCH_DATA)
    load_s = time.perf_counter() - t0
    results = []
    for item in job["ideas"]:
        t1 = time.perf_counter()
        try:
            idea = space.validate(item)          # the sandbox refuses whatever the search space refuses
            res = {"ok": True, **run(data, idea.family, idea.params, job["start"])}
        except (Exception, SystemExit) as e:     # find_start raises SystemExit when history is short
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        res.update(trial=item.get("trial"), seconds=round(time.perf_counter() - t1, 2))
        results.append(res)
    return {"results": results, "load_seconds": round(load_s, 2), "host": platform.node(),
            "python": platform.python_version(), "pid": os.getpid(),
            "data_last_session": str(data.dates[-1].date())}


def main(argv: list[str]) -> int:
    job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    Path(argv[2]).write_text(json.dumps(run_job(job)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
