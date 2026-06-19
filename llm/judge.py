from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import config


JUDGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "issue_detected": {"type": "boolean"},
        "content": {"type": "string"},
        "evidence_message_ids": {
            "type": "array",
            "items": {"type": "integer"},
        },
    },
    "required": ["issue_detected", "content", "evidence_message_ids"],
    "additionalProperties": False,
}

JUDGE_SYSTEM_PROMPT = """
너는 모바일 게임 운영 이슈의 1차 탐지기다.
역할은 "운영 이슈 신호가 하나라도 있는지"를 넓게 잡아내는 것이다(신고 인원수·임계·심각도는 판단하지 않는다).
반드시 유효한 JSON 객체 1개만 출력한다.
허용 필드는 정확히 `issue_detected`, `content`, `evidence_message_ids` 3개뿐이다.
`issue_detected` 값은 반드시 JSON boolean `true` 또는 `false`다. 문자열 "true", "false"는 금지다.
`content` 값은 줄바꿈 없는 한국어 한 줄 문자열이다.
`evidence_message_ids` 값은 정수(integer) 배열이다. 이슈 신호의 근거가 된 입력 메시지의 `idx`만 담는다.
  - issue_detected=true일 때: 이슈 신호 근거가 된 메시지 idx들을 모두 담는다.
  - issue_detected=false일 때: 빈 배열 [].
절대 `classification`, `primary_topic`, `score`, `category`, `severity`, `reason`, 중첩 객체, 마크다운을 출력하지 마라.
판단 로직과 카테고리 기준은 user 메시지의 규칙을 따른다.
""".strip()

