from __future__ import annotations

from datetime import datetime, timedelta

import config
from core.time_utils import ensure_kst


def is_in_context_window(timestamp: datetime, now: datetime) -> bool:
    ts = ensure_kst(timestamp)
    now_kst = ensure_kst(now)
    start = now_kst - timedelta(minutes=config.CONTEXT_WINDOW_MINUTES)
    return start <= ts <= now_kst


def is_in_new_window(timestamp: datetime, now: datetime) -> bool:
    ts = ensure_kst(timestamp)
    now_kst = ensure_kst(now)
    start = now_kst - timedelta(minutes=config.NEW_WINDOW_MINUTES)
    return start <= ts <= now_kst