from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class SourceFile:
    source_id: str
    source_type: str
    path: Path
    matched_by: str
    room_name: str | None = None


@dataclass(frozen=True)
class SnapshotFile:
    source_id: str
    source_type: str
    original_path: Path
    snapshot_path: Path
    matched_by: str
    room_name: str | None = None


@dataclass(frozen=True)
class SnapshotResult:
    run_id: str
    snapshot_root: Path
    files: list[SnapshotFile]


@dataclass(frozen=True)
class NormalizedMessage:
    source_id: str
    timestamp: datetime
    sender: str
    text: str
    message_id: str
    is_new: bool
    raw_text: str