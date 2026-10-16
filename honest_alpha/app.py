"""The live view: python -m uvicorn honest_alpha.app:app --port 8800

Read-only. It reads runs/<program>/ledger.jsonl and live.json and recomputes
every verdict with the gate; nothing here can change a ledger.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from honest_alpha import deflated, gate, settings
from honest_alpha.ledger import Ledger, LedgerError

app = FastAPI(title="Honest Alpha")
STATIC = Path(__file__).with_name("static")
Z95 = 1.6448536269514722


def _program_dir(program: str) -> Path:
    d = (settings.RUNS / program).resolve()
    if d.parent != settings.RUNS.resolve() or not (d / "ledger.jsonl").exists():
        raise HTTPException(404, f"no program {program!r}")
    return d


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/programs")
def programs():
    if not settings.RUNS.exists():
        return []
    dirs = [d for d in settings.RUNS.iterdir() if (d / "ledger.jsonl").exists()]
    return [d.name for d in sorted(dirs, key=lambda d: (d / "ledger.jsonl").stat().st_mtime, reverse=True)]


@app.get("/api/state")
def state(program: str):
    d = _program_dir(program)
    try:
        ledger = Ledger(d / "ledger.jsonl")
        intact = True
    except LedgerError as e:
        return {"program": program, "intact": False, "error": str(e)}
    live = {}
    if (d / "live.json").exists():
        try:
            live = json.loads((d / "live.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            live = {}
    if not ledger.first("baseline"):
        return {"program": program, "intact": intact, "live": live, "evaluation": None}
    ev = gate.evaluate(ledger)
    base = ledger.first("baseline")["result"]["costs"][str(ev["rules"]["cost_bps"])]
    margin = Z95 / math.sqrt(base["n_obs"] - 1)

    # the bar as it stood after each trial: luck rises with every try
    trials = ledger.trials()
    srs, series = [], []
    for k, t in enumerate(trials, 1):
        srs.append(t["result"]["costs"][str(ev["rules"]["cost_bps"])]["sr_daily"])
        bar = deflated.luck_bar(k, srs)
        series.append({"trial": t["trial"], "luck": round(bar * gate.ANNUAL, 4),
                       "needed": round((bar + margin) * gate.ANNUAL, 4)})
    ev["needed_sharpe"] = series[-1]["needed"] if series else round(margin * gate.ANNUAL, 4)
    ev["bar_series"] = series

    keep = ("trial", "reason", "why", "verdict", "at", "hash", "tip", "flagged", "handling", "lesson", "round",
            "recall_trace", "holdout_skipped", "note", "sha256", "request", "stop", "researcher", "instructions",
            "model", "holdout_decision", "comment", "eligible")
    events = [{"kind": e["kind"], **{k: e[k] for k in keep if k in e}}
              for e in ledger.entries() if e["kind"] in ("look", "refusal", "candidate", "note", "rejected")]
    for e, src in zip([x for x in events if x["kind"] == "rejected"], ledger.entries("rejected")):
        e["proposal"] = src["proposal"]
    rep = ledger.entries("report")
    cost = str(ev["rules"]["cost_bps"])
    looks = {e["trial"]: {"curve": e["result"].get("curve"), "spy": e["spy"].get("curve"), "verdict": e["verdict"],
                          "cagr": e["result"]["costs"][cost]["cagr"], "spy_cagr": e["spy"]["costs"][cost]["cagr"],
                          "first": e["result"].get("first"), "last": e["result"].get("last")}
             for e in ledger.entries("look")}
    rounds: dict = {}
    for t in trials:
        rounds.setdefault(t.get("round"), {"round": t.get("round"), "trials": []})["trials"].append(t["trial"])
    for e in ledger.entries("note"):
        if "lesson" in e:
            r = rounds.setdefault(e.get("round"), {"round": e.get("round"), "trials": []})
            r.setdefault("lesson", e["lesson"])
            r["recall_hits"] = e.get("recall_hits", r.get("recall_hits"))
            if e.get("stop"):
                r["stop"] = e["lesson"]
    return {
        "program": program, "intact": intact, "live": live, "evaluation": ev, "events": events[-80:],
        "report": rep[-1] if rep else None,
        "ledger": {"entries": len(ledger.entries()), "head": ledger.head},
        "curves": {t["trial"]: t["result"].get("curve") for t in trials},
        "spy_curve": ledger.first("baseline")["result"].get("curve"),
        "looks": looks,
        "rounds": sorted((r for r in rounds.values() if r["round"] is not None), key=lambda r: r["round"]),
    }
