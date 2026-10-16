"""SurrealDB Agent Memory: what the research program has learned, shared by every agent that reads it.

The ledger is the record and memory is how the agents recall it. Each fact
names the trial and the ledger hash it came from, so a recalled memory can be
traced to the ledger line that produced it, and each recall's trace id is
written back to the ledger. Facts about the program's state ("the luck bar
is now ...") are restated each round, so the newer fact supersedes the older
one instead of overwriting it.

With SPECTRON_URL, SPECTRON_CONTEXT_ID and SPECTRON_API_KEY unset, or the
service unreachable, memory is off and the agents read the ledger alone.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from honest_alpha import tracing

ROOT_SCOPE = "honest-alpha"
TRIALS_SCOPE = "honest-alpha/trials"


class Memory:
    def __init__(self):
        self.url = os.environ.get("SPECTRON_URL", "").rstrip("/")
        self.ctx = os.environ.get("SPECTRON_CONTEXT_ID", "")
        key = os.environ.get("SPECTRON_API_KEY", "")
        self.enabled = bool(self.url and self.ctx and key)
        self.errors: list[str] = []
        self._lock = threading.Lock()
        if self.enabled:
            import httpx
            self.http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {key}"})
            self.pool = ThreadPoolExecutor(max_workers=4)

    def _post(self, path: str, body: dict) -> dict:
        r = self.http.post(f"{self.url}/api/v1/{self.ctx}/{path}", json=body)
        r.raise_for_status()
        return r.json()

    def _fail(self, what: str, e: Exception) -> None:
        with self._lock:
            self.errors.append(f"{what}: {type(e).__name__}: {str(e)[:200]}")

    def ensure_scopes(self) -> None:
        if not self.enabled:
            return
        for path in (ROOT_SCOPE, TRIALS_SCOPE):
            try:
                self._post("scopes", {"path": path})
            except Exception:
                pass                       # already registered, or registered in SurrealDB Studio

    def remember(self, text: str, scope: str = TRIALS_SCOPE, wait: bool = False):
        """Write one fact. In the background unless `wait`; a failure is kept, never raised."""
        if not self.enabled:
            return None

        def write():
            try:
                return self._post("facts", {"text": text, "infer": "full", "scopes": [[scope]]})
            except Exception as e:
                self._fail("remember", e)
                return None

        return write() if wait else self.pool.submit(write)

    def recall(self, query: str, agent: str, k: int = 8, lens: str = TRIALS_SCOPE) -> dict | None:
        """Ranked hits for `query`, with the retrieval trace id; None when memory is off or failing."""
        if not self.enabled:
            return None
        with tracing.span(f"recall ({agent})", "RETRIEVER", **{"input.value": query}) as s:
            try:
                out = self._post("query", {"query": query, "lens": [[lens]], "k": k, "source": agent})
            except Exception as e:
                self._fail("recall", e)
                return None
            hits = [_hit_text(h) for h in out.get("hits", [])]
            hits = [h for h in hits if h][:k]
            trace_id = (out.get("trace") or {}).get("traceId")
            tracing.put(s, **{"output.value": "\n".join(hits), "memory.trace_id": trace_id,
                              "memory.tier": out.get("tier")})
            return {"hits": hits, "trace_id": trace_id, "tier": out.get("tier"), "query": query}

    def close(self) -> None:
        if self.enabled:
            self.pool.shutdown(wait=True)


def _hit_text(h: dict) -> str:
    for key in ("text", "content", "snippet", "value", "summary", "name"):
        v = h.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""
