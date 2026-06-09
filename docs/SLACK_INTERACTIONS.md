# Slack 음소거 인터랙션 설정

`should_alert=true` Slack 메시지에는 1단계 버튼이 함께 발송됩니다.

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

1. Slack이 이 프로젝트의 `/slack/interactions` 주소로 클릭 이벤트를 보냅니다.
2. 프로젝트가 Slack 서명을 검증합니다.
3. `음소거`를 누르면 시간 선택 버튼을 Slack에 다시 보냅니다.
4. 시간을 선택하면 해당 시간만큼 `should_alert=true` 알림을 중지합니다.
5. `음소거 해제`를 누르면 저장된 음소거 상태를 즉시 삭제합니다.
6. 음소거 시간이 끝나면 `alert 알림 중지가 종료되어 재개되었습니다` 메시지를 보냅니다.

## 1. Slack 앱 설정

Slack App 관리 화면에서:

1. `Basic Information` -> `App Credentials`
2. `Signing Secret` 복사
3. `.env`에 추가

```env
SLACK_INTERACTIONS_ENABLED=1
SLACK_SIGNING_SECRET=복사한_SIGNING_SECRET
SLACK_INTERACTION_HOST=127.0.0.1
SLACK_INTERACTION_PORT=8787
```

## 2. 공개 HTTPS 주소 준비

Slack은 로컬 `127.0.0.1`로 직접 요청할 수 없습니다. 아래 중 하나로 공개 HTTPS 주소를 만들어야 합니다.

Cloudflare Tunnel 예시:

```powershell
cloudflared tunnel --protocol http2 --url http://127.0.0.1:8787
```

ngrok 예시:

```powershell
ngrok http 8787
```

생성된 HTTPS 주소 뒤에 `/slack/interactions`를 붙입니다.

```text
https://example-tunnel.trycloudflare.com/slack/interactions
```

## 3. Slack Interactivity 켜기

Slack App 관리 화면에서:

1. `Interactivity & Shortcuts`
2. `Interactivity`를 On
3. `Request URL`에 공개 URL 입력
4. `Save Changes`

## 4. 서버 실행

main 실행 시 인터랙션 서버가 함께 켜집니다.

```powershell
python main.py
```

테스트 실행 시에도 함께 켜집니다.

```powershell
python test_all_cases.py c08 -r 1 --variants v3
```

실행 로그에 아래처럼 나오면 수신 서버가 켜진 상태입니다.

```text
[SLACK INTERACTION] listening=http://127.0.0.1:8787/slack/interactions
```

## 주의

- `SLACK_INTERACTIONS_ENABLED=0`이면 버튼은 메시지에 보이더라도 클릭 처리는 되지 않습니다.
- `SLACK_SIGNING_SECRET`이 비어 있으면 인터랙션 서버가 시작되지 않습니다.
- 음소거는 `should_alert=true` 메시지만 막습니다. `SLACK_NOTIFY_ALL=1` 상태에서는 false 임시 알림은 계속 전송될 수 있습니다.
- 테스트 프로세스가 음소거 종료 전에 종료되면 재개 메시지를 보낼 수 없습니다. 운영 루프에서는 프로세스가 계속 살아 있으므로 정상 동작합니다.
