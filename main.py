from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import config
from alerts.slack import send_slack_notification
from alerts.slack_interactions import (
    start_slack_interaction_server,
    start_socket_mode_client,
)
from core.time_utils import KST, to_iso_kst
from llm.judge import (
    active_llm_model_name,
    detect_issue_candidates,
    format_response_for_display,
    format_response_for_storage,
    format_thinking_status_for_display,
    issue_candidate_sender_count,
    judge_messages,
    matched_issue_keywords,
    print_llm_response,
    verify_alert_cloud,
)
from pipeline.normalize import (
    normalize_snapshot,
    write_normalized_messages,
    write_parse_errors,
)
from sources.discovery import discover_all_sources
from sources.snapshot import cleanup_old_snapshots, create_snapshot
from storage.db import (
    connect_db,
    count_messages,
    fetch_messages_since,
    init_db,
    insert_local_llm_run,
    insert_messages,
    prune_messages_older_than,
)

def get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def print_source_counts(messages: list[Any]) -> None:
    counter: Counter[str] = Counter()
    for message in messages:
        source_id = str(get_field(message, "source_id", "unknown"))
        counter[source_id] += 1
    for source_id in sorted(counter):
        print(f"[NORMALIZE] source_id={source_id}, messages={counter[source_id]}")


def _parse_alert_category(parsed: dict[str, Any] | None) -> str:
    content = str((parsed or {}).get("content") or "")
    head = content.strip()[:80]
    for category in (
        "계정/운영 리스크",
        "서버/접속 장애",
        "결제 문제",
        "계정 문제",
        "운영 리스크",
        "일반 대화",
    ):
        if category in head:
            return category
    return ""


def _verify_under_daily_limit(now: datetime) -> bool:
    """오늘 2차(클라우드) 검증 실제 호출 수가 일일 상한 미만인지 확인."""
    limit = int(getattr(config, "VERIFY_DAILY_LIMIT", 0) or 0)
    if limit <= 0:
        return True
    day = now.strftime("%Y-%m-%d")
    try:
        with connect_db(config.DB_PATH) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM local_llm_runs "
                "WHERE substr(created_at,1,10)=? "
                "AND cloud_verify_status IN ('ok','error','parse_error')",
                (day,),
            ).fetchone()[0]
    except Exception:
        return True
    return int(n or 0) < limit


def _verify_log(
    *,
    called: bool,
    route: str,
    status: str,
    confirmed: bool | None,
    detail: str,
    keywords: list[str] | None = None,
) -> None:
    """2차(클라우드) 검증 흐름을 색상으로 강조 기록.

    - LLM_RESPONSE_GREEN_OUTPUT=1이면 초록으로 강조한다.
    - 2차 confirmed=True(=Slack 발송)면 결과·응답 줄을 빨강으로 표시한다.
    - 기록 항목: ① 2차 호출 여부 ② 호출 경로(1차 9B alert vs 키워드 게이트 강제)
      ③ 매칭된 이슈 키워드 ④ 2차 응답.
    """
    on = config.LLM_RESPONSE_GREEN_OUTPUT
    esc = chr(27)
    green, red, reset = esc + "[92m", esc + "[91m", esc + "[0m"
    bar = "=" * 80

    def _ln(text: str, is_red: bool = False) -> None:
        if on:
            print(f"{red if is_red else green}{text}{reset}")
        else:
            print(text)

    _ln(bar)
    _ln(f"[2차 검증] {'호출됨' if called else '미호출'} | 경로: {route} | status={status}")
    if keywords:
        _ln(f"[이슈 키워드] 매칭 {len(keywords)}종: {keywords}")
    if confirmed is not None:
        _ln(
            f"[2차 결과] confirmed={confirmed} → {'발송' if confirmed else '차단'}",
            is_red=bool(confirmed),
        )
    if detail:
        _ln(f"[2차 응답] {detail}", is_red=bool(confirmed))
    _ln(bar)


