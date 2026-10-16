"""NYSE (XNYS) sessions and America/New_York helpers (strategy_spec.md sections 3 and 4).

A "session" is a NYSE trading day. The session index is a position in this
calendar, whether or not a given ticker has a bar on that session.
"""

from __future__ import annotations

import datetime as dt
import functools

import exchange_calendars as xcals

from quantdesk import config

ET = config.ET

# The calendar is expensive to build and is immutable once built.
_START = "2000-01-01"
_END = "2035-12-31"


@functools.lru_cache(maxsize=1)
def _calendar():
    return xcals.get_calendar(config.CALENDAR_NAME, start=_START, end=_END)


@functools.lru_cache(maxsize=1)
def _session_list() -> tuple[dt.date, ...]:
    return tuple(d.date() for d in _calendar().sessions)


@functools.lru_cache(maxsize=1)
def _session_index() -> dict[dt.date, int]:
    return {d: i for i, d in enumerate(_session_list())}


def is_session(day: dt.date) -> bool:
    return day in _session_index()


def session_index(day: dt.date) -> int:
    """Position of `day` in the XNYS session calendar.

    Raises KeyError when `day` is not a session; every caller in this codebase
    works in session dates, so a non-session date is a programming error rather
    than a data condition.
    """
    return _session_index()[day]


def sessions_between(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every session in `[start, end]`, inclusive at both ends."""
    return [d for d in _session_list() if start <= d <= end]


def previous_session(day: dt.date) -> dt.date:
    """The session before `day`, whether or not `day` is itself a session."""
    sessions = _session_list()
    if day in _session_index():
        i = _session_index()[day]
        if i == 0:
            raise ValueError(f"no session before {day}")
        return sessions[i - 1]
    earlier = [d for d in sessions if d < day]
    if not earlier:
        raise ValueError(f"no session before {day}")
    return earlier[-1]


def next_session(day: dt.date) -> dt.date:
    """The session after `day`, whether or not `day` is itself a session."""
    sessions = _session_list()
    later = [d for d in sessions if d > day]
    if not later:
        raise ValueError(f"no session after {day}")
    return later[0]


def sessions_since(start: dt.date, end: dt.date) -> int:
    """`session_index(end) - session_index(start)`, both must be sessions."""
    return session_index(end) - session_index(start)


def last_completed_session(now_et: dt.datetime | None = None) -> dt.date:
    """The most recent session whose close has passed.

    A session closes at 16:00 ET (13:00 on an early-close day); this helper uses
    the calendar's own close time so early closes are handled.
    """
    now_et = now_et or dt.datetime.now(tz=ET)
    cal = _calendar()
    for day in reversed(_session_list()):
        if day > now_et.date():
            continue
        close = cal.session_close(day.isoformat()).tz_convert(ET)
        if now_et >= close.to_pydatetime():
            return day
    raise ValueError("no completed session found")


def session_open_et(day: dt.date) -> dt.datetime:
    """The opening bell of `day`, in ET, with offset."""
    return _calendar().session_open(day.isoformat()).tz_convert(ET).to_pydatetime()


def session_close_et(day: dt.date) -> dt.datetime:
    """The closing bell of `day`, in ET, with offset."""
    return _calendar().session_close(day.isoformat()).tz_convert(ET).to_pydatetime()


def is_regular_hours(moment: dt.datetime) -> bool:
    """True when `moment` falls inside a NYSE regular session."""
    if moment.tzinfo is None:
        raise ValueError("timestamp must carry a timezone")
        # every timestamp written to disk carries its offset
    local = moment.astimezone(ET)
    day = local.date()
    if not is_session(day):
        return False
    return session_open_et(day) <= local < session_close_et(day)


def now_et() -> dt.datetime:
    return dt.datetime.now(tz=ET)


def stamp(moment: dt.datetime | None = None) -> str:
    """An ET timestamp with its offset, for writing to disk."""
    moment = moment or now_et()
    return moment.astimezone(ET).replace(microsecond=0).isoformat()
