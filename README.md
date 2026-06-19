# issue_monitor

모바일 게임 커뮤니티(카카오톡)·인게임 채팅을 5분 주기로 수집해, **로컬 LLM 1차 판정 + OpenAI 2차 검증** 하이브리드로 "운영자가 지금 확인해야 할 이슈"만 가려 Slack으로 보내는 파이프라인입니다.

## 설계 개요

커뮤니티 채팅은 대부분 잡담이고 운영 이슈(서버/접속 장애·결제 문제·계정/운영 리스크·핵 신고)는 드물게 섞입니다. 모든 메시지를 고가의 클라우드 LLM에 보내면 비용·지연이 크므로 **recall(1차) → precision(2차)** 2단계로 처리합니다.

1. **1차 — 로컬 LLM (recall 전용 트리거)**: 로컬 llama.cpp(Qwen 계열)가 컨텍스트 윈도우를 넓게 훑어 **"이슈 신호가 하나라도 있는가"만** 판정합니다(`issue_detected`). 신고 인원수·임계·심각도는 보지 않으며, 인원이 적다고 일반 대화로 강등하지 않습니다 — 놓치지 않는 것이 목표입니다. 비용 0·빠름.
2. **키워드 게이트 (backstop)**: 1차가 놓쳐도, 신규 메시지에 이슈 키워드(`issue_keywords.txt`)가 있으면 강제로 2차 대상에 포함합니다. 1차 판정과 OR로 결합됩니다.
3. **2차 — OpenAI (precision, 원점 판단)**: 게이트를 통과한 경우에만 호출됩니다. **1차의 분류·요약을 전혀 받지 않고** raw 메시지만으로 처음부터 재분류·유효 신고자 식별·임계 판단을 합니다(1차 해석에 prime되지 않음). 드물게만 호출돼 비용이 통제됩니다.
4. **Python 교차검증 + 발송**: 2차가 confirmed해도, 2차가 지목한 신고자 메시지의 **고유 작성자 수를 코드가 다시 세어** 카테고리별 임계를 충족할 때만 Slack으로 보냅니다. 신고자 수에 따라 채널을 분기하고, Socket Mode 버튼으로 음소거할 수 있습니다.

> **역할 분리**: 1차는 "이슈 가능성"만(broad), 2차는 "진짜 이슈인지"(strict)를 raw에서 독립 판단합니다. **최종 임계 판단의 권위는 Python**에 둡니다 — LLM이 진술한 인원수를 그대로 믿지 않고, 신고자로 지목된 메시지 idx의 고유 작성자 수를 코드가 재계산합니다.

## 판정 단계별 결정

| 단계 | 입력 | 결정하는 것 | 판단하지 않는 것 |
|------|------|------------|------------------|
| 1차(로컬) | 최근 N분 전체 메시지 | 이슈 신호 유무(`issue_detected`) + 근거 idx | 인원수·임계·심각도 |
| 키워드 게이트 | 신규(`is_new`) 메시지 | 이슈 키워드 매칭 시 강제 escalate | — |
| 2차(클라우드) | raw 메시지 (1차 결과 미전달) | 카테고리 재분류·유효 신고자 idx·confirm | 추가 채널 라우팅 임계 |
| Python | 2차가 반환한 신고자 idx | 고유 작성자 재카운트 vs 임계 → 발송 여부·채널 | — |

## 정확도·안정성 장치 (무엇을 어떻게 막는가)

**누락(이슈를 놓침) 방지**
- 1차를 임계 없는 broad 탐지기로 둬, 신고 1명·모호한 표현이어도 2차로 넘깁니다("인원이 적다고 일반 대화로 강등 금지").
- 키워드 게이트가 1차 누락을 backstop — 1차·키워드 둘 중 하나만 걸려도 2차를 호출합니다.

**오탐(잡담을 이슈로) 방지**
- 2차가 raw에서 **원점 재분류**하므로 1차의 잘못된 해석에 휩쓸리지 않습니다.
- 신고 vs 의견 구분: 확률 불만·스펙 부족·난이도 한탄·과금 효율 평가·구매 검토 등은 이슈로 세지 않습니다.
- **시제·현재 상태**: 과거형·이미 해소("지금은 됨")·경향("요즘 자주")·제3자 대리 발화는 제외하고, 본인이 직접 겪고 **현재도 미해소**인 피해만 유효 신고로 셉니다.
- **소스별 동조 신뢰도**: 인게임(직접 목격, 사진 불가)은 동조성 신고를 넓게 인정하고, 외부 커뮤니티(사진·전언이 섞임)는 본인의 직접 경험이 드러난 동조만 인정해 단순 반응 에코를 제외합니다. 신고자 카운트는 방이 아니라 작성자 기준입니다.
- 공지·봇 자동 안내 메시지는 신고에서 제외합니다.
- **임계 + Python 권위**: 카테고리별 고유 신고자 수 임계를 충족해야 발송합니다. 2차가 "N명"이라 주장해도 Python이 신고자 idx의 고유 작성자 수를 재계산해 미달이면 차단합니다(2차 confirm과 Python 재검증 둘 다 통과해야 발송).

