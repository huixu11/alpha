"""Build the two datasets.

build/search_data.pkl is what every search sandbox gets. It is loaded from bars
physically cut at SEARCH_END, so no later price exists anywhere in it.
build/full_data.pkl runs through the last cached session; only the gate reads
it, on this machine, and it is never uploaded.
"""
from __future__ import annotations

import argparse
import hashlib
import pickle
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honest_alpha import settings  # noqa: E402

settings.use_engine()
from quantdesk.cache import BarCache  # noqa: E402
from tools import regime_backtest as rb  # noqa: E402


def cut_cache(src: Path, dst: Path, end: pd.Timestamp) -> tuple[int, pd.Timestamp]:
    """Copy every cached history cut at `end`; returns (files written, latest bar written)."""
    source, target = BarCache(src), BarCache(dst)
    latest, n = pd.Timestamp.min, 0
    for path in sorted(src.glob("*.parquet")):
        frame = source.read_raw(path.stem)
        frame = frame[frame.index <= end]
        if len(frame) == 0:
            continue
        target.write(path.stem, frame)
        latest = max(latest, frame.index[-1])
        n += 1
    return n, latest


def build(cache_root: Path, end: pd.Timestamp | None, out: Path) -> None:
    t0 = time.perf_counter()
    base = rb.Params(**settings.FIXED_PARAMS)
    data = rb.load_data(cache_root, settings.UNIVERSE_CSV, end, base, membership_csv=settings.MEMBERSHIP_CSV)
    blob = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
    out.write_bytes(blob)
    print(f"{out.name}: {len(data.tickers)} stocks, {data.dates[0].date()} .. {data.dates[-1].date()}, "
          f"{len(blob) / 1e6:.0f} MB, sha256 {hashlib.sha256(blob).hexdigest()}, {time.perf_counter() - t0:.0f}s",
          flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip-full", action="store_true", help="build only the search data")
    args = ap.parse_args()
    settings.BUILD.mkdir(parents=True, exist_ok=True)
    end = pd.Timestamp(settings.SEARCH_END)

    t0 = time.perf_counter()
    n, latest = cut_cache(settings.SOURCE_CACHE, settings.SEARCH_CACHE, end)
    if latest > end:
        raise SystemExit(f"the cut cache holds a bar dated {latest.date()} after {end.date()}")
    print(f"search cache: {n} histories cut at {end.date()} (latest bar {latest.date()}) in "
          f"{time.perf_counter() - t0:.0f}s", flush=True)
    build(settings.SEARCH_CACHE, end, settings.SEARCH_DATA)
    if not args.skip_full:
        build(settings.SOURCE_CACHE, None, settings.FULL_DATA)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
