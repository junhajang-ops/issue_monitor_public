from __future__ import annotations

from datetime import datetime, timedelta

from config import CONTEXT_WINDOW_MINUTES, NEW_WINDOW_MINUTES
from core.time_utils import ensure_kst


def is_in_context_window(timestamp: datetime, now: datetime) -> bool:
    ts = ensure_kst(timestamp)
    now_kst = ensure_kst(now)
    start = now_kst - timedelta(minutes=CONTEXT_WINDOW_MINUTES)
    return start <= ts <= now_kst


def is_in_new_window(timestamp: datetime, now: datetime) -> bool:
    ts = ensure_kst(timestamp)
    now_kst = ensure_kst(now)
    start = now_kst - timedelta(minutes=NEW_WINDOW_MINUTES)
    return start <= ts <= now_kst