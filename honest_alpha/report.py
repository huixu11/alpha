"""Grounding: every number in the report must come from the evaluation, every citation from the ledger."""
from __future__ import annotations

import re

CITE = re.compile(r"\[(T-\d{4})\]")
TRIAL_ID = re.compile(r"\bT-\d{4}\b")
NUMBER = re.compile(r"(?<![\w.])[-+\u2212]?\d+(?:,\d{3})*(?:\.\d+)?")


def whitelist(evaluation: dict) -> set[str]:
    """Every number of the evaluation as it may be written: as is, to 0-3 decimals, or as a percent."""
    out: set[str] = {str(y) for y in range(2013, 2027)}

    def add(x) -> None:
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, int):
            out.add(str(x))
            out.add(str(abs(x)))
        elif isinstance(x, float):
            for d in range(4):
                out.add(f"{x:.{d}f}")
                out.add(f"{abs(x):.{d}f}")
            for d in range(3):
                out.add(f"{x * 100:.{d}f}")
                out.add(f"{abs(x) * 100:.{d}f}")

    def walk(v) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(k, str) and k.isdigit():
                    out.add(k)
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            for m in NUMBER.finditer(v):
                out.add(m.group(0).lstrip("+"))
        else:
            add(v)

    walk(evaluation)
    return out


def check(text: str, evaluation: dict) -> dict:
    trials = {t["trial"] for t in evaluation["trials"]}
    cites = CITE.findall(text)
    bad = sorted({c for c in cites if c not in trials})
    bare = TRIAL_ID.sub("", CITE.sub("", text))
    allowed = whitelist(evaluation)
    numbers = [m.group(0).lstrip("+").replace("\u2212", "-").replace(",", "") for m in NUMBER.finditer(bare)]
    unsupported = [n for n in numbers if n not in allowed and n.lstrip("-") not in allowed]
    return {
        "numbers": len(numbers), "unsupported": unsupported, "citations": sorted(set(cites)),
        "bad_citations": bad, "grounded": not unsupported and not bad,
        "score": round(1 - len(unsupported) / len(numbers), 3) if numbers else 1.0,
    }


INSTRUCTION_HINTS = re.compile(
    r"(ignore (all|any|the|previous)|system (note|prompt|message)|mark (it|this|the strategy)[^.]{0,30}(pass|approv)"
    r"|\bbuy (it|now|this)|place (an )?order|you (must|should) (now )?(approve|pass|buy))", re.I)


def flag_instructions(text: str) -> list[str]:
    """Phrases in outside text that read as orders to the agent. Flagged and shown; they change nothing."""
    return [m.group(0) for m in INSTRUCTION_HINTS.finditer(text)]
