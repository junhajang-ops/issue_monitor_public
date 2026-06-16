# issue_monitor

모바일 게임 커뮤니티(카카오톡)·인게임 채팅을 주기적으로 수집해, **로컬 LLM 1차 판정 + OpenAI 2차 검증** 하이브리드로 운영 알림 대상 이슈만 가려 Slack으로 보내는 파이프라인입니다.

- 로컬 LLM(llama.cpp, Qwen 계열)이 1차로 넓게 판정(recall)
- alert일 때만 OpenAI가 2차로 정밀 검증(precision)
- 확정 시 Slack 발송 (Socket Mode 음소거 인터랙션 포함)

## 구조

| 경로 | 역할 |
|------|------|
| `main.py` | 5분 주기 메인 루프 |
| `config.py` | 환경변수 기반 전역 설정 |
| `core/` | 시간·식별자·데이터 모델 |
| `sources/` | 카카오톡·인게임 파일 탐색·스냅샷 |
| `parsers/` | 원본 → NormalizedMessage 변환 |
| `pipeline/` | 컨텍스트 윈도우·정규화 |
| `llm/` | 프롬프트·호출·응답 파싱 |
| `storage/` | SQLite 스키마·read/write |
| `alerts/` | Slack 발송·상호작용·상태 |
| `tools/` | 분석·리플레이 스크립트 |

## 설정

```bash
pip install -r requirements.txt
cp .env.example .env          # 키·경로 값을 채웁니다
```

- `.env` — OpenAI/Slack 키, 로그 경로 등 (예시: `.env.example`)
- `kakao_sources.local.json` — 수집할 카카오톡 방 이름/파일 토큰. 없으면 placeholder가 쓰입니다.
  ```json
  {
    "sources": [
      {"source_id": "kakao_a", "room_name": "방 이름", "filename_contains": ["파일명토큰"]}
    ],
    "replay_files": [["kakao_a", "파일명.txt"]]
  }
  ```
- 별도 구성 필요(저장소 미포함): llama.cpp 서버 + 로컬 모델, 채팅 수집기(playwright).

## 실행

```bash
python main.py            # 5분 주기 루프
python main.py --once     # 1사이클(디버그)
```

분석·리플레이는 `tools/`의 `llm_replay.py`, `llm_check.py`, `run_prompt_samples.py` 참고.

## 문서

- `docs/SLACK_INTERACTIONS.md` — Slack 음소거 인터랙션(Socket Mode)
- `docs/DB_SCHEMA.md` — SQLite 스키마