def run_cycle() -> float:
    cycle_started = time.perf_counter()
    now = datetime.now(KST)
    now_for_discovery = now.replace(tzinfo=None)

    print("=" * 80)
    print(f"[RUN] now={now.strftime('%Y-%m-%d %H:%M:%S KST')}")

    source_files = discover_all_sources(now_for_discovery)
    print(f"[DISCOVERY] matched_files={len(source_files)}")

    snapshot = create_snapshot(source_files, now_for_discovery)
    print(f"[SNAPSHOT] run_id={snapshot.run_id}")
    print(f"[SNAPSHOT] copied_files={len(snapshot.files)}")

    messages, parse_errors = normalize_snapshot(snapshot, now)

    write_normalized_messages(snapshot, messages)
    write_parse_errors(snapshot, parse_errors)

    print(f"[NORMALIZE] messages={len(messages)}")
    print_source_counts(messages)
    print(f"[NORMALIZE] parse_errors={len(parse_errors)}")

    first_seen_at = now.isoformat()
    db_cutoff = now - timedelta(minutes=config.DB_MESSAGE_RETENTION_MINUTES)
    db_cutoff_iso = to_iso_kst(db_cutoff)
    new_window_cutoff_iso = to_iso_kst(now - timedelta(minutes=config.NEW_WINDOW_MINUTES))

    with connect_db(config.DB_PATH) as conn:
        inserted_count = insert_messages(
            conn,
            messages,
            run_id=snapshot.run_id,
            first_seen_at=first_seen_at,
        )
        pruned_count = prune_messages_older_than(conn, db_cutoff_iso)
        recent_rows = fetch_messages_since(conn, db_cutoff_iso, new_window_cutoff_iso)
        total_count = count_messages(conn)

    print(f"[DB] path={config.DB_PATH}")
    print(
        f"[DB] retention_minutes={config.DB_MESSAGE_RETENTION_MINUTES}, "
        f"cutoff={db_cutoff.strftime('%Y-%m-%d %H:%M:%S KST')}"
    )
    print(f"[DB] inserted_messages={inserted_count}")
    print(f"[DB] pruned_messages={pruned_count}")
    print(f"[DB] total_messages={total_count}")

    if config.LLM_JUDGE_ENABLED:
        print(
            f"[LLM] provider={config.LLM_PROVIDER}, "
            f"model={active_llm_model_name()}, "
            f"messages={len(recent_rows)}, "
            f"timeout_sec={config.LLM_TIMEOUT_SEC}, "
            f"think={config.LLM_THINKING_MODE}, "
            f"force_json={config.LLM_FORCE_JSON}"
        )
        judge_result = judge_messages(recent_rows)
        display_text = format_response_for_display(judge_result)
        storage_text = format_response_for_storage(judge_result)
        parsed = judge_result.parsed_response or {}
        should_alert = bool(parsed.get("should_alert", False))
        current_category = _parse_alert_category(parsed)
        # 키워드 게이트: 이슈 키워드를 서로 다른 2명 이상이 언급하면 9B 판정과 무관하게
        # 2차로 넘긴다(recall 최우선). 2차(precision)가 오탐을 거른다.
        keyword_sender_count = issue_candidate_sender_count(recent_rows)
        keyword_gate = keyword_sender_count >= 2
        print_llm_response(display_text, should_alert=should_alert)
        print(
            f"[LLM] status={judge_result.status}, "
            f"elapsed_sec={judge_result.elapsed_sec:.2f}"
        )
        print(f"[LLM INPUT] messages={len(recent_rows)}")
        if judge_result.error:
            print(f"[LLM] error={judge_result.error}")

        if config.LLM_THINKING_STATUS_OUTPUT:
            print(format_thinking_status_for_display(judge_result))
        slack_content = str(parsed.get("content") or judge_result.error or "")

        # LLM이 신고로 판단한 메시지 idx만 evidence로 전달.
        # build_prompt에서 idx는 1부터 부여되므로 enumerate(start=1)과 매칭.
        evidence_ids_set = set(parsed.get("evidence_message_ids") or [])
        evidence_rows = [
            row
            for index, row in enumerate(recent_rows, start=1)
            if index in evidence_ids_set
        ]
        if evidence_ids_set:
            print(
                f"[LLM] evidence_message_ids={sorted(evidence_ids_set)}, "
                f"matched_rows={len(evidence_rows)}"
            )
        # 키워드 게이트로만 통과(9B가 신고 미인식)했는데 evidence가 비면 키워드 후보로 보강.
        if not evidence_rows and keyword_gate:
            cand_ids = {i for i, _ in detect_issue_candidates(recent_rows)}
            evidence_rows = [
                row for index, row in enumerate(recent_rows, start=1) if index in cand_ids
            ]
            print(f"[VERIFY] evidence backfilled from keyword candidates: {sorted(cand_ids)}")

        # 단일 사이클 + 하이브리드 2차 검증.
        # 로컬 1차 should_alert=true면 OpenAI 2차 검증으로 최종 확정.
        slack_should_alert = should_alert
        send_slack = False
        has_possible_issue_override: bool | None = None
        cloud_verify: dict[str, Any] | None = None
        decision_note = ""

        if judge_result.status != "ok":
            # 1차 판정을 신뢰할 수 없으면 차단.
            send_slack = False
            slack_should_alert = False
            has_possible_issue_override = False
            decision_note = f"llm_not_ok_{judge_result.status}"
            _verify_log(
                called=False, route="-", status=f"1차 비정상({judge_result.status})",
                confirmed=None, detail="1차 LLM 응답이 비정상이라 2차 미호출, 차단",
            )
        elif should_alert or keyword_gate:
            # 호출 경로: 1차 9B가 직접 alert인지, 9B는 false인데 키워드 게이트로 강제 전달인지.
            route = (
                "1차 로컬(9B) alert"
                if should_alert
                else f"키워드 게이트 강제(9B=false, 이슈키워드 {keyword_sender_count}명)"
            )
            keyword_hits = matched_issue_keywords(recent_rows)
            if config.VERIFY_ENABLED and _verify_under_daily_limit(now):
                cloud_verify = verify_alert_cloud(recent_rows, current_category, slack_content)
                cv_status = cloud_verify.get("status")
                if cv_status == "ok":
                    send_slack = bool(cloud_verify.get("confirmed"))
                    decision_note = "cloud_confirmed" if send_slack else "cloud_rejected"
                    # 게이트로만 통과(9B false)했는데 2차 confirmed면 9B의 false content 대신 2차 reason 사용.
                    if send_slack and not should_alert:
                        slack_content = str(cloud_verify.get("reason") or slack_content)
                        slack_should_alert = True
                        decision_note = "cloud_confirmed_via_keyword_gate"
                    _verify_log(
                        called=True, route=route, status="ok",
                        confirmed=send_slack, detail=str(cloud_verify.get("reason") or ""),
                        keywords=keyword_hits,
                    )
                else:
                    # 2차 장애/키없음/파싱실패 → 로컬 판정대로 발송(recall 우선).
                    send_slack = True
                    decision_note = f"cloud_fallback_{cv_status}"
                    if not should_alert:
                        slack_should_alert = True
                        decision_note = f"cloud_fallback_{cv_status}_keyword_gate"
                    _verify_log(
                        called=True, route=route, status=cv_status, confirmed=None,
                        detail=f"2차 장애 → 1차 판정대로 발송(fallback): {str(cloud_verify.get('error'))[:160]}",
                        keywords=keyword_hits,
                    )
            else:
                # 검증 비활성 또는 일일 상한 초과 → 로컬 판정대로.
                send_slack = True
                decision_note = "verify_disabled_or_daily_limit"
                if not should_alert:
                    slack_should_alert = True
                _verify_log(
                    called=False, route=route, status="skipped", confirmed=None,
                    detail="2차 비활성 또는 일일 상한 초과 → 1차 판정대로 발송",
                    keywords=keyword_hits,
                )
        else:
            send_slack = False
            decision_note = "local_no_alert"
            _verify_log(
                called=False, route="-", status="not_alert", confirmed=None,
                detail="1차 should_alert=false & 키워드 게이트 미통과 → 2차 미호출",
            )

        # 2차(클라우드) confirmed 시, 2차가 전체 맥락을 보고 재선정한 evidence를 우선 사용.
        # (1차 로컬 9B는 evidence를 빈약/불안정하게 고르는 경향) 없으면 1차 evidence 유지.
        if (
            cloud_verify is not None
            and cloud_verify.get("status") == "ok"
            and bool(cloud_verify.get("confirmed"))
        ):
            cloud_ev_ids = set(cloud_verify.get("evidence_message_ids") or [])
            if cloud_ev_ids:
                cloud_evidence_rows = [
                    row
                    for index, row in enumerate(recent_rows, start=1)
                    if index in cloud_ev_ids
                ]
                if cloud_evidence_rows:
                    evidence_rows = cloud_evidence_rows
                    print(
                        f"[VERIFY] cloud re-selected evidence={sorted(cloud_ev_ids)}, "
                        f"matched_rows={len(evidence_rows)}"
                    )

        slack_fields = {
            "run_id": snapshot.run_id,
            "provider": config.LLM_PROVIDER,
            "model": active_llm_model_name(),
            "llm_status": judge_result.status,
            "llm_elapsed_sec": judge_result.elapsed_sec,
            "analyzed_messages": len(recent_rows),
            "evidence_messages": len(evidence_rows),
            "category": current_category or "unknown",
            "decision": decision_note,
        }
        if cloud_verify is not None:
            slack_fields["cloud_verify"] = cloud_verify.get("status")
            slack_fields["cloud_confirmed"] = cloud_verify.get("confirmed")

        if send_slack:
            send_slack_notification(
                title="issue_monitor main",
                should_alert=slack_should_alert,
                content=slack_content,
                fields=slack_fields,
                evidence_messages=evidence_rows,
            )
        else:
            print(f"[SLACK] skipped=true reason={decision_note} category={current_category}")

        with connect_db(config.DB_PATH) as conn:
            insert_local_llm_run(
                conn,
                run_id=snapshot.run_id,
                window_start=to_iso_kst(now - timedelta(minutes=config.NEW_WINDOW_MINUTES)),
                window_end=to_iso_kst(now),
                context_window_start=db_cutoff_iso,
                message_count=len(recent_rows),
                new_message_count=inserted_count,
                raw_response=storage_text,
                status=judge_result.status,
                error=judge_result.error,
                created_at=to_iso_kst(datetime.now(KST)),
                parsed_response=judge_result.parsed_response,
                has_possible_issue_override=has_possible_issue_override,
                llm_meta=judge_result.llm_meta,
                cloud_verify=cloud_verify,
            )
        print("[LLM] saved_to=local_llm_runs")
        print(f"[RUN] now={now.strftime('%Y-%m-%d %H:%M:%S KST')}")
    else:
        print("[LLM] skipped: LLM_JUDGE_ENABLED=0")

    deleted_snapshots = cleanup_old_snapshots()
    print(f"[CLEANUP] deleted_snapshots={deleted_snapshots}")

    elapsed_sec = time.perf_counter() - cycle_started
    print(f"[CYCLE] elapsed_sec={elapsed_sec:.2f}")
    return elapsed_sec


