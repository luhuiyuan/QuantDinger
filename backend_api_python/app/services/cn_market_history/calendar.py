"""XSHG session dates used for Shanghai and Shenzhen daily-bar coverage."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import pandas as pd


@lru_cache(maxsize=1)
def _xshg_calendar():
    import exchange_calendars

    return exchange_calendars.get_calendar("XSHG")


def expected_a_share_sessions(
    start_date: date,
    end_date: date,
    *,
    listed_on: date | None = None,
    delisted_on: date | None = None,
    confirmed_non_trading_dates: set[date] | None = None,
) -> tuple[date, ...]:
    effective_start = max(start_date, listed_on) if listed_on else start_date
    effective_end = min(end_date, delisted_on) if delisted_on else end_date
    if effective_end < effective_start:
        return ()
    sessions = _xshg_calendar().sessions_in_range(
        pd.Timestamp(effective_start),
        pd.Timestamp(effective_end),
    )
    excluded = confirmed_non_trading_dates or set()
    return tuple(session.date() for session in sessions if session.date() not in excluded)


def previous_a_share_sessions(before_date: date, count: int) -> tuple[date, ...]:
    """Return the last ``count`` exchange sessions strictly before a date."""
    session_count = max(0, int(count or 0))
    if session_count == 0:
        return ()
    calendar = _xshg_calendar()
    anchor = calendar.date_to_session(
        pd.Timestamp(before_date) - pd.Timedelta(days=1),
        direction="previous",
    )
    sessions = calendar.sessions_window(anchor, -session_count)
    return tuple(session.date() for session in sessions)
