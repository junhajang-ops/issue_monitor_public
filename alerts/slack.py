from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Iterable

import requests

import config
from alerts.slack_interactions import MUTE_MENU_ACTION_ID, UNMUTE_ACTION_ID, UNMUTE_OPTION_VALUE
from alerts.slack_state import get_alert_snooze_remaining_seconds, is_alert_snoozed

SLACK_API_BASE = "https://slack.com/api"

# P1-4: 짧은 일시 오류에만 1회 재시도. 인증/요청 자체 문제는 즉시 실패.
_RETRY_BACKOFF_SEC = 1.0
_RETRYABLE_STATUS = {500, 502, 503, 504}
_NO_RETRY_STATUS = {400, 401, 403, 429}


def _truncate(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + " ... [truncated]"


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _extract_category(content: str) -> str:
    """LLM content에서 '카테고리: X.' 형태의 첫 문장만 추출.

    LLM 응답 content는 보통 '카테고리: 일반 대화. ...' 형태로 시작한다.
    Slack 메시지에는 카테고리 줄만 표시하고 나머지 설명은 생략한다.
    """
    if not content:
        return ""
    stripped = content.lstrip()
    if not stripped.startswith("카테고리"):
        return ""
    # 첫 마침표(.)까지 잘라서 반환. 없으면 첫 줄/전체.
    dot = stripped.find(".")
    if dot != -1:
        return stripped[: dot + 1].rstrip()
    nl = stripped.find("\n")
    if nl != -1:
        return stripped[:nl].rstrip()
    return stripped.rstrip()


def should_send_slack(*, should_alert: bool, is_test: bool = False) -> bool:
    if not config.SLACK_ALERT_ENABLED:
        return False
    if is_test and not config.SLACK_NOTIFY_TESTS:
        return False
    if should_alert and is_alert_snoozed():
        remaining = get_alert_snooze_remaining_seconds()
        print(f"[SLACK] skipped: alert_snoozed_remaining_sec={remaining}")
        return False
    return bool(should_alert or config.SLACK_NOTIFY_ALL)


def _build_blocks(text: str, *, should_alert: bool) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate(text, 2800),
            },
        }
    ]
    if should_alert:
        blocks.append(
            {
                "type": "actions",
                "block_id": "issue_monitor_alert_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": MUTE_MENU_ACTION_ID,
                        "text": {
                            "type": "plain_text",
                            "text": "\uc74c\uc18c\uac70",
                            "emoji": True,
                        },
                        "value": "open_mute_menu",
                    },
                    {
                        "type": "button",
                        "action_id": UNMUTE_ACTION_ID,
                        "text": {
                            "type": "plain_text",
                            "text": "\uc74c\uc18c\uac70 \ud574\uc81c",
                            "emoji": True,
                        },
                        "value": UNMUTE_OPTION_VALUE,
                    },
                ],
            }
        )
    return blocks


def _row_field(row: Any, name: str, default: str = "") -> str:
    """sqlite3.Row와 dict 모두 안전하게 읽는 헬퍼."""
    try:
        return str(row[name]) if row[name] is not None else default
    except (KeyError, IndexError, TypeError):
        return default


def _build_evidence_groups(
    messages: Iterable[Any],
    *,
    max_per_source: int,
) -> list[tuple[str, int, int, str]]:
    """source_id별로 메시지를 그룹화하고 최근 max_per_source개만 텍스트화.

    반환: [(source_id, total, displayed, text), ...]
    """
    groups: dict[str, list[Any]] = defaultdict(list)
    for m in messages:
        src = _row_field(m, "source_id", "unknown")
        groups[src].append(m)

    result: list[tuple[str, int, int, str]] = []
    for source_id in sorted(groups.keys()):
        rows = sorted(groups[source_id], key=lambda r: _row_field(r, "timestamp", ""))
        total = len(rows)
        displayed_rows = rows[-max_per_source:] if total > max_per_source else rows
        lines = []
        for r in displayed_rows:
            ts = _row_field(r, "timestamp", "")
            sender = _row_field(r, "sender", "")
            text = _row_field(r, "text", "").replace("\n", " ")
            lines.append(f"{ts} | {sender} | {text}")
        body = "\n".join(lines)
        result.append((source_id, total, len(displayed_rows), body))
    return result


