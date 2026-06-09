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
    local_llm_score REAL,
    rule_score REAL,
    source_correlation_score REAL,
    candidate_score REAL,
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

CREATE TABLE IF NOT EXISTS cloud_llm_runs (
    run_id TEXT PRIMARY KEY,
    candidate_score REAL,
    final_has_issue INTEGER,
    issue_type TEXT,
    severity TEXT,
    confidence REAL,
    affected_sources TEXT,
    evidence_message_ids TEXT,
    raw_response TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    issue_key TEXT NOT NULL,
    issue_type TEXT,
    severity TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    affected_sources TEXT,
    sent_at TEXT NOT NULL,
    channels TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_issue_key_sent_at
ON alerts (issue_key, sent_at);

CREATE TABLE IF NOT EXISTS issue_states (
    issue_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_alerted_at TEXT,
    cooldown_until TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    latest_severity TEXT
);
