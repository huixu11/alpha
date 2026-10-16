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
import time
from concurrent.futures import ThreadPoolExecutor

from honest_alpha import tracing

ROOT_SCOPE = "honest-alpha"


class Memory:
    """One program's memory: its facts live under honest-alpha/<program>/trials and no other program sees them."""

    def __init__(self, program: str | None = None):
        self.url = os.environ.get("SPECTRON_URL", "").rstrip("/")
        self.ctx = os.environ.get("SPECTRON_CONTEXT_ID", "")
        key = os.environ.get("SPECTRON_API_KEY", "")
        self.enabled = bool(self.url and self.ctx and key)
        self.root = f"{ROOT_SCOPE}/{program}" if program else ROOT_SCOPE
        self.scope = f"{self.root}/trials"
        self.errors: list[str] = []
        self._lock = threading.Lock()
        self._pending: list = []
        if self.enabled:
            import httpx
            self.http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {key}"})
            self.pool = ThreadPoolExecutor(max_workers=8)

    def _post(self, path: str, body: dict) -> dict:
        import httpx
        for attempt in range(3):
            try:
                r = self.http.post(f"{self.url}/api/v1/{self.ctx}/{path}", json=body)
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                if attempt == 2 or (status is not None and status < 500 and status != 429):
                    raise
                time.sleep(1.5 * (attempt + 1) ** 2)

    def _fail(self, what: str, e: Exception) -> None:
        import httpx
        detail = (f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError)
                  else f"{type(e).__name__}")           # never the URL: it names the context
        with self._lock:
            self.errors.append(f"{what}: {detail}")

    def ensure_scopes(self) -> None:
        if not self.enabled:
            return
        for path in dict.fromkeys((ROOT_SCOPE, self.root, self.scope)):
            try:
                self._post("scopes", {"path": path})
            except Exception:
                pass                       # already registered, or registered in SurrealDB Studio

    def remember(self, text: str, wait: bool = False):
        """Write one fact. In the background unless `wait`; a failure is kept, never raised."""
        if not self.enabled:
            return None

        def write():
            try:
                return self._post("facts", {"text": text, "infer": "full", "scopes": [[self.scope]]})
            except Exception as e:
                self._fail("remember", e)
                return None

        if wait:
            return write()
        f = self.pool.submit(write)
        with self._lock:
            self._pending.append(f)
        return f

    def drain(self, timeout: float = 90) -> None:
        """Wait for the background writes, so a recall sees every fact written so far."""
        with self._lock:
            pending, self._pending = self._pending, []
        for f in pending:
            try:
                f.result(timeout=timeout)
            except Exception:
                pass

    def recall(self, query: str, agent: str, k: int = 8) -> dict | None:
        """Ranked hits for `query`, with the retrieval trace id; None when memory is off or failing."""
        if not self.enabled:
            return None
        self.drain()
        with tracing.span(f"recall ({agent})", "RETRIEVER", **{"input.value": query,
                          "memory.scope": self.scope}) as s:
            try:
                out = self._post("query", {"query": query, "lens": [[self.scope]], "k": k, "source": agent})
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
