from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

import config
from core.time_utils import KST, to_iso_kst

STATE_PATH = config.DATA_DIR / "slack_state.json"


def _read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict[str, Any]) -> None:
    """Atomically persist Slack state.

    P1-3: 부분 쓰기 후 충돌·전원 차단으로 파일이 손상되면 다음 _read_state()가
    빈 dict를 반환해 음소거 상태가 사라진다. 같은 디렉터리 임시 파일에 먼저 쓰고
    os.replace로 atomic하게 교체한다. 실패 시 기존 파일을 그대로 두고 [WARN]만 출력.
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, STATE_PATH)
    except OSError as exc:
        print(f"[WARN] slack_state write failed, keeping previous state: {exc}")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def set_alert_snooze(*, minutes: int, user_label: str = "", channel_id: str = "") -> datetime:
    now = datetime.now(KST)
    until = now + timedelta(minutes=minutes)
    state = _read_state()
    state["alert_snooze_until"] = to_iso_kst(until)
    state["alert_snooze_by"] = user_label
    # 재개 통지를 '음소거 누른 그 채널'로만 보내기 위해 채널 id 저장(억제 자체는 전역).
    state["alert_snooze_channel"] = channel_id
    state["alert_snooze_created_at"] = to_iso_kst(now)
    _write_state(state)
    return until


def clear_alert_snooze(*, user_label: str = "") -> None:
    state = _read_state()
    now = datetime.now(KST)
    state.pop("alert_snooze_until", None)
    state.pop("alert_snooze_channel", None)
    state["alert_snooze_cleared_by"] = user_label
    state["alert_snooze_cleared_at"] = to_iso_kst(now)
    _write_state(state)


def pop_expired_snooze() -> str | None:
    """음소거가 만료됐으면 (재개 통지용) 채널 id를 1회 반환하고 만료 상태를 정리한다.

    - 음소거 없음/아직 유효 → None.
    - 만료 시 alert_snooze_until·channel을 제거(idempotent: 다음 호출은 None) → 1회만 통지.
    - 저장된 채널이 없으면 상태만 정리하고 None(엉뚱한 채널 통지 방지).
    영속 메인 루프가 매 사이클 호출 → 재시작·음소거 길이와 무관하게 정확히 1회.
    """
    state = _read_state()
    until_val = state.get("alert_snooze_until")
    if not until_val:
        return None
    try:
        until = datetime.fromisoformat(str(until_val))
    except ValueError:
        state.pop("alert_snooze_until", None)
        state.pop("alert_snooze_channel", None)
        _write_state(state)
        return None
    if datetime.now(KST) < until:
        return None  # 아직 음소거 중
    channel = str(state.get("alert_snooze_channel") or "")
    state.pop("alert_snooze_until", None)
    state.pop("alert_snooze_channel", None)
    state["alert_snooze_resumed_at"] = to_iso_kst(datetime.now(KST))
    _write_state(state)
    return channel or None


def get_alert_snooze_until() -> datetime | None:
    value = _read_state().get("alert_snooze_until")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def get_alert_snooze_remaining_seconds() -> int:
    until = get_alert_snooze_until()
    if not until:
        return 0
    remaining = int((until - datetime.now(KST)).total_seconds())
    return max(0, remaining)


def is_alert_snoozed() -> bool:
    return get_alert_snooze_remaining_seconds() > 0