def _post_chat_message(
    *,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
) -> str | None:
    """chat.postMessage 호출. 성공 시 ts 반환, 실패 시 None.

    P1-4: 일시 네트워크 오류 / 5xx / JSON 파싱 실패에 한해 1회 재시도(1초).
    400/401/403/429는 재시도해도 결과가 같거나 rate-limit 가중 우려가 있어 제외.
    """
    if not config.SLACK_BOT_TOKEN:
        return None
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts
    headers = {
        "Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }

    for attempt in (1, 2):
        try:
            response = requests.post(
                f"{SLACK_API_BASE}/chat.postMessage",
                json=payload,
                headers=headers,
                timeout=config.SLACK_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            print(f"[SLACK] chat.postMessage error (attempt {attempt})={exc}")
            if attempt == 1:
                time.sleep(_RETRY_BACKOFF_SEC)
                continue
            return None

        status_code = response.status_code
        if status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            print(
                f"[SLACK] chat.postMessage rate-limited: status=429 "
                f"retry_after={retry_after!r}"
            )
            return None
        if status_code in _NO_RETRY_STATUS:
            print(f"[SLACK] chat.postMessage non-retryable: status={status_code}")
            return None
        if status_code in _RETRYABLE_STATUS and attempt == 1:
            print(f"[SLACK] chat.postMessage retryable status={status_code}, retry")
            time.sleep(_RETRY_BACKOFF_SEC)
            continue

        try:
            data = response.json()
        except ValueError as exc:
            print(f"[SLACK] chat.postMessage json parse failed (attempt {attempt})={exc}")
            if attempt == 1:
                time.sleep(_RETRY_BACKOFF_SEC)
                continue
            return None

        if not data.get("ok"):
            print(f"[SLACK] chat.postMessage failed: {data.get('error')}")
            return None
        return str(data.get("ts") or "")

    return None


def _send_evidence_thread(
    *,
    channel: str,
    thread_ts: str,
    messages: Iterable[Any],
) -> None:
    """alert 원본 메시지를 source_id별 그룹으로 thread에 게시."""
    groups = _build_evidence_groups(
        messages, max_per_source=config.SLACK_EVIDENCE_MAX_PER_SOURCE
    )
    if not groups:
        _post_chat_message(
            channel=channel,
            text=":mag: evidence: 분석 대상 메시지가 없습니다.",
            thread_ts=thread_ts,
        )
        return

    for source_id, total, displayed, body in groups:
        truncated_note = ""
        if displayed < total:
            truncated_note = f" (최근 {displayed}건 표시 / 전체 {total}건)"
        header = f":mag: *[{source_id}]* 총 {total}건{truncated_note}"
        # Slack 단일 메시지 한도(4000자) 안전 마진 고려
        body_block = "```" + _truncate(body, 3500) + "```"
        text = header + "\n" + body_block
        ts = _post_chat_message(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )
        if ts is None:
            print(f"[SLACK] evidence post failed for source={source_id}")


def send_alert_resume_notice(channel: str) -> bool:
    """음소거 만료 시 '재개되었습니다' 통지를 지정 채널에만 Bot token으로 전송.

    response_url(약 30분 후 만료)이 아니라 chat.postMessage를 써서
    음소거 길이·프로세스 재시작과 무관하게 전송된다. 채널이 비면 보내지 않는다
    (음소거 누른 채널로만 가야 하므로 기본 채널로 fallback하지 않는다).
    """
    if not channel:
        return False
    now_text = time.strftime("%Y-%m-%d %H:%M:%S KST", time.localtime())
    ts = _post_chat_message(
        channel=channel,
        text=f":bell: [issue_monitor] alert 알림 중지가 종료되어 재개되었습니다. ({now_text})",
    )
    return ts is not None


def send_slack_notification(
    *,
    title: str,
    should_alert: bool,
    content: str,
    fields: dict[str, Any] | None = None,
    is_test: bool = False,
    evidence_messages: Iterable[Any] | None = None,
    channel: str | None = None,
) -> bool:
    if not should_send_slack(should_alert=should_alert, is_test=is_test):
        return False

    icon = ":rotating_light:" if should_alert else ":white_check_mark:"
    label = "ALERT" if should_alert else "TEMP/FALSE"
    if is_test:
        label = "TEST-" + label

    lines = [
        f"{icon} *[{label}] {title}*",
        f"*should_alert:* `{str(should_alert).lower()}`",
    ]

    for key, value in (fields or {}).items():
        lines.append(f"*{key}:* `{_format_value(value)}`")

    text = "\n".join(lines)

    target_channel = channel or config.SLACK_CHANNEL
    channel_override = channel is not None

    # should_alert=True는 기존처럼 Bot Token+Channel로 main 메시지+thread evidence를 보낸다.
    # channel override가 있으면 TEMP/FALSE도 특정 채널에 보낼 수 있게 bot 경로를 사용한다.
    use_bot_post = bool(
        config.SLACK_BOT_TOKEN
        and target_channel
        and (should_alert or channel_override)
    )

    if use_bot_post:
        blocks = _build_blocks(text, should_alert=should_alert) if should_alert else None
        ts = _post_chat_message(
            channel=target_channel,
            text=text,
            blocks=blocks,
        )
        if ts is None:
            if channel_override:
                print(f"[SLACK] bot post failed for channel={target_channel}")
                return False
            print("[SLACK] bot post failed, falling back to webhook")
        else:
            print(f"[SLACK] sent=true via=bot ts={ts}")
            if should_alert and evidence_messages is not None:
                _send_evidence_thread(
                    channel=target_channel,
                    thread_ts=ts,
                    messages=evidence_messages,
                )
            return True

    if channel_override:
        print(f"[SLACK] skipped: channel override requires bot token/channel={target_channel}")
        return False

    # Webhook fallback (기존 흐름)
    if not config.SLACK_WEBHOOK_URL:
        print("[SLACK] skipped: SLACK_WEBHOOK_URL is empty")
        return False

    payload: dict[str, Any] = {"text": text}
    if should_alert:
        payload["blocks"] = _build_blocks(text, should_alert=should_alert)

    # P1-4: webhook도 동일한 1회 재시도 정책 적용.
    for attempt in (1, 2):
        try:
            response = requests.post(
                config.SLACK_WEBHOOK_URL,
                json=payload,
                timeout=config.SLACK_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            print(f"[SLACK] webhook error (attempt {attempt})={exc}")
            if attempt == 1:
                time.sleep(_RETRY_BACKOFF_SEC)
                continue
            return False

        status_code = response.status_code
        if status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            print(f"[SLACK] webhook rate-limited: status=429 retry_after={retry_after!r}")
            return False
        if status_code in _NO_RETRY_STATUS:
            print(f"[SLACK] webhook non-retryable: status={status_code}")
            return False
        if status_code in _RETRYABLE_STATUS and attempt == 1:
            print(f"[SLACK] webhook retryable status={status_code}, retry")
            time.sleep(_RETRY_BACKOFF_SEC)
            continue
        if status_code >= 400:
            print(f"[SLACK] webhook failed: status={status_code}")
            return False

        print("[SLACK] sent=true via=webhook")
        return True

    return False