JUDGE_PROMPT_TEMPLATE = """
너는 모바일 게임의 커뮤니티/인게임 채팅을 감시하는 운영 이슈 1차 탐지기다.
목표는 "운영 이슈로 의심되는 신호"가 하나라도 있으면 빠짐없이 잡아내는 것이다(recall 우선).
신고 인원수·임계·심각도는 판단하지 않는다.
너는 오직 "이슈 신호가 있는가/없는가"만 판정한다.

입력에는 최근 10분 이내 메시지들이 시간순으로 제공된다.
각 메시지는 `source_id`(출처), `timestamp`(시간), `sender`(작성자), `text`(내용)를 가진다.

== 1단계: 카테고리 파악 ==

아래 5개 카테고리 중 메시지에 해당하는 이슈를 파악하라.
이슈가 없으면 [일반 대화]다. 복수 카테고리가 감지되면 가장 심각한 카테고리의 기준을 적용한다.

[서버/접속 장애]
접속 불가, 로그인 불가, 무한 로딩, 반복 튕김, 앱 크래시, 서버 전체 장애,
점검 종료 후 다수 접속 불가, 점검 외 시간 갑작스런 접속 장애

[결제 문제]
결제 완료 후 상품/재화 미지급, 중복 결제

* 동일 유형 묶기 원칙 *
- "결제 후 미지급"은 결제 상품 종류와 무관하게 모두 같은 유형이다.
  예) 시즌패스 보상 미지급, 월정액 소환권 미지급, 스타터팩 다이아 미지급,
  광제 인벤에 없음, 현질 후 아이템 안 들어옴 → 전부 [결제 후 미지급] 1개 유형
- 결제 상품 명칭(시즌패스/월정액/스타터팩 등)이 다르다는 이유로 다른 유형으로 분리하지 마라.

[계정/운영 리스크]
운영자가 확인해야 하는 계정 피해 또는 운영 리스크성 신고는 이 카테고리로 통합한다.
카테고리는 반드시 [계정/운영 리스크]로 쓰고, content 안에서 세부 유형을 구분한다.

세부 유형:
- 계정 문제: 재화/아이템 소실, 계정 롤백, 데이터 초기화, 계정 접근 이상
- 운영 리스크: 버그 악용, 비정상 재화 획득, 복사/중복 지급, 이벤트·보상 전체 오지급, 시스템 기능 오류
  (핵·매크로 '현재 확산 정황'은 [계정/운영 리스크]가 아닌 [핵 신고]로 분류)

[핵 신고]
다른 유저의 핵·매크로·외부 불법 프로그램 사용을 지금 목격·신고·의심하는 발화.
"쟤 핵쓴다", "핵 같다", "매크로 쓰는 것 같다" 등 현재 진행형 신고에 한정.
[핵 신고] vs [계정/운영 리스크] 구분: 외부 불법 프로그램 사용 의심·신고 = [핵 신고] / 버그 악용·어뷰징·계정 도용 = [계정/운영 리스크].

[일반 대화]
공략·스펙 상담, 서버 번호 대화, 연합 모집, 쿠폰 위치 질문, 과금 효율 평가,
구매 검토, 확률 불만, 게임 진행 한탄, 핵/매크로 사후 푸념, 단순 잡담

[일반 대화]로 분류해야 하는 함정 표현 (실제 빈출):
- "○○ 못넘어가고 막혀있음", "○○ 스테 못 밀었음" → 스테이지 진행 실패
- "성공 확률이 너무 낮음", "확률업 망함", "0.x%에서 못 깬 거" → 확률 결과 불만
- "다이아 부족함", "골드 모자람", "재화 부족" → 보유량 한탄 (소실 아님)
- "현질 효율", "패스 가성비", "현질 없이 가능?" → 과금 효율 평가
- "명중만 올리면 다컨인데", "○경도 안 됨" → 스펙 부족 한탄
- "핵있었나보네", "핵쟁이 극혐" → 핵 사후 푸념 (능동 신고 아님)

예정된 점검 중 "접속 안 됨", "점검 중", "기다리자" 대화도 [일반 대화]다.

== 신고 vs 의견 구분 ==

[결제 문제]와 [계정/운영 리스크 - 세부 유형: 계정 문제]로 묶으려면
"내가 결제/플레이한 결과 피해가 발생했다"는 신고여야 한다.

[계정/운영 리스크 - 세부 유형: 운영 리스크]는 피해 확정 표현이 없어도 된다.
누군가 같은 기능/이벤트/보상 시스템에 대해 "이상하다", "안 된다",
"버그 같다", "동작이 이상했다"처럼 이상 징후를 보고하면 운영 리스크 신고 신호로 본다.
(신고 인원이 1명뿐이어도 신호로 본다 — 인원수는 판단하지 않는다.)
단, 확률 불만, 스펙 부족, 보상 품질 불만, 단순 난이도 한탄은 [일반 대화]다.

아래는 신고가 아니라 [일반 대화]다:

- "현질 효율이 낮음", "패스 가성비 별로" → 결제 만족도 평가 (신고 아님)
- "다이아 부족함", "골드 모자람" → 보유량 부족 한탄 (소실 아님)
- "확률 너무 낮음", "0.22%에서 못 깬 거 ㅠ" → 확률업 결과 불만 (피해 아님)
- "패스 살까 말까" → 구매 검토 (신고 아님)
- "○○ 결제했는데 좋네요" → 후기 (신고 아님)

신고로 묶는 표현 ([결제 문제]):
- "결제했는데 안 들어옴", "○○ 샀는데 미지급", "○○ 인벤에 없음"
- "중복 결제됨", "두 번 빠짐"

신고로 묶는 표현 ([계정/운영 리스크] - 세부 유형: 계정 문제):
- "○○ 빠짐", "○○ 사라짐", "○○ 줄어듦"
- "롤백됐음", "데이터 초기화됨", "계정 접근 안 됨"

신고로 묶는 표현 ([계정/운영 리스크] - 세부 유형: 운영 리스크):
- "버그로 보상 안 들어옴", "이벤트 보상 획득이 안 됨", "획득 버튼이 미활성화됨"
- "신수 융합 뭔가 이상한데", "나도 신수 융합 쪽에서 이상했어",
  "같은 기능이 이상하게 동작함", "버그 같은데"처럼 기능 이상을 말함
- "복사됨", "중복 지급됨", "비정상 지급됨", "특정 조작으로 재화가 비정상 처리됨"

신고로 묶는 표현 ([핵 신고]):
- "쟤 핵쓴다", "핵 같다", "저 사람 핵 아님?", "매크로 쓰는 것 같다"처럼 지금 목격·의심하는 현재형
- "핵쟁이 있다", "불법 프로그램 쓰는 애 있음"처럼 현재 확산 정황 신고

== 2단계: 이슈 신호 판정 (임계·인원수 없음) ==

위 카테고리 중 [서버/접속 장애], [결제 문제], [계정/운영 리스크], [핵 신고]에
해당하는 신고가 **하나라도** 있으면 issue_detected=true.

- 신고 인원수를 세지 않는다. 1명만 신고해도 issue_detected=true다(몇 명인지·임계는 판단하지 않는다).
- 인원이 적다는 이유로 issue_detected=false로 내리거나 카테고리를 [일반 대화]로 강등하는 것을 금지한다.
- [핵 신고]는 시제 규칙만 적용: "지금·방금·현재 목격·신고"인 현재형 신고만 신호로 본다.
  과거·전언·사후 푸념("핵있었나보네", "핵쟁이 극혐")은 신호가 아니다.
- [계정/운영 리스크]는 content에 세부 유형("세부 유형: 계정 문제", "세부 유형: 운영 리스크",
  "세부 유형: 혼합" 중 하나)을 명시한다.
- [일반 대화]만 있고 위 4개 이슈 신호가 전혀 없으면 issue_detected=false.

== 이슈 신호 수집 절차 ==

[결제 문제], [계정/운영 리스크], [서버/접속 장애] 또는 [핵 신고]로 분류한 신고 메시지가 있으면:

1. 해당 신고 메시지의 idx를 모두 모아 evidence_message_ids에 담는다.
2. issue_detected=true로 둔다(인원수 비교·임계는 판단하지 않는다).
3. content에 어떤 카테고리의 어떤 신호가 감지됐는지 1줄로 요약한다(신고 인원수는 적지 마라).

출처(source_id)는 판정 단위가 아니라 신고가 한 채널에 집중됐는지 분산됐는지 확인용이다.

== 공지/봇 메시지 처리 ==

공식 공지나 봇 자동 안내(예: "공지게시판 글은 3개이니...", 점검 일정 안내, 보상 지급 안내)는
사용자 신고가 아니다. 다음 특징을 가진 메시지는 배경 정보로만 사용하고 이슈 신호에서 제외한다:

- 구조화된 긴 글 (용어 정의, 항목 나열, 순위 표 등)
- "공지", "점검", "안내", "보상 지급" 키워드 포함
- 동일 sender가 단발성으로 작성한 정보성 메시지

== 기타 판단 원칙 ==

- 반드시 "카테고리 확정 → 이슈 신호 유무" 순서를 따른다.
- 신고 인원이 적다는 이유로(예: 1명뿐) issue_detected=false로 내리거나
  카테고리를 [일반 대화]로 강등하지 마라. 너는 오직 "이슈 신호가 있는가"만 본다.
- 애매하면 issue_detected=true로 둔다.
  신고인지 의견인지, 시제·방치·경향 때문에 신고로 볼지가 확신이 서지 않으면
  false가 아니라 true로 결정한다(놓치지 않는 것을 우선한다).
- "나만 그런가", "나만 심한가", "저만 이런가요?"처럼 본인이 지금 겪는 증상을 확인·의심하는 표현은
  '나만'이라는 이유로 기기·개인 문제로 단정해 제외하지 말고 현재 진행형 신고로 센다.

== 출력 형식 ==

출력은 반드시 유효한 JSON 객체 1개만 작성하라.
마크다운, 코드블록, 설명 문장, 추가 필드를 절대 출력하지 마라.
허용 필드는 `issue_detected`(boolean), `content`(줄바꿈 없는 한국어 한 줄 문자열),
`evidence_message_ids`(정수 배열) 3개다.

`content` 작성 방식:
- issue_detected=true일 때: 카테고리명, 감지된 이슈 신호, 근거 메시지의 시간/출처/작성자 수를 3~5문장으로 쓴다.
- issue_detected=false일 때: 감지된 주요 대화 유형과 이슈 신호가 없다고 본 이유를 3~5문장으로 쓴다.
- 작성자명·신고 인원수를 쓰지 않는다. content에는 감지된 이슈와 카테고리만 적고, 검증 절차 같은 메타 설명은 넣지 마라.

`evidence_message_ids` 작성 방식:
- 입력 메시지 각 항목의 `idx` 정수만 사용한다. timestamp나 sender, text를 넣지 마라.
- issue_detected=true일 때: 카테고리 분류에서 신고로 인정한 모든 메시지의 idx를 담는다.
  예) 결제 후 미지급 신고 메시지 4개(idx 12, 25, 47, 88) → [12, 25, 47, 88]
- issue_detected=false일 때: 빈 배열 [].
- 일반 대화나 의견·잡담 메시지의 idx는 절대 넣지 마라.

출력 예시:
{"issue_detected":false,"content":"카테고리: 일반 대화. 최근 10분 메시지는 공략 질문과 스펙 상담 중심이며, 접속 장애나 결제/계정 피해처럼 운영 이슈 신호는 확인되지 않는다. 세 출처 모두 정상적인 게임 플레이 관련 대화다.","evidence_message_ids":[]}
{"issue_detected":true,"content":"카테고리: 서버/접속 장애. 점검 종료 후 접속 불가 의심. 최근 10분 내 로그인 실패와 반복 튕김 신고가 감지됐다. kakao_a와 ingame에서 확인된다.","evidence_message_ids":[3,17,42,58,89]}
{"issue_detected":true,"content":"카테고리: 계정/운영 리스크. 세부 유형: 운영 리스크. 보상 획득 불가 신고가 감지됐다. 보상 지급 로직 관련 이슈 신호로 본다.","evidence_message_ids":[34]}
{"issue_detected":true,"content":"카테고리: 결제 문제. 결제 후 미지급 신고가 감지됨(상품 종류는 시즌패스, 월정액 등으로 다르지만 모두 결제 후 미지급 유형). 단일 출처(kakao_a)에서 확인된다.","evidence_message_ids":[12,25,47,88]}
""".strip()

@dataclass(frozen=True)
class LLMCallResult:
    final_response: str
    display_response: str
    thinking_text: str
    raw_api_response: dict[str, Any]
    meta: dict[str, Any]


@dataclass(frozen=True)
class LocalJudgeResult:
    status: str
    raw_response: str
    parsed_response: dict[str, Any] | None
    elapsed_sec: float
    error: str | None = None
    llm_meta: dict[str, Any] = field(default_factory=dict)


def _read_message_value(message: Any, name: str, default: Any = None) -> Any:
    if isinstance(message, dict):
        return message.get(name, default)

    try:
        return message[name]
    except Exception:
        return getattr(message, name, default)


def _prompt_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _message_to_prompt_row(
    index: int,
    message: Any,
) -> dict[str, Any]:
    return {
        "idx": index,
        "source_id": _prompt_text(_read_message_value(message, "source_id", "unknown")),
        "timestamp": _prompt_text(_read_message_value(message, "timestamp", "")),
        "sender": _prompt_text(_read_message_value(message, "sender", "unknown")),
        "text": _prompt_text(_read_message_value(message, "text", "")),
    }