**비용·오발송 방지**
- 2차는 게이트 통과 시에만 호출 + 하루 호출 상한(`VERIFY_DAILY_LIMIT`).
- **2차가 응답하지 않으면(오류/비활성/상한) 발송하지 않습니다** — 검증되지 않은 1차 단독 발송을 원천 차단합니다.
- 신규(`is_new`) 메시지 기준 게이트로 같은 신고의 중복 2차 호출·중복 발송을 방지합니다.

## 데이터 흐름

```
카카오톡 / 인게임 로그 파일
  └─ sources   : 파일 탐색 · 방 이름(room= 헤더) 매칭 · 입력 스냅샷 보존
      └─ parsers   : 원본 → NormalizedMessage
          └─ pipeline  : 최근 N분 컨텍스트 윈도우 구성 · 정규화
              └─ llm/judge : 1차 로컬 LLM 이슈 신호 판정  (+ issue_keywords 게이트)
                  └─ issue_detected/게이트 시 → 2차 OpenAI 원점 정밀 검증
                      └─ main : Python 교차검증(고유 신고자 재카운트 vs 임계)
                          └─ storage : SQLite에 1차·2차 결과 기록
                              └─ alerts : confirmed + 임계 통과만 Slack 발송 + 음소거 인터랙션
```

각 사이클(기본 5분, `RUN_INTERVAL_SECONDS`)마다 이 흐름을 1회 수행합니다. `python main.py --once`로 단일 사이클을 디버그하거나, `python main.py --replay <run_id>`로 과거 스냅샷을 재실행할 수 있습니다.

## 컴포넌트 책임

| 경로 | 책임 |
|------|------|
| `main.py` | 주기 루프 진입점 — 한 사이클의 수집→판정→검증→교차검증→발송 오케스트레이션 |
| `config.py` | `.env` 기반 전역 설정. 임계·채널 등 일부 값은 매 사이클 재로드(핫리로드) |
| `core/` | 시간(KST)·식별자·`NormalizedMessage` 등 공용 타입·유틸 |
| `sources/` | 원본 파일 탐색(`discovery`)과 입력 스냅샷 보존(`snapshot`). 방 이름을 파일 `room=` 헤더와 대조해 매칭 |
| `parsers/` | 카카오톡 텍스트·인게임 JSONL → `NormalizedMessage` |
| `pipeline/` | 컨텍스트 윈도우(`windowing`, 최근 N분)·정규화(`normalize`) |
| `llm/` | 1차/2차 프롬프트·호출·응답 파싱(`judge`). 1차는 `issue_detected`, 2차는 `confirmed`/`category`/`reporter_message_ids`/`evidence_message_ids` JSON 강제 |
| `storage/` | SQLite 스키마(`schema.sql`)·read/write(`db`). 1차·2차 판정 결과 보존 |
| `alerts/` | Slack 발송(`slack`, Bot token/Webhook), Socket Mode 음소거 인터랙션(`slack_interactions`), 음소거 상태(`slack_state`) |
| `tools/` | 분석·재실행 도구 — `llm_replay`(실제 재실행), `llm_check`(2차 호출 분석), `run_prompt_samples`(프롬프트 점검), `_replay_core`(재실행 공용 모듈: 스냅샷 재구성) |

## 임계·채널 설정 (env, 카테고리별)

카테고리별 최소 고유 신고자 수를 `.env`로 제어합니다(기본 채널·추가 채널 각각, 핫리로드).

| 카테고리 | 기본 채널 env | 추가(A) 채널 env |
|---|---|---|
| 서버/접속 장애 | `SLACK_CHANNEL_MIN_OUTAGE` | `SLACK_CHANNEL_A_MIN_OUTAGE` |
| 계정/운영 리스크 | `SLACK_CHANNEL_MIN_RISK` | `SLACK_CHANNEL_A_MIN_RISK` |
| 결제 문제 | `SLACK_CHANNEL_MIN_PAYMENT` | `SLACK_CHANNEL_A_MIN_PAYMENT` |
| 핵 신고 | `SLACK_CHANNEL_MIN_CHEAT` | `SLACK_CHANNEL_A_MIN_CHEAT` |

임계 변경은 프롬프트가 아니라 **2차 프롬프트 주입값(기본 채널 임계) + Python 교차검증**에 반영됩니다. 추가 채널 라우팅 임계는 LLM이 보지 않고 Python만 적용합니다.

## 설정·실행

```bash
pip install -r requirements.txt
cp .env.example .env          # OpenAI/Slack 키·로그 경로·임계 입력
python main.py                # 5분 주기 루프 (--once: 1사이클)
```

- 수집 대상 카카오톡 방은 `kakao_sources.local.json`(없으면 placeholder)에서 읽습니다. `room_name`이 파일의 `room=` 헤더와 정확히 일치해야 매칭됩니다.
- **별도 구성 필요(저장소 미포함)**: llama.cpp 서버 + 로컬 모델, 채팅 수집기(playwright).
- 무인 복구용 자동시작 런처(`start_monitor.ps1`/`.bat`)는 llama-server → 수집기 → llama `/health` 대기 → 메인 순으로 기동합니다(경로는 환경에 맞게 수정).

## 문서

- `docs/SLACK_INTERACTIONS.md` — Slack 음소거 인터랙션(Socket Mode) 설정
- `docs/DB_SCHEMA.md` — SQLite 스키마 상세
