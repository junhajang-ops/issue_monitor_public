"""특정 run을 하이브리드 파이프라인(1차 로컬 → 2차 OpenAI)으로 **실제 재실행**한다.

- 스냅샷이 있으면 스냅샷 normalized 입력을, 없으면 원본에서 재구성한 입력을 사용(_replay_core.reconstruct).
- 1차: judge_messages(로컬), 2차: verify_alert_cloud(OpenAI).
- 2차 confirmed=true(+ 기본채널 교차검증 통과)면 Slack 테스트 알림 발송을 시도한다.
  2차 미응답(오류/비활성/상한)이면 발송하지 않는다(main.py와 동일: 1차 fallback 제거).
  실제 발송 여부는 SLACK_NOTIFY_TESTS / SLACK_NOTIFY_ALL 설정을 따른다.
- 매 실행 결과(1차 raw thinking + 2차 전체 응답)는 DB replay_runs 테이블에 기록된다.

사용법(프로젝트 루트에서 실행):
  python tools/llm_replay.py                    # 회귀 세트(RUNS 7종) 전체 재실행
  python tools/llm_replay.py 20260610_002903    # 지정 run 재실행(스냅샷 사용)
  python tools/llm_replay.py 20260604_011503    # 스냅샷 없으면 원본 재구성 후 재실행
  python tools/llm_replay.py <run_id> <run_id>  # 여러 run 연속 재실행
  REPLAY_ROUNDS=3 python tools/llm_replay.py <run_id>   # 반복 횟수
  REPLAY_DUMP_THINKING=1 python tools/llm_replay.py ... # 1차 thinking 콘솔 출력

주의: 실제 OpenAI 2차 호출이 발생한다. Slack은 테스트 알림 설정이 켜진 경우에만 발송된다.
"""
import datetime
import json
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


def _ensure_replay_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS replay_runs (
            replay_id       TEXT PRIMARY KEY,
            run_id          TEXT NOT NULL,
            round           INTEGER,
            replayed_at     TEXT NOT NULL,
            local_should_alert  INTEGER,
            local_category      TEXT,
            local_raw_response  TEXT,
            cv_called       INTEGER NOT NULL DEFAULT 0,
            cv_status       TEXT,
            cv_confirmed    INTEGER,
            cv_vcat         TEXT,
            cv_reason       TEXT,
            cv_thinking     TEXT,
            cv_reporter_count   INTEGER,
            cv_reporter_ids     TEXT,
            cv_evidence_ids     TEXT,
            cv_prompt_tokens    INTEGER,
            cv_completion_tokens INTEGER,
            cv_total_tokens     INTEGER,
            cv_reasoning_tokens INTEGER,
            cv_raw_json     TEXT,
            send_slack      INTEGER,
            decision_note   TEXT
        )
    """)
    conn.commit()


def _save_replay_result(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    round_num: int,
    local_should_alert: bool,
    local_category: str,
    local_raw_response: str,
    cv: dict | None,
    vcat: str,
    reporter_count: int,
    send_slack: bool,
    decision_note: str,
) -> None:
    now = datetime.datetime.now()
    replay_id = f"{run_id}_R{round_num}_{now.strftime('%Y%m%d%H%M%S%f')}"
    cv_called = cv is not None
    conn.execute(
        """
        INSERT OR REPLACE INTO replay_runs (
            replay_id, run_id, round, replayed_at,
            local_should_alert, local_category, local_raw_response,
            cv_called, cv_status, cv_confirmed, cv_vcat, cv_reason, cv_thinking,
            cv_reporter_count, cv_reporter_ids, cv_evidence_ids,
            cv_prompt_tokens, cv_completion_tokens, cv_total_tokens, cv_reasoning_tokens,
            cv_raw_json,
            send_slack, decision_note
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            replay_id, run_id, round_num, now.isoformat(),
            int(local_should_alert), local_category, local_raw_response,
            int(cv_called),
            cv.get("status") if cv else None,
            (int(cv["confirmed"]) if cv.get("confirmed") is not None else None) if cv else None,
            vcat if cv else None,
            cv.get("reason") if cv else None,
            cv.get("thinking") if cv else None,
            reporter_count if cv else None,
            json.dumps(cv.get("reporter_message_ids") or [], ensure_ascii=False) if cv else None,
            json.dumps(cv.get("evidence_message_ids") or [], ensure_ascii=False) if cv else None,
            cv.get("prompt_tokens") if cv else None,
            cv.get("completion_tokens") if cv else None,
            cv.get("total_tokens") if cv else None,
            cv.get("reasoning_tokens") if cv else None,
            json.dumps(cv.get("raw_api_json") or cv, ensure_ascii=False, default=str) if cv else None,
            int(send_slack),
            decision_note,
        ),
    )
    conn.commit()


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


