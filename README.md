# issue_monitor

모바일 게임 `모바일 게임`의 카카오톡 커뮤니티·인게임 채팅을 5분 주기로 수집하여,
로컬 LLM(Qwen 계열)에게 운영 알림 대상 이슈 여부를 판정시키고, 필요 시 Slack으로
알림을 발송하는 파이프라인입니다.

판정 정책은 두 사이클 is_new 기준입니다.
한 사이클에서 [계정/운영 리스크] 카테고리가 감지되면 다음 사이클에 신규 evidence가
있어야 알림을 확정합니다. 자세한 흐름은 `PROJECT_HISTORY.md`를 참고하세요.

---

## 빠른 시작

### 1. 환경
```
Python 3.11 권장
.venv 가상환경 사용 권장
```

### 2. 의존성 설치
```bash
# 운영 실행만 필요한 경우
pip install -r requirements.txt

# 시각화·테스트 등 부가 도구까지 필요한 경우
pip install -r requirements-dev.txt
```

### 3. `.env` 설정
프로젝트 루트에 `.env` 파일을 두고 다음 항목을 채웁니다.

| 항목 | 설명 |
|------|------|
| `KAKAO_BASE_DIR` | 카카오톡 로그 루트 경로 |
| `INGAME_BASE_DIR` | 인게임 채팅 로그 루트 경로 |
| `LOCAL_LLM_ENDPOINT` | Ollama 등 로컬 LLM 엔드포인트 (예: `http://localhost:11434`) |
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

### 5. 테스트
```bash
# 단일 사이클 시나리오 매트릭스
python test_all_cases.py

# 두 사이클 is_new 시나리오
python test_two_cycle.py
```
자세한 옵션은 `docs/TEST_COMMANDS.md`를 참고하세요.

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

- `PROJECT_HISTORY.md` — 변경 이력과 의사결정 기록
- `docs/PROMPT_DRAFT_CRITICAL_ALERT.md` — 프롬프트 초안 및 카테고리 정의
- `docs/SLACK_INTERACTIONS.md` — Slack interactivity(음소거 등) 운영 가이드
- `docs/TEST_COMMANDS.md` — 테스트 실행 옵션 모음
