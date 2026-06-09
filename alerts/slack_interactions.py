from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import requests

import config
from alerts.slack_state import clear_alert_snooze, get_alert_snooze_until, set_alert_snooze

MUTE_MENU_ACTION_ID = "issue_monitor_mute_menu"
MUTE_DURATION_ACTION_ID = "issue_monitor_mute_duration"
UNMUTE_ACTION_ID = "issue_monitor_unmute"
UNMUTE_OPTION_VALUE = "unmute"
MUTE_OPTIONS = {
    "mute_10m": ("\u0031\u0030\ubd84 \uc74c\uc18c\uac70", 10),
    "mute_60m": ("\u0031\uc2dc\uac04 \uc74c\uc18c\uac70", 60),
    "mute_180m": ("\u0033\uc2dc\uac04 \uc74c\uc18c\uac70", 180),
    "mute_360m": ("\u0036\uc2dc\uac04 \uc74c\uc18c\uac70", 360),
    "mute_1440m": ("\u0032\u0034\uc2dc\uac04 \uc74c\uc18c\uac70", 1440),
    "mute_2880m": ("\u0034\u0038\uc2dc\uac04 \uc74c\uc18c\uac70", 2880),
}
_server: ThreadingHTTPServer | None = None


def _verify_slack_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    if not config.SLACK_SIGNING_SECRET:
        return False
    try:
        request_ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - request_ts) > 60 * 5:
        return False

    base = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    digest = hmac.new(
        config.SLACK_SIGNING_SECRET.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()
    expected = "v0=" + digest
    return hmac.compare_digest(expected, signature)


def _post_response_url(response_url: str, text: str) -> None:
    if not response_url:
        return
    _post_response_payload(
        response_url,
        {"replace_original": False, "response_type": "in_channel", "text": text},
    )


def _post_response_payload(response_url: str, payload: dict) -> None:
    if not response_url:
        return
    try:
        response = requests.post(
            response_url,
            json=payload,
            timeout=config.SLACK_TIMEOUT_SEC,
        )
        body_preview = (response.text or "")[:300]
        print(
            "[SLACK INTERACTION] "
            f"response_status={response.status_code}, "
            f"payload_keys={sorted(payload.keys())}, "
            f"body={body_preview!r}"
        )
    except requests.RequestException as exc:
        print(f"[SLACK INTERACTION] response_error={exc}")


def _duration_blocks() -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\uc74c\uc18c\uac70 \uc2dc\uac04\uc744 \uc120\ud0dd\ud574\uc8fc\uc138\uc694.",
            },
        },
        {
            "type": "actions",
            "block_id": "issue_monitor_mute_duration_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": f"{MUTE_DURATION_ACTION_ID}_{value}",
                    "text": {
                        "type": "plain_text",
                        "text": label,
                        "emoji": True,
                    },
                    "value": value,
                }
                for value, (label, _minutes) in MUTE_OPTIONS.items()
            ],
        },
    ]


def _schedule_resume_message(response_url: str, until_iso: str) -> None:
    def notify_resume() -> None:
        until = get_alert_snooze_until()
        if not until or until.isoformat() != until_iso:
            return
        now_text = datetime_now_label()
        _post_response_url(
            response_url,
            f":bell: [issue_monitor] alert \uc54c\ub9bc \uc911\uc9c0\uac00 \uc885\ub8cc\ub418\uc5b4 \uc7ac\uac1c\ub418\uc5c8\uc2b5\ub2c8\ub2e4. ({now_text})",
        )

    until = get_alert_snooze_until()
    delay = max(0, int((until.timestamp() - time.time()) if until else 0))
    timer = threading.Timer(delay, notify_resume)
    timer.daemon = True
    timer.start()


def datetime_now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S KST", time.localtime())