def replay_run(conn, rid: str, round_num: int = 1) -> None:
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
        print(f"[{rid}] 입력=재구성 {len(msgs)}건 (당시 1차 입력 {r['message_count']}건)")

    # --- 1차: 로컬 ---
    jr = judge_messages(msgs)
    parsed = jr.parsed_response or {}
    # 1차 출력 필드: issue_detected(신규) 우선, should_alert(구) 하위호환.
    should_alert = bool(parsed.get("issue_detected", parsed.get("should_alert", False)))
    category = _parse_alert_category(parsed)
    content = str(parsed.get("content") or jr.error or "")
    # 키워드 게이트(main.py와 동일): 이슈 키워드가 '신규(is_new)' 메시지에 하나라도 있으면 발동.
    cand_idx = {i for i, _ in detect_issue_candidates(msgs)}
    gate_rows = [m for i, m in enumerate(msgs, start=1) if i in cand_idx and bool(m.get("is_new"))]
    keyword_gate = bool(gate_rows)
    gate_kws = matched_issue_keywords(gate_rows)
    ev_ids = set(parsed.get("evidence_message_ids") or [])
    evidence_rows = [m for i, m in enumerate(msgs, start=1) if i in ev_ids]
    if not evidence_rows and keyword_gate:
        evidence_rows = [m for i, m in enumerate(msgs, start=1) if i in cand_idx]
    kw_str = f" 키워드:[{','.join(gate_kws)}]" if keyword_gate else ""
    print(f"  [1차 로컬] status={jr.status} issue_detected={should_alert} keyword_gate={keyword_gate}{kw_str} category={category}")
    print(f"            evidence {sorted(ev_ids)} → {len(evidence_rows)}건")
    print(f"            {content[:130]}")
    if os.getenv("REPLAY_DUMP_THINKING"):
        _raw = jr.raw_response or ""
        if "[THINKING]" in _raw and "[RESPONSE]" in _raw:
            _thinking = _raw.split("[RESPONSE]")[0].replace("[THINKING]", "").strip()
        else:
            _thinking = _raw
        print(f"  [1차 THINKING]\n{_thinking}\n  [/1차 THINKING]")

    if jr.status != "ok" or not (should_alert or keyword_gate):
        print("  → 1차 alert/게이트 아님 (2차 미호출)")
        _save_replay_result(
            conn,
            run_id=rid, round_num=round_num,
            local_should_alert=should_alert, local_category=category,
            local_raw_response=jr.raw_response or "",
            cv=None, vcat=category, reporter_count=0,
            send_slack=False, decision_note="local_no_alert",
        )
        if config.SLACK_NOTIFY_ALL:
            _replay_send(
                rid, False, content,
                {"run_id": rid, "category": category, "decision": "local_no_alert", "llm_status": jr.status},
                evidence_rows, "local_no_alert",
            )
        return
    if not should_alert and keyword_gate:
        print(f"  [키워드 게이트 발동] 9B=false지만 신규 메시지 이슈 키워드 → 2차 호출  키워드:[{','.join(gate_kws)}]")

    # --- 2차: OpenAI ---
    cv = verify_alert_cloud(msgs, category, content)
    cv_status = cv.get("status")
    _cv_cat = str(cv.get("category") or "").strip()
    vcat = _cv_cat if _cv_cat in ("서버/접속 장애", "결제 문제", "계정/운영 리스크", "핵 신고") else category
    print(
        f"  [2차 클라우드] status={cv_status} confirmed={cv.get('confirmed')} "
        f"vcat={vcat} tokens={cv.get('total_tokens')}"
    )
    print(f"            {str(cv.get('reason') or cv.get('error'))[:130]}")

    # --- 발송 결정 (main.py와 동일 정책) ---
    reporter_count = 0
    if cv_status == "ok":
        send = bool(cv.get("confirmed"))
        note = "cloud_confirmed" if send else "cloud_rejected"

        if send:
            reporter_ev_ids = set(cv.get("reporter_message_ids") or [])
            reporter_rows = [m for i, m in enumerate(msgs, start=1) if i in reporter_ev_ids]
            reporter_count = len({str(m.get("sender", m.get("sender_id", ""))) for m in reporter_rows if m.get("sender", m.get("sender_id", ""))})
            cloud_ev = set(cv.get("evidence_message_ids") or [])
            if cloud_ev:
                cloud_rows = [m for i, m in enumerate(msgs, start=1) if i in cloud_ev]
                if cloud_rows:
                    evidence_rows = cloud_rows
                    print(f"            [2차 evidence 재선정] {sorted(cloud_ev)} → {len(evidence_rows)}건")
            print(f"            reporter_message_ids={sorted(reporter_ev_ids)} reporter_count={reporter_count}")

            base_min = config.min_reporters_base(vcat)
            if reporter_count < base_min:
                send = False
                note = "cloud_confirmed_base_undercount"
                print(f"            [기본 채널 교차검증 차단] reporter_count={reporter_count} < {base_min} (vcat={vcat})")
    else:
        # 2차 미응답(오류/키없음/파싱실패) → 발송하지 않음 (main.py와 동일: 1차 fallback 제거).
        send = False
        note = f"cloud_error_{cv_status}_blocked"

    _save_replay_result(
        conn,
        run_id=rid, round_num=round_num,
        local_should_alert=should_alert, local_category=category,
        local_raw_response=jr.raw_response or "",
        cv=cv, vcat=vcat, reporter_count=reporter_count,
        send_slack=send, decision_note=note,
    )

    if send or config.SLACK_NOTIFY_ALL:
        _replay_send(
            rid, send, content,
            {
                "run_id": rid, "category": vcat, "decision": note,
                "cloud_confirmed": cv.get("confirmed"),
                "cloud_status": cv_status, "reporter_count": reporter_count,
            },
            evidence_rows, note,
        )
    else:
        print(f"  → 발송 안 함 ({note})")


def main(round_num: int = 1) -> None:
    targets = [a for a in sys.argv[1:] if not a.startswith("-")] or list(RUNS)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    _ensure_replay_table(conn)
    try:
        for rid in targets:
            replay_run(conn, rid, round_num=round_num)
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
        main(round_num=_r)