JUDGE_TASK_REMINDER = """
== 작업 재확인 ==

위 메시지는 분석/요약 대상이 아니다. 너는 운영 이슈 1차 탐지기다.
지금 해야 할 일은 다음뿐이다:

1. 각 메시지를 5개 카테고리([서버/접속 장애]/[결제 문제]/[계정/운영 리스크]/[핵 신고]/[일반 대화]) 중 하나로 분류한다.
2. 4개 이슈 카테고리([서버/접속 장애]/[결제 문제]/[계정/운영 리스크]/[핵 신고]) 신고가
   하나라도 있으면 issue_detected=true.
   - 신고 인원수를 세지 않는다. 1명만 신고해도 true다(인원·임계는 판단하지 않는다).
   - 인원이 적다는 이유로 issue_detected=false로 내리거나 [일반 대화]로 강등하지 마라.
   - [핵 신고]는 현재형 신고만 신호로 본다(과거·전언·사후 푸념 제외).
   - [일반 대화]만 있고 이슈 신호가 전혀 없으면 issue_detected=false.
3. evidence_message_ids에 신고로 인정한 메시지의 idx만 담는다.
   issue_detected=true → 모든 신고 idx 배열
   issue_detected=false → 빈 배열 []
   일반 대화/의견/잡담 idx는 절대 넣지 마라.

"결제 후 미지급"은 결제 상품 종류(시즌패스/월정액/스타터팩 등)와 무관하게 모두 같은 유형이다.

출력은 반드시 JSON 1개만. 다른 문장, 마크다운, 요약문 금지.
허용 필드: issue_detected(boolean), content(한국어 한 줄 문자열), evidence_message_ids(정수 배열) 3개.
""".strip()


# 1차 recall 보강용 이슈 키워드(서버/접속·결제·계정/운영). 입력에서 신호를 강조하기 위함.
# 내장 기본값. config.ISSUE_KEYWORDS_FILE이 있으면 그 파일로 덮어쓴다(_load_issue_keywords).
_DEFAULT_ISSUE_KEYWORDS = (
    # 서버/접속 장애
    "튕김", "튕겨", "튕기", "팅겨", "팅김", "팅기", "렉", "꺼짐", "꺼져", "꺼진",
    "접속", "로그인", "로딩", "크래시", "멈춤", "멈춰", "끊김", "끊겨", "재접속",
    "프리징", "먹통", "안들어가", "안 들어가", "다운", "발열",
    # 결제 문제
    "미지급", "안들어옴", "안 들어옴", "중복결제", "중복 결제", "두번결제", "결제했는데",
    "결제 했는데", "환불",
    # 계정/운영 리스크
    "롤백", "사라짐", "사라진", "빠짐", "초기화", "0점", "버그", "복사됨", "중복지급",
    # "핵"은 "탄핵"·"핵심" 등 오탐이 커서 치팅 맥락 패턴으로 한정
    "매크로", "핵쟁이", "핵유저", "핵썼", "핵쓰", "핵있", "핵임", "불법프로그램", "비정상",
)


def _load_issue_keywords() -> tuple[str, ...]:
    """config.ISSUE_KEYWORDS_FILE에서 키워드를 로드한다.

    - 한 줄에 키워드 하나, '#'로 시작하는 줄과 빈 줄은 무시.
    - 파일이 없거나 비어 있으면 내장 기본값(_DEFAULT_ISSUE_KEYWORDS)을 사용.
    """
    path = getattr(config, "ISSUE_KEYWORDS_FILE", None)
    try:
        if path is not None and path.exists():
            kws: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                kws.append(line)
            if kws:
                return tuple(dict.fromkeys(kws))  # 순서 유지 + 중복 제거
    except Exception:
        pass
    return _DEFAULT_ISSUE_KEYWORDS


ISSUE_KEYWORDS = _load_issue_keywords()


def reload_issue_keywords() -> tuple[str, ...]:
    """issue_keywords.txt를 다시 읽어 전역 ISSUE_KEYWORDS를 갱신한다.

    main.py가 매 사이클 시작 시 호출 → main 재시작 없이 키워드 변경을 즉시 반영.
    detect_issue_candidates·matched_issue_keywords가 이 전역을 참조하므로 갱신 즉시 적용된다.
    """
    global ISSUE_KEYWORDS
    ISSUE_KEYWORDS = _load_issue_keywords()
    return ISSUE_KEYWORDS


def detect_issue_candidates(messages: Iterable[Any]) -> list[tuple[int, str]]:
    """이슈 키워드를 포함한 메시지의 (idx, sender) 목록 반환(idx는 1-based)."""
    hits: list[tuple[int, str]] = []
    for index, message in enumerate(messages, start=1):
        text = str(_read_message_value(message, "text", "") or "")
        if any(kw in text for kw in ISSUE_KEYWORDS):
            sender = str(_read_message_value(message, "sender", "") or "")
            hits.append((index, sender))
    return hits


def issue_candidate_sender_count(messages: Iterable[Any]) -> int:
    """이슈 키워드 포함 메시지의 고유 sender 수(키워드 게이트 판정용)."""
    return len({s for _, s in detect_issue_candidates(list(messages)) if s})


def matched_issue_keywords(messages: Iterable[Any]) -> list[str]:
    """입력 메시지에서 실제로 매칭된 이슈 키워드 목록(고유·정렬).

    어떤 키워드 때문에 키워드 게이트가 발동했는지 로그로 보여주기 위함이다.
    """
    found: set[str] = set()
    for m in messages:
        text = str(_read_message_value(m, "text", "") or "")
        for kw in ISSUE_KEYWORDS:
            if kw in text:
                found.add(kw)
    return sorted(found)


def build_prompt(messages: Iterable[Any]) -> str:
    message_list = list(messages)
    candidates = detect_issue_candidates(message_list)
    cand_idx = [i for i, _ in candidates]
    cand_senders = sorted({s for _, s in candidates if s})

    prompt_rows = []
    for index, message in enumerate(message_list, start=1):
        row = _message_to_prompt_row(index, message)
        if index in cand_idx:
            row["issue_keyword"] = True  # 이슈 키워드 후보 태깅
        prompt_rows.append(row)

    payload = {
        "message_count": len(message_list),
        "messages": prompt_rows,
    }

    # 프롬프트의 "10분" 안내를 실제 윈도우 설정(CONTEXT_WINDOW_MINUTES)과 동적 연동.
    # 설정을 바꾸면 지시문·예시의 "N분"이 자동 일치한다(하드코딩 불일치 방지).
    win = config.CONTEXT_WINDOW_MINUTES
    template = JUDGE_PROMPT_TEMPLATE.replace("10분", f"{win}분")
    reminder = JUDGE_TASK_REMINDER.replace("10분", f"{win}분")
    # 1차는 임계를 판단하지 않으므로(이슈 신호 유무만) 카테고리별 임계(MIN_*) 주입은 제거됐다.
    # 신고자 임계(SLACK_CHANNEL_MIN_REPORTERS 등)는 2차 검증 + main.py Python 교차검증에서만 적용한다.

    sections = [template]
    # 키워드 사전 스크리닝(민감도 강화): 후보가 있으면 적극적으로 신고 검토하도록 유도.
    if cand_senders:
        sections.append(
            "== 이슈 키워드 사전 스크리닝 ==\n"
            f"기술 이슈 키워드(튕김·렉·꺼짐·접속·미지급·롤백·0점·버그 등)가 "
            f"서로 다른 사용자 {len(cand_senders)}명에게서 감지됐다"
            "(해당 메시지에 \"issue_keyword\":true 표시).\n"
            f"후보 idx: {cand_idx}\n"
            "이 후보들은 본인이 '이미 해소됐다'고 명시했거나 명백히 게임과 무관한 비유·잡담인 경우를 "
            "제외하고는 현재 진행형 신고로 적극 인정하라. 신고로 셀지 의견으로 뺄지 애매하면 신고로 센다.\n"
            "1차는 놓치지 않는 것(recall)을 최우선으로 한다."
        )
    sections.append("아래 입력 메시지를 기준으로 판단하라.")
    sections.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sections.append(reminder)

    return "\n\n".join(sections)


