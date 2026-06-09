from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return int(value)


def _optional_float_env(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return float(value)

# =========================
# 프로젝트 기본 경로
# =========================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "issue_monitor.sqlite3"

# =========================
# snapshot 보존 정책
# =========================

# 최근 몇 개 run snapshot을 유지할지 설정합니다.
# 5분 주기 기준 50개면 약 4시간 10분치입니다.
SNAPSHOT_RETENTION_RUNS = int(os.getenv("SNAPSHOT_RETENTION_RUNS", "50"))

# 0이면 cleanup 비활성화, 1이면 활성화
SNAPSHOT_CLEANUP_ENABLED = os.getenv("SNAPSHOT_CLEANUP_ENABLED", "1") == "1"

# =========================
# 실행 주기 / 윈도우
# =========================

RUN_INTERVAL_SECONDS = int(os.getenv("RUN_INTERVAL_SECONDS", "300"))
CONTEXT_WINDOW_MINUTES = int(os.getenv("CONTEXT_WINDOW_MINUTES", "10"))
NEW_WINDOW_MINUTES = int(os.getenv("NEW_WINDOW_MINUTES", "5"))
LOOP_ENABLED = os.getenv("LOOP_ENABLED", "1") == "1"

# DB messages 테이블은 LLM 판단용 working set입니다.
# 전체 원본은 data/snapshots/raw에 보존되므로, DB에는 최근 판단 윈도우만 유지합니다.
DB_MESSAGE_RETENTION_MINUTES = int(
    os.getenv("DB_MESSAGE_RETENTION_MINUTES", str(CONTEXT_WINDOW_MINUTES))
)

# =========================
# 원본 소스 경로
# =========================

KAKAO_BASE_DIR = Path(
    os.getenv(
        "KAKAO_BASE_DIR",
        "[LOCAL]/Documents/[REDACTED]/Pictures/kakao_logs/llm/rooms",
    )
)

INGAME_BASE_DIR = Path(
    os.getenv(
        "INGAME_BASE_DIR",
        "[LOCAL]/Desktop/playwright_chat_reader/output",
    )
)

# =========================
# 카카오톡 대상 방 설정
# =========================

KAKAO_SOURCES = [
    {
        "source_id": "kakao_a",
        "room_name": "모바일게임 원조 커뮤니티(비번)",
        "filename_contains": [
            "모바일게임_원조_커뮤니티",
            "원조_커뮤니티",
            "원조",
        ],
    },
    {
        "source_id": "kakao_b",
        "room_name": "모바일게임 정보&소통방 ver.2",
        "filename_contains": [
            "모바일게임_정보",
            "정보&소통방",
            "정보_소통방",
            "정보",
        ],
    },
]

# =========================
# 인게임 대상 파일 설정
# =========================

INGAME_SOURCES = [
    {
        "source_id": "ingame",
        "file_glob": "llm_messages*.jsonl",
    },
]

# =========================
# Local LLM (llama.cpp) 설정
# =========================

LOCAL_LLM_ENDPOINT = os.getenv("LOCAL_LLM_ENDPOINT", "http://localhost:8080")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "Qwen3.5-9B")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "local").strip().lower()
LLM_JUDGE_ENABLED = os.getenv("LLM_JUDGE_ENABLED", "1") == "1"
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "180"))
LLM_THINKING_MODE = os.getenv("LLM_THINKING_MODE", "1") == "1"
LLM_FORCE_JSON = os.getenv("LLM_FORCE_JSON", "1") == "1"
LLM_KEEP_ALIVE = os.getenv("LLM_KEEP_ALIVE", "30m")
LLM_NUM_CTX = _optional_int_env("LLM_NUM_CTX")
LLM_NUM_PREDICT = _optional_int_env("LLM_NUM_PREDICT")
LLM_TEMPERATURE = _optional_float_env("LLM_TEMPERATURE")
LLM_TOP_P = _optional_float_env("LLM_TOP_P")
LLM_TOP_K = _optional_int_env("LLM_TOP_K")
LLM_PRESENCE_PENALTY = _optional_float_env("LLM_PRESENCE_PENALTY")
LLM_RESPONSE_GREEN_OUTPUT = os.getenv("LLM_RESPONSE_GREEN_OUTPUT", "1") == "1"
LLM_SHOW_THINKING_OUTPUT = os.getenv("LLM_SHOW_THINKING_OUTPUT", "0") == "1"
LLM_THINKING_STATUS_OUTPUT = os.getenv("LLM_THINKING_STATUS_OUTPUT", "1") == "1"
LLM_THINKING_PREVIEW_CHARS = int(os.getenv("LLM_THINKING_PREVIEW_CHARS", "0"))
LLM_DISPLAY_RAW_WITH_THINKING = os.getenv("LLM_DISPLAY_RAW_WITH_THINKING", "1") == "1"
LLM_STORE_RAW_WITH_THINKING = os.getenv("LLM_STORE_RAW_WITH_THINKING", "0") == "1"

