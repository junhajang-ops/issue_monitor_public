# Slack 음소거 인터랙션 설정

ALERT(알림) Slack 메시지에는 1단계 버튼이 함께 발송됩니다.

1단계:

- `음소거`
- `음소거 해제`

`음소거`를 선택하면 2단계 시간 선택 버튼이 Slack에 추가로 표시됩니다.

2단계:

- `10분 음소거`
- `1시간 음소거`
- `3시간 음소거`
- `6시간 음소거`
- `24시간 음소거`
- `72시간 음소거`

동작 흐름:

1. Slack이 **Socket Mode(웹소켓)**로 클릭 이벤트를 이 프로젝트에 전달합니다.
2. 프로젝트가 이벤트를 처리합니다.
3. `음소거`를 누르면 시간 선택 버튼을 Slack에 다시 보냅니다.
4. 시간을 선택하면 해당 시간만큼 ALERT(알림) 메시지를 중지합니다.
5. `음소거 해제`를 누르면 저장된 음소거 상태를 즉시 삭제합니다.
6. 음소거 시간이 끝나면 `alert 알림 중지가 종료되어 재개되었습니다` 메시지를 보냅니다.

## 1. Slack 앱 설정 (Socket Mode)

현재 인터랙션 수신은 **Socket Mode(웹소켓)** 기본입니다. 과거 HTTP + 공개 URL(Cloudflare Tunnel/ngrok) 방식은 더 이상 필요하지 않습니다.

Slack App 관리 화면에서:

1. `Socket Mode` → **On**
2. `Basic Information` → `App-Level Tokens` → `connections:write` scope로 토큰 생성 (`xapp-...`)
3. `Interactivity & Shortcuts` → `Interactivity` **On** (Socket Mode에서는 **Request URL 입력 불필요**)
4. `.env`에 추가:

```env
SLACK_INTERACTIONS_ENABLED=1
SLACK_INTERACTION_MODE=socket
SLACK_APP_TOKEN=<your-xapp-token-here>
```

> **공개 HTTPS 주소(cloudflared/ngrok)는 불필요**합니다. Socket Mode가 Slack과 아웃바운드 웹소켓으로 직접 연결합니다.

## 2. 서버 실행

`main.py` 실행 시 Socket Mode 클라이언트가 함께 켜집니다.

```bash
python main.py
```

`slack_sdk`가 필요합니다(`requirements.txt`에 포함). `SLACK_APP_TOKEN`이 비어 있으면 클라이언트가 시작되지 않습니다.

## 주의

- `SLACK_INTERACTIONS_ENABLED=0`이면 버튼이 메시지에 보이더라도 클릭 처리는 되지 않습니다.
- `SLACK_APP_TOKEN`(`xapp-`)이 비어 있으면 Socket Mode 클라이언트가 시작되지 않습니다.
- 음소거는 ALERT(알림) 메시지만 막습니다. `SLACK_NOTIFY_ALL=1` 상태에서는 비알림(임시) 메시지는 계속 전송될 수 있습니다.
- 운영 루프는 프로세스가 계속 살아 있으므로 음소거 종료 시 재개 메시지가 정상 동작합니다.
- (레거시) HTTP 방식은 `SLACK_INTERACTION_MODE=http` + `SLACK_SIGNING_SECRET` + 공개 URL로도 동작하지만, 기본·권장은 Socket Mode입니다.
