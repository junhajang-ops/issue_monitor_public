from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

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


def run_case(case_id: str, *, dry_run: bool, show_messages: bool) -> None:
    case = SAMPLE_CASES[case_id]
    messages = case["messages"]

    print("=" * 80)
    print(f"[CASE] {case_id}")
    print(f"[DESC] {case['description']}")
    print(f"[EXPECTED] {case['expected']}")
    print(f"[MESSAGES] count={len(messages)}")

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
