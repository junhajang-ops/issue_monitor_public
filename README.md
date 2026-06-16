# issue_monitor

모바일 게임 커뮤니티(카카오톡)·인게임 채팅을 5분 주기로 수집해, **로컬 LLM 1차 판정 + OpenAI 2차 검증** 하이브리드로 "운영자가 지금 확인해야 할 이슈"만 가려 Slack으로 보내는 파이프라인입니다.

## 설계 개요

커뮤니티 채팅은 대부분 잡담이고 운영 이슈(장애·결제·계정·운영 리스크)는 드물게 섞입니다. 모든 메시지를 고가의 클라우드 LLM에 보내면 비용·지연이 크므로 **2단계 하이브리드**로 처리합니다.

1. **1차 — 로컬 LLM (recall 우선)**: 로컬 llama.cpp(Qwen 계열)가 모든 컨텍스트 윈도우를 넓게 훑어 이슈 가능성을 판정합니다. 비용 0·빠름, 놓치지 않는 것(recall)이 목표라 다소 느슨하게 잡습니다.
2. **키워드 게이트**: 1차가 놓치기 쉬운 신호를 `issue_keywords.txt` 부분문자열 매칭으로 보강해 2차 검증 대상에 포함합니다.
3. **2차 — OpenAI (precision 우선)**: 1차가 alert로 본(또는 게이트에 걸린) 경우에만 OpenAI가 정밀 재판정해 오탐을 걸러냅니다. 드물게만 호출되어 비용이 통제됩니다.
4. **발송**: 최종 confirmed 이슈만 Slack으로. 고유 신고자 수 임계로 채널을 분기하고, Socket Mode 버튼으로 알림을 음소거할 수 있습니다.

## 데이터 흐름

```
카카오톡 / 인게임 로그 파일
  └─ sources   : 파일 탐색 · 방 이름(room= 헤더) 매칭 · 입력 스냅샷 보존
      └─ parsers   : 원본 → NormalizedMessage
          └─ pipeline  : 최근 N분 컨텍스트 윈도우 구성 · 정규화
              └─ llm/judge : 1차 로컬 LLM 판정  (+ issue_keywords 게이트)
                  └─ alert/게이트 시 → 2차 OpenAI 정밀 검증
                      └─ storage : SQLite에 1차·2차 결과 기록
                          └─ alerts : confirmed만 Slack 발송 + 음소거 인터랙션
```

각 사이클(기본 5분, `RUN_INTERVAL_SECONDS`)마다 이 흐름을 1회 수행합니다. `python main.py --once`로 단일 사이클을 디버그할 수 있습니다.

## 컴포넌트 책임

| 경로 | 책임 |
|------|------|
| `main.py` | 주기 루프 진입점 — 한 사이클의 수집→판정→검증→발송 오케스트레이션 |
| `config.py` | `.env` 기반 전역 설정. 일부 값은 매 사이클 재로드 |
| `core/` | 시간(KST)·식별자·`NormalizedMessage` 등 공용 타입·유틸 |
| `sources/` | 원본 파일 탐색(`discovery`)과 입력 스냅샷 보존(`snapshot`). 방 이름을 파일 `room=` 헤더와 대조해 매칭 |
| `parsers/` | 카카오톡 텍스트·인게임 JSONL → `NormalizedMessage` |
| `pipeline/` | 컨텍스트 윈도우(`windowing`, 최근 N분)·정규화(`normalize`) |
| `llm/` | 1차 프롬프트·호출·응답 파싱(`judge`). `should_alert`/`content`/`evidence_message_ids` JSON 강제 |
| `storage/` | SQLite 스키마(`schema.sql`)·read/write(`db`). 1차·2차 판정 결과 보존 |
| `alerts/` | Slack 발송(`slack`, Bot token/Webhook), Socket Mode 음소거 인터랙션(`slack_interactions`), 음소거 상태(`slack_state`) |
| `tools/` | 분석·재실행 도구 — `llm_replay`(실제 재실행), `llm_check`(2차 호출 분석), `run_prompt_samples`(프롬프트 점검) |

## 설정·실행

```bash
pip install -r requirements.txt
cp .env.example .env          # OpenAI/Slack 키·로그 경로 입력
python main.py                # 5분 주기 루프 (--once: 1사이클)
```

- 수집 대상 카카오톡 방은 `kakao_sources.local.json`(없으면 placeholder)에서 읽습니다. `room_name`이 파일의 `room=` 헤더와 정확히 일치해야 매칭됩니다.
- **별도 구성 필요(저장소 미포함)**: llama.cpp 서버 + 로컬 모델, 채팅 수집기(playwright).
- 무인 복구용 자동시작 런처(`start_monitor.ps1`/`.bat`)는 llama-server → 수집기 → llama `/health` 대기 → 메인 순으로 기동합니다(경로는 환경에 맞게 수정).

## 문서

- `docs/SLACK_INTERACTIONS.md` — Slack 음소거 인터랙션(Socket Mode) 설정
- `docs/DB_SCHEMA.md` — SQLite 스키마 상세