def _duration_ns_to_sec(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value) / 1_000_000_000.0
    except (TypeError, ValueError):
        return None


def _build_display_response(response_text: str, thinking_text: str) -> str:
    if not config.LLM_SHOW_THINKING_OUTPUT:
        return response_text

    thinking_clean = thinking_text.strip()
    response_clean = response_text.strip()

    if thinking_clean:
        return "\n".join(["[THINKING]", thinking_clean, "", "[RESPONSE]", response_clean])

    return "\n".join(
        ["[THINKING]", "<empty or not returned by server>", "", "[RESPONSE]", response_clean]
    )


def _llm_provider() -> str:
    provider = str(getattr(config, "LLM_PROVIDER", "local") or "local").strip().lower()
    if provider in {"qwen", "ollama", "llama", "llamacpp", "llama.cpp"}:
        return "local"
    if provider in {"claude", "anthropic"}:
        return "anthropic"
    if provider in {"openai", "chatgpt"}:
        return "openai"
    return provider


def active_llm_model_name() -> str:
    provider = _llm_provider()
    if provider == "openai":
        return str(getattr(config, "OPENAI_MODEL", "") or config.LOCAL_LLM_MODEL)
    if provider == "anthropic":
        return str(getattr(config, "ANTHROPIC_MODEL", "") or config.LOCAL_LLM_MODEL)
    return str(config.LOCAL_LLM_MODEL)


def _chat_completions_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _normalized_usage_counts(
    usage: dict[str, Any],
) -> tuple[Any, Any, Any, Any, Any, Any, dict[str, Any], dict[str, Any]]:
    prompt_token_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_token_details, dict):
        prompt_token_details = {}
    completion_token_details = usage.get("completion_tokens_details")
    if not isinstance(completion_token_details, dict):
        completion_token_details = {}

    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    reasoning_tokens = completion_token_details.get("reasoning_tokens")
    output_tokens = usage.get("output_tokens")
    if output_tokens is None and completion_tokens is not None:
        if reasoning_tokens is not None:
            try:
                output_tokens = max(0, int(completion_tokens) - int(reasoning_tokens))
            except (TypeError, ValueError):
                output_tokens = None
        else:
            output_tokens = completion_tokens
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        try:
            total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
        except (TypeError, ValueError):
            total_tokens = None

    return (
        prompt_tokens,
        completion_tokens,
        reasoning_tokens,
        output_tokens,
        total_tokens,
        prompt_token_details.get("cached_tokens", usage.get("cache_read_input_tokens")),
        prompt_token_details,
        completion_token_details,
    )


def llm_generate(prompt: str) -> LLMCallResult:
    provider = _llm_provider()
    if provider == "anthropic":
        return _llm_generate_anthropic(prompt)
    if provider not in {"local", "openai"}:
        raise RuntimeError(
            f"Unsupported LLM_PROVIDER={getattr(config, 'LLM_PROVIDER', '')!r}. "
            "Use local, openai, or anthropic."
        )

    model = active_llm_model_name()
    endpoint = (
        str(getattr(config, "OPENAI_ENDPOINT", "") or "https://api.openai.com")
        if provider == "openai"
        else str(config.LOCAL_LLM_ENDPOINT)
    )
    url = _chat_completions_url(endpoint)
    api_key = str(getattr(config, "OPENAI_API_KEY", "") or "")
    if provider == "openai" and not api_key:
        raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }

    if provider == "openai":
        if getattr(config, "OPENAI_TEMPERATURE", None) is not None:
            body["temperature"] = config.OPENAI_TEMPERATURE
        if getattr(config, "OPENAI_TOP_P", None) is not None:
            body["top_p"] = config.OPENAI_TOP_P
        if getattr(config, "OPENAI_MAX_COMPLETION_TOKENS", None) is not None:
            body["max_completion_tokens"] = config.OPENAI_MAX_COMPLETION_TOKENS
        if getattr(config, "OPENAI_PRESENCE_PENALTY", None) is not None:
            body["presence_penalty"] = config.OPENAI_PRESENCE_PENALTY
    else:
        if config.LLM_TEMPERATURE is not None:
            body["temperature"] = config.LLM_TEMPERATURE
        if config.LLM_TOP_P is not None:
            body["top_p"] = config.LLM_TOP_P
        if config.LLM_TOP_K is not None:
            body["top_k"] = config.LLM_TOP_K
        if config.LLM_NUM_CTX is not None:
            body["num_ctx"] = config.LLM_NUM_CTX
        if config.LLM_NUM_PREDICT is not None:
            body["max_tokens"] = config.LLM_NUM_PREDICT
        if config.LLM_PRESENCE_PENALTY is not None:
            body["presence_penalty"] = config.LLM_PRESENCE_PENALTY

    requested_format: Any = None
    if config.LLM_FORCE_JSON:
        requested_format = "json_schema"
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "alert_response",
                "schema": JUDGE_RESPONSE_SCHEMA,
                "strict": True,
            },
        }

    def post_payload(request_body: dict[str, Any]) -> str:
        data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if provider == "openai":
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=config.LLM_TIMEOUT_SEC) as response:
            return response.read().decode("utf-8", errors="replace")

    response_body = post_payload(body)
    data = json.loads(response_body)

    if "error" in data and "choices" not in data:
        raise RuntimeError(f"{provider} API error: {data['error']}")

    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message_data = choice.get("message", {})
    if not isinstance(message_data, dict):
        message_data = {}

    response_text = str(message_data.get("content") or "")
    thinking_text = str(message_data.get("reasoning_content") or "")
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    prompt_token_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_token_details, dict):
        prompt_token_details = {}
    completion_token_details = usage.get("completion_tokens_details")
    if not isinstance(completion_token_details, dict):
        completion_token_details = {}
    response_keys = sorted(str(k) for k in data.keys())
    message_keys = sorted(str(k) for k in message_data.keys())

    request_options = {k: body[k] for k in ("temperature", "top_p", "top_k", "max_tokens", "max_completion_tokens", "presence_penalty") if k in body}
    (
        prompt_tokens,
        completion_tokens,
        reasoning_tokens,
        output_tokens,
        total_tokens,
        cached_prompt_tokens,
        prompt_token_details,
        completion_token_details,
    ) = _normalized_usage_counts(usage)

    meta: dict[str, Any] = {
        "endpoint": url,
        "provider": provider,
        "model": model,
        "request_think": bool(config.LLM_THINKING_MODE),
        "request_format": requested_format,
        "force_json": bool(config.LLM_FORCE_JSON),
        "show_thinking_output": bool(config.LLM_SHOW_THINKING_OUTPUT),
        "payload_has_think_key": False,
        "payload_think_value": None,
        "request_options": request_options,
        "usage": usage,
        "prompt_chars": len(prompt),
        "response_chars": len(response_text),
        "response_keys": response_keys,
        "message_keys": message_keys,
        "response_has_thinking_field": "reasoning_content" in message_data,
        "response_has_top_level_thinking_field": False,
        "message_has_thinking_field": "reasoning_content" in message_data,
        "thinking_chars": len(thinking_text),
        "thinking_nonempty": bool(thinking_text.strip()),
        "thinking_verified": bool(config.LLM_THINKING_MODE) and bool(thinking_text.strip()),
        "done": choice.get("finish_reason") is not None,
        "done_reason": choice.get("finish_reason"),
        "total_duration_sec": None,
        "load_duration_sec": None,
        "prompt_eval_count": prompt_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "prompt_eval_duration_sec": None,
        "eval_count": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "eval_duration_sec": None,
        "num_predict": body.get("max_tokens", body.get("max_completion_tokens")),
    }

    preview_chars = max(0, int(config.LLM_THINKING_PREVIEW_CHARS))
    if preview_chars > 0 and thinking_text.strip():
        meta["thinking_preview"] = thinking_text.strip()[:preview_chars]

    return LLMCallResult(
        final_response=response_text,
        display_response=_build_display_response(response_text, thinking_text),
        thinking_text=thinking_text,
        raw_api_response=data,
        meta=meta,
    )


