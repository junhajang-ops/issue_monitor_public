from __future__ import annotations

import hashlib
from datetime import datetime

from core.time_utils import to_iso_kst


def make_message_id(
    source_id: str,
    timestamp: datetime,
    sender: str,
    text: str,
) -> str:
    """
    동일 메시지 중복 저장 방지를 위한 안정적인 ID를 생성합니다.
    """
    base = "|".join(
        [
            source_id.strip(),
            to_iso_kst(timestamp),
            (sender or "unknown").strip(),
            (text or "").strip(),
        ]
    )
    digest = hashlib.sha1(base.encode("utf-8", errors="replace")).hexdigest()
    return f"{source_id}:{digest}"