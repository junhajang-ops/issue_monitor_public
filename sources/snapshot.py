from __future__ import annotations

import random
import shutil
import time
from datetime import datetime
from pathlib import Path

from config import SNAPSHOT_CLEANUP_ENABLED, SNAPSHOT_DIR, SNAPSHOT_RETENTION_RUNS
from core.models import SnapshotFile, SnapshotResult, SourceFile


RETENTION_LOCK_FILENAME = ".retention.lock"
RETENTION_LOCK_RETRY_COUNT = 5
RETENTION_LOCK_RETRY_DELAY_MIN_SECONDS = 0.2
RETENTION_LOCK_RETRY_DELAY_MAX_SECONDS = 0.5
SOURCE_COPY_RETRY_COUNT = 5
SOURCE_COPY_RETRY_DELAY_MIN_SECONDS = 0.2
SOURCE_COPY_RETRY_DELAY_MAX_SECONDS = 0.5


def make_run_id(now: datetime) -> str:
    return now.strftime("%Y%m%d_%H%M%S")


def _safe_snapshot_filename(source_file: SourceFile, index: int) -> str:
    suffix = source_file.path.suffix or ".txt"
    stem = source_file.path.stem

    safe_stem = (
        stem.replace("<", "_")
        .replace(">", "_")
        .replace(":", "_")
        .replace('"', "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace("|", "_")
        .replace("?", "_")
        .replace("*", "_")
    )

    return f"{index:03d}_{safe_stem}{suffix}"


def _sleep_random(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def _retention_lock_path(source_path: Path) -> Path:
    return source_path.parent / RETENTION_LOCK_FILENAME


def _wait_for_retention_lock(source_path: Path) -> bool:
    """
    playwright_chat_reader retention 정리 중에는 output/.retention.lock이 생깁니다.

    lock이 보이면 짧게 재시도하고, 계속 남아 있으면 이번 파일 복사를 스킵합니다.
    """
    lock_path = _retention_lock_path(source_path)

    for attempt in range(1, RETENTION_LOCK_RETRY_COUNT + 1):
        if not lock_path.exists():
            return True

        print(
            f"[SNAPSHOT] retention lock detected, waiting "
            f"attempt={attempt}/{RETENTION_LOCK_RETRY_COUNT}, lock={lock_path}"
        )
        _sleep_random(
            RETENTION_LOCK_RETRY_DELAY_MIN_SECONDS,
            RETENTION_LOCK_RETRY_DELAY_MAX_SECONDS,
        )

    if lock_path.exists():
        print(
            f"[WARN] retention lock still exists after retries. "
            f"skip this source for current run: {source_path}"
        )
        return False

    return True


def _copy_with_retry(source_path: Path, dest_path: Path) -> bool:
    """
    원본 파일이 append 또는 retention rewrite 중일 수 있으므로 copy2를 짧게 재시도합니다.
    """
    for attempt in range(1, SOURCE_COPY_RETRY_COUNT + 1):
        try:
            shutil.copy2(source_path, dest_path)
            return True
        except FileNotFoundError:
            print(f"[WARN] source disappeared before copy: {source_path}")
            return False
        except PermissionError as exc:
            print(
                f"[WARN] permission error while copying "
                f"attempt={attempt}/{SOURCE_COPY_RETRY_COUNT}: {source_path} / {exc}"
            )
        except OSError as exc:
            print(
                f"[WARN] failed to copy "
                f"attempt={attempt}/{SOURCE_COPY_RETRY_COUNT}: {source_path} / {exc}"
            )

        if attempt < SOURCE_COPY_RETRY_COUNT:
            _sleep_random(
                SOURCE_COPY_RETRY_DELAY_MIN_SECONDS,
                SOURCE_COPY_RETRY_DELAY_MAX_SECONDS,
            )

    print(f"[WARN] copy failed after retries. skip this source for current run: {source_path}")
    return False


def create_snapshot(source_files: list[SourceFile], now: datetime) -> SnapshotResult:
    run_id = make_run_id(now)
    snapshot_root = SNAPSHOT_DIR / run_id
    raw_root = snapshot_root / "raw"

    copied: list[SnapshotFile] = []
    counters: dict[str, int] = {}

    raw_root.mkdir(parents=True, exist_ok=True)

    for source_file in source_files:
        counters[source_file.source_id] = counters.get(source_file.source_id, 0) + 1
        index = counters[source_file.source_id]

        dest_dir = raw_root / source_file.source_id
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_path = dest_dir / _safe_snapshot_filename(source_file, index)

        if not _wait_for_retention_lock(source_file.path):
            continue

        if not _copy_with_retry(source_file.path, dest_path):
            continue

        copied.append(
            SnapshotFile(
                source_id=source_file.source_id,
                source_type=source_file.source_type,
                original_path=source_file.path,
                snapshot_path=dest_path,
                matched_by=source_file.matched_by,
                room_name=source_file.room_name,
            )
        )

    return SnapshotResult(
        run_id=run_id,
        snapshot_root=snapshot_root,
        files=copied,
    )


def cleanup_old_snapshots() -> int:
    """
    최근 SNAPSHOT_RETENTION_RUNS개 snapshot만 남기고 오래된 run 폴더를 삭제합니다.
    반환값: 삭제한 snapshot 폴더 개수
    """
    if not SNAPSHOT_CLEANUP_ENABLED:
        return 0

    if SNAPSHOT_RETENTION_RUNS <= 0:
        return 0

    if not SNAPSHOT_DIR.exists():
        return 0

    run_dirs = [path for path in SNAPSHOT_DIR.iterdir() if path.is_dir()]
    run_dirs.sort(key=lambda path: path.name, reverse=True)

    keep = set(run_dirs[:SNAPSHOT_RETENTION_RUNS])
    delete_targets = [path for path in run_dirs if path not in keep]

    deleted = 0
    for path in delete_targets:
        try:
            shutil.rmtree(path)
            deleted += 1
        except OSError as exc:
            print(f"[WARN] failed to delete old snapshot: {path} / {exc}")

    return deleted