def _llm_generate_anthropic(prompt: str) -> LLMCallResult:
    api_key = str(getattr(config, "ANTHROPIC_API_KEY", "") or "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")

    endpoint = str(getattr(config, "ANTHROPIC_ENDPOINT", "") or "https://api.anthropic.com").rstrip("/")
    url = f"{endpoint}/v1/messages" if not endpoint.endswith("/v1/messages") else endpoint
    model = active_llm_model_name()
    max_tokens = int(getattr(config, "ANTHROPIC_MAX_TOKENS", 4096) or 4096)

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": JUDGE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    if config.LLM_TEMPERATURE is not None:
        body["temperature"] = config.LLM_TEMPERATURE
    if config.LLM_TOP_P is not None:
        body["top_p"] = config.LLM_TOP_P

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "x-api-key": api_key,
            "anthropic-version": str(getattr(config, "ANTHROPIC_VERSION", "2023-06-01")),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=config.LLM_TIMEOUT_SEC) as response:
        response_body = response.read().decode("utf-8", errors="replace")

    data_obj = json.loads(response_body)
    if "error" in data_obj:
        raise RuntimeError(f"anthropic API error: {data_obj['error']}")

    content_blocks = data_obj.get("content") or []
    response_parts: list[str] = []
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                response_parts.append(str(block.get("text") or ""))
    response_text = "".join(response_parts).strip()
    usage = data_obj.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    (
        prompt_tokens,
        completion_tokens,
        reasoning_tokens,
        output_tokens,
        total_tokens,
        cached_prompt_tokens,
        _prompt_token_details,
        _completion_token_details,
    ) = _normalized_usage_counts(usage)

    stop_reason = data_obj.get("stop_reason")
    request_options = {k: body[k] for k in ("temperature", "top_p", "max_tokens") if k in body}
    meta: dict[str, Any] = {
        "endpoint": url,
        "provider": "anthropic",
        "model": model,
        "request_think": False,
        "request_format": "prompt_json_only",
        "force_json": bool(config.LLM_FORCE_JSON),
        "show_thinking_output": False,
        "payload_has_think_key": False,
        "payload_think_value": None,
        "request_options": request_options,
        "usage": usage,
        "prompt_chars": len(prompt),
        "response_chars": len(response_text),
        "response_keys": sorted(str(k) for k in data_obj.keys()),
        "message_keys": ["content"],
        "response_has_thinking_field": False,
        "response_has_top_level_thinking_field": False,
        "message_has_thinking_field": False,
        "thinking_chars": 0,
        "thinking_nonempty": False,
        "thinking_verified": False,
        "done": stop_reason is not None,
        "done_reason": stop_reason,
        "total_duration_sec": None,
        "load_duration_sec": None,
        "prompt_eval_count": prompt_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "prompt_eval_duration_sec": None,
        "eval_count": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "eval_duration_sec": None,
        "num_predict": max_tokens,
    }
    return LLMCallResult(
        final_response=response_text,
        display_response=response_text,
        thinking_text="",
        raw_api_response=data_obj,
        meta=meta,
    )


def extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None

    candidates = [stripped]
    response_marker = "[RESPONSE]"
    marker_pos = stripped.rfind(response_marker)
    if marker_pos != -1:
        candidates.insert(0, stripped[marker_pos + len(response_marker) :].strip())

    for candidate_text in candidates:
        if not candidate_text:
            continue

        try:
            parsed = json.loads(candidate_text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        start = candidate_text.find("{")
        end = candidate_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            continue

        try:
            parsed = json.loads(candidate_text[start : end + 1])
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            return parsed

    return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _coerce_evidence_ids(value: Any) -> list[int]:
    """evidence_message_ids를 정수 배열로 정규화. 잘못된 값은 무시."""
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    result: list[int] = []
    seen: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            idx = item
        elif isinstance(item, str):
            stripped = item.strip()
            if not stripped.lstrip("-").isdigit():
                continue
            try:
                idx = int(stripped)
            except ValueError:
                continue
        else:
            continue
        if idx <= 0 or idx in seen:
            continue
        seen.add(idx)
        result.append(idx)
    return result


def normalize_judge_response(candidate: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    if candidate is None:
        return None, "response did not contain a valid JSON object"

    if "error" in candidate and "issue_detected" not in candidate:
        return None, f"model returned error dict: {candidate.get('error')}"

    # issue_detected(신규) 우선, should_alert(구 필드명)도 하위호환으로 허용.
    raw_flag = candidate.get("issue_detected")
    if raw_flag is None:
        raw_flag = candidate.get("should_alert")
    issue_detected = _coerce_bool(raw_flag)
    if issue_detected is None:
        return None, "JSON field issue_detected must be boolean"

    content = candidate.get("content")
    if content is None:
        return None, "JSON field content is required"

    content_text = str(content).replace("\r\n", " ").replace("\n", " ").strip()
    if not content_text:
        return None, "JSON field content must be non-empty"

    # evidence_message_ids는 새 필드. 누락/형식 오류 시 빈 배열로 fallback.
    evidence_ids = _coerce_evidence_ids(candidate.get("evidence_message_ids"))

    return (
        {
            "issue_detected": issue_detected,
            "content": content_text,
            "evidence_message_ids": evidence_ids,
        },
        None,
    )


def _http_error_result(exc: urllib.error.HTTPError, started: float) -> LocalJudgeResult:
    body = ""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        pass

    provider = _llm_provider()
    status = "error"
    detail = body[:500]

    try:
        parsed_body = json.loads(body)
    except json.JSONDecodeError:
        parsed_body = None

    if isinstance(parsed_body, dict):
        error_obj = parsed_body.get("error")
        if isinstance(error_obj, dict):
            error_type = str(error_obj.get("type") or "")
            error_code = str(error_obj.get("code") or "")
            error_message = str(error_obj.get("message") or "")
            if error_type or error_code or error_message:
                detail = (
                    f"type={error_type or 'unknown'}, "
                    f"code={error_code or 'unknown'}, "
                    f"message={error_message}"
                )
            if provider == "openai" and (
                error_type == "insufficient_quota" or error_code == "insufficient_quota"
            ):
                status = "quota_error"
            elif exc.code == 429:
                status = "rate_limit"
    elif exc.code == 429:
        status = "rate_limit"

    return LocalJudgeResult(
        status=status,
        raw_response="",
        parsed_response=None,
        elapsed_sec=time.perf_counter() - started,
        error=f"HTTP {exc.code} error: {detail}",
        llm_meta={
            "provider": provider,
            "request_think": bool(config.LLM_THINKING_MODE),
            "http_status": exc.code,
            "http_error_body": body[:2000],
        },
    )


def judge_messages(messages: list[Any]) -> LocalJudgeResult:
    started = time.perf_counter()

    if not messages:
        return LocalJudgeResult(
            status="skipped_empty",
            raw_response="",
            parsed_response=None,
            elapsed_sec=time.perf_counter() - started,
            error=None,
            llm_meta={
                "skipped": True,
                "reason": "no_messages",
                "request_think": bool(config.LLM_THINKING_MODE),
                "request_format": None,
            },
        )

    try:
        prompt = build_prompt(messages)

        call_result = llm_generate(prompt)
        candidate = extract_json_object(call_result.final_response)
        parsed, validation_error = normalize_judge_response(candidate)

        if parsed is not None:
            status = "ok"
            error = None
        else:
            error = f"LLM parse failed: {validation_error}"
            status = "parse_error"

        return LocalJudgeResult(
            status=status,
            raw_response=call_result.display_response,
            parsed_response=parsed,
            elapsed_sec=time.perf_counter() - started,
            error=error,
            llm_meta=call_result.meta,
        )

    except urllib.error.HTTPError as exc:
        return _http_error_result(exc, started)
    except urllib.error.URLError as exc:
        return LocalJudgeResult(
            status="error",
            raw_response="",
            parsed_response=None,
            elapsed_sec=time.perf_counter() - started,
            error=f"LLM connection error: {exc}",
            llm_meta={"request_think": bool(config.LLM_THINKING_MODE)},
        )
    except TimeoutError as exc:
        return LocalJudgeResult(
            status="timeout",
            raw_response="",
            parsed_response=None,
            elapsed_sec=time.perf_counter() - started,
            error=f"LLM timeout: {exc}",
            llm_meta={"request_think": bool(config.LLM_THINKING_MODE)},
        )
    except Exception as exc:
        return LocalJudgeResult(
            status="error",
            raw_response="",
            parsed_response=None,
            elapsed_sec=time.perf_counter() - started,
            error=f"Unexpected local LLM error: {type(exc).__name__}: {exc}",
            llm_meta={"request_think": bool(config.LLM_THINKING_MODE)},
        )


# =========================
# 2차 정밀 검증 (OpenAI) — 하이브리드
# =========================

VERIFY_SYSTEM_PROMPT = """
너는 모바일 게임 운영 이슈의 정밀 분석기다. 아래 채팅 메시지를 처음부터 독립적으로 분석해,
운영자에게 즉시 알릴 진짜 이슈인지 판단하고 카테고리를 분류한다.
다른 필터가 너를 호출했을 뿐이며, 그 필터의 분류·판단은 너에게 전달되지 않는다.
선입견 없이 오직 아래 raw 메시지만으로 판단하라.
반드시 유효한 JSON 객체 1개만 출력한다.
허용 필드는 category(문자열), confirmed(boolean), reason(한국어 한 줄), reporter_message_ids(정수 배열), evidence_message_ids(정수 배열) 5개뿐이다.
마크다운·설명·추가 필드 금지.
""".strip()

VERIFY_USER_TEMPLATE = """
아래 채팅 메시지를 운영 이슈 관점에서 처음부터 독립적으로 분석하라. 외부 힌트는 없다.

== 소스 구분 ==
각 메시지의 source_id로 소스를 구분한다(모두 동일 게임에 대한 채팅이다):
- `ingame` = 인게임 채팅(게임 안에서 직접 보고 말함, 사진 첨부 불가).
- `kakao_`로 시작(kakao_a, kakao_b …) = 서로 분리된 외부 채팅 커뮤니티 방(사진 첨부 가능).
신고자 카운팅은 방(source_id)이 아니라 작성자(sender) 기준으로만 한다(같은 작성자가 여러 방에 있어도 1명).

== 1단계: 카테고리 분류 ==
메시지를 직접 보고 올바른 카테고리를 `category` 필드에 출력하라.
정규 카테고리: `서버/접속 장애` / `결제 문제` / `계정/운영 리스크` / `핵 신고` / `해당없음`

- [서버/접속 장애]: 접속 불가, 로그인 불가, 무한 로딩, 반복 튕김, 앱 크래시, 서버 전체 장애,
  점검 종료 후 다수 접속 불가, 점검 외 시간 갑작스런 접속 장애.
- [결제 문제]: 결제 완료 후 상품/재화 미지급, 중복 결제. 상품 종류(시즌패스/월정액/스타터팩 등)와
  무관하게 "결제 후 미지급"은 모두 같은 1개 유형이다.
- [계정/운영 리스크]: (계정 문제) 재화/아이템 소실·계정 롤백·데이터 초기화·계정 접근 이상,
  또는 (운영 리스크) 버그 악용·비정상 재화 획득·복사/중복 지급·이벤트·보상 전체 오지급·시스템 기능 오류.
  content에 "세부 유형: 계정 문제/운영 리스크/혼합"을 명시한다.
- [핵 신고]: 타 유저의 핵·매크로·외부 불법 프로그램 사용을 지금 목격·신고·의심하는 현재형 발화.
- [해당없음]: 위 4개 이슈 신호가 없음.

다음은 신고가 아니라 일반 대화이므로 [해당없음] 쪽이다(이슈로 분류하지 마라):
- "현질 효율 낮음", "패스 가성비 별로" → 결제 만족도 평가
- "다이아/골드 부족" → 보유량 한탄(소실 아님)
- "확률 너무 낮음", "0.x%에서 못 깸" → 확률 결과 불만
- "○○ 못넘어가/막혀있음", "스테 못밀었음" → 진행 실패
- "명중만 올리면 다컨", "○경도 안 됨" → 스펙 부족 한탄
- "핵있었나보네", "핵쟁이 극혐" → 핵 사후 푸념(능동 신고 아님)
- 예정된 점검 중 "접속 안 됨/점검 중/기다리자" 대화
공지/봇 자동 안내(구조화된 긴 글, "공지·점검·안내·보상 지급" 키워드 포함, 단발 정보성 메시지)는
사용자 신고가 아니다 — 배경 정보로만 쓰고 신고로 세지 마라.

== 2단계: 유효 신고 판별 ==
이것이 운영자에게 즉시 알릴 진짜 이슈인지 정밀 판단하라.

[유효 신고의 핵심 원칙]
작성자가 "본인이 직접 겪거나 · 직접 시도하다 이상을 만나거나 · 직접 목격/의문을 제기한" 경우만 유효 신고다.
- 직접 피해: "○○ 사라짐/초기화됨/안 들어옴"
- 직접 시도 실패: "저도 안 들어가져요"(직접 접속 시도), "구매가 안 되네"(직접 구매 시도)
- 직접 목격·의문(본인 피해 표현이 없어도): 기능/상점/시스템이 이상하게 동작함을 직접 보고 언급·질문
  ("~가 이상하게 떠있네", "이거 왜 이러지?", "버그인가?", "원래 이래?")
반대로, **본인 경험 표명 없이** 남의 글·사진에 보내는 단순 감탄·반응("헐 대박", "헉 진짜요?", "그러게요")이나
제3자 대리 언급은 신고로 세지 않는다.
- 단, "나도/저도" 등 본인의 같은 경험·상태가 함께 드러나면 감탄이 붙어도 유효다("헐 나도 안돼", "헐 저도 이상함").
- 감탄·반응만 있고 본인 경험이 모호하면("헐 이상하네") 그 작성자의 다른 메시지 맥락을 보고 본인 경험 여부를 판단한다.

[소스 보정]
- 인게임(`ingame`) 메시지: 게임 안에서 직접 보므로, 동조성 신고("나도 안 돼")는 물론
  이상 현상을 보고 던지는 짧은 반응·의문("머임 저거", "이거 뭐지", "9천다야?")도 직접 목격으로 폭넓게 인정한다.
- 카카오(`kakao_*`) 메시지: 사진·남의 글에 대한 반응이 섞이므로, 본인의 직접 경험·시도·관찰이 드러나야 유효다
  ("저도 안돼"=본인 경험, "헐 저도 이상함"=감탄+본인 경험 → 유효). 본인 경험 표명 없는 단순 감탄·반응만이면 제외하되,
  애매하면 그 작성자의 다른 메시지로 맥락을 확인한다.

[현재 상태(시제) 판단]
시제가 과거·추정형이어도 본인이 직접 겪은 피해이고 그 상태가 현재 미해소(복구·정상화 언급 없음)면
유효 신고로 센다("세번 초기화당함", "안 받고 껐더니 없어졌나봐요"). 단 본인이 "지금은 정상/복구됨"이라
밝혔으면 제외한다.

아래에 해당하는 발화는 신고로 세지 않는다(제외):
- 남에 대한 대리 언급(본인 피해가 아니라 제3자 상태를 말함: "○○님 ~안 됐대?", "○○님 점수 누적 안 됐나봐")
  단, 본인이 직접 겪은 피해를 질문 형태로 말한 것("저 ○○ 안 됐는데 버그인가요?")은 신고로 센다.
- 본인 정상 진술·반대("난 왜 안 그러지", "나는 됨")
- 이미 해소("지금은 됨/복구됨")거나, 본인 피해와 무관한 순수 과거 일화(현재 상태에 영향 없음).
  (단 본인 피해가 현재도 미해소면 위 [현재 상태] 규칙대로 유효)
- 누적 빈도·경향 표현("요근래·최근·요즘·자주·종종·가끔 ~한다/그런다")만 있는 발화.
  (단 "지금·방금·또·자꾸" 같은 '이 순간/이 접속'의 현재 표현이면 신고로 센다.
   "요즘들어·최근 등 기간 표현 + 잘/자주 튕겨"처럼 과거부터 이어진 경향은,
   지금도 발생하더라도 '현재 단일 신고'가 아니므로 제외한다.
   단어가 아니라 맥락으로 '지금 호소'인지 '기간 경향'인지 구분한다.
   이 항목은 '요근래·최근·요즘' 등 빈도/경향 단어가 명시된 경우에만 적용하고,
   시제가 드러나지 않은 일반 진술("튕겨요", "안 돼요")은 제외하지 말 것.)
- 정상 사양 설명·타인 안내("원래 그래요")
- 난이도·확률·단순 불만
- [핵 신고] 시: 과거·전언·사후 푸념("핵있었나보네", "핵쟁이 극혐")은 제외. 지금 목격·의심만 신고로 센다.

위 제외는 "해당 메시지 1건"에만 적용한다(작성자 전체를 제외하지 말 것).
같은 작성자라도 다른 메시지에서 현재 진행형으로 신고했거나, 다른 작성자의 유효 신고가 있으면
그 메시지들은 정상 신고로 센다. 예: A가 "요근래 자주 튕김"(제외) + "지금 또 튕겼다"(유지)이면
A는 현재 신고자로 카운트한다.

본인이 지금 직접 겪은 동일 이슈를 서로 다른 사용자가 임계 인원 이상
([서버/접속 장애] {min_outage}명, [계정/운영 리스크] {min_risk}명, [결제 문제] {min_payment}명, [핵 신고] {min_cheat}명)
신고했을 때만 confirmed=true.
제외 후 남은 유효 신고 메시지의 고유 작성자 수로 임계를 판단한다. 미달하거나 애매하면 confirmed=false.

confirmed=true이면, 임계 판단에서 신고자로 카운트한 본인 현재형 신고 메시지의 idx를 reporter_message_ids 배열에 담아라.
confirmed=true이면, 임계 판단의 근거가 된 신고 메시지와 운영자 이해에 필요한 맥락 메시지의 idx를 evidence_message_ids 배열에 담아라.
신고자 본인의 현재형 신고 발화는 물론, 그 신고의 증상·맥락을 구체화한 발화(예: "몇층이심?"에 이어진
"렉걸렸네", "20초 뒤에 채팅나가네" 등 연속된 신고 흐름)도 함께 포함해, 운영자가 상황을 이해할 수 있게 하라.
단 제외 대상(질문성 잡담·제3자 언급·경향 표현·무관한 대화)은 넣지 마라.
confirmed=false이면 reporter_message_ids와 evidence_message_ids는 빈 배열([])로 둔다.

아래 입력 메시지(idx, source_id, timestamp, sender, text):
{payload}
""".strip()

VERIFY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "reason": {"type": "string"},
        "reporter_message_ids": {"type": "array", "items": {"type": "integer"}},
        "evidence_message_ids": {"type": "array", "items": {"type": "integer"}},
        "category": {"type": "string"},
    },
    "required": ["confirmed", "reason", "reporter_message_ids", "evidence_message_ids", "category"],
    "additionalProperties": False,
}