def handle_payload(payload: dict) -> str:
    """Slack interaction payload 처리. HTTP/Socket Mode 양쪽에서 재사용."""
    for action in payload.get("actions", []):
        action_id = action.get("action_id")
        if action_id == MUTE_MENU_ACTION_ID:
            response_url = str(payload.get("response_url") or "")
            print(
                "[SLACK INTERACTION] "
                f"mute_menu_clicked=true, has_response_url={bool(response_url)}"
            )
            feedback_thread = threading.Thread(
                target=_post_response_payload,
                args=(
                    response_url,
                    {
                        "replace_original": False,
                        "response_type": "in_channel",
                        "text": "음소거 시간을 선택해주세요.",
                        "blocks": _duration_blocks(),
                    },
                ),
                daemon=True,
            )
            feedback_thread.start()
            return "ok"

        if not action_id.startswith(MUTE_DURATION_ACTION_ID) and action_id != UNMUTE_ACTION_ID:
            continue

        user = payload.get("user", {})
        user_label = user.get("username") or user.get("name") or user.get("id") or ""
        response_url = str(payload.get("response_url") or "")

        if action_id == UNMUTE_ACTION_ID or action.get("value") == UNMUTE_OPTION_VALUE:
            clear_alert_snooze(user_label=str(user_label))
            feedback_text = (
                ":bell: alert 음소거를 해제했습니다. "
                "이제 should_alert=true 알림이 다시 전송됩니다."
            )
            feedback_thread = threading.Thread(
                target=_post_response_url,
                args=(response_url, feedback_text),
                daemon=True,
            )
            feedback_thread.start()
            print(f"[SLACK INTERACTION] unmute=true, user={user_label}")
            return "ok"

        value = str(action.get("value") or "")
        if value not in MUTE_OPTIONS:
            return "ignored"

        label, minutes = MUTE_OPTIONS[value]
        until = set_alert_snooze(minutes=minutes, user_label=str(user_label))
        feedback_text = (
            f":mute: {label}를 설정했습니다. "
            f"재개 예정: {until.strftime('%Y-%m-%d %H:%M:%S KST')}"
        )
        feedback_thread = threading.Thread(
            target=_post_response_url,
            args=(response_url, feedback_text),
            daemon=True,
        )
        feedback_thread.start()
        _schedule_resume_message(response_url, until.isoformat())
        print(
            "[SLACK INTERACTION] "
            f"mute=true, snooze_minutes={minutes}, user={user_label}"
        )
        return "ok"
    return "ignored"


class SlackInteractionHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/slack/interactions":
            self._send_text(404, "not found")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length)
        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")
        if not _verify_slack_signature(raw_body, timestamp, signature):
            self._send_text(401, "invalid signature")
            return

        form = parse_qs(raw_body.decode("utf-8"))
        payload_text = (form.get("payload") or [""])[0]
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            self._send_text(400, "invalid payload")
            return

        handled = handle_payload(payload)
        self._send_text(200, handled)


def start_slack_interaction_server() -> None:
    global _server
    if _server is not None:
        return
    if not config.SLACK_INTERACTIONS_ENABLED:
        print("[SLACK INTERACTION] skipped: SLACK_INTERACTIONS_ENABLED=0")
        return
    if not config.SLACK_SIGNING_SECRET:
        print("[SLACK INTERACTION] skipped: SLACK_SIGNING_SECRET is empty")
        return

    _server = ThreadingHTTPServer(
        (config.SLACK_INTERACTION_HOST, config.SLACK_INTERACTION_PORT),
        SlackInteractionHandler,
    )
    thread = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()
    print(
        "[SLACK INTERACTION] "
        f"listening=http://{config.SLACK_INTERACTION_HOST}:{config.SLACK_INTERACTION_PORT}/slack/interactions"
    )


_socket_client = None


def start_socket_mode_client() -> None:
    """Slack Socket Mode(WebSocket) 연결. public URL/cloudflared 불필요.

    Slack이 로컬로 HTTP POST를 보내는 대신, 로컬이 Slack으로 WebSocket을 연다.
    버튼 클릭(interactive) payload는 기존 handle_payload()로 그대로 처리한다.
    """
    global _socket_client
    if _socket_client is not None:
        return
    if not config.SLACK_INTERACTIONS_ENABLED:
        print("[SLACK INTERACTION] skipped: SLACK_INTERACTIONS_ENABLED=0")
        return
    if not config.SLACK_APP_TOKEN:
        print("[SLACK INTERACTION] skipped: SLACK_APP_TOKEN is empty")
        return

    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web import WebClient
    except ImportError as exc:
        print(f"[SLACK INTERACTION] slack_sdk import failed, socket mode unavailable: {exc}")
        return

    web = WebClient(token=config.SLACK_BOT_TOKEN) if config.SLACK_BOT_TOKEN else None
    client = SocketModeClient(app_token=config.SLACK_APP_TOKEN, web_client=web)

    def _on_request(c, req):  # type: ignore[no-untyped-def]
        try:
            # 3초 내 ack 필수(미수신 시 Slack 재전송). ack 먼저 보내고 처리.
            c.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id)
            )
            if req.type == "interactive":
                handle_payload(req.payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[SLACK INTERACTION] socket handler error: {exc}")

    client.socket_mode_request_listeners.append(_on_request)
    client.connect()
    _socket_client = client
    print("[SLACK INTERACTION] socket mode connected (app-level token, no public URL needed)")
