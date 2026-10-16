"""Paths and the fixed parts of the experiment."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
BUILD = ROOT / "build"
RUNS = ROOT / "runs"

QUANTDESK = Path(os.environ.get("QUANTDESK_DIR", str(ROOT.parent / "quantdesk")))
SOURCE_CACHE = QUANTDESK / "data" / "sp500_cache"
UNIVERSE_CSV = QUANTDESK / "data" / "sp500" / "universe_sp500.csv"
MEMBERSHIP_CSV = QUANTDESK / "data" / "sp500" / "membership.csv"

SEARCH_START = "2015-06-30"
SEARCH_END = "2022-12-30"      # the last session that exists anywhere in the search data
HOLDOUT_START = "2022-12-30"   # the holdout simulation starts from this close

SEARCH_CACHE = BUILD / "search_cache"
SEARCH_DATA = BUILD / "search_data.pkl"
FULL_DATA = BUILD / "full_data.pkl"

# engine settings load_data reads, so no idea may change them
FIXED_PARAMS = {"bucket_cap": 1.0, "delist_after": 10}

SANDBOX_FILES = ("__init__.py", "settings.py", "space.py", "runner.py")


def snapshot_name() -> str:
    """Named after the code and data it holds, so a sandbox never runs code older than this checkout."""
    if os.environ.get("HONEST_ALPHA_SNAPSHOT"):
        return os.environ["HONEST_ALPHA_SNAPSHOT"]
    h = hashlib.sha256()
    for p in [ROOT / "honest_alpha" / f for f in SANDBOX_FILES] + sorted(ENGINE.rglob("*.py")):
        h.update(p.relative_to(ROOT).as_posix().encode())
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    h.update(str(SEARCH_DATA.stat().st_size if SEARCH_DATA.exists() else -1).encode())
    return f"honest-alpha-search-{h.hexdigest()[:10]}"


def load_env() -> None:
    """Read ROOT/.env into the environment of the machine that runs the agent; sandboxes never get it."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


def use_engine() -> None:
    """Make the vendored quantdesk engine importable as `quantdesk` and `tools`."""
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
