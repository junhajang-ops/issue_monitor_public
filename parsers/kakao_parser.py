from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.time_utils import parse_kakao_kst_timestamp


KAKAO_START_RE = re.compile(
    r"^\[(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+KST\]\s+"
    r"room=(?P<room>.*?)\s+\|\s+sender=(?P<sender>.*?)\s+\|\s+msg=(?P<msg>.*)$"
)


@dataclass(frozen=True)
class ParsedMessage:
    source_id: str
    timestamp: datetime
    sender: str
    text: str
    raw_text: str


def _iter_message_blocks(path: Path) -> list[str]:
    """
    카카오톡 로그는 msg에 줄바꿈이 포함될 수 있으므로,
    새 timestamp 라인이 나오기 전까지 하나의 메시지 블록으로 묶습니다.
    """
    blocks: list[str] = []
    current: list[str] = []

    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")

            if KAKAO_START_RE.match(line):
                if current:
                    blocks.append("\n".join(current))
                current = [line]
            else:
                if current:
                    current.append(line)
                elif line.strip():
                    continue

    if current:
        blocks.append("\n".join(current))

    return blocks


def parse_kakao_file(
    path: Path,
    source_id: str,
    expected_room_name: str | None = None,
) -> tuple[list[ParsedMessage], list[str]]:
    messages: list[ParsedMessage] = []
    errors: list[str] = []

    for block in _iter_message_blocks(path):
        first_line = block.split("\n", 1)[0]
        match = KAKAO_START_RE.match(first_line)

        if not match:
            errors.append(f"unmatched block: {block[:300]}")
            continue

        room = match.group("room").strip()
        if expected_room_name and room != expected_room_name:
            continue

        try:
            timestamp = parse_kakao_kst_timestamp(match.group("timestamp"))
        except ValueError as exc:
            errors.append(f"timestamp parse failed: {exc} / {first_line}")
            continue

        sender = match.group("sender").strip() or "unknown"
        first_msg = match.group("msg")

        rest = ""
        if "\n" in block:
            rest = block.split("\n", 1)[1]

        text = first_msg if not rest else f"{first_msg}\n{rest}"
        text = clean_kakao_text(text)

        if not text:
            continue

        messages.append(
            ParsedMessage(
                source_id=source_id,
                timestamp=timestamp,
                sender=sender,
                text=text,
                raw_text=block,
            )
        )

    return messages, errors


def clean_kakao_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    cleaned = "\n".join(line for line in lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned