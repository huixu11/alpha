"""Copy a program's ledger, make its worst trial look like a winner, and show that the chain catches it.

    python scripts/tamper_demo.py sp500-live      # then open the live view on sp500-live-tampered
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honest_alpha import settings  # noqa: E402
from honest_alpha.ledger import Ledger, LedgerError  # noqa: E402


def main(program: str) -> int:
    src, dst = settings.RUNS / program, settings.RUNS / f"{program}-tampered"
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    lines = (src / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    trials = [(i, json.loads(line)) for i, line in enumerate(lines) if json.loads(line)["kind"] == "trial"]
    i, e = min(trials, key=lambda p: p[1]["result"]["costs"]["15"]["sharpe"])
    old = e["result"]["costs"]["15"]["sharpe"]
    for bps in e["result"]["costs"]:
        e["result"]["costs"][bps]["sharpe"] = 2.5
    lines[i] = json.dumps(e)
    (dst / "ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"edited {e['trial']} ({e['short']}): Sharpe after 15 bps {old:.2f} -> 2.50, in {dst.name}/ledger.jsonl")
    try:
        Ledger(dst / "ledger.jsonl")
    except LedgerError as err:
        print(f"caught: {err}")
        return 0
    print("NOT caught")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "sp500-live"))
