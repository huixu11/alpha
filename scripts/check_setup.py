"""Check every service the hunt uses, printing what works and never a secret.

    python scripts/check_setup.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honest_alpha import settings  # noqa: E402

settings.load_env()
from honest_alpha import tracing  # noqa: E402

TRACING = tracing.setup()


def check(name: str, fn) -> None:
    t0 = time.perf_counter()
    try:
        print(f"[ ok ] {name}: {fn()} ({time.perf_counter() - t0:.1f}s)", flush=True)
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:300]}", flush=True)


def claude() -> str:
    from honest_alpha import llm
    client, model, where = llm.client()
    with tracing.span("setup check", "CHAIN"):
        msg = client.messages.create(model=model, max_tokens=8, messages=[{"role": "user", "content": "Reply with OK."}])
    return f"{where}, model {model}, replied {''.join(b.text for b in msg.content if b.type == 'text')!r}"


def daytona() -> str:
    from daytona import Daytona
    name = settings.snapshot_name()
    return f"snapshot {name} is {Daytona().snapshot.get(name).state}"


def arize() -> str:
    if not TRACING:
        raise RuntimeError("ARIZE_SPACE_ID or ARIZE_API_KEY is not set")
    with tracing.span("setup check: arize", "CHAIN", **{"input.value": "ping"}):
        pass
    tracing.flush()
    return "tracer registered and a test span flushed; look for project honest-alpha in Arize"


def memory() -> str:
    from honest_alpha.memory import Memory
    m = Memory()
    if not m.enabled:
        raise RuntimeError("SPECTRON_URL, SPECTRON_CONTEXT_ID or SPECTRON_API_KEY is not set")
    m.ensure_scopes()
    out = m.recall("What is the current luck bar?", agent="setup-check")
    if out is None:
        raise RuntimeError(m.errors[-1] if m.errors else "recall failed")
    return f"recall answered, tier {out['tier']}, {len(out['hits'])} hits, trace {out['trace_id']}"


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    check("Claude", claude)
    check("Daytona", daytona)
    check("Arize", arize)
    check("SurrealDB Agent Memory", memory)
