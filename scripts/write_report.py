"""Have the reporter rewrite a program's research note from its ledger, checked against it like every note.

    python scripts/write_report.py sp500-eager
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honest_alpha import settings  # noqa: E402

settings.load_env()
from honest_alpha import tracing  # noqa: E402

tracing.setup()
from honest_alpha import agent, llm  # noqa: E402


def main(program: str) -> int:
    sys.stdout.reconfigure(errors="replace")
    hunt = agent.Hunt(program, executor=SimpleNamespace(name="none"))
    hunt.client, hunt.model, hunt.where = llm.client()
    hunt.memory.ensure_scopes()
    with tracing.span(f"rewrite report {program}", "AGENT", **{"input.value": f"program {program}"}):
        note = hunt.write_report()
    hunt.memory.close()
    tracing.flush()
    print("\n" + note["text"] + "\n\ngrounding: " + json.dumps(note["grounding"]))
    return 0 if note["grounding"]["grounded"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
