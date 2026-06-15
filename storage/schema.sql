PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    sender TEXT,
    text TEXT NOT NULL,
    is_new INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    raw_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp
ON messages (timestamp);

CREATE INDEX IF NOT EXISTS idx_messages_source_timestamp
ON messages (source_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_messages_run_id
ON messages (run_id);

CREATE TABLE IF NOT EXISTS local_llm_runs (
    run_id TEXT PRIMARY KEY,
    window_start TEXT,
    window_end TEXT,
    context_window_start TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    new_message_count INTEGER NOT NULL DEFAULT 0,
    has_possible_issue INTEGER,
    llm_prompt_tokens INTEGER,
    llm_cached_prompt_tokens INTEGER,
    llm_completion_tokens INTEGER,
    llm_reasoning_tokens INTEGER,
    llm_output_tokens INTEGER,
    llm_total_tokens INTEGER,
    llm_prompt_chars INTEGER,
    llm_response_chars INTEGER,
    llm_thinking_chars INTEGER,
    llm_token_usage_json TEXT,
    raw_response TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL
);
