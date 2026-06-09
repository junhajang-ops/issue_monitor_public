from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.id_utils import make_message_id
from core.models import NormalizedMessage, SnapshotFile, SnapshotResult
from core.time_utils import to_iso_kst
from parsers.ingame_parser import parse_ingame_file
from parsers.kakao_parser import parse_kakao_file
from pipeline.windowing import is_in_context_window, is_in_new_window


def normalize_snapshot(snapshot: SnapshotResult, now: Any) -> tuple[list[NormalizedMessage], list[str]]:
    all_messages: list[NormalizedMessage] = []
    errors: list[str] = []

    for snapshot_file in snapshot.files:
        parsed, parse_errors = _parse_snapshot_file(snapshot_file)
        errors.extend([f"{snapshot_file.snapshot_path}: {err}" for err in parse_errors])

        for msg in parsed:
            if not is_in_context_window(msg.timestamp, now):
                continue

            is_new = is_in_new_window(msg.timestamp, now)
            message_id = make_message_id(
                source_id=msg.source_id,
                timestamp=msg.timestamp,
                sender=msg.sender,
                text=msg.text,
            )

            all_messages.append(
                NormalizedMessage(
                    source_id=msg.source_id,
                    timestamp=msg.timestamp,
                    sender=msg.sender,
                    text=msg.text,
                    message_id=message_id,
                    is_new=is_new,
                    raw_text=msg.raw_text,
                )
            )

    all_messages.sort(key=lambda item: item.timestamp)
    return all_messages, errors


def _parse_snapshot_file(snapshot_file: SnapshotFile):
    if snapshot_file.source_type == "kakao":
        return parse_kakao_file(
            path=snapshot_file.snapshot_path,
            source_id=snapshot_file.source_id,
            expected_room_name=snapshot_file.room_name,
        )

    if snapshot_file.source_type == "ingame":
        return parse_ingame_file(
            path=snapshot_file.snapshot_path,
            source_id=snapshot_file.source_id,
        )

    return [], [f"unsupported source_type: {snapshot_file.source_type}"]


def write_normalized_messages(snapshot: SnapshotResult, messages: list[NormalizedMessage]) -> Path:
    output_dir = snapshot.snapshot_root / "normalized"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / "messages.jsonl"

    with output_path.open("w", encoding="utf-8") as f:
        for msg in messages:
            row = {
                "source_id": msg.source_id,
                "timestamp": to_iso_kst(msg.timestamp),
                "sender": msg.sender,
                "text": msg.text,
                "message_id": msg.message_id,
                "is_new": msg.is_new,
                "raw_text": msg.raw_text,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return output_path


def write_parse_errors(snapshot: SnapshotResult, errors: list[str]) -> Path | None:
    if not errors:
        return None

    output_dir = snapshot.snapshot_root / "skipped"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / "parse_errors.log"

    with output_path.open("w", encoding="utf-8") as f:
        for err in errors:
            f.write(err + "\n")

    return output_path