"""Constants.

Every value here is quoted from strategy_spec.md v1.2.0 section 15 (the
parameter table) or from execution_spec.md v1.2.0. Nothing in this module is a
judgement call; if a number here disagrees with the spec, this module is a bug.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

SPEC_VERSION = "1.2.0"

# --- paths -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

DOCS_DIR = REPO_ROOT / "docs"
DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
UNIVERSE_CSV = DATA_DIR / "universe.csv"
REVISIONS_LOG = DATA_DIR / "revisions.log"
CALLS_DIR = REPO_ROOT / "calls"
FILLS_DIR = REPO_ROOT / "fills"
RECORD_DIR = REPO_ROOT / "record"
LETTERS_DIR = REPO_ROOT / "letters"
PROPOSALS_DIR = REPO_ROOT / "proposals"
PROMPTS_DIR = REPO_ROOT / "prompts"
SITE_DIR = REPO_ROOT / "site"

MODE_FILE = REPO_ROOT / "MODE"
LIVE_CONFIRM_FILE = REPO_ROOT / "LIVE_CONFIRM"
STOP_FILE = REPO_ROOT / "STOP"

# The primary provider's persisted request budget (section 3, Rate discipline).
# Gitignored: it is machine state, not a record. See `providers/tiingo.py`.
TIINGO_BUDGET_JSON = DATA_DIR / "tiingo_budget.json"

# --- `.env`, loaded once per process by every entry point -------------------
#
# `README.md` says secrets "come from the environment or from `.env`". Until
# 2026-09-06 nothing read the file, so a correctly filled `.env` produced
# `TIINGO_API_KEY is not set`. `load_env` is the one loader; every `main()`
# calls it and library code never does, so a test that sets the environment
# directly is unaffected. See docs/spec_issues.md, "Broker: tastytrade", item 8.

ENV_FILE_NAME = ".env"

# Names `.env` may never set, whatever it contains. `execution_spec.md` §1
# requires three *independent* gates for `LIVE`: the `MODE` file, the
# `LIVE_CONFIRM` file, and the environment variable `QUANTDESK_LIVE` set for
# the process. A `.env` in the repository root that could set the variable
# would collapse two of the three into files sitting side by side, which is
# what `docs/RUNBOOK.md` §5 already tells the operator never to do. The loader
# enforces it rather than trusting the instruction.
ENV_FILE_REFUSED_NAMES = ("QUANTDESK_LIVE",)


def env_file(root: str | Path | None = None) -> Path:
    """Where `.env` lives: the repository root, or `root` when one is given."""
    base = REPO_ROOT if root is None else Path(root)
    return base / ENV_FILE_NAME


def load_env(
    root: str | Path | None = None,
    *,
    override: bool = False,
    environ: dict | None = None,
) -> Path | None:
    """Load `.env` from the repository root. A set variable always wins.

    Called at the top of every entry point's `main()`. `override=False` is the
    rule: a variable already in the environment beats the file, so an operator
    who exports one for a single run, and a test that sets one directly, both
    still win. Returns the file that was read, or `None` when there is none.

    A missing, unreadable or malformed `.env` is not an error: the process
    continues with whatever the environment already holds, and the failure
    surfaces later as the specific `... is not set` message for the one secret
    that was actually needed.
    """
    from dotenv import dotenv_values

    target = env_file(root)
    try:
        if not target.is_file():
            return None
        values = dotenv_values(target, encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {target}: {exc}", file=sys.stderr)
        return None
    except Exception as exc:                                  # noqa: BLE001
        print(f"cannot parse {target}: {type(exc).__name__}", file=sys.stderr)
        return None
    destination = os.environ if environ is None else environ
    refused: list[str] = []
    for name, value in values.items():
        if value is None:
            continue
        if name in ENV_FILE_REFUSED_NAMES:
            refused.append(name)
            continue
        if override or name not in destination:
            destination[name] = value
    if refused:
        print(
            f"{target}: ignoring {', '.join(sorted(refused))}; "
            "execution_spec.md section 1 requires it to be set for the process, "
            "not read from a file beside MODE",
            file=sys.stderr,
        )
    return target

# --- time ------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
CALENDAR_NAME = "XNYS"

# --- strategy_spec.md section 15 -------------------------------------------

RSI_PERIOD = 14                       # section 4
MIN_HISTORY_BARS = 250                # section 4
COOLDOWN_SESSIONS = 5                 # section 4
OVERSOLD_THRESHOLD = 30.0             # section 5
TREND_WINDOW = 200                    # section 5
MIN_PRICE = 5.00                      # section 5
DOLLAR_VOLUME_WINDOW = 20             # section 5
MIN_MEDIAN_DOLLAR_VOLUME = 25_000_000.0   # section 5
TARGET_THRESHOLD = 50.0               # section 6
STOP_FRACTION = "0.93"                # section 6, exact Decimal string
TIME_STOP_SESSIONS = 10               # section 6
MISSING_DATA_EXIT_SESSIONS = 3        # section 6
DELISTING_WRITEOFF_SESSIONS = 10      # section 6
CORPORATE_ACTION_THRESHOLD = 0.005    # section 6, on close[T-1]
CORPORATE_ACTION_RATIO_TOLERANCE = 0.001  # section 6
DRAWDOWN_HALT_FRACTION = 0.85         # section 6
MAX_POSITIONS = 10                    # section 7
MAX_PER_BUCKET = 3                    # section 7
ALLOCATION_FRACTION = 0.10            # section 8
MIN_ALLOCATION_FRACTION = 0.05        # section 8
SALE_PROCEEDS_HAIRCUT = 0.95          # section 8
STARTING_CASH = 100_000.00            # section 8
SLIPPAGE_BPS = 5.0                    # section 9, per side
COMMISSION_PER_SHARE = 0.0049         # section 9
MIN_COMMISSION = 0.99                 # section 9
PLATFORM_FEE_PER_SHARE = 0.005        # section 9
MIN_PLATFORM_FEE = 1.00               # section 9
SETTLEMENT_FEE_PER_SHARE = 0.003      # section 9, buys and sells
REG_FEE_RATE = 0.00003                # section 9, of proceeds, sells only
TAF_PER_SHARE = 0.000166              # section 9, sells only
FRESHNESS_THRESHOLD = 0.95            # section 3
CHANGED_PRICE_THRESHOLD = 0.60        # section 3
CROSSCHECK_SAMPLE = 5                 # section 3, plus SPY; min(5, len(universe))
CROSSCHECK_MAX_ATTEMPTS = 2           # section 3, at most 12 requests per session
CROSSCHECK_TOL_FRACTION = 0.005       # section 3
CROSSCHECK_TOL_ABSOLUTE = 0.02        # section 3
CROSSCHECK_DISPUTE_COUNT = 2          # section 3
REVISION_WINDOW = 30                  # section 3
REVISION_TOLERANCE = 0.0001           # section 3
BACKTEST_COST_BPS = (5.0, 15.0, 30.0)  # section 16
# Section 16.2: a backtest has no operator to record a restart, so a halt is
# followed by an automatic restart this many sessions after the halt session,
# with `hwm` re-based per section 6.1. The record's halt is cleared by a human
# and by nothing else; this constant is read only by the backtest and the nulls.
BACKTEST_HALT_RESTART_SESSIONS = 20   # section 16.2
# Sections 16.3 and 16.4: the nulls are evaluated at the same cost scenario the
# decision rule of section 16.2 reads, 30 bps per side plus commission, not at
# the 5 bps default of section 9. A null run at a cheaper cost than the
# observed statistic it is compared against is not a null of that statistic.
NULL_COST_BPS = 30.0                  # section 16.3, 16.4
NULL_DRAWS = 1000                     # section 16
NULL_SEED = 20260904                  # section 16
NULL_OFFSET_MARGIN = 20               # section 16, range [20, N-20]
NULL_MIN_BARS = 60                    # section 16
PLACEBO_VOL_BAND = 0.20               # section 16
PLACEBO_MIN_SESSION_GAP = 20          # section 16.4
PLACEBO_VOL_WINDOW = 14               # section 16.4, bar indices b-13..b

BENCHMARK_TICKER = "SPY"
BACKTEST_START = "2015-01-02"         # section 16.2

# --- execution_spec.md ------------------------------------------------------

MAX_ORDERS_PER_DAY = 25               # section 5 check 7, BUY only
QUOTE_STALENESS_SECONDS = 120         # section 4 step 3
RECONCILE_CASH_TOLERANCE_ABS = 1.00   # section 6
RECONCILE_CASH_TOLERANCE_FRAC = 0.0001  # section 6
SUPERVISOR_HEARTBEAT_MINUTES = 90     # section 8

# --- which broker adapter, execution_spec.md section 3 ----------------------
#
# Section 3 names tastytrade as the chosen broker and keeps moomoo as an
# alternative adapter. `BROKER` selects between them for the runner, the
# reconciliation job and the smoke test; nothing else in the system reads it.
# An unrecognized value is the default, so a typo never silently picks the
# other broker.

BROKERS = ("tastytrade", "moomoo")
BROKER_DEFAULT = "tastytrade"

# The environment variable each adapter's configured account id is read from.
# Safety check 5 compares `get_account_id()` against it, and section 1's
# `LIVE_CONFIRM` sentence names it. tastytrade's sandbox is a different account
# from production, so `PAPER` prefers the sandbox number when one is set.
ACCOUNT_ID_VARS = {
    "tastytrade": ("TASTYTRADE_ACCOUNT_NUMBER",),
    "moomoo": ("MOOMOO_ACCOUNT_ID",),
}
SANDBOX_ACCOUNT_ID_VARS = {
    "tastytrade": ("TASTYTRADE_SANDBOX_ACCOUNT_NUMBER", "TASTYTRADE_ACCOUNT_NUMBER"),
    "moomoo": ("MOOMOO_ACCOUNT_ID",),
}


def broker_name(env: dict | None = None) -> str:
    """The selected adapter (`BROKER`), defaulting to `BROKER_DEFAULT`."""
    env = os.environ if env is None else env
    value = str(env.get("BROKER") or "").strip().lower()
    return value if value in BROKERS else BROKER_DEFAULT


# --- the read-only broker session, execution_spec.md section 2 stage 3 ------
#
# The supervisor and the dashboard run on a read-only adapter that has no
# order methods and refuses non-GET requests. A separately scoped OAuth
# application is used when the broker allows one, and is optional otherwise:
# tastytrade allows one personal application per account, so on this broker
# the names below are normally empty and the read session reads the same grant
# the runner does — as a class that cannot place an order whatever credential
# it holds. They are read by `quantdesk/broker/tastytrade.py` when it is asked
# for a read session, and by the refusal below.

READONLY_CREDENTIAL_VARS = (
    "TASTYTRADE_READONLY_CLIENT_SECRET",
    "TASTYTRADE_READONLY_REFRESH_TOKEN",
)

# The halves of the trading grant's names, rather than the names themselves.
# `tests/test_supervisor_no_trade_path.py` walks the supervisor's import
# graph — this module is in it — and fails if any module there so much as
# names a trading credential in code. That rule is worth keeping: the check
# below asks only whether some tastytrade grant is non-empty, it never reads a
# value, and the session itself is still built outside this graph, by
# `quantdesk/broker/select.py`.
_GRANT_PREFIX = "TASTYTRADE_"
_GRANT_SUFFIXES = ("CLIENT_SECRET", "REFRESH_TOKEN")
_GRANT_SANDBOX_INFIX = "SANDBOX_"


def missing_readonly_credentials(env: dict | None = None) -> list[str]:
    """Which of `READONLY_CREDENTIAL_VARS` are absent or empty."""
    env = os.environ if env is None else env
    return [
        name for name in READONLY_CREDENTIAL_VARS
        if not str(env.get(name) or "").strip()
    ]


def live_grant_present(env: dict | None = None) -> bool:
    """Whether a production tastytrade OAuth grant exists to read with.

    Both halves of one — a client secret and a refresh token — among the
    names that are not the sandbox's. The read-only application's pair
    qualifies, and so does the runner's own, which is what the read session
    falls back to when no separately scoped application exists.
    """
    env = os.environ if env is None else env
    found = set()
    for name, value in env.items():
        if not name.startswith(_GRANT_PREFIX) or _GRANT_SANDBOX_INFIX in name:
            continue
        if not str(value or "").strip():
            continue
        for suffix in _GRANT_SUFFIXES:
            if name.endswith(suffix):
                found.add(suffix)
    return found == set(_GRANT_SUFFIXES)


def readonly_credentials_refusal(mode: str, env: dict | None = None) -> str | None:
    """The message the supervisor and the dashboard refuse to start with.

    Only `LIVE` refuses. `DRY_RUN` constructs no broker session at all and
    `PAPER` is the sandbox, where nothing that matters can be lost.

    In `LIVE` the refusal fires when there is no grant to build a read session
    from at all — neither the separately scoped read-only application nor the
    production trading grant it falls back to. A half-configured read-only
    application is also refused, because it is a mistake rather than a
    decision: leaving all four `TASTYTRADE_READONLY_` names empty is how an
    operator says they do not have a second application.
    """
    if str(mode).strip().upper() != "LIVE":
        return None
    env = os.environ if env is None else env
    missing = missing_readonly_credentials(env)
    if not missing:
        return None
    if len(missing) < len(READONLY_CREDENTIAL_VARS):
        return (
            f"{' and '.join(missing)} is not set, while the other half of the "
            "read-only OAuth application is. Set both, or leave every "
            "TASTYTRADE_READONLY_ name empty and the read session will use the "
            "runner's grant on an adapter that has no order methods "
            "(execution_spec.md section 2; docs/TASTYTRADE_SETUP.md)."
        )
    if live_grant_present(env):
        return None
    return (
        "no tastytrade OAuth grant is configured, so no broker session can be "
        "read from. execution_spec.md section 2 makes a read-only broker "
        "session a prerequisite for stage 3: the supervisor and the dashboard "
        "run on an adapter that has no order methods and refuses non-GET "
        "requests, built from the read-only application's grant when the "
        "broker allows a second application and from the runner's when it does "
        "not (docs/TASTYTRADE_SETUP.md)."
    )


# --- daily job timing, strategy_spec.md section 11 --------------------------

SIGNAL_EARLIEST = "18:30"
SIGNAL_RETRIES = ("19:30", "20:30")
SIGNAL_DEADLINE = "21:00"

# --- providers --------------------------------------------------------------

PRIMARY_PROVIDER = "tiingo"           # section 3, primary
CROSSCHECK_PROVIDER = "alphavantage"  # section 3, cross-check

# Three or more provider errors in one fetch means the session status is ERROR
# (BUILD_PROMPT.md, Phase A). The rule is the same for both providers.
PROVIDER_ERROR_LIMIT = 3

TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
TIINGO_REQUESTS_PER_HOUR = 50         # section 3, Rate discipline
TIINGO_REQUESTS_PER_DAY = 1000        # section 3, Rate discipline
TIINGO_RAW_FIELDS = ("open", "high", "low", "close", "volume")
# Never read. Section 3, `Fields not used`.
TIINGO_FORBIDDEN_FIELDS = ("adjOpen", "adjHigh", "adjLow", "adjClose", "adjVolume")

ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"
ALPHAVANTAGE_FUNCTION = "TIME_SERIES_DAILY"   # raw, as-traded; free tier
ALPHAVANTAGE_REQUESTS_PER_DAY = 25    # section 3, cross-check rate limit
# Seconds between two cross-check requests. Section 3 bounds the cross-check by
# requests per *day* and says nothing about a burst; Alpha Vantage also throttles
# a burst, and refused 11 of 12 requests fired back to back on 2026-09-06, which
# turned an otherwise clean session into ERROR. This spaces them instead. It is a
# minimum interval, not a budget, so it cannot make the session exceed the 25 the
# spec allows. See docs/spec_issues.md, Phase A, item 16.
ALPHAVANTAGE_MIN_REQUEST_INTERVAL = 15.0

# --- statuses and codes, strategy_spec.md sections 12 and 13 ----------------

STATUS_PRECEDENCE = (
    "ERROR",
    "DISPUTED",
    "CROSSCHECK_UNAVAILABLE",
    "STALE",
    "HALTED",
    "OK",
)

RULE_CODES = (
    "ENTRY",
    "DATA",
    "STOP",
    "TARGET",
    "TIME",
    "CARRY",
    "HALTED",
    "CORPORATE_ACTION_EXIT",
)

EXIT_PRECEDENCE = ("DATA", "STOP", "TARGET", "TIME")

FLAG_CODES = (
    "DELAYED",
    "SKIPPED_NO_DATA",
    "WRITTEN_OFF",
    "ORDER_EXPIRED",
    "UNFILLED",
    "UNFILLED_SELL",
    "CASH_NEGATIVE",
    "CANCELLED_CASH",
    "DECLINED_CAPACITY",
    "DECLINED_SECTOR",
    "DECLINED_CASH",
    "DECLINED_PRICE",
    "CROSSCHECK_WARN",
    "CROSSCHECK_FLAGGED",
    "CORPORATE_ACTION",
)


def status_rank(status: str) -> int:
    """Lower is higher precedence (strategy_spec.md section 12)."""
    return STATUS_PRECEDENCE.index(status)


def worst_status(*statuses: str) -> str:
    """The highest-precedence status among those given."""
    return min(statuses, key=status_rank)


def secret(name: str) -> str | None:
    """Read a secret from the environment. Never logged, never written."""
    value = os.environ.get(name)
    if value is not None and value.strip() == "":
        return None
    return value
