"""The append-only record of a research program: every rule, trial, look, refusal and report.

Each line is one JSON entry that carries the hash of the line before it, so a
deleted or edited trial breaks the chain and `verify` says where. The number
of trials the gate deflates by is counted here, never taken from the agent.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path

GENESIS = "0" * 64

KINDS = ("rules", "baseline", "trial", "rejected", "look", "refusal", "candidate", "report", "note")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(entry: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in entry.items() if k != "hash"}).encode()).hexdigest()


class LedgerError(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        if self.path.exists():
            with self.path.open(encoding="utf-8") as h:
                self._entries = [json.loads(line) for line in h if line.strip()]
            self.verify()

    def verify(self) -> None:
        prev = GENESIS
        for i, e in enumerate(self._entries):
            if e.get("seq") != i or e.get("prev") != prev or entry_hash(e) != e.get("hash"):
                raise LedgerError(f"{self.path.parent.name}/{self.path.name} is broken at entry {i}: "
                                  f"it was edited or a line was removed")
            prev = e["hash"]

    def append(self, kind: str, body: dict) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}")
        body = json.loads(canonical(body))      # a frozen copy: exactly what the file holds, sharing no object with the caller
        with self._lock:
            entry = {"seq": len(self._entries), "kind": kind,
                     "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     "prev": self._entries[-1]["hash"] if self._entries else GENESIS, **body}
            entry["hash"] = entry_hash(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as h:
                h.write(canonical(entry) + "\n")
                h.flush()
                os.fsync(h.fileno())
            self._entries.append(entry)
            return entry

    def entries(self, kind: str | None = None) -> list[dict]:
        return [e for e in self._entries if kind is None or e["kind"] == kind]

    def first(self, kind: str) -> dict | None:
        return next((e for e in self._entries if e["kind"] == kind), None)

    def trials(self) -> list[dict]:
        """Backtests that ran to the end; a crashed run tells nothing and is not a trial."""
        return [e for e in self._entries if e["kind"] == "trial" and e["result"].get("ok")]

    def trial(self, trial_id: str) -> dict | None:
        return next((e for e in self.trials() if e["trial"] == trial_id), None)

    def keys(self) -> set[str]:
        return {e["key"] for e in self._entries if e["kind"] == "trial"}

    def next_trial_ids(self, n: int) -> list[str]:
        """Fresh ids; one whose sandbox failed before the trial ran is never handed out again."""
        used = [int(e["trial"][2:]) for e in self._entries if e["kind"] == "trial"]
        used += [int(e["infra_failure"][2:]) for e in self._entries if e.get("infra_failure")]
        last = max(used, default=0)
        return [f"T-{last + i + 1:04d}" for i in range(n)]

    @property
    def head(self) -> str:
        return self._entries[-1]["hash"] if self._entries else GENESIS
