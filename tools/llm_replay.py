"""특정 run을 하이브리드 파이프라인(1차 로컬 → 2차 OpenAI)으로 **실제 재실행**한다.

- 스냅샷이 있으면 스냅샷 normalized 입력을, 없으면 원본에서 재구성한 입력을 사용(_replay_core.reconstruct).
- 1차: judge_messages(로컬), 2차: verify_alert_cloud(OpenAI).
- 2차 confirmed=true(또는 2차 장애 fallback)면 Slack 테스트 알림 발송을 시도한다.
  실제 발송 여부는 SLACK_NOTIFY_TESTS / SLACK_NOTIFY_ALL 설정을 따른다.

사용법(프로젝트 루트에서 실행):
  python tools/llm_replay.py                    # 회귀 세트(RUNS 7종) 전체 재실행
  python tools/llm_replay.py 20260610_002903    # 지정 run 재실행(스냅샷 사용)
  python tools/llm_replay.py 20260604_011503    # 스냅샷 없으면 원본 재구성 후 재실행
  python tools/llm_replay.py <run_id> <run_id>  # 여러 run 연속 재실행
  REPLAY_ROUNDS=3 python tools/llm_replay.py <run_id>   # 반복 횟수

주의: 실제 OpenAI 2차 호출이 발생한다. Slack은 테스트 알림 설정이 켜진 경우에만 발송된다.
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from _replay_core import RUNS, nearest_runs, reconstruct  # noqa: E402
from alerts.slack import send_slack_notification  # noqa: E402
from llm.judge import (  # noqa: E402
    detect_issue_candidates,
    judge_messages,
    matched_issue_keywords,
    verify_alert_cloud,
)
from main import _parse_alert_category  # noqa: E402


def _replay_send(rid, should_alert, content, fields, evidence_rows, note) -> None:
    """재구성 알림 발송: 기본 B, SLACK_TEMP_TO_A=1이면 A 채널에도(양쪽).

    실제 발송 여부는 send_slack_notification 내부 게이트(NOTIFY_TESTS/NOTIFY_ALL)를 따른다.
    """
    channels = [None]  # None → 기본 B 채널
    if (
        config.SLACK_TEMP_TO_A
        and config.SLACK_CHANNEL_A
        and config.SLACK_CHANNEL_A != config.SLACK_CHANNEL
    ):
        channels.append(config.SLACK_CHANNEL_A)
    for ch in channels:
        sent = send_slack_notification(
            title=f"[재실행 {rid}] issue_monitor",
            should_alert=should_alert,
            content=content,
            fields=fields,
            is_test=True,
            evidence_messages=evidence_rows,
            channel=ch,
        )
        print(f"  → Slack 테스트 알림({note}) channel={ch or 'B(default)'}: sent={sent}")


def replay_run(conn, rid: str) -> None:
    r = conn.execute(
        "SELECT context_window_start, window_end, message_count "
        "FROM local_llm_runs WHERE run_id=?",
        (rid,),
    ).fetchone()
    if r is None:
        print(f"[{rid}] DB에 run 없음 — 스킵")
        before, after = nearest_runs(rid)
        if before:
            print("  ↑ 이전(가까운 순): " + ", ".join(before))
        if after:
            print("  ↓ 이후(가까운 순): " + ", ".join(after))
        if not before and not after:
            print("  (DB에 run 기록이 없습니다)")
        return
    msgs, src = reconstruct(rid, r["context_window_start"], r["window_end"])
    print("=" * 78)
    if src == "snapshot":
        print(f"[{rid}] 입력=스냅샷 {len(msgs)}건")
    else:
        # 재구성은 원본에서 복원하므로 당시 기록과 비교(다르면 ±경계 오차 확인용).
        print(f"[{rid}] 입력=재구성 {len(msgs)}건 (당시 1차 입력 {r['message_count']}건)")

    # --- 1차: 로컬 ---
    jr = judge_messages(msgs)
    parsed = jr.parsed_response or {}
    should_alert = bool(parsed.get("should_alert", False))
    category = _parse_alert_category(parsed)
    content = str(parsed.get("content") or jr.error or "")
    # 키워드 게이트(main.py와 동일): 이슈 키워드가 '신규(is_new)' 메시지에 하나라도 있으면 발동.
    cand_idx = {i for i, _ in detect_issue_candidates(msgs)}
    gate_rows = [m for i, m in enumerate(msgs, start=1) if i in cand_idx and bool(m.get("is_new"))]
    keyword_gate = bool(gate_rows)
    gate_kws = matched_issue_keywords(gate_rows)  # 게이트에 실제 기여한(신규 메시지 매칭) 키워드
    ev_ids = set(parsed.get("evidence_message_ids") or [])
    evidence_rows = [m for i, m in enumerate(msgs, start=1) if i in ev_ids]
    if not evidence_rows and keyword_gate:
        evidence_rows = [m for i, m in enumerate(msgs, start=1) if i in cand_idx]
    kw_str = f" 키워드:[{','.join(gate_kws)}]" if keyword_gate else ""
    print(f"  [1차 로컬] status={jr.status} should_alert={should_alert} keyword_gate={keyword_gate}{kw_str} category={category}")
    print(f"            evidence {sorted(ev_ids)} → {len(evidence_rows)}건")
    print(f"            {content[:130]}")

    if jr.status != "ok" or not (should_alert or keyword_gate):
        print("  → 1차 alert/게이트 아님 (2차 미호출)")
        if config.SLACK_NOTIFY_ALL:
            # main.py와 동일: NOTIFY_ALL이면 1차 미통과(false)도 발송. TEMP_TO_A=1이면 A·B 양쪽.
            _replay_send(
                rid,
                False,
                content,
                {
                    "run_id": rid,
                    "category": category,
                    "decision": "local_no_alert",
                    "llm_status": jr.status,
                },
                evidence_rows,
                "local_no_alert",
            )
        return
    if not should_alert and keyword_gate:
        print(f"  [키워드 게이트 발동] 9B=false지만 신규 메시지 이슈 키워드 → 2차 호출  키워드:[{','.join(gate_kws)}]")

    # --- 2차: OpenAI ---
    cv = verify_alert_cloud(msgs, category, content)
    print(
        f"  [2차 클라우드] status={cv.get('status')} confirmed={cv.get('confirmed')} "
        f"tokens={cv.get('total_tokens')}"
    )
    print(f"            {str(cv.get('reason') or cv.get('error'))[:130]}")

    # --- 발송 결정 (main.py와 동일 정책) ---
    cv_status = cv.get("status")
    if cv_status == "ok":
        send = bool(cv.get("confirmed"))
        note = "cloud_confirmed" if send else "cloud_rejected"
    else:
        send = True  # 2차 장애/상한 → 로컬대로 발송(fallback)
        note = f"cloud_fallback_{cv_status}"

    if cv_status == "ok" and bool(cv.get("confirmed")):
        cloud_ev = set(cv.get("evidence_message_ids") or [])
        if cloud_ev:
            cloud_rows = [m for i, m in enumerate(msgs, start=1) if i in cloud_ev]
            if cloud_rows:
                evidence_rows = cloud_rows
                print(f"            [2차 evidence 재선정] {sorted(cloud_ev)} → {len(evidence_rows)}건")

    if send or config.SLACK_NOTIFY_ALL:
        _replay_send(
            rid,
            send,
            content,
            {
                "run_id": rid,
                "category": category,
                "decision": note,
                "cloud_confirmed": cv.get("confirmed"),
                "cloud_status": cv_status,
            },
            evidence_rows,
            note,
        )
    else:
        print(f"  → 발송 안 함 ({note})")


def main() -> None:
    targets = [a for a in sys.argv[1:] if not a.startswith("-")] or list(RUNS)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        for rid in targets:
            replay_run(conn, rid)
    finally:
        conn.close()
    print("\n=== 완료 ===")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    rounds = int(os.getenv("REPLAY_ROUNDS", "1"))
    for _r in range(1, rounds + 1):
        if rounds > 1:
            print(f"\n{'#' * 78}\n# ROUND {_r}/{rounds}\n{'#' * 78}", flush=True)
        main()