# =========================
# Cloud LLM / 알림 설정 예정값
# =========================

CLOUD_LLM_ENDPOINT = os.getenv("CLOUD_LLM_ENDPOINT", "")
CLOUD_LLM_API_KEY = os.getenv("CLOUD_LLM_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", CLOUD_LLM_API_KEY)
OPENAI_ENDPOINT = os.getenv("OPENAI_ENDPOINT", "https://api.openai.com")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", os.getenv("CLOUD_LLM_MODEL", "gpt-5.4"))
OPENAI_MAX_COMPLETION_TOKENS = _optional_int_env("OPENAI_MAX_COMPLETION_TOKENS")
OPENAI_TEMPERATURE = _optional_float_env("OPENAI_TEMPERATURE")
OPENAI_TOP_P = _optional_float_env("OPENAI_TOP_P")
OPENAI_PRESENCE_PENALTY = _optional_float_env("OPENAI_PRESENCE_PENALTY")
# reasoning 모델(gpt-5 계열)에서 thinking 강제/강도. minimal|low|medium|high.
# 기본 high로 2차 검증 시 항상 추론을 수행하게 한다. 비reasoning 모델이면 서버가 무시.
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "high").strip().lower() or None
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", CLOUD_LLM_API_KEY)
ANTHROPIC_ENDPOINT = os.getenv("ANTHROPIC_ENDPOINT", "https://api.anthropic.com")
ANTHROPIC_MODEL = os.getenv(
    "ANTHROPIC_MODEL",
    os.getenv("CLOUD_LLM_MODEL", "claude-sonnet-4-5"),
)
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
ANTHROPIC_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", str(LLM_NUM_PREDICT or 4096)))

# =========================
# 하이브리드 2차 검증 (로컬 1차 alert 시 클라우드 재검증)
# =========================

# 2차 검증 활성화. 1(기본)이면 로컬 should_alert=true 시 클라우드로 재검증한다.
VERIFY_ENABLED = os.getenv("VERIFY_ENABLED", "1") == "1"
# 2차 검증 provider (현재 openai 지원). LLM_PROVIDER(1차)와 독립.
VERIFY_PROVIDER = os.getenv("VERIFY_PROVIDER", "openai").strip().lower()
# 하루 2차 호출 상한(비용 가드). 초과 시 2차 건너뛰고 로컬 판정대로 발송.
VERIFY_DAILY_LIMIT = int(os.getenv("VERIFY_DAILY_LIMIT", "100"))

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_ALERT_ENABLED = os.getenv("SLACK_ALERT_ENABLED", "0") == "1"
SLACK_NOTIFY_ALL = os.getenv("SLACK_NOTIFY_ALL", "0") == "1"
SLACK_NOTIFY_TESTS = os.getenv("SLACK_NOTIFY_TESTS", "0") == "1"
SLACK_TIMEOUT_SEC = int(os.getenv("SLACK_TIMEOUT_SEC", "10"))
SLACK_INTERACTIONS_ENABLED = os.getenv("SLACK_INTERACTIONS_ENABLED", "0") == "1"
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_INTERACTION_HOST = os.getenv("SLACK_INTERACTION_HOST", "127.0.0.1")
SLACK_INTERACTION_PORT = int(os.getenv("SLACK_INTERACTION_PORT", "8787"))
# 상호작용 수신 방식: "socket"(WebSocket, public URL 불필요) | "http"(기존 HTTP 서버 + cloudflared)
SLACK_INTERACTION_MODE = os.getenv("SLACK_INTERACTION_MODE", "socket").strip().lower()
# Socket Mode용 App-Level Token (xapp-...). socket 모드에서 필요.
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
SLACK_SNOOZE_MINUTES = int(os.getenv("SLACK_SNOOZE_MINUTES", "10"))

# Bot Token 방식 (should_alert=True 시 chat.postMessage + thread evidence)
# 미설정 시 기존 Webhook 흐름 사용 (스레드 evidence 비활성).
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
SLACK_EVIDENCE_MAX_PER_SOURCE = int(os.getenv("SLACK_EVIDENCE_MAX_PER_SOURCE", "20"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# =========================
# 점수 / 알림 기준
# =========================

CANDIDATE_SCORE_THRESHOLD = float(os.getenv("CANDIDATE_SCORE_THRESHOLD", "0.50"))
FINAL_CONFIDENCE_THRESHOLD = float(os.getenv("FINAL_CONFIDENCE_THRESHOLD", "0.70"))

ALERT_MIN_SEVERITY = os.getenv("ALERT_MIN_SEVERITY", "medium")
