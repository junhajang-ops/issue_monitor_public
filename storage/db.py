from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "issue_monitor.sqlite3"
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect_db(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create a SQLite connection with project defaults."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(
    conn: sqlite3.Connection,
    schema_path: Path | str = DEFAULT_SCHEMA_PATH,
) -> None:
    """Initialize all MVP tables if they do not exist."""
    schema_file = Path(schema_path)
    sql = schema_file.read_text(encoding="utf-8")
    conn.executescript(sql)
    _ensure_local_llm_run_columns(conn)
    conn.commit()


def _ensure_local_llm_run_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial MVP schema.

    CREATE TABLE IF NOT EXISTS does not alter existing SQLite tables, so this
    keeps the operational DB compatible when schema.sql grows over time.
    """
    existing = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(local_llm_runs)").fetchall()
    }
    columns: dict[str, str] = {
        "llm_prompt_tokens": "INTEGER",
        "llm_cached_prompt_tokens": "INTEGER",
        "llm_completion_tokens": "INTEGER",
        "llm_reasoning_tokens": "INTEGER",
        "llm_output_tokens": "INTEGER",
        "llm_total_tokens": "INTEGER",
        "llm_prompt_chars": "INTEGER",
        "llm_response_chars": "INTEGER",
        "llm_thinking_chars": "INTEGER",
        "llm_token_usage_json": "TEXT",
        # 하이브리드 2차 검증(OpenAI) 결과
        "cloud_verify_status": "TEXT",
        "cloud_verified": "INTEGER",
        "cloud_verify_reason": "TEXT",
        "cloud_prompt_tokens": "INTEGER",
        "cloud_completion_tokens": "INTEGER",
        "cloud_total_tokens": "INTEGER",
        # 2차 응답 전체 JSON(confirmed·reason·reporter_message_ids·evidence_message_ids 등 사후 복원용)
        "cloud_raw_json": "TEXT",
        # 2차가 카운트한 고유 신고자 수(A 채널 임계 SLACK_CHANNEL_A_MIN_REPORTERS 판단 근거). 발송 시점 계산값 보존.
        "cloud_reporter_count": "INTEGER",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE local_llm_runs ADD COLUMN {name} {column_type}")


def _get_value(message: Any, name: str, default: Any = None) -> Any:
    """Read a value from either a dict-like message or an object/dataclass."""
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def _to_iso_text(value: Any) -> str:
    """Convert datetime-like values to ISO text; preserve existing strings."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def insert_messages(
    conn: sqlite3.Connection,
    messages: Iterable[Any],
    *,
    run_id: str,
    first_seen_at: str,
) -> int:
    """
    Insert normalized messages into SQLite.

    - Deduplication is controlled by message_id PRIMARY KEY.
    - Existing message rows are not overwritten.
    - Return value is the count of rows newly inserted in this call.
    """
    rows: list[tuple[Any, ...]] = []

    for message in messages:
        message_id = _get_value(message, "message_id")
        source_id = _get_value(message, "source_id")
        timestamp = _to_iso_text(_get_value(message, "timestamp"))
        sender = _get_value(message, "sender", "")
        text = _get_value(message, "text", "")
        is_new = 1 if bool(_get_value(message, "is_new", False)) else 0
        raw_text = _get_value(message, "raw_text", None)

        if not message_id:
            raise ValueError("message_id is required")
        if not source_id:
            raise ValueError(f"source_id is required for message_id={message_id}")
        if not timestamp:
            raise ValueError(f"timestamp is required for message_id={message_id}")
        if text is None or str(text) == "":
            raise ValueError(f"text is required for message_id={message_id}")

        rows.append(
            (
                str(message_id),
                str(source_id),
                timestamp,
                None if sender is None else str(sender),
                str(text),
                is_new,
                first_seen_at,
                run_id,
                None if raw_text is None else str(raw_text),
            )
        )

    if not rows:
        return 0

    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO messages (
            message_id,
            source_id,
            timestamp,
            sender,
            text,
            is_new,
            first_seen_at,
            run_id,
            raw_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def count_messages(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM messages").fetchone()
    return int(row["count"])


def prune_messages_older_than(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """
    Delete messages older than the DB working-set cutoff.

    The raw source files are already copied into snapshots before parsing, so the
    messages table should stay small and only contain the recent window that the
    local LLM will judge.
    """
    before = conn.total_changes
    conn.execute(
        """
        DELETE FROM messages
        WHERE timestamp < ?
        """,
        (cutoff_iso,),
    )
    conn.commit()
    return conn.total_changes - before


# 판정 이력 테이블의 (테이블, 시간 컬럼) 매핑. created_at 기준.
_DB_RETENTION_TABLES = (
    ("local_llm_runs", "created_at"),
)


def prune_db_runs_older_than(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """판정 이력·알림 테이블에서 보관 기한(cutoff)보다 오래된 행을 삭제한다.

    - local_llm_runs: created_at 기준.
    - messages는 working set이라 별도(prune_messages_older_than)로 관리한다.
    - SQLite는 DELETE 후 파일이 자동 축소되지 않지만, retention이 일정하면 빈 공간이
      재사용되어 파일 크기가 평형을 이룬다(즉시 축소가 필요하면 VACUUM 수동 실행).
    """
    before = conn.total_changes
    for table, col in _DB_RETENTION_TABLES:
        try:
            conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff_iso,))
        except sqlite3.Error:
            # 테이블/컬럼이 없으면 건너뛴다(스키마 변형 안전성).
            continue
    conn.commit()
    return conn.total_changes - before


def fetch_messages_since(
    conn: sqlite3.Connection,
    cutoff_iso: str,
    new_window_cutoff_iso: str | None = None,
) -> list[sqlite3.Row]:
    """Return recent messages ordered for local LLM context building.

    is_new is computed dynamically: 1 if timestamp >= new_window_cutoff_iso, else 0.
    This ensures messages aren't permanently marked new across multiple runs.
    If new_window_cutoff_iso is omitted, the stored is_new value is used.
    """
    if new_window_cutoff_iso is not None:
        return list(
            conn.execute(
                """
                SELECT
                    message_id,
                    source_id,
                    timestamp,
                    sender,
                    text,
                    CASE WHEN timestamp >= ? THEN 1 ELSE 0 END AS is_new,
                    first_seen_at,
                    run_id,
                    raw_text
                FROM messages
                WHERE timestamp >= ?
                ORDER BY timestamp ASC, source_id ASC, message_id ASC
                """,
                (new_window_cutoff_iso, cutoff_iso),
            )
        )

    return list(
        conn.execute(
            """
            SELECT
                message_id,
                source_id,
                timestamp,
                sender,
                text,
                is_new,
                first_seen_at,
                run_id,
                raw_text
            FROM messages
            WHERE timestamp >= ?
            ORDER BY timestamp ASC, source_id ASC, message_id ASC
            """,
            (cutoff_iso,),
        )
    )


def insert_local_llm_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    window_start: str,
    window_end: str,
    context_window_start: str,
    message_count: int,
    new_message_count: int,
    raw_response: str,
    status: str,
    error: str | None,
    created_at: str,
    parsed_response: Mapping[str, Any] | None = None,
    has_possible_issue_override: bool | None = None,
    llm_meta: Mapping[str, Any] | None = None,
    cloud_verify: Mapping[str, Any] | None = None,
    cloud_reporter_count: int | None = None,
) -> None:
    """Persist one local Ollama judgment result for later inspection."""
    parsed = parsed_response or {}
    meta = llm_meta or {}
    token_usage = _extract_llm_token_usage(meta)

    cv = cloud_verify or {}
    cv_confirmed = cv.get("confirmed")
    cv_confirmed_int = None if cv_confirmed is None else (1 if cv_confirmed else 0)

    if has_possible_issue_override is not None:
        has_possible_issue_int = 1 if has_possible_issue_override else 0
    else:
        has_possible_issue = parsed.get("issue_detected", parsed.get("should_alert"))
        if has_possible_issue is None:
            has_possible_issue_int = None
        else:
            has_possible_issue_int = 1 if bool(has_possible_issue) else 0

    conn.execute(
        """
        INSERT OR REPLACE INTO local_llm_runs (
            run_id,
            window_start,
            window_end,
            context_window_start,
            message_count,
            new_message_count,
            has_possible_issue,
            llm_prompt_tokens,
            llm_cached_prompt_tokens,
            llm_completion_tokens,
            llm_reasoning_tokens,
            llm_output_tokens,
            llm_total_tokens,
            llm_prompt_chars,
            llm_response_chars,
            llm_thinking_chars,
            llm_token_usage_json,
            raw_response,
            status,
            error,
            created_at,
            cloud_verify_status,
            cloud_verified,
            cloud_verify_reason,
            cloud_prompt_tokens,
            cloud_completion_tokens,
            cloud_total_tokens,
            cloud_raw_json,
            cloud_reporter_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            window_start,
            window_end,
            context_window_start,
            message_count,
            new_message_count,
            has_possible_issue_int,
            token_usage["prompt_tokens"],
            token_usage["cached_prompt_tokens"],
            token_usage["completion_tokens"],
            token_usage["reasoning_tokens"],
            token_usage["output_tokens"],
            token_usage["total_tokens"],
            token_usage["prompt_chars"],
            token_usage["response_chars"],
            token_usage["thinking_chars"],
            token_usage["usage_json"],
            raw_response if raw_response else json.dumps(parsed, ensure_ascii=False),
            status,
            error,
            created_at,
            cv.get("status"),
            cv_confirmed_int,
            cv.get("reason"),
            cv.get("prompt_tokens"),
            cv.get("completion_tokens"),
            cv.get("total_tokens"),
            json.dumps(cv, ensure_ascii=False) if cv else None,
            cloud_reporter_count,
        ),
    )
    conn.commit()


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_llm_token_usage(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize token-related LLM metadata for DB storage.

    Exact availability differs by server/model. llama.cpp exposes prompt and
    completion token counts; OpenAI-compatible responses may also expose cached
    input and reasoning token counts through usage detail objects.
    """
    usage_obj = meta.get("usage")
    usage = usage_obj if isinstance(usage_obj, Mapping) else {}
    prompt_details_obj = usage.get("prompt_tokens_details")
    prompt_details = prompt_details_obj if isinstance(prompt_details_obj, Mapping) else {}
    completion_details_obj = usage.get("completion_tokens_details")
    completion_details = (
        completion_details_obj if isinstance(completion_details_obj, Mapping) else {}
    )

    prompt_tokens = _optional_int(
        meta.get("prompt_eval_count", usage.get("prompt_tokens"))
    )
    completion_tokens = _optional_int(
        meta.get("eval_count", usage.get("completion_tokens"))
    )
    total_tokens = _optional_int(usage.get("total_tokens"))
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)

    cached_prompt_tokens = _optional_int(
        meta.get("cached_prompt_tokens", prompt_details.get("cached_tokens"))
    )
    reasoning_tokens = _optional_int(
        meta.get("reasoning_tokens", completion_details.get("reasoning_tokens"))
    )
    output_tokens = _optional_int(meta.get("output_tokens"))
    if output_tokens is None and completion_tokens is not None:
        if reasoning_tokens is not None:
            output_tokens = max(0, completion_tokens - reasoning_tokens)
        else:
            output_tokens = completion_tokens

    try:
        usage_json = json.dumps(usage_obj or {}, ensure_ascii=False, sort_keys=True)
    except TypeError:
        usage_json = json.dumps({}, ensure_ascii=False)

    return {
        "prompt_tokens": prompt_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "prompt_chars": _optional_int(meta.get("prompt_chars")),
        "response_chars": _optional_int(meta.get("response_chars")),
        "thinking_chars": _optional_int(meta.get("thinking_chars")),
        "usage_json": usage_json,
    }


def fetch_latest_ok_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the latest successful local LLM run, if any."""
    return conn.execute(
        """
        SELECT *
        FROM local_llm_runs
        WHERE status = 'ok'
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()


def fetch_latest_run_any_status(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the latest local LLM run regardless of status.

    Used by pending detection in main.py: when the latest run is not 'ok'
    (e.g. 'error', 'timeout', 'parse_error', 'skipped_empty') we must not
    silently fall back to an older 'ok' run, otherwise stale pending
    alerts could be confirmed by what looks like a successful 2nd cycle.
    """
    return conn.execute(
        """
        SELECT *
        FROM local_llm_runs
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
