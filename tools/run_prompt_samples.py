from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from alerts.slack import send_slack_notification
from llm.judge import (
    build_prompt,
    detect_issue_candidates,
    format_response_for_display,
    judge_messages,
    matched_issue_keywords,
    verify_alert_cloud,
)
from main import (
    _is_new_row,
    _parse_alert_category,
    _rows_by_prompt_indices,
    _unique_sender_count,
    _verify_under_daily_limit,
)


KST = timezone(timedelta(hours=9))
BASE_TIME = datetime(2026, 5, 11, 19, 0, 0, tzinfo=KST)


def make_messages(case_id: str, rows: list[tuple]) -> list[dict[str, Any]]:
    """rows 각 항목은 (분offset, sender, text) 또는 (분offset, sender, text, source_id).

    - source_id를 4번째로 주면 그 값을 그대로 사용한다(예: "kakao_a" / "kakao_b" / "ingame").
    - 생략하면 index 기반 기본값("ingame" 또는 "kakao_a")을 부여한다.
    즉 문제 메시지를 어느 채널(출처)에서 온 것으로 둘지 케이스에서 직접 지정/확인할 수 있다.
    """
    messages: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if len(row) == 4:
            minute_offset, sender, text, source_id = row
        else:
            minute_offset, sender, text = row
            source_id = "ingame" if index % 3 else "kakao_a"
        timestamp = BASE_TIME + timedelta(minutes=minute_offset)
        messages.append(
            {
                "message_id": f"{case_id}_{index:02d}",
                "source_id": source_id,
                "timestamp": timestamp.isoformat(),
                "sender": sender,
                "text": text,
                "is_new": minute_offset >= -5,
            }
        )
    return messages


