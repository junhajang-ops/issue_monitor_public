from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from config import (
    CONTEXT_WINDOW_MINUTES,
    INGAME_BASE_DIR,
    INGAME_SOURCES,
    KAKAO_BASE_DIR,
    KAKAO_SOURCES,
)
from core.models import SourceFile


KAKAO_ROOM_RE = re.compile(r"room=(?P<room>.*?)\s+\|")

KAKAO_FOLDER_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<index>\d+)_(?P<start>\d{4})-(?P<end>\d{4})$"
)

TEXT_EXTENSIONS = {".txt", ".log"}


def _safe_read_head(path: Path, max_chars: int = 12000) -> str:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
            return f.read(max_chars)
    except Exception:
        return ""


def _filename_matches(path: Path, contains_list: Iterable[str]) -> bool:
    name = path.name
    return any(token and token in name for token in contains_list)


def _room_matches_by_content(path: Path, room_name: str) -> bool:
    head = _safe_read_head(path)
    if not head:
        return False

    if f"room={room_name}" in head:
        return True

    for match in KAKAO_ROOM_RE.finditer(head):
        if match.group("room").strip() == room_name:
            return True

    return False


def _folder_may_overlap_context(folder_name: str, now: datetime) -> bool:
    match = KAKAO_FOLDER_RE.match(folder_name)
    if not match:
        return True

    date_text = match.group("date")
    start_text = match.group("start")
    end_text = match.group("end")

    try:
        start_dt = datetime.strptime(f"{date_text} {start_text}", "%Y-%m-%d %H%M")
        end_dt = datetime.strptime(f"{date_text} {end_text}", "%Y-%m-%d %H%M")
    except ValueError:
        return True

    end_dt = end_dt.replace(second=59)
    context_start = now - timedelta(minutes=CONTEXT_WINDOW_MINUTES)

    return start_dt <= now and end_dt >= context_start


def _iter_candidate_kakao_files(now: datetime) -> list[Path]:
    if not KAKAO_BASE_DIR.exists():
        print(f"[WARN] KAKAO_BASE_DIR not found: {KAKAO_BASE_DIR}")
        return []

    candidates: list[Path] = []

    for child in sorted(KAKAO_BASE_DIR.iterdir()):
        if child.is_dir():
            if not _folder_may_overlap_context(child.name, now):
                continue
            for file_path in sorted(child.rglob("*")):
                if file_path.is_file() and file_path.suffix.lower() in TEXT_EXTENSIONS:
                    candidates.append(file_path)
        elif child.is_file() and child.suffix.lower() in TEXT_EXTENSIONS:
            candidates.append(child)

    return candidates


def _dedupe_source_files(files: list[SourceFile]) -> list[SourceFile]:
    seen: set[tuple[str, Path]] = set()
    deduped: list[SourceFile] = []

    for item in files:
        try:
            resolved = item.path.resolve()
        except OSError:
            resolved = item.path

        key = (item.source_id, resolved)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def discover_kakao_files(now: datetime) -> list[SourceFile]:
    results: list[SourceFile] = []
    candidate_files = _iter_candidate_kakao_files(now)

    for source in KAKAO_SOURCES:
        source_id = source["source_id"]
        room_name = source["room_name"]
        filename_contains = source.get("filename_contains", [])

        for path in candidate_files:
            filename_ok = _filename_matches(path, filename_contains)
            content_ok = _room_matches_by_content(path, room_name)

            if filename_ok and content_ok:
                results.append(
                    SourceFile(
                        source_id=source_id,
                        source_type="kakao",
                        path=path,
                        matched_by="filename+room",
                        room_name=room_name,
                    )
                )
            elif content_ok:
                results.append(
                    SourceFile(
                        source_id=source_id,
                        source_type="kakao",
                        path=path,
                        matched_by="room",
                        room_name=room_name,
                    )
                )
            elif filename_ok:
                print(
                    f"[SKIP] filename matched but room not verified: "
                    f"source_id={source_id}, file={path}"
                )

    return _dedupe_source_files(results)


def discover_ingame_files(now: datetime) -> list[SourceFile]:
    del now

    if not INGAME_BASE_DIR.exists():
        print(f"[WARN] INGAME_BASE_DIR not found: {INGAME_BASE_DIR}")
        return []

    results: list[SourceFile] = []

    for source in INGAME_SOURCES:
        source_id = source["source_id"]
        file_glob = source["file_glob"]

        for path in sorted(INGAME_BASE_DIR.glob(file_glob)):
            if path.is_file():
                results.append(
                    SourceFile(
                        source_id=source_id,
                        source_type="ingame",
                        path=path,
                        matched_by=file_glob,
                    )
                )

    return _dedupe_source_files(results)


def discover_all_sources(now: datetime) -> list[SourceFile]:
    results: list[SourceFile] = []
    results.extend(discover_kakao_files(now))
    results.extend(discover_ingame_files(now))
    return results