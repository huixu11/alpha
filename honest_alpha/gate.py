"""The gate. Code, not the model, decides what counts as a candidate.

A trial becomes a candidate only if it
  1. beats SPY's Sharpe over the same window after 15 bps per side, and still at 30 bps;
  2. has a Deflated Sharpe of at least 0.95, deflated by every trial and look in the ledger;
  3. beats SPY again on the locked holdout, keeping at least half its search Sharpe.

The rules are written into the ledger with their sha256 before the first trial,
and every verdict is recomputed from the ledger, so anyone holding the ledger
gets the same verdicts. The holdout data lives on this machine only; a search
sandbox never receives it.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from honest_alpha import deflated, settings
from honest_alpha.ledger import Ledger, canonical

RULES = {
    "program": "honest-alpha v1",
    "trial_budget": 60,
    "search_window": [settings.SEARCH_START, settings.SEARCH_END],
    "holdout_start": settings.HOLDOUT_START,
    "benchmark": "spy_bh",
    "cost_bps": 15,
    "stress_bps": 30,
    "dsr_min": 0.95,
    "holdout_looks": 3,
    "holdout_min_ratio": 0.5,
}

ANNUAL = math.sqrt(252)


def rules_hash(rules: dict = RULES) -> str:
    return hashlib.sha256(canonical(rules).encode()).hexdigest()


class Refused(Exception):
    """A request the rules do not allow. The refusal itself is written to the ledger."""


def registered_rules(ledger: Ledger) -> dict:
    e = ledger.first("rules")
    if e is None:
        raise Refused("no rules are registered in this ledger")
    if rules_hash(e["rules"]) != e["sha256"]:
        raise Refused("the registered rules do not match their own hash")
    if e["sha256"] != rules_hash():
        raise Refused(f"the code's rules ({rules_hash()[:12]}) differ from the ones registered "
                      f"before the first trial ({e['sha256'][:12]}); start a new ledger to change them")
    return e["rules"]


def register(ledger: Ledger, baseline: dict) -> None:
    """Write the rules and SPY's search-window numbers before any trial runs."""
    if ledger.entries():
        registered_rules(ledger)
        return
    ledger.append("rules", {"rules": RULES, "sha256": rules_hash()})
    ledger.append("baseline", {"window": "search", "result": baseline})


def _sharpe(result: dict, bps: int) -> float:
    return result["costs"][str(bps)]["sharpe"]


def evaluate(ledger: Ledger) -> dict:
    """Every verdict, recomputed from the ledger alone."""
    rules = registered_rules(ledger)
    base = ledger.first("baseline")["result"]
    cost, stress = rules["cost_bps"], rules["stress_bps"]
    spy_cost, spy_stress = _sharpe(base, cost), _sharpe(base, stress)
    trials = ledger.trials()
    looks = {e["trial"]: e for e in ledger.entries("look")}
    n = len(trials) + len(looks)
    srs = [t["result"]["costs"][str(cost)]["sr_daily"] for t in trials]
    bar = deflated.luck_bar(n, srs)

    rows = []
    for t in trials:
        r = t["result"]["costs"]
        c, s = r[str(cost)], r[str(stress)]
        beats = c["sharpe"] > spy_cost and s["sharpe"] > spy_stress
        dsr = deflated.psr(c["sr_daily"], bar, c["n_obs"], c["skew"], c["kurt"])
        look = looks.get(t["trial"])
        row = {
            "trial": t["trial"], "idea": t["idea"], "short": t["short"], "rationale": t.get("rationale", ""),
            "round": t.get("round"), "executor": t.get("executor"),
            "sharpe": {str(b): r[str(b)]["sharpe"] for b in (5, cost, stress)},
            "cagr": c["cagr"], "max_drawdown": c["max_drawdown"], "turnover": c["turnover"],
            "beats_spy": beats, "dsr": round(dsr, 4), "not_luck": dsr >= rules["dsr_min"],
            "holdout": look["verdict"] if look else None,
        }
        row["candidate"] = bool(beats and row["not_luck"] and look and look["verdict"]["passed"])
        row["stopped_at"] = ("costs" if not beats else "luck" if not row["not_luck"]
                             else "holdout" if not row["candidate"] else None)
        row["why"] = _why(row, spy_cost, spy_stress, rules)
        rows.append(row)

    funnel = {
        "trials": len(rows),
        "beat_spy_after_costs": sum(r["beats_spy"] for r in rows),
        "not_luck": sum(r["beats_spy"] and r["not_luck"] for r in rows),
        "holdout_passed": sum(r["candidate"] for r in rows),
    }
    best_naive = max(rows, key=lambda r: r["sharpe"]["5"], default=None)
    return {
        "rules": rules, "rules_sha256": rules_hash(rules), "ledger_head": ledger.head,
        "spy": {"sharpe": {str(b): _sharpe(base, b) for b in (5, cost, stress)}, "cagr": base["costs"][str(cost)]["cagr"]},
        "n": n, "n_trials": len(trials), "n_looks": len(looks), "budget": rules["trial_budget"],
        "looks_left": rules["holdout_looks"] - len(looks),
        "luck_bar_sharpe": round(bar * ANNUAL, 4), "funnel": funnel,
        "best_naive": best_naive["trial"] if best_naive else None,
        "trials": rows,
    }


