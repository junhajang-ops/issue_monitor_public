from __future__ import annotations

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def parse_kakao_kst_timestamp(value: str) -> datetime:
    """
    예: 2026-05-08 18:35:10
    반환: timezone-aware KST datetime
    """
    dt = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=KST)


def parse_flexible_timestamp(value: str) -> datetime:
    """
    인게임 jsonl timestamp 후보를 가능한 범위에서 파싱합니다.
    KST 정보가 없으면 KST로 간주합니다.
    """
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")

    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except ValueError:
        pass

    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"]:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=KST)
        except ValueError:
            continue

    raise ValueError(f"unsupported timestamp format: {value}")


def to_iso_kst(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).isoformat()


def ensure_kst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)