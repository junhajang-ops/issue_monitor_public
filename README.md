# issue_monitor

모바일 게임 `모바일 게임`의 카카오톡 커뮤니티·인게임 채팅을 5분 주기로 수집하여,
로컬 LLM(Qwen 계열)에게 운영 알림 대상 이슈 여부를 판정시키고, 필요 시 Slack으로
알림을 발송하는 파이프라인입니다.

판정은 하이브리드 단일 사이클입니다.
로컬 LLM이 1차로 넓게 판정(recall)하고, alert 시 OpenAI가 2차로 정밀 검증(precision)해
최종 확정합니다. 자세한 흐름은 `docs/PROJECT_HISTORY.md`를 참고하세요.

---

## 빠른 시작

### 1. 환경
```
Python 3.11 권장
.venv 가상환경 사용 권장
```

### 2. 의존성 설치
```bash
pip install -r requirements.txt
```

### 3. `.env` 설정
`.env.example`을 복사해 `.env`를 만들고, `<your-...-here>` 자리의 키를 실제 값으로 채웁니다.
```bash
cp .env.example .env                 # Windows PowerShell: Copy-Item .env.example .env
```
주요 항목:

| 항목 | 설명 |
|------|------|
| `KAKAO_BASE_DIR` | 카카오톡 로그 루트 경로 |
| `INGAME_BASE_DIR` | 인게임 채팅 로그 루트 경로 |
| `LOCAL_LLM_ENDPOINT` | 로컬 LLM(llama.cpp) 엔드포인트 (예: `http://localhost:8080`) |
| `OPENAI_API_KEY` | 2차 검증용 OpenAI API 키 (`VERIFY_ENABLED=1`일 때 필요) |
| `LOCAL_LLM_MODEL` | 로컬 LLM 모델 이름 |
| `LLM_TIMEOUT_SEC` | LLM 요청 타임아웃 초 (기본 180) |
| `SLACK_BOT_TOKEN` | Slack bot 토큰 (없으면 Webhook으로 폴백) |
| `SLACK_CHANNEL` | 알림 채널 ID |
| `SLACK_WEBHOOK_URL` | (선택) Webhook URL |
| `SLACK_ALERT_ENABLED` | `1`이면 실제 알림 발송 |

전체 항목은 `config.py`를 참조하세요.

### 4. 실행
```bash
# 5분 주기 무한 루프
python main.py

# 1사이클만 실행 (디버그용)
python main.py --once
```

### 5. 테스트/검증
```powershell
# 특정 run을 실제 재실행(1차 로컬 → 2차 OpenAI → Slack 발송). 스냅샷 없으면 원본 재구성.
.venv\Scripts\python.exe tools\llm_replay.py 20260604_011503   # 인자 없으면 회귀 7종 전체
$env:REPLAY_ROUNDS='3'; .venv\Scripts\python.exe tools\llm_replay.py 20260604_011503  # 반복

# 합성 시나리오 9종으로 1차 프롬프트 점검(--dry-run 으로 프롬프트만)
.venv\Scripts\python.exe tools\run_prompt_samples.py --case server_outage_many_users

# 2차 검증(OpenAI) 호출 분석 — 경로별 목록 + 입력 메시지 조회(스냅샷 만료 run은 자동 재구성)
.venv\Scripts\python.exe tools\llm_check.py            # 2차 호출한 최근 50 run([번호] 표시)
.venv\Scripts\python.exe tools\llm_check.py keyword    # 키워드 게이트로 2차 호출만
.venv\Scripts\python.exe tools\llm_check.py local      # 로컬 모델(should_alert)로 2차 호출만
.venv\Scripts\python.exe tools\llm_check.py 1 3 10     # 직전 목록의 1·3·10번 입력 메시지 전체(run_id 직접도 가능)
```

### 6. 재부팅 시 자동 실행

PC 재부팅(Windows Update 등) 후 파이프라인을 무인 복구하도록 자동 로그온 + Startup 런처가 구성되어 있습니다.

| 구성 | 내용 |
|------|------|
| `start_monitor.ps1` | llama-server → playwright(login_check.py) → llama `/health` 대기 → issue_monitor 순차 기동. 각 컴포넌트 중복 가드 포함 |
| `start_monitor.bat` | 위 스크립트를 ExecutionPolicy 우회로 실행하는 런처 |
| Startup 등록 | `start_monitor.bat`가 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`에 복사됨 |
| 자동 로그온 | 레지스트리 `Winlogon\AutoAdminLogon=1` (비밀번호 없는 `user` 계정) |

수동 실행도 가능합니다:
```powershell
powershell -ExecutionPolicy Bypass -File start_monitor.ps1
```

**주의**
- 카카오 앱플레이어는 **앱플레이어 자체 설정으로 자동 실행**됩니다(부팅 시 자동 기동).
- Slack 음소거 상호작용은 **Socket Mode(웹소켓)**로 동작하여 cloudflared/공개 URL이 **불필요**합니다(기존 cloudflared 터널 방식 대체).
- playwright는 `chrome_profile` 로그인 세션이 유효해야 무인 수집됩니다. 만료 시 `first_login.py`로 재로그인하세요.

---

## 폴더 구조

| 경로 | 역할 |
|------|------|
| `main.py` | 5분 주기 메인 루프 진입점 |
| `config.py` | 환경변수 기반 전역 설정 |
| `core/` | 시간·식별자·데이터 모델 등 공용 타입 |
| `sources/` | 카카오톡·인게임 파일 탐색과 스냅샷 보존 |
| `parsers/` | 원본 파일을 NormalizedMessage로 변환 |
| `pipeline/` | 컨텍스트 윈도우·정규화 파이프라인 |
| `llm/` | LLM 프롬프트·호출·응답 파싱 |
| `storage/` | SQLite 스키마와 read/write 유틸 |
| `alerts/` | Slack 발송·상호작용·상태 저장 |
| `tools/` | 분석·디버깅 스크립트 |
| `docs/` | 프롬프트 초안, Slack 가이드, 테스트 명령어 모음 |
| `data/` | 운영 SQLite DB, 스냅샷, 상태 파일 (gitignore 대상) |

---

## 참고 문서

- `docs/PROJECT_HISTORY.md` — 변경 이력과 의사결정 기록
- `docs/PROMPT_DRAFT_CRITICAL_ALERT.md` — 프롬프트 초안 및 카테고리 정의
- `docs/SLACK_INTERACTIONS.md` — Slack interactivity(음소거 등) 운영 가이드
- `tools/llm_replay.py` — 특정 run을 1차→2차로 실제 재실행(스냅샷 없으면 원본 재구성, Slack 발송 포함)
- `tools/llm_check.py` — 2차 검증 호출을 경로별(로컬/키워드/로컬+키워드)로 분석 + 입력 메시지 조회(스냅샷 만료 run은 자동 재구성)
- `tools/run_prompt_samples.py` — 합성 시나리오 9종으로 1차 프롬프트 점검(`SAMPLE_CASES`)
- `tools/_replay_core.py` — run 입력 재구성 공용 로직(llm_replay·llm_check 공유)
- `issue_keywords.txt` — 1차 키워드 게이트용 키워드 목록(편집 가능, **매 사이클 자동 재로드** → main 재시작 불필요)