def _load_peak_sample() -> list[dict[str, Any]]:
    """tools/peak_sample.jsonl(실제 저녁 피크 스냅샷에서 추출한 normalized 메시지)을 로드.

    파일이 없으면 빈 리스트(케이스가 빈 입력이 됨). 실데이터라 잡담+신고가 자연 혼재한다.
    """
    p = Path(__file__).resolve().parent / "peak_sample.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _mix_with_noise(case_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """케이스 메시지(문제 등)에 실제 피크 잡담(peak_sample)을 배경으로 섞는다(모든 케이스 기본 동작).

    - 잡담의 최신 시각(window_end)을 기준으로 케이스 메시지의 상대 시각(BASE_TIME offset)을
      평행이동 → 케이스 메시지가 윈도우 끝(최근 5분=new 근처)에 그대로 분포한다.
    - is_new 등 케이스 메시지의 필드는 보존(make_messages가 offset>=-5를 is_new로 부여).
    - peak_sample.jsonl이 없으면 케이스 메시지만 그대로 반환.
    """
    noise = _load_peak_sample()
    if not noise:
        return case_messages
    stamps = [m["timestamp"] for m in noise if m.get("timestamp")]
    base = max(datetime.fromisoformat(t) for t in stamps)
    mixed = []
    for m in case_messages:
        try:
            offset = datetime.fromisoformat(m["timestamp"]) - BASE_TIME
        except Exception:
            offset = timedelta()
        mixed.append({**m, "timestamp": (base + offset).isoformat()})
    return sorted(noise + mixed, key=lambda x: x.get("timestamp", ""))


SAMPLE_CASES: dict[str, dict[str, Any]] = {
    "normal_chat": {
        "description": "정상 잡담만 있는 경우",
        "expected": "should_alert false — 일반 잡담만",
        "messages": make_messages(
            "normal_chat",
            [
                (-6, "민수", "오늘 사냥 효율 괜찮네요"),
                (-4, "하늘", "저는 장비 강화 재료 모으는 중입니다"),
                (-2, "도윤", "보스 몇 시에 열려요?"),
                (-1, "유나", "다들 득템하세요"),
            ],
        ),
    },
    "single_complaint": {
        "description": "단일 유저 불만 1건",
        "expected": "should_alert false — 단일 불만(임계 미달)",
        "messages": make_messages(
            "single_complaint",
            [
                (-4, "민수", "오늘 강화 너무 안 붙어서 짜증나네요"),
                (-2, "하늘", "저도 재료 더 모아야겠어요"),
                (-1, "민수", "운이 너무 없네"),
            ],
        ),
    },
    "bug_three_reporters": {
        "description": "케이스 1: 같은 버그를 서로 다른 유저 3명이 보고",
        "expected": "category 서버/접속 장애, reporter 3명, should_alert true",
        "messages": make_messages(
            "bug_three_reporters",
            [
                (-6, "민수", "오늘 레이드 보상 뭐 나왔어요?"),
                (-5, "하늘", "서버 렉", "ingame"),
                (-4, "도윤", "우편 열면 로딩걸림", "kakao_a"),
                (-3, "유나", "지금 접속 안되나요?", "ingame"),
                (-1, "서준", "지금 안됨", "ingame"),
            ],
        ),
    },
    "bug_two_reporters": {
        "description": "케이스 2: 같은 버그를 서로 다른 유저 2명이 보고",
        "expected": "category 계정/운영 리스크, reporter 2명, should_alert true",
        "messages": make_messages(
            "bug_two_reporters",
            [
                (-6, "민수", "신수 융합 재료는 어느 던전에서 모으나요?", "ingame"),
                (-5, "하늘", "잠금 켜둔 상태에서 융합 눌렀는데 재료가 사라졌습니다", "ingame"),
                (-3, "도윤", "저도 같은 현상 봤어요 잠금 상태인데 재화가 빠졌습니다", "ingame"),
                (-1, "유나", "난 괜찮은데?"),
            ],
        ),
    },
    "server_outage_many_users": {
        "description": "여러 유저의 서버 장애 신고",
        "expected": "should_alert true — [서버/접속 장애] 서로 다른 2명 이상 신고",
        "messages": make_messages(
            "server_outage_many_users",
            [
                (-5, "민수", "갑자기 접속이 안 됩니다"),
                (-4, "하늘", "저도 로그인에서 계속 멈춰요"),
                (-3, "도윤", "서버 터진 것 같은데요?"),
                (-2, "유나", "로딩만 돌고 게임 안 들어가집니다"),
                (-1, "서준", "재접해도 접속 실패 떠요"),
            ],
        ),
    },
    "payment_missing_item": {
        "description": "결제 후 상품 미지급 신고",
        "expected": "should_alert true 후보 — [결제 문제] 미지급, 서로 다른 3명 기준",
        "messages": make_messages(
            "payment_missing_item",
            [
                (-4, "민수", "패키지 결제됐는데 상품이 안 들어왔습니다"),
                (-3, "하늘", "영수증은 있는데 우편함이 비어 있어요"),
                (-1, "도윤", "저도 방금 결제하고 다이아 안 들어왔어요"),
            ],
        ),
    },
    "refund_group_complaint": {
        "description": "환불 불만 다수 발생",
        "expected": "should_alert true 후보 — [결제 문제] 환불 지연/오류",
        "messages": make_messages(
            "refund_group_complaint",
            [
                (-5, "민수", "환불 신청했는데 며칠째 답이 없습니다"),
                (-4, "하늘", "저도 환불 처리 지연 중이에요"),
                (-2, "도윤", "결제 오류 난 건데 환불 안 된다고만 하네요"),
                (-1, "유나", "환불 문의 답변 받은 분 있나요?"),
            ],
        ),
    },
    "exploit_report": {
        "description": "핵/치트 의심 제보",
        "expected": "[계정/운영 리스크] 핵/치트 의심 — 근거가 약하면 should_alert false",
        "messages": make_messages(
            "exploit_report",
            [
                (-5, "민수", "랭킹 1위 전투력이 갑자기 말도 안 되게 올랐어요"),
                (-3, "하늘", "사냥터에서 순간이동처럼 움직이는 캐릭터 봤습니다"),
                (-1, "도윤", "매크로인지 핵인지 같은 루트만 계속 돌고 있어요"),
            ],
        ),
    },
    "profanity_only": {
        "description": "욕설/분위기 악화만 있고 실제 운영 이슈는 없는 경우",
        "expected": "should_alert false — 욕설/감정 표출뿐, 운영 이슈 아님",
        "messages": make_messages(
            "profanity_only",
            [
                (-4, "민수", "아 진짜 너무 짜증난다"),
                (-3, "하늘", "오늘 운이 왜 이래"),
                (-2, "도윤", "운영 뭐하냐는 말 나오네"),
                (-1, "유나", "그냥 좀 쉬었다 해야겠다"),
            ],
        ),
    },
    "same_user_spam": {
        "description": "같은 유저가 반복 도배하는 경우",
        "expected": "should_alert false — 동일 sender 반복(서로 다른 신고자 아님)",
        "messages": make_messages(
            "same_user_spam",
            [
                (-5, "민수", "서버 이상한 것 같아요"),
                (-4, "민수", "서버 이상한 것 같아요"),
                (-3, "민수", "서버 이상한 것 같아요"),
                (-2, "민수", "왜 아무도 답 안 해요 서버 이상하다니까요"),
                (-1, "하늘", "저는 접속 잘 됩니다"),
            ],
        ),
    },
    "repeated_multi_user_latency": {
        "description": "여러 유저가 같은 렉/튕김 현상을 말하는 경우",
        "expected": "[서버/접속 장애] 렉/튕김 — 서로 다른 신고자 수에 따라 should_alert",
        "messages": make_messages(
            "repeated_multi_user_latency",
            [
                (-6, "민수", "사냥터에서 렉이 너무 심해요"),
                (-4, "하늘", "저도 스킬 누르면 3초 뒤에 나갑니다"),
                (-3, "도윤", "방금 두 번 튕겼어요"),
                (-2, "유나", "채널 이동하면 멈춥니다"),
                (-1, "서준", "렉 때문에 보상 못 받았어요"),
            ],
        ),
    },
}


def print_case_list() -> None:
    print("사용 가능한 샘플 케이스:")
    for case_id, case in SAMPLE_CASES.items():
        print(f"- {case_id}: {case['description']}")


def run_case(case_id: str, *, dry_run: bool, show_messages: bool) -> None:
    case = SAMPLE_CASES[case_id]
    case_msgs = case["messages"]
    messages = _mix_with_noise(case_msgs)  # 기본: 실제 피크 잡담을 배경으로 섞음

    print("=" * 80)
    print(f"[CASE] {case_id}")
    print(f"[DESC] {case['description']}")
    print(f"[EXPECTED] {case['expected']}")
    print(f"[MESSAGES] count={len(messages)} (케이스 {len(case_msgs)} + 잡담 배경)")

    if show_messages:
        print(json.dumps(messages, ensure_ascii=False, indent=2))

    if dry_run:
        prompt = build_prompt(messages)
        print(f"[DRY RUN] prompt_chars={len(prompt)}")
        print("[DRY RUN] Ollama 호출 없이 프롬프트 구성만 확인했습니다.")
        return

    # === main.py run_cycle 과 동일한 1차 → 키워드게이트 → 2차 → 발송(B/A) 로직 ===
    now = datetime.now(KST)
    result = judge_messages(messages)
    parsed = result.parsed_response or {}
    should_alert = bool(parsed.get("should_alert", False))
    category = _parse_alert_category(parsed)
    content = str(parsed.get("content") or result.error or "")

    _issue_cands = detect_issue_candidates(messages)
    keyword_sender_count = len({s for _, s in _issue_cands if s})
    cand_idx = {i for i, _ in _issue_cands}
    keyword_gate = any(
        (i in cand_idx) and _is_new_row(row) for i, row in enumerate(messages, start=1)
    )

    print(format_response_for_display(result))
    print(f"[1차] status={result.status} should_alert={should_alert} keyword_gate={keyword_gate} category={category}")

    evidence_ids_set = set(parsed.get("evidence_message_ids") or [])
    evidence_rows = [row for i, row in enumerate(messages, start=1) if i in evidence_ids_set]
    if not evidence_rows and keyword_gate:
        evidence_rows = [row for i, row in enumerate(messages, start=1) if i in cand_idx]

    slack_should_alert = should_alert
    send_slack = False
    cloud_verify: dict[str, Any] | None = None
    decision_note = ""
    reporter_count = 0

    if result.status != "ok":
        decision_note = f"llm_not_ok_{result.status}"
        slack_should_alert = False
    elif should_alert or keyword_gate:
        route = "1차 로컬(9B) alert" if should_alert else f"키워드 게이트(9B=false, {keyword_sender_count}명)"
        keyword_hits = matched_issue_keywords(messages)
        if config.VERIFY_ENABLED and _verify_under_daily_limit(now):
            cloud_verify = verify_alert_cloud(messages, category, content)
            cv_status = cloud_verify.get("status")
            if cv_status == "ok":
                send_slack = bool(cloud_verify.get("confirmed"))
                decision_note = "cloud_confirmed" if send_slack else "cloud_rejected"
                if send_slack and not should_alert:
                    content = str(cloud_verify.get("reason") or content)
                    slack_should_alert = True
                    decision_note = "cloud_confirmed_via_keyword_gate"
                print(f"  [2차] route={route} status=ok confirmed={send_slack} 키워드:{keyword_hits}")
                print(f"        {str(cloud_verify.get('reason') or '')[:140]}")
            else:
                send_slack = True
                decision_note = f"cloud_fallback_{cv_status}"
                if not should_alert:
                    slack_should_alert = True
                    decision_note = f"cloud_fallback_{cv_status}_keyword_gate"
                print(f"  [2차] route={route} status={cv_status} → fallback 발송")
        else:
            send_slack = True
            decision_note = "verify_disabled_or_daily_limit"
            if not should_alert:
                slack_should_alert = True
            print(f"  [2차] route={route} 비활성/상한 → 1차 판정대로 발송")
    else:
        decision_note = "local_no_alert"
        print("  [2차] 미호출 (1차 alert/게이트 아님)")

    # 2차 confirmed 시 evidence 재선정 + reporter_count (main과 동일)
    if cloud_verify and cloud_verify.get("status") == "ok" and bool(cloud_verify.get("confirmed")):
        reporter_ev_ids = set(cloud_verify.get("reporter_message_ids") or [])
        reporter_rows = _rows_by_prompt_indices(messages, reporter_ev_ids)
        reporter_count = _unique_sender_count(reporter_rows)
        cloud_ev_ids = set(cloud_verify.get("evidence_message_ids") or [])
        if cloud_ev_ids:
            cloud_rows = _rows_by_prompt_indices(messages, cloud_ev_ids)
            if cloud_rows:
                evidence_rows = cloud_rows
        print(f"  [2차] reporter_count={reporter_count} evidence={len(evidence_rows)}건")

    slack_fields = {
        "case": case_id,
        "category": category or "unknown",
        "decision": decision_note,
        "analyzed_messages": len(messages),
        "evidence_messages": len(evidence_rows),
        "reporter_count": reporter_count,
    }
    if cloud_verify is not None:
        slack_fields["cloud_verify"] = cloud_verify.get("status")
        slack_fields["cloud_confirmed"] = cloud_verify.get("confirmed")

    title = f"[샘플 {case_id}] issue_monitor"
    if send_slack:
        sent_b = send_slack_notification(
            title=title, should_alert=slack_should_alert, content=content,
            fields=slack_fields, is_test=True, evidence_messages=evidence_rows,
        )
        a_by_threshold = (
            cloud_verify is not None
            and cloud_verify.get("status") == "ok"
            and bool(cloud_verify.get("confirmed"))
            and reporter_count >= 3
        )
        if (
            sent_b
            and (a_by_threshold or (config.SLACK_TEMP_TO_A and config.SLACK_NOTIFY_ALL))
            and config.SLACK_CHANNEL_A
            and config.SLACK_CHANNEL_A != config.SLACK_CHANNEL
        ):
            sent_a = send_slack_notification(
                title=title, should_alert=slack_should_alert, content=content,
                fields=slack_fields, is_test=True, evidence_messages=evidence_rows,
                channel=config.SLACK_CHANNEL_A,
            )
            print(f"  [SLACK] channel_a sent={str(sent_a).lower()} reporter_count={reporter_count} temp_to_a={config.SLACK_TEMP_TO_A}")
        print(f"  [SLACK] sent_b={str(sent_b).lower()} decision={decision_note}")
    elif config.SLACK_NOTIFY_ALL:
        sent_b = send_slack_notification(
            title=title, should_alert=False, content=content,
            fields=slack_fields, is_test=True, evidence_messages=evidence_rows,
        )
        sent_a = None
        if config.SLACK_TEMP_TO_A and config.SLACK_CHANNEL_A and config.SLACK_CHANNEL_A != config.SLACK_CHANNEL:
            sent_a = send_slack_notification(
                title=title, should_alert=False, content=content,
                fields=slack_fields, is_test=True, evidence_messages=evidence_rows,
                channel=config.SLACK_CHANNEL_A,
            )
        print(f"  [SLACK] notify_all sent_b={str(sent_b).lower()} sent_a={str(sent_a).lower() if sent_a is not None else '-'} reason={decision_note}")
    else:
        print(f"  [SLACK] skipped reason={decision_note}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local LLM prompt sample cases.")
    parser.add_argument("--case", choices=sorted(SAMPLE_CASES), help="Run one sample case.")
    parser.add_argument("--all", action="store_true", help="Run all sample cases.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build prompts without calling Ollama.",
    )
    parser.add_argument(
        "--show-messages",
        action="store_true",
        help="Print sample messages before running.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.case and not args.all:
        print_case_list()
        print("")
        print("예: python tools/run_prompt_samples.py --case server_outage_many_users")
        print("예: python tools/run_prompt_samples.py --all --dry-run")
        return

    case_ids = sorted(SAMPLE_CASES) if args.all else [args.case]
    for case_id in case_ids:
        run_case(case_id, dry_run=args.dry_run, show_messages=args.show_messages)


if __name__ == "__main__":
    main()