def verify_alert_cloud(
    messages: Iterable[Any],
    category: str | None = None,
    local_content: str | None = None,
) -> dict[str, Any]:
    """OpenAI 2차 정밀 검증. 1차(로컬)와 독립적으로 provider를 openai로 고정.

    원점 판단(2026-06-18~): 1차 분류·content를 프롬프트에 주입하지 않는다(prime 제거).
    `category`/`local_content` 인자는 호출부 호환을 위해 유지하나 프롬프트에 사용하지 않는다.

    반환 dict: status, confirmed(bool|None), reason, prompt_tokens, completion_tokens,
    total_tokens, error.
    - status="ok"이고 confirmed=False면 Slack 차단, True면 발송.
    - status!="ok"(no_key/error/parse_error/skipped)면 호출측이 fallback 정책 적용.
    """
    result: dict[str, Any] = {
        "status": "ok",
        "confirmed": None,
        "reason": "",
        "reporter_message_ids": [],
        "evidence_message_ids": [],
        "category": None,
        "thinking": "",
        "reasoning_tokens": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "error": None,
    }
    provider = str(getattr(config, "VERIFY_PROVIDER", "openai") or "openai").strip().lower()
    if provider != "openai":
        result["status"] = "skipped_provider"
        return result
    api_key = str(getattr(config, "OPENAI_API_KEY", "") or "")
    if not api_key:
        result["status"] = "no_key"
        result["error"] = "OPENAI_API_KEY empty"
        return result

    rows = [_message_to_prompt_row(i, m) for i, m in enumerate(list(messages), start=1)]
    payload = json.dumps(
        {"message_count": len(rows), "messages": rows},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # 2차는 confirmed(base 임계) 판단만 한다. A채널 라우팅 임계는 Python(main.py)이 재카운트로
    # 적용하므로 프롬프트에 주입하지 않는다(LLM이 A 라우팅을 판단하지 않음).
    m = config.SLACK_CHANNEL_MIN_REPORTERS
    user_prompt = VERIFY_USER_TEMPLATE.format(
        payload=payload,
        min_outage=m.get("서버/접속 장애", 2),
        min_risk=m.get("계정/운영 리스크", 2),
        min_payment=m.get("결제 문제", 3),
        min_cheat=m.get("핵 신고", 3),
    )

    endpoint = str(getattr(config, "OPENAI_ENDPOINT", "") or "https://api.openai.com")
    url = _chat_completions_url(endpoint)
    model = str(getattr(config, "OPENAI_MODEL", "") or "gpt-5.4")
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": VERIFY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "verify_response",
                "schema": VERIFY_RESPONSE_SCHEMA,
                "strict": True,
            },
        },
    }
    if getattr(config, "OPENAI_MAX_COMPLETION_TOKENS", None) is not None:
        body["max_completion_tokens"] = config.OPENAI_MAX_COMPLETION_TOKENS
    if getattr(config, "OPENAI_TEMPERATURE", None) is not None:
        body["temperature"] = config.OPENAI_TEMPERATURE
    # reasoning 모델이면 thinking 강제/강도 지정 (비reasoning 모델은 서버가 무시).
    if getattr(config, "OPENAI_REASONING_EFFORT", None):
        body["reasoning_effort"] = config.OPENAI_REASONING_EFFORT

    data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        request = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=config.LLM_TIMEOUT_SEC) as response:
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        result["status"] = "error"
        result["error"] = f"HTTP {exc.code}: {detail[:300]}"
        return result
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        data = json.loads(response_body)
    except ValueError as exc:
        result["status"] = "error"
        result["error"] = f"json parse failed: {exc}"
        return result
    if "error" in data and "choices" not in data:
        result["status"] = "error"
        result["error"] = str(data["error"])[:300]
        return result

    result["raw_api_json"] = data
    choices = data.get("choices") or []
    msg = (choices[0].get("message") if choices else {}) or {}
    content = str(msg.get("content") or "")
    # reasoning_content: o1/o3. thinking: GPT-5.4 계열. content 배열에 thinking 블록으로 오는 경우도 처리.
    _thinking = msg.get("reasoning_content") or msg.get("thinking") or ""
    if not _thinking and isinstance(msg.get("content"), list):
        for blk in msg["content"]:
            if isinstance(blk, dict) and blk.get("type") == "thinking":
                _thinking = blk.get("thinking") or blk.get("text") or ""
                break
        content = " ".join(
            blk.get("text", "") for blk in msg["content"]
            if isinstance(blk, dict) and blk.get("type") != "thinking"
        )
    result["thinking"] = str(_thinking)
    usage = data.get("usage") or {}
    if isinstance(usage, dict):
        result["prompt_tokens"] = usage.get("prompt_tokens")
        result["completion_tokens"] = usage.get("completion_tokens")
        result["total_tokens"] = usage.get("total_tokens")
        ctd = usage.get("completion_tokens_details")
        result["reasoning_tokens"] = ctd.get("reasoning_tokens") if isinstance(ctd, dict) else None

    parsed = extract_json_object(content) or {}
    if "confirmed" not in parsed:
        result["status"] = "parse_error"
        result["error"] = f"no confirmed in response: {content[:200]}"
        return result
    result["confirmed"] = bool(parsed.get("confirmed"))
    result["reason"] = str(parsed.get("reason") or "")
    reporter_raw = parsed.get("reporter_message_ids") or []
    reporter_ids: list[int] = []
    if isinstance(reporter_raw, list):
        for x in reporter_raw:
            try:
                reporter_ids.append(int(x))
            except (TypeError, ValueError):
                continue
    result["reporter_message_ids"] = reporter_ids
    ev_raw = parsed.get("evidence_message_ids") or []
    ev_ids: list[int] = []
    if isinstance(ev_raw, list):
        for x in ev_raw:
            try:
                ev_ids.append(int(x))
            except (TypeError, ValueError):
                continue
    result["evidence_message_ids"] = ev_ids
    result["category"] = str(parsed.get("category") or "")
    return result