def _why(row: dict, spy_cost: float, spy_stress: float, rules: dict) -> str:
    sh = row["sharpe"]
    cost, stress = str(rules["cost_bps"]), str(rules["stress_bps"])
    if not row["beats_spy"]:
        if sh[cost] <= spy_cost:
            return f"after {cost} bps its Sharpe {sh[cost]:.2f} does not beat SPY's {spy_cost:.2f}"
        return f"beats SPY at {cost} bps but not at {stress} bps ({sh[stress]:.2f} vs {spy_stress:.2f})"
    if not row["not_luck"]:
        return f"Deflated Sharpe {row['dsr']:.2f} is below {rules['dsr_min']}: consistent with the luck of this many tries"
    if row["holdout"] is None:
        return "passed the search gate; the holdout has not been opened for it"
    if not row["holdout"]["passed"]:
        return row["holdout"]["why"]
    return "candidate: passed every gate; registered for forward tracking, not traded"


def holdout_verdict(trial: dict, holdout: dict, spy_holdout: dict, rules: dict) -> dict:
    cost = str(rules["cost_bps"])
    search_sh = trial["result"]["costs"][cost]["sharpe"]
    sh, spy = holdout["costs"][cost]["sharpe"], spy_holdout["costs"][cost]["sharpe"]
    floor = rules["holdout_min_ratio"] * search_sh
    passed = sh > spy and sh >= floor
    if sh <= spy:
        why = f"on the holdout its Sharpe {sh:.2f} does not beat SPY's {spy:.2f}"
    elif sh < floor:
        why = f"on the holdout its Sharpe {sh:.2f} kept less than half of its search Sharpe {search_sh:.2f}"
    else:
        why = f"on the holdout its Sharpe {sh:.2f} beats SPY's {spy:.2f}"
    return {"passed": passed, "sharpe": sh, "spy_sharpe": spy, "search_sharpe": search_sh, "why": why}


def request_look(ledger: Ledger, trial_id: str, reason: str, run_holdout) -> dict:
    """Open the holdout for one trial, if the rules allow it.

    `run_holdout(idea) -> (result, spy_result)` runs on the full data, on this
    machine. Refusals are written to the ledger and raised.
    """
    rules = registered_rules(ledger)
    state = evaluate(ledger)
    row = next((r for r in state["trials"] if r["trial"] == trial_id), None)

    def refuse(why: str):
        ledger.append("refusal", {"request": "holdout_look", "trial": trial_id, "reason": reason, "why": why})
        raise Refused(why)

    if state["looks_left"] <= 0:
        refuse(f"all {rules['holdout_looks']} holdout looks are spent; the holdout stays locked")
    if row is None:
        refuse(f"there is no completed trial {trial_id}")
    if row["holdout"] is not None:
        refuse(f"{trial_id} has already seen the holdout")
    if not row["beats_spy"]:
        refuse(f"{trial_id} did not beat SPY after costs on the search window, so the holdout cannot help it")
    trial = ledger.trial(trial_id)
    result, spy = run_holdout(trial["idea"])
    verdict = holdout_verdict(trial, result, spy, rules)
    entry = ledger.append("look", {"trial": trial_id, "reason": reason, "result": result,
                                   "spy": spy, "verdict": verdict})
    after = evaluate(ledger)
    final = next(r for r in after["trials"] if r["trial"] == trial_id)
    if final["candidate"]:
        blob = canonical({"trial": trial["idea"], "rules": after["rules_sha256"], "look": entry["hash"]})
        ledger.append("candidate", {"trial": trial_id, "idea": trial["idea"],
                                    "sha256": hashlib.sha256(blob.encode()).hexdigest(),
                                    "note": "registered for forward tracking; no order path exists"})
    return {"trial": trial_id, **verdict, "candidate": final["candidate"]}


def holdout_runner(full_data_path: Path = settings.FULL_DATA):
    """The only reader of the full data. Loaded lazily, at the first look."""
    cache = {}

    def run_holdout(idea: dict):
        from honest_alpha import runner
        if "data" not in cache:
            cache["data"] = runner.load(full_data_path)
        data = cache["data"]
        start = RULES["holdout_start"]
        return (runner.run(data, idea["family"], idea["params"], start),
                runner.run(data, "spy_bh", {}, start))

    return run_holdout
