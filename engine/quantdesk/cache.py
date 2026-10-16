"""Parquet bar cache, incremental, with the 30-bar revision check.

strategy_spec.md section 3: fetches are incremental; on every fetch the
provider's last 30 bars per ticker are compared against the cache, any
difference greater than 0.0001 is written to `data/revisions.log` before the
cache is updated, and the difference triggers the corporate-action path of
section 6 for that ticker.

**What is stored.** The cache holds the *raw* `open`, `high`, `low`, `close`,
`volume`, `split_factor` and `div_cash` of every session, never the adjusted
values, so the split-only construction of section 3 can be redone from the
cache alone after an incremental fetch. `read` returns the constructed
split-adjusted bars; `read_raw` returns what is stored. Freshness and the
cross-check compare raw closes (section 3), so both readers exist.

**What counts as a revision.** The comparison is between the *constructed*
series before the fetch and after it. Section 3 requires that "a newly reported
`splitFactor` and a revision caused by a provider restatement are treated
identically; both are corporate actions for section 6.1". A new split leaves
every raw price untouched and changes only one session's `splitFactor`, so a
comparison on raw prices would miss it entirely, while a comparison on the
constructed series catches both, and section 6.1's ratio
`close[T-1]_new / close[T-1]_old` is defined on the split-adjusted close. The
`split_factor` field is compared as well, so the log records which fields
changed.

Published call files and recorded fills are never changed by anything here.
The cache is a convenience; the record is the call files and `record/trades.csv`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from quantdesk import config
from quantdesk.calendar import stamp
from quantdesk.providers.base import (
    BARS_COLUMNS,
    empty_bars,
    empty_raw,
    normalize_raw,
    split_adjust,
)

# Fields compared by the revision check: the constructed bar fields plus the
# raw split factor, so the log names which one moved (section 3).
REVISION_FIELDS = (*BARS_COLUMNS, "split_factor")


@dataclasses.dataclass(frozen=True)
class Revision:
    """One revised bar found by comparing the provider against the cache."""

    ticker: str
    date: dt.date
    field: str
    old: float
    new: float

    def as_log_line(self, detected_at: str) -> str:
        return json.dumps(
            {
                "detected_at": detected_at,
                "ticker": self.ticker,
                "date": self.date.isoformat(),
                "field": self.field,
                "old": self.old,
                "new": self.new,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )


class BarCache:
    """One parquet file per ticker under `root`."""

    def __init__(self, root: Path | None = None, revisions_log: Path | None = None):
        self.root = Path(root) if root is not None else config.CACHE_DIR
        self.revisions_log = (
            Path(revisions_log) if revisions_log is not None else config.REVISIONS_LOG
        )
        # One evening reads each ticker several times: raw for freshness and
        # the cross-check, constructed for the rule, and both again for the
        # revision comparison. The memo below keys on the file's own stat, so a
        # write by anything invalidates it and a stale frame can never be
        # served. It is a speed cache and holds nothing the file does not.
        self._raw: dict[str, tuple[tuple[int, int], pd.DataFrame]] = {}
        self._adjusted: dict[str, tuple[tuple[int, int], pd.DataFrame]] = {}

    # -- paths --------------------------------------------------------------

    def path_for(self, ticker: str) -> Path:
        # `BRK-B` is a legal Windows filename; no mapping is needed.
        return self.root / f"{ticker}.parquet"

    def _stamp(self, path: Path) -> tuple[int, int] | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    # -- read and write -----------------------------------------------------

    def read_raw(self, ticker: str) -> pd.DataFrame:
        """What is stored: raw OHLCV plus `split_factor` and `div_cash`."""
        path = self.path_for(ticker)
        stamp = self._stamp(path)
        if stamp is None:
            return empty_raw()
        hit = self._raw.get(ticker)
        if hit is not None and hit[0] == stamp:
            return hit[1].copy()
        frame = normalize_raw(pd.read_parquet(path))
        self._raw[ticker] = (stamp, frame)
        return frame.copy()

    def read(self, ticker: str) -> pd.DataFrame:
        """The split-adjusted-only bars, constructed from the raw cache."""
        path = self.path_for(ticker)
        stamp = self._stamp(path)
        if stamp is None:
            return empty_bars()
        hit = self._adjusted.get(ticker)
        if hit is not None and hit[0] == stamp:
            return hit[1].copy()
        raw = self.read_raw(ticker)
        frame = empty_bars() if raw.empty else split_adjust(raw)
        self._adjusted[ticker] = (stamp, frame)
        return frame.copy()

    def write(self, ticker: str, frame: pd.DataFrame) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(ticker)
        normalize_raw(frame).to_parquet(path, index=True)
        self._raw.pop(ticker, None)
        self._adjusted.pop(ticker, None)
        return path

    def tickers(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.parquet"))

    # -- the incremental update --------------------------------------------

    @staticmethod
    def _comparable(raw: pd.DataFrame) -> pd.DataFrame:
        """The constructed bars plus the raw split factor, for comparison."""
        if raw.empty:
            return raw
        out = split_adjust(raw)
        out["split_factor"] = raw["split_factor"].reindex(out.index).astype("float64")
        return out

    def detect_revisions(
        self,
        ticker: str,
        incoming: pd.DataFrame,
        window: int = config.REVISION_WINDOW,
        tolerance: float = config.REVISION_TOLERANCE,
    ) -> list[Revision]:
        """Compare the last `window` constructed bars, before against after.

        `incoming` is a raw frame. The comparison is between the series the
        cache constructs today and the series it will construct once
        `incoming` is merged in, so a newly reported `splitFactor` — which
        moves no raw price and would therefore be invisible on a raw
        comparison — is caught exactly like a provider restatement, which is
        what section 3 requires.

        Only bars that exist on both sides are compared; a bar the cache does
        not have yet is new data, not a revision.
        """
        cached_raw = self.read_raw(ticker)
        incoming = normalize_raw(incoming)
        if cached_raw.empty or incoming.empty:
            return []
        before = self._comparable(cached_raw)
        after = self._comparable(self._merge_raw(cached_raw, incoming))
        # Section 3 fixes the window at the provider's last `window` bars.
        tail = after.loc[after.index.isin(incoming.tail(window).index)]
        shared = tail.index.intersection(before.index)
        revisions: list[Revision] = []
        for date in sorted(shared):
            for field in REVISION_FIELDS:
                old = float(before.at[date, field])
                new = float(after.at[date, field])
                if abs(new - old) > tolerance:
                    revisions.append(
                        Revision(ticker, date.date(), field, old, new)
                    )
        return revisions

    @staticmethod
    def _merge_raw(cached: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
        if cached.empty:
            return incoming
        merged = pd.concat([cached, incoming])
        return merged[~merged.index.duplicated(keep="last")].sort_index()

    def log_revisions(self, revisions: list[Revision], detected_at: str | None = None) -> None:
        """Write revisions before the cache is updated (section 3)."""
        if not revisions:
            return
        detected_at = detected_at or stamp()
        self.revisions_log.parent.mkdir(parents=True, exist_ok=True)
        with self.revisions_log.open("a", encoding="ascii") as handle:
            for revision in revisions:
                handle.write(revision.as_log_line(detected_at) + "\n")

    def update(
        self,
        ticker: str,
        incoming: pd.DataFrame,
        full_refetch: bool = False,
    ) -> list[Revision]:
        """Merge `incoming` into the cache, logging revisions first.

        Returns the revisions found, which the caller feeds to the
        corporate-action path of section 6.1.
        """
        incoming = normalize_raw(incoming)
        revisions = self.detect_revisions(ticker, incoming)
        self.log_revisions(revisions)
        if full_refetch:
            merged = incoming
        else:
            merged = self._merge_raw(self.read_raw(ticker), incoming)
        self.write(ticker, merged)
        return revisions

    def last_session(self, ticker: str) -> dt.date | None:
        frame = self.read_raw(ticker)
        if frame.empty:
            return None
        return frame.index[-1].date()

    def incremental_start(self, ticker: str, floor: dt.date) -> dt.date:
        """Where an incremental fetch should start.

        `window` bars back from the cache's last session so the revision check
        has something to compare, or `floor` when the cache is empty.
        """
        frame = self.read_raw(ticker)
        if frame.empty:
            return floor
        if len(frame) <= config.REVISION_WINDOW:
            return floor
        return max(floor, frame.index[-config.REVISION_WINDOW].date())
