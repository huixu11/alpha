"""Ask the gate, as a person, to show one trial the locked holdout. The look is recorded and counts as a trial.

    python scripts/request_look.py sp500-eager T-0056 "the best Sharpe after costs"
    python scripts/request_look.py sp500-eager T-0056 "..." --report      # then rewrite the research note
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honest_alpha import settings  # noqa: E402

settings.load_env()
from honest_alpha import tracing  # noqa: E402

tracing.setup()
from honest_alpha import agent, gate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("program")
    ap.add_argument("trial")
    ap.add_argument("reason")
    ap.add_argument("--report", action="store_true", help="have the reporter rewrite the note afterwards")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")

    hunt = agent.Hunt(args.program, executor=SimpleNamespace(name="none"))
    with tracing.span(f"holdout look {args.trial} (by a person)", "TOOL",
                      **{"tool.name": "request_holdout", "input.value": json.dumps(vars(args))}) as s:
        try:
            out = gate.request_look(hunt.ledger, args.trial, f"requested by a person: {args.reason}",
                                    gate.holdout_runner())
        except gate.Refused as e:
            print(f"refused: {e}")
            tracing.put(s, **{"output.value": f"refused: {e}"})
            tracing.flush()
            return 1
        tracing.put(s, **{"output.value": json.dumps(out)})
    print(f"{args.trial}: {out['why']}" + (" -> CANDIDATE" if out["candidate"] else ""))
    hunt.memory.ensure_scopes()
    hunt.memory.remember(f"In Honest Alpha program {args.program}, a person opened the holdout for {args.trial}: "
                         f"{out['why']}. Candidate: {'yes' if out['candidate'] else 'no'}.", wait=True)
    if args.report:
        from honest_alpha import llm
        hunt.client, hunt.model, hunt.where = llm.client()
        note = hunt.write_report()
        print("\n" + note["text"] + "\n\ngrounding: " + json.dumps(note["grounding"]))
    hunt.memory.close()
    tracing.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
