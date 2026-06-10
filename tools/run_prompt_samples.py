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
from llm.judge import build_prompt, format_response_for_display, judge_messages


KST = timezone(timedelta(hours=9))
BASE_TIME = datetime(2026, 5, 11, 19, 0, 0, tzinfo=KST)


def make_messages(case_id: str, rows: list[tuple[int, str, str]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, (minute_offset, sender, text) in enumerate(rows, start=1):
        timestamp = BASE_TIME + timedelta(minutes=minute_offset)
        source_id = "ingame" if index % 3 else "kakao_a"
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
        "expected": "issue_score 0.00~0.20, severity none 또는 low, should_alert false",
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
        "expected": "issue_score 0.21~0.45, severity low, should_alert false",
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
        "expected": "category 계정/운영 리스크, reporter 3명, should_alert true",
        "messages": make_messages(
            "bug_three_reporters",
            [
                (-6, "민수", "오늘 레이드 보상 뭐 나왔어요?"),
                (-5, "하늘", "신수 융합 잠금 눌렀는데도 재료가 계속 빠집니다"),
                (-4, "도윤", "저도 신수 융합 잠금 상태에서 연속 탭하니까 재화가 소모됐어요"),
                (-3, "유나", "신수 융합 잠금 켜져 있는데 재료 빠지는 거 저만 그런 게 아니네요"),
                (-1, "서준", "일단 신수 융합은 건드리지 말아야겠네요"),
            ],
        ),
    },
    "bug_two_reporters": {
        "description": "케이스 2: 같은 버그를 서로 다른 유저 2명이 보고",
        "expected": "category 계정/운영 리스크, reporter 2명, should_alert true",
        "messages": make_messages(
            "bug_two_reporters",
            [
                (-6, "민수", "신수 융합 재료는 어느 던전에서 모으나요?"),
                (-5, "하늘", "잠금 켜둔 상태에서 융합 눌렀는데 재료가 사라졌습니다"),
                (-3, "도윤", "저도 같은 현상 봤어요 잠금 상태인데 재화가 빠졌습니다"),
                (-1, "유나", "난 괜찮은데?"),
            ],
        ),
    },
    "server_outage_many_users": {
        "description": "여러 유저의 서버 장애 신고",
        "expected": "issue_score 0.80 이상 또는 severity high/critical, should_alert true",
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
        "expected": "issue_score 0.65 이상, category payment, should_alert true 후보",
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
        "expected": "issue_score 0.65 이상, category refund, should_alert true 후보",
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
        "expected": "issue_score 0.65 이상 가능, category exploit, 근거가 약하면 medium",
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
        "expected": "issue_score 0.00~0.30, category sentiment 또는 abuse, should_alert false",
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
        "expected": "동일 sender 반복만으로는 high 지양, should_alert false 권장",
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
        "expected": "issue_score 0.65 이상 후보, category latency/server, should_alert는 심각도에 따라 결정",
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


def _evidence_rows(messages: list[dict[str, Any]], ids: Any) -> list[dict[str, Any]]:
    if not isinstance(ids, list):
        return []
    evidence_ids = {int(i) for i in ids if isinstance(i, int)}
    return [m for i, m in enumerate(messages, start=1) if i in evidence_ids]


def _send_sample_slack(
    *,
    case_id: str,
    case: dict[str, Any],
    messages: list[dict[str, Any]],
    result: Any,
) -> None:
    parsed = result.parsed_response or {}
    should_alert = bool(parsed.get("should_alert", False))
    # SLACK_TEMP_TO_A=1(테스트)이면 임계(should_alert)·NOTIFY_ALL과 무관하게 발송 시도.
    if not config.SLACK_TEMP_TO_A and not should_alert and not config.SLACK_NOTIFY_ALL:
        print("[SLACK] skipped=true reason=sample_false_notify_all_off")
        return

    content = str(parsed.get("content") or result.error or "")
    evidence = _evidence_rows(messages, parsed.get("evidence_message_ids"))
    fields = {
        "case": case_id,
        "expected": case["expected"],
        "actual": should_alert,
        "llm_status": result.status,
        "llm_elapsed_sec": result.elapsed_sec,
        "analyzed_messages": len(messages),
        "evidence_messages": len(evidence),
    }
    # 기본 B. SLACK_TEMP_TO_A=1이면 A 채널에도 함께 발송(양쪽).
    sample_channels = [None]  # None → 기본 B 채널
    if (
        config.SLACK_TEMP_TO_A
        and config.SLACK_CHANNEL_A
        and config.SLACK_CHANNEL_A != config.SLACK_CHANNEL
    ):
        sample_channels.append(config.SLACK_CHANNEL_A)
    for ch in sample_channels:
        sent = send_slack_notification(
            title="issue_monitor prompt sample",
            should_alert=should_alert,
            content=content,
            fields=fields,
            is_test=True,
            evidence_messages=evidence,
            channel=ch,
        )
        print(
            f"[SLACK] sample sent={str(sent).lower()} "
            f"should_alert={str(should_alert).lower()} channel={ch or 'B(default)'}"
        )


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

    result = judge_messages(messages)
    print(f"[RESULT] status={result.status}, elapsed_sec={result.elapsed_sec:.2f}")
    if result.error:
        print(f"[ERROR] {result.error}")
    print(format_response_for_display(result))
    _send_sample_slack(
        case_id=case_id,
        case=case,
        messages=messages,
        result=result,
    )


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