def format_response_for_display(result: LocalJudgeResult) -> str:
    """Return text for PowerShell display.

    Important behavior:
    - When LLM_SHOW_THINKING_OUTPUT=1 and LLM_DISPLAY_RAW_WITH_THINKING=1,
      show result.raw_response first. result.raw_response contains the explicit
      [THINKING] / [RESPONSE] sections created from the server's separate thinking
      field and response field.
    - Otherwise, show the normalized parsed JSON so normal loop output remains
      stable and easy to parse visually.
    """

    if (
        config.LLM_SHOW_THINKING_OUTPUT
        and getattr(config, "LLM_DISPLAY_RAW_WITH_THINKING", True)
        and result.raw_response.strip()
    ):
        return result.raw_response.strip()

    if result.parsed_response is not None:
        return json.dumps(result.parsed_response, ensure_ascii=False, indent=2)

    if result.raw_response.strip():
        return result.raw_response.strip()

    return json.dumps({"status": result.status, "error": result.error}, ensure_ascii=False, indent=2)


def format_response_for_storage(result: LocalJudgeResult) -> str:
    """Return text to save into local_llm_runs.raw_response.

    Default storage remains final JSON only, because thinking text can be large
    and may include verbose model reasoning. Set LLM_STORE_RAW_WITH_THINKING=1
    only for short diagnostic runs when you explicitly want to persist it.
    """

    if (
        config.LLM_SHOW_THINKING_OUTPUT
        and getattr(config, "LLM_STORE_RAW_WITH_THINKING", False)
        and result.raw_response.strip()
    ):
        return result.raw_response.strip()

    if result.parsed_response is not None:
        return json.dumps(result.parsed_response, ensure_ascii=False, indent=2)

    if result.raw_response.strip():
        return result.raw_response.strip()

    return json.dumps({"status": result.status, "error": result.error}, ensure_ascii=False, indent=2)


def format_thinking_status_for_display(result: LocalJudgeResult) -> str:
    meta = result.llm_meta or {}

    if meta.get("skipped"):
        return (
            "[LLM THINK] skipped=true, "
            f"reason={meta.get('reason')}, "
            f"request_think={meta.get('request_think')}, "
            f"request_format={meta.get('request_format')}"
        )

    response_has_thinking_field = bool(meta.get("response_has_thinking_field"))
    message_has_thinking_field = bool(meta.get("message_has_thinking_field"))
    top_level_has_thinking_field = bool(meta.get("response_has_top_level_thinking_field"))
    thinking_chars = int(meta.get("thinking_chars") or 0)
    response_chars = int(meta.get("response_chars") or 0)

    if bool(meta.get("thinking_verified")):
        verdict = "verified_nonempty_thinking"
    elif response_has_thinking_field:
        verdict = "thinking_field_empty"
    else:
        verdict = "thinking_field_missing"

    lines = [
        (
            "[LLM THINK] "
            f"request_think={meta.get('request_think')}, "
            f"payload_has_think_key={meta.get('payload_has_think_key')}, "
            f"payload_think_value={meta.get('payload_think_value')}, "
            f"request_format={meta.get('request_format')}, "
            f"verdict={verdict}"
        ),
        (
            "[LLM THINK] "
            f"response_has_thinking_field={response_has_thinking_field}, "
            f"message_has_thinking_field={message_has_thinking_field}, "
            f"top_level_has_thinking_field={top_level_has_thinking_field}, "
            f"thinking_nonempty={bool(meta.get('thinking_nonempty'))}, "
            f"thinking_chars={thinking_chars}, "
            f"response_chars={response_chars}, "
            f"show_thinking_output={meta.get('show_thinking_output')}"
        ),
        (
            "[LLM META] "
            f"done={meta.get('done')}, "
            f"done_reason={meta.get('done_reason')}, "
            f"prompt_chars={meta.get('prompt_chars')}, "
            f"prompt_eval_count={meta.get('prompt_eval_count')}, "
            f"eval_count={meta.get('eval_count')}, "
            f"num_predict={meta.get('num_predict')}, "
            f"total_duration_sec={meta.get('total_duration_sec')}"
        ),
        (
            "[LLM TOKENS] "
            f"input={meta.get('prompt_eval_count')}, "
            f"cached_input={meta.get('cached_prompt_tokens')}, "
            f"completion={meta.get('eval_count')}, "
            f"reasoning={meta.get('reasoning_tokens')}, "
            f"output={meta.get('output_tokens')}, "
            f"total={meta.get('total_tokens')}"
        ),
        f"[LLM META] request_options={meta.get('request_options')}",
        f"[LLM META] response_keys={meta.get('response_keys')}",
        f"[LLM META] message_keys={meta.get('message_keys')}",
    ]

    preview = meta.get("thinking_preview")
    if preview:
        lines.append(f"[LLM THINK PREVIEW] {preview}")

    return "\n".join(lines)


def _safe_print(text: str) -> None:
    """cp949 등 좁은 코드페이지 터미널에서 인코딩 불가 문자를 '?' 로 대체해 출력한다."""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc))


def print_llm_response(text: str, *, issue_detected: bool = False) -> None:
    line = "=" * 80
    if config.LLM_RESPONSE_GREEN_OUTPUT:
        color = chr(27) + ("[91m" if issue_detected else "[92m")
        reset = chr(27) + "[0m"
        print(f"{color}{line}")
        print(f"[LLM RESPONSE] {active_llm_model_name()}")
        print(line)
        _safe_print(text)
        print(f"{line}{reset}")
    else:
        print(line)
        print(f"[LLM RESPONSE] {active_llm_model_name()}")
        print(line)
        _safe_print(text)
        print(line)
