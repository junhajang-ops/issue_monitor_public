from __future__ import annotations

import json
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
SNAPSHOTS_TRUE_DIR = DATA_DIR / "snapshots_true"
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

# 판정 이력 테이블(local_llm_runs) 보관 일수.
# 이 일수가 지난 행은 매 사이클 prune된다. messages(채팅 working set)는 위
# DB_MESSAGE_RETENTION_MINUTES(분)로 별도 관리되며, 채팅 원본은 외부에 보존된다.
DB_RETENTION_DAYS = int(os.getenv("DB_RETENTION_DAYS", "30"))

# =========================
# 원본 소스 경로
# =========================

KAKAO_BASE_DIR = Path(
    os.getenv(
        "KAKAO_BASE_DIR",
        "C:/Users/user/Documents/XuanZhi9/Pictures/kakao_logs/llm/rooms",
    )
)

INGAME_BASE_DIR = Path(
    os.getenv(
        "INGAME_BASE_DIR",
        "C:/Users/user/Desktop/playwright_chat_reader/output",
    )
)

# =========================
# 카카오톡 대상 방 설정
# =========================

# 방 이름(room_name)·파일명 토큰(filename_contains)은 환경마다 다르고 민감할 수 있어
# 로컬 전용 JSON(kakao_sources.local.json, gitignore)에서 읽는다. 파일이 없으면 아래 placeholder를 사용한다.
# 실제 매칭(discovery._room_matches_by_content)은 room_name이 카톡 export 파일의 room= 헤더와 정확히 일치해야 하므로,
# 각자 환경에 맞게 kakao_sources.local.json을 작성해야 한다. 스키마는 README/.local.json 예시 참고.
def _load_kakao_config() -> dict:
    path = Path(os.getenv("KAKAO_SOURCES_FILE", str(BASE_DIR / "kakao_sources.local.json")))
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):  # 레거시: 소스 리스트만 있는 형태도 허용
                data = {"sources": data, "replay_files": []}
            return data
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] failed to load KAKAO sources from {path}: {exc}")
    return {
        "sources": [
            {"source_id": "kakao_a", "room_name": "커뮤니티 방 이름 A", "filename_contains": ["커뮤니티A"]},
            {"source_id": "kakao_b", "room_name": "커뮤니티 방 이름 B", "filename_contains": ["커뮤니티B"]},
        ],
        "replay_files": [
            ["kakao_a", "커뮤니티A.txt"],
            ["kakao_b", "커뮤니티B.txt"],
        ],
    }


_KAKAO_CONFIG = _load_kakao_config()
KAKAO_SOURCES = _KAKAO_CONFIG.get("sources", [])
# llm_replay/llm_check가 원본 재구성 시 참조하는 실제 파일명 목록.
KAKAO_REPLAY_FILES = _KAKAO_CONFIG.get("replay_files", [])

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
# 이슈 키워드 게이트
# =========================

# 1차 키워드 게이트용 키워드 목록 파일(한 줄에 하나, '#'로 시작하면 주석, 빈 줄 무시).
# 파일이 없거나 비어 있으면 judge.py 내장 기본값을 사용한다. 수정 후 재시작 시 반영.
ISSUE_KEYWORDS_FILE = Path(
    os.getenv("ISSUE_KEYWORDS_FILE", str(BASE_DIR / "issue_keywords.txt"))
)

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

