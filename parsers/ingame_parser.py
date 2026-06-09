from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.time_utils import KST, parse_flexible_timestamp


@dataclass(frozen=True)
class ParsedMessage:
    source_id: str
    timestamp: datetime
    sender: str
    text: str
    raw_text: str


def parse_ingame_file(path: Path, source_id: str) -> tuple[list[ParsedMessage], list[str]]:
    messages: list[ParsedMessage] = []
    errors: list[str] = []

    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        lines = f.readlines()

    last_line_no = len(lines)

    for line_no, line in enumerate(lines, start=1):
        raw = line.rstrip("\n")
        if not raw.strip():
            continue

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            if line_no == last_line_no:
                print(
                    f"[WARN] ignore partial last jsonl line: "
                    f"file={path}, line={line_no}, error={exc}"
                )
                continue

            errors.append(f"line {line_no}: json decode failed: {exc} / {raw[:300]}")
            continue

        if not isinstance(obj, dict):
            errors.append(f"line {line_no}: json row is not object / {raw[:300]}")
            continue

        try:
            timestamp = extract_ingame_timestamp(obj)
        except ValueError as exc:
            if line_no == last_line_no:
                print(
                    f"[WARN] ignore unparsable last jsonl line timestamp: "
                    f"file={path}, line={line_no}, error={exc}"
                )
                continue

            errors.append(f"line {line_no}: timestamp parse failed: {exc} / {raw[:300]}")
            continue

        sender = extract_first_string(
            obj,
            ["sender", "sender_name", "nickname", "user", "name"],
            default="unknown",
        )
        text = extract_first_string(
            obj,
            ["text", "message", "msg", "content", "message_text"],
            default="",
        )

        text = clean_ingame_text(text)
        if not text:
            continue

        messages.append(
            ParsedMessage(
                source_id=source_id,
                timestamp=timestamp,
                sender=sender.strip() or "unknown",
                text=text,
                raw_text=raw,
            )
        )

    return messages, errors


def extract_ingame_timestamp(obj: dict[str, Any]) -> datetime:
    """
    지원 형식:
    1) {"timestamp": "2026-05-08T18:45:00+09:00", ...}
    2) {"created_at": "2026-05-08 18:45:00", ...}
    3) {"saved_date": "2026-05-08", "message_time": "18:45", ...}
    4) {"saved_date": "2026-05-08", "message_time": "18:45:12", ...}
    """
    direct_value = (
        obj.get("timestamp")
        or obj.get("time")
        or obj.get("created_at")
        or obj.get("datetime")
    )

    if direct_value:
        return parse_flexible_timestamp(str(direct_value))

    saved_date = obj.get("saved_date") or obj.get("date")
    message_time = obj.get("message_time") or obj.get("time_text")

    if saved_date and message_time:
        date_text = str(saved_date).strip()
        time_text = str(message_time).strip()

        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]:
            try:
                dt = datetime.strptime(f"{date_text} {time_text}", fmt)
                return dt.replace(tzinfo=KST)
            except ValueError:
                continue

        raise ValueError(f"unsupported saved_date/message_time: {date_text} {time_text}")

    raise ValueError("missing timestamp fields")


def extract_first_string(obj: dict[str, Any], keys: list[str], default: str) -> str:
    for key in keys:
        value = obj.get(key)
        if value is not None:
            return str(value)
    return default


def clean_ingame_text(text: str) -> str:
    return str(text).replace("\r\n", "\n").strip()