def run_loop(*, once: bool) -> None:
    while True:
        elapsed_sec = run_cycle()

        if once:
            print("[LOOP] once=true, exit")
            return

        sleep_sec = max(0.0, float(config.RUN_INTERVAL_SECONDS) - elapsed_sec)
        print(
            f"[LOOP] cycle_elapsed_sec={elapsed_sec:.2f}, "
            f"sleep_sec={sleep_sec:.2f}, "
            f"next_cycle_after={config.RUN_INTERVAL_SECONDS}s interval"
        )

        if sleep_sec > 0:
            time.sleep(sleep_sec)
        else:
            print("[LOOP] cycle exceeded interval, next cycle starts immediately")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Issue monitor local LLM loop")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    # P1-1: schema/마이그레이션은 프로세스 시작 시 1회만 적용.
    # run_cycle 내부에서 매 사이클마다 init_db 하던 호출을 제거했다.
    with connect_db(config.DB_PATH) as conn:
        init_db(conn)
    # Slack 상호작용(음소거 버튼) 수신: socket(WebSocket) 또는 http(기존 서버+cloudflared)
    if config.SLACK_INTERACTION_MODE == "socket":
        start_socket_mode_client()
    else:
        start_slack_interaction_server()
    once = bool(args.once) or not config.LOOP_ENABLED
    run_loop(once=once)


if __name__ == "__main__":
    main()
