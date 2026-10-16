"""Run a research program end to end: propose, backtest in sandboxes, judge, open the holdout, report.

    python scripts/hunt.py --program demo
    python scripts/hunt.py --program dry --proposer random --batch 4 --rounds 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honest_alpha import settings  # noqa: E402

settings.load_env()
from honest_alpha import tracing  # noqa: E402

tracing.setup()                    # before anything imports anthropic
from honest_alpha import agent, executors  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--program", default="main", help="the ledger lives in runs/<program>/")
    ap.add_argument("--batch", type=int, default=10, help="ideas per round")
    ap.add_argument("--rounds", type=int, help="stop the search after this many rounds")
    ap.add_argument("--executor", choices=("daytona", "local"))
    ap.add_argument("--proposer", choices=("claude", "random"), default="claude")
    ap.add_argument("--style", choices=sorted(agent.STYLES), default="careful",
                    help="careful: one hypothesis per trial; eager: chase the highest Sharpe with the whole budget")
    ap.add_argument("--tip-file", action="append", default=[], help="outside text the researcher reads as data")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")

    tips = [Path(p).read_text(encoding="utf-8").strip() for p in args.tip_file]
    hunt = agent.Hunt(args.program, executors.pick(args.executor), batch=args.batch, tips=tips,
                      proposer=args.proposer, max_rounds=args.rounds, style=args.style)
    try:
        out = hunt.run()
    finally:
        tracing.flush()
    ev = out["evaluation"]
    print(json.dumps({"funnel": ev["funnel"], "n": ev["n"], "luck_bar_sharpe": ev["luck_bar_sharpe"],
                      "needed_sharpe": ev["needed_sharpe"], "spy_sharpe": ev["spy"]["sharpe"]}, indent=1))
    if out["report"]:
        print("\n" + out["report"]["text"] + "\n\ngrounding: " + json.dumps(out["report"]["grounding"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
