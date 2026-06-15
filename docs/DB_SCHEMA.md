# DB 스키마 상세 (`data/issue_monitor.sqlite3`)

SQLite, **활성 테이블 2개**: `local_llm_runs`(사이클 판정 이력)·`messages`(메시지 원본).

보존(retention): `local_llm_runs`는 `DB_RETENTION_DAYS`(기본 30일), `messages`는 `DB_MESSAGE_RETENTION_MINUTES` 경과분을 매 사이클 prune.

---

## 1. `local_llm_runs` — 사이클당 1행 (1차+2차 통합 판정 이력) ✅활성

한 사이클(메시지 수집 → 1차 로컬 판정 → 키워드 게이트 → 2차 OpenAI 검증 → Slack 발송)의 결과·메타를 한 행에 저장한다.
`INSERT OR REPLACE`로 `run_id` 기준 저장(`storage/db.py: insert_local_llm_run`).

### 기본/윈도우
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `run_id` | TEXT | 사이클 식별자 `YYYYMMDD_HHMMSS`(스냅샷 폴더명과 동일). PK 역할 |
| `window_start` | TEXT | NEW 윈도우 시작 = `now − NEW_WINDOW_MINUTES`(이 이후 메시지를 '신규(is_new)'로 간주) |
| `window_end` | TEXT | 사이클 실행 시각(`now`) |
| `context_window_start` | TEXT | 컨텍스트 윈도우 시작(= DB 조회 cutoff). 1차 입력 메시지를 모으는 시작 경계 |
| `message_count` | INTEGER | 1차에 입력된 메시지 수(컨텍스트 윈도우 내 `recent_rows` 길이) |
| `new_message_count` | INTEGER | 이번 사이클에 **새로 수집(insert)된** 메시지 수(dedup 후). NEW 윈도우 내 개수와는 다름 |
| `status` | TEXT | 1차 로컬 LLM 호출 상태: `ok` / `error` / `timeout` 등 |
| `error` | TEXT | 1차 에러 메시지(정상 시 NULL) |
| `created_at` | TEXT | 행 기록 시각(KST ISO) |

### 1차 판정
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `has_possible_issue` | INTEGER | 1차 `should_alert` 결과(1=alert, 0=아님, NULL=판정불가). 키워드 게이트 override 반영 |

### 1차 원문/토큰
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `raw_response` | TEXT | 1차 로컬 LLM 응답 원문(thinking 포함 가능 → 수백 줄일 수 있음). 마지막 JSON이 판정 결과 |
| `llm_prompt_tokens` | INTEGER | 1차 프롬프트 토큰 |
| `llm_cached_prompt_tokens` | INTEGER | 1차 캐시된 프롬프트 토큰 |
| `llm_completion_tokens` | INTEGER | 1차 생성 토큰 |
| `llm_reasoning_tokens` | INTEGER | 1차 reasoning 토큰(해당 모델만) |
| `llm_output_tokens` | INTEGER | 1차 출력 토큰 |
| `llm_total_tokens` | INTEGER | 1차 총 토큰 |
| `llm_prompt_chars` | INTEGER | 1차 프롬프트 문자 수 |
| `llm_response_chars` | INTEGER | 1차 응답 문자 수 |
| `llm_thinking_chars` | INTEGER | 1차 thinking 문자 수 |
| `llm_token_usage_json` | TEXT | 1차 토큰 사용량 원본 JSON |

### 2차 검증(OpenAI) 결과
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `cloud_verify_status` | TEXT | 2차 호출 상태: `ok`/`no_key`/`error`/`parse_error`/`skipped_provider`. **NULL이면 2차 미호출**(1차 alert·게이트 미통과) |
| `cloud_verified` | INTEGER | 2차 최종 판정: 1=confirmed(발송), 0=rejected(차단), NULL=2차 미호출/비정상 |
| `cloud_verify_reason` | TEXT | 2차 판정 사유(한국어 한 줄). 당시 신고자 수 등 근거 서술 — **사후 분석에 매우 유용** |
| `cloud_prompt_tokens` | INTEGER | 2차 프롬프트 토큰 |
| `cloud_completion_tokens` | INTEGER | 2차 생성 토큰 |
| `cloud_total_tokens` | INTEGER | 2차 총 토큰 |
| `cloud_raw_json` | TEXT | **2차 응답 전체 JSON**(`confirmed`·`reason`·`reporter_message_ids`·`evidence_message_ids` 등). 2026-06-15 추가 → 그 이전 run은 NULL |
| `cloud_reporter_count` | INTEGER | **2차가 카운트한 고유 신고자 수**(A 채널 추가 전송 임계 `SLACK_CHANNEL_A_MIN_REPORTERS` 판단 근거). 발송 시점 계산값 보존. 2026-06-15 추가 → 이전 run은 NULL |

---

## 2. `messages` — 메시지 원본 ✅활성

수집한 채팅 메시지를 `message_id` PK로 dedup 저장(`storage/db.py: insert_messages`). 기존 행은 덮어쓰지 않음.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `message_id` | TEXT | 메시지 고유 ID(중복 제거 PK) |
| `source_id` | TEXT | 출처 채널: `ingame` / `kakao_a` / `kakao_b` |
| `timestamp` | TEXT | 메시지 작성 시각(ISO KST) |
| `sender` | TEXT | 작성자 표시명 |
| `text` | TEXT | 정규화된 본문(판정 입력) |
| `is_new` | INTEGER | NEW 윈도우 내 메시지면 1, 아니면 0(수집 시점 기준) |
| `first_seen_at` | TEXT | 최초 수집 시각 |
| `run_id` | TEXT | 이 메시지를 처음 수집한 사이클 |
| `raw_text` | TEXT | 정규화 전 원본 텍스트 |

---

## 조회

`python tools/llm_check.py <run_id>` 로 특정 run의 위 모든 필드를 한 번에 확인할 수 있다
(`cloud_reporter_count`·`cloud_verify_reason` 포함, 2차 재호출·비용 없이 DB만 읽음).

---

## 삭제 이력 (2026-06-15)

아래 컬럼·테이블은 2026-06-15에 삭제됨(MVP 잔재·미구현, 모두 0건/NULL).

**`local_llm_runs` 삭제 컬럼**: `local_llm_score`, `rule_score`, `source_correlation_score`, `candidate_score` (초기 규칙기반 스코어 설계 잔재, 항상 NULL)

**삭제 테이블**: `cloud_llm_runs`(2차 전용 테이블 설계, INSERT 코드 없음 → `cloud_*` 컬럼으로 통합), `alerts`(발송 이력 테이블 설계, 미구현), `issue_states`(이슈 상태 추적 설계, 미구현)