# 2차 검증 활성화. 1(기본)이면 1차 issue_detected=true(또는 키워드 게이트) 시 클라우드로 재검증한다.
# 주의: 0이면 2차 미수행 → (fallback 제거됨) 어떤 것도 발송되지 않는다.
VERIFY_ENABLED = os.getenv("VERIFY_ENABLED", "1") == "1"
# 2차 검증 provider (현재 openai 지원). LLM_PROVIDER(1차)와 독립.
VERIFY_PROVIDER = os.getenv("VERIFY_PROVIDER", "openai").strip().lower()
# 하루 2차 호출 상한(비용 가드). 초과 시 2차 건너뛰고 로컬 판정대로 발송.
VERIFY_DAILY_LIMIT = int(os.getenv("VERIFY_DAILY_LIMIT", "100"))

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_ALERT_ENABLED = os.getenv("SLACK_ALERT_ENABLED", "0") == "1"
SLACK_NOTIFY_ALL = os.getenv("SLACK_NOTIFY_ALL", "0") == "1"
SLACK_NOTIFY_TESTS = os.getenv("SLACK_NOTIFY_TESTS", "0") == "1"
SLACK_TEMP_TO_A = os.getenv("SLACK_TEMP_TO_A", "0") == "1"
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

# Bot Token 방식 (발송 시 chat.postMessage + thread evidence)
# 미설정 시 기존 Webhook 흐름 사용 (스레드 evidence 비활성).
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
SLACK_CHANNEL_A = os.getenv("SLACK_CHANNEL_A", "")
# A 채널(추가 전송) 발송 최소 고유 신고자 수(2차 confirmed 시). reload 대상.
SLACK_CHANNEL_A_MIN_REPORTERS = int(os.getenv("SLACK_CHANNEL_A_MIN_REPORTERS", "4"))
# 카테고리별 신고자 임계 (기본 채널 / 추가 채널). 키 = 정규 카테고리 문자열. reload 대상.
# 기본값 = 현 동작 보존: 기본채널(장애·리스크 2, 결제·핵 3), 추가채널(기존 flat 4, 핵 5).
_a_default = os.getenv("SLACK_CHANNEL_A_MIN_REPORTERS", "4")
SLACK_CHANNEL_MIN_REPORTERS = {
    "서버/접속 장애":   int(os.getenv("SLACK_CHANNEL_MIN_OUTAGE",   "2")),
    "계정/운영 리스크": int(os.getenv("SLACK_CHANNEL_MIN_RISK",      "2")),
    "결제 문제":        int(os.getenv("SLACK_CHANNEL_MIN_PAYMENT",   "3")),
    "핵 신고":          int(os.getenv("SLACK_CHANNEL_MIN_CHEAT",     "3")),
}
SLACK_CHANNEL_A_MIN_REPORTERS_BY_CAT = {
    "서버/접속 장애":   int(os.getenv("SLACK_CHANNEL_A_MIN_OUTAGE",   _a_default)),
    "계정/운영 리스크": int(os.getenv("SLACK_CHANNEL_A_MIN_RISK",      _a_default)),
    "결제 문제":        int(os.getenv("SLACK_CHANNEL_A_MIN_PAYMENT",   _a_default)),
    "핵 신고":          int(os.getenv("SLACK_CHANNEL_A_MIN_CHEAT",     "5")),
}
SLACK_EVIDENCE_MAX_PER_SOURCE = int(os.getenv("SLACK_EVIDENCE_MAX_PER_SOURCE", "20"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# =========================
# 점수 / 알림 기준
# =========================

CANDIDATE_SCORE_THRESHOLD = float(os.getenv("CANDIDATE_SCORE_THRESHOLD", "0.50"))
FINAL_CONFIDENCE_THRESHOLD = float(os.getenv("FINAL_CONFIDENCE_THRESHOLD", "0.70"))

ALERT_MIN_SEVERITY = os.getenv("ALERT_MIN_SEVERITY", "medium")


def min_reporters_base(category: str) -> int:
    """카테고리별 기본 채널 신고자 임계. 미상 카테고리는 2 fallback."""
    return SLACK_CHANNEL_MIN_REPORTERS.get(category, 2)


def min_reporters_a(category: str) -> int:
    """카테고리별 추가(A) 채널 신고자 임계. 미상 카테고리는 SLACK_CHANNEL_A_MIN_REPORTERS fallback."""
    return SLACK_CHANNEL_A_MIN_REPORTERS_BY_CAT.get(category, SLACK_CHANNEL_A_MIN_REPORTERS)


# =========================
# 매 run 재로드(런타임 튜닝값)
# =========================
# 아래 키들은 main.run_cycle 시작 시 reload_config()로 다시 읽혀,
# .env 수정이 main 재시작 없이 다음 사이클부터 반영된다.
# (경로·API 토큰·엔드포인트·소스 목록 등은 reload 대상이 아니다 — 재시작 시에만 반영.)
# 주의: 값/기본값을 바꿀 때는 위의 모듈-레벨 정의와 아래 reload_config()를 함께 수정할 것.
_RELOADABLE_KEYS = (
    "RUN_INTERVAL_SECONDS", "CONTEXT_WINDOW_MINUTES", "NEW_WINDOW_MINUTES",
    "DB_MESSAGE_RETENTION_MINUTES", "SNAPSHOT_RETENTION_RUNS", "LLM_TIMEOUT_SEC",
    "LLM_SHOW_THINKING_OUTPUT", "LLM_THINKING_STATUS_OUTPUT", "LLM_DISPLAY_RAW_WITH_THINKING",
    "LLM_STORE_RAW_WITH_THINKING", "LLM_TEMPERATURE", "LLM_TOP_P", "LLM_TOP_K",
    "LLM_PRESENCE_PENALTY", "LLM_RESPONSE_GREEN_OUTPUT", "SLACK_ALERT_ENABLED",
    "SLACK_NOTIFY_ALL", "SLACK_NOTIFY_TESTS", "SLACK_TEMP_TO_A", "SLACK_INTERACTIONS_ENABLED",
    "SLACK_CHANNEL", "SLACK_CHANNEL_A", "SLACK_CHANNEL_A_MIN_REPORTERS",
    "SLACK_CHANNEL_MIN_REPORTERS", "SLACK_CHANNEL_A_MIN_REPORTERS_BY_CAT",
    "SLACK_EVIDENCE_MAX_PER_SOURCE",
)


def reload_config() -> dict:
    """`.env`를 다시 읽어 런타임 튜닝값(_RELOADABLE_KEYS)을 갱신한다.

    - main.run_cycle 시작 시 호출 → 재시작 없이 다음 사이클부터 반영.
    - 경로·토큰·엔드포인트·소스 목록은 갱신하지 않는다(런타임 변경 비권장).
    - 갱신된 값 dict를 반환(로그용).
    """
    global RUN_INTERVAL_SECONDS, CONTEXT_WINDOW_MINUTES, NEW_WINDOW_MINUTES
    global DB_MESSAGE_RETENTION_MINUTES, SNAPSHOT_RETENTION_RUNS, LLM_TIMEOUT_SEC
    global LLM_SHOW_THINKING_OUTPUT, LLM_THINKING_STATUS_OUTPUT, LLM_DISPLAY_RAW_WITH_THINKING
    global LLM_STORE_RAW_WITH_THINKING, LLM_TEMPERATURE, LLM_TOP_P, LLM_TOP_K
    global LLM_PRESENCE_PENALTY, LLM_RESPONSE_GREEN_OUTPUT, SLACK_ALERT_ENABLED
    global SLACK_NOTIFY_ALL, SLACK_NOTIFY_TESTS, SLACK_TEMP_TO_A, SLACK_INTERACTIONS_ENABLED
    global SLACK_CHANNEL, SLACK_CHANNEL_A, SLACK_CHANNEL_A_MIN_REPORTERS
    global SLACK_CHANNEL_MIN_REPORTERS, SLACK_CHANNEL_A_MIN_REPORTERS_BY_CAT
    global SLACK_EVIDENCE_MAX_PER_SOURCE

    load_dotenv(override=True)

    RUN_INTERVAL_SECONDS = int(os.getenv("RUN_INTERVAL_SECONDS", "300"))
    CONTEXT_WINDOW_MINUTES = int(os.getenv("CONTEXT_WINDOW_MINUTES", "10"))
    NEW_WINDOW_MINUTES = int(os.getenv("NEW_WINDOW_MINUTES", "5"))
    DB_MESSAGE_RETENTION_MINUTES = int(
        os.getenv("DB_MESSAGE_RETENTION_MINUTES", str(CONTEXT_WINDOW_MINUTES))
    )
    SNAPSHOT_RETENTION_RUNS = int(os.getenv("SNAPSHOT_RETENTION_RUNS", "50"))
    LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "180"))
    LLM_SHOW_THINKING_OUTPUT = os.getenv("LLM_SHOW_THINKING_OUTPUT", "0") == "1"
    LLM_THINKING_STATUS_OUTPUT = os.getenv("LLM_THINKING_STATUS_OUTPUT", "1") == "1"
    LLM_DISPLAY_RAW_WITH_THINKING = os.getenv("LLM_DISPLAY_RAW_WITH_THINKING", "1") == "1"
    LLM_STORE_RAW_WITH_THINKING = os.getenv("LLM_STORE_RAW_WITH_THINKING", "0") == "1"
    LLM_TEMPERATURE = _optional_float_env("LLM_TEMPERATURE")
    LLM_TOP_P = _optional_float_env("LLM_TOP_P")
    LLM_TOP_K = _optional_int_env("LLM_TOP_K")
    LLM_PRESENCE_PENALTY = _optional_float_env("LLM_PRESENCE_PENALTY")
    LLM_RESPONSE_GREEN_OUTPUT = os.getenv("LLM_RESPONSE_GREEN_OUTPUT", "1") == "1"
    SLACK_ALERT_ENABLED = os.getenv("SLACK_ALERT_ENABLED", "0") == "1"
    SLACK_NOTIFY_ALL = os.getenv("SLACK_NOTIFY_ALL", "0") == "1"
    SLACK_NOTIFY_TESTS = os.getenv("SLACK_NOTIFY_TESTS", "0") == "1"
    SLACK_TEMP_TO_A = os.getenv("SLACK_TEMP_TO_A", "0") == "1"
    SLACK_INTERACTIONS_ENABLED = os.getenv("SLACK_INTERACTIONS_ENABLED", "0") == "1"
    SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
    SLACK_CHANNEL_A = os.getenv("SLACK_CHANNEL_A", "")
    SLACK_CHANNEL_A_MIN_REPORTERS = int(os.getenv("SLACK_CHANNEL_A_MIN_REPORTERS", "4"))
    _a_default = os.getenv("SLACK_CHANNEL_A_MIN_REPORTERS", "4")
    SLACK_CHANNEL_MIN_REPORTERS = {
        "서버/접속 장애":   int(os.getenv("SLACK_CHANNEL_MIN_OUTAGE",   "2")),
        "계정/운영 리스크": int(os.getenv("SLACK_CHANNEL_MIN_RISK",      "2")),
        "결제 문제":        int(os.getenv("SLACK_CHANNEL_MIN_PAYMENT",   "3")),
        "핵 신고":          int(os.getenv("SLACK_CHANNEL_MIN_CHEAT",     "3")),
    }
    SLACK_CHANNEL_A_MIN_REPORTERS_BY_CAT = {
        "서버/접속 장애":   int(os.getenv("SLACK_CHANNEL_A_MIN_OUTAGE",   _a_default)),
        "계정/운영 리스크": int(os.getenv("SLACK_CHANNEL_A_MIN_RISK",      _a_default)),
        "결제 문제":        int(os.getenv("SLACK_CHANNEL_A_MIN_PAYMENT",   _a_default)),
        "핵 신고":          int(os.getenv("SLACK_CHANNEL_A_MIN_CHEAT",     "5")),
    }
    SLACK_EVIDENCE_MAX_PER_SOURCE = int(os.getenv("SLACK_EVIDENCE_MAX_PER_SOURCE", "20"))

    return {k: globals()[k] for k in _RELOADABLE_KEYS}
