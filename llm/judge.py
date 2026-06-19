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
from llm.prompt_judge import (
    JUDGE_RESPONSE_SCHEMA,
    JUDGE_SYSTEM_PROMPT,
    JUDGE_PROMPT_TEMPLATE,
    JUDGE_TASK_REMINDER,
)
from llm.prompt_verify import (
    VERIFY_SYSTEM_PROMPT,
    VERIFY_USER_TEMPLATE,
    VERIFY_RESPONSE_SCHEMA,
)


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
    - status="ok"이고 confirmed=False면 Slack 차단, True면 (Python 교차검증 통과 시) 발송.
    - status!="ok"(no_key/error/parse_error/skipped)면 호출측이 발송을 차단한다(1차 fallback 없음; 오류는 DB에 기록).
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
