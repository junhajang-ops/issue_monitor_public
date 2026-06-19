"""2차 검증(OpenAI) 호출 분석 + 1차 입력 메시지 조회 툴.

2차 호출 경로(둘은 동시 성립 가능 → '로컬+키워드'):
  - 로컬 경로  : 1차 9B가 should_alert=true 로 올림
  - 키워드 경로: 9B 판정과 무관하게 이슈 키워드가 신규(is_new) 메시지에 있어 게이트가 강제 전달

데이터 출처:
  - 2차 호출/결과 : local_llm_runs.cloud_verify_* (DB)
  - should_alert  : local_llm_runs.raw_response 의 마지막 JSON
  - 키워드 게이트 : 그 run 의 스냅샷 normalized/messages.jsonl 로 재현(detect_issue_candidates + is_new)

사용법(프로젝트 루트에서 실행):
  python tools/llm_check.py              # (1) 2차 호출한 최근 50 run (줄마다 [번호] 표시)
  python tools/llm_check.py all          #     동일
  python tools/llm_check.py keyword      # (2) 키워드 게이트로 2차 호출한 최근 50 run
  python tools/llm_check.py local        # (3) 로컬 모델(should_alert)로 2차 호출한 최근 50 run
  python tools/llm_check.py 1 3 10 11    # (4) 직전 목록의 1·3·10·11번 run 정규화 메시지를 한 번에 전체 출력
  python tools/llm_check.py <run_id> [<run_id> ...]   # run_id 직접 조회(여러 개 가능)
  python tools/llm_check.py 1 3 --raw            # 번호 조회 시 raw 원본 파일도
  python tools/llm_check.py <run_id> --thinking  # 1차 raw_response(thinking 포함 원문) 전체 출력
  python tools/llm_check.py [모드] --limit 30    # 표시 개수 조정
  python tools/llm_check.py [대상] --out out.txt # UTF-8 파일 저장(콘솔 한글 깨짐 회피)

(1~3 목록을 한 번 보면 직전 목록이 캐시되어, 줄 앞 [번호]만으로 (4) 상세를 여러 건 동시에 봅니다.
 번호 대신 run_id 를 직접 적어도 됩니다. 캐시는 data/.llm_check_last.json 에 저장됩니다.)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from _replay_core import nearest_runs, reconstruct  # noqa: E402
from llm.judge import detect_issue_candidates, matched_issue_keywords  # noqa: E402

SNAP = config.SNAPSHOT_DIR
RUNID_RE = re.compile(r"^\d{8}_\d{6}$")
SA_RE = re.compile(r'"(?:issue_detected|should_alert)"\s*:\s*(true|false)')


def _should_alert(raw_response: str | None):
    ms = SA_RE.findall(raw_response or "")
    return None if not ms else (ms[-1] == "true")


def _gate_from_msgs(msgs):
    """메시지 리스트(dict, is_new 포함)로 키워드 게이트를 재현.

    반환: (gate(bool), kws(list))
    - kws는 '게이트에 실제 기여한' 키워드만 = 신규(is_new) 메시지에서 매칭된 키워드.
      (로컬 단독 run은 게이트 미발동이므로 kws=[] → 키워드 표시 없이 '경로:로컬'만 보인다.)
    """
    cand_idx = {i for i, _ in detect_issue_candidates(msgs)}
    gate_rows = [m for i, m in enumerate(msgs, start=1) if i in cand_idx and bool(m.get("is_new"))]
    return bool(gate_rows), matched_issue_keywords(gate_rows)


def _keyword_gate(run_id: str):
    """목록용: 스냅샷 normalized 를 읽어 게이트 재현. 스냅샷 없으면 (None, [])."""
    path = SNAP / run_id / "normalized" / "messages.jsonl"
    if not path.exists():
        return None, []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return _gate_from_msgs(rows)


def _fetch_2nd_runs():
    with sqlite3.connect(str(config.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT run_id, created_at, cloud_verified, cloud_verify_status, raw_response "
            "FROM local_llm_runs WHERE cloud_verify_status IS NOT NULL "
            "ORDER BY created_at DESC"
        ).fetchall()


def _cache_path() -> Path:
    return Path(config.DB_PATH).parent / ".llm_check_last.json"


def _save_cache(mode: str, ids: list[str]) -> None:
    try:
        _cache_path().write_text(
            json.dumps({"mode": mode, "ids": ids}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _load_cache():
    p = _cache_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _route_label(sa, gate) -> str:
    if sa and gate:
        return "로컬+키워드"
    if sa:
        return "로컬"
    if gate:
        return "키워드"
    if sa is None and gate is None:
        return "정보없음"
    return "해당없음(로컬·키워드 모두 미해당)"


def _run_db_info(run_id: str):
    """DB에서 should_alert·2차결과·window를 읽는다. run 없으면 None."""
    with sqlite3.connect(str(config.DB_PATH)) as conn:
        row = conn.execute(
            "SELECT raw_response, cloud_verify_status, cloud_verified, "
            "context_window_start, window_end FROM local_llm_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "sa": _should_alert(row[0]),
        "called": row[1] is not None,
        "verified": row[2],
        "cws": row[3],
        "we": row[4],
    }


def _fmt_run(idx, run_id, created_at, verified, sa, gate, kws) -> str:
    date = created_at.split("T")[0] if created_at else ""
    hhmm = created_at.split("T")[1][:5] if (created_at and "T" in created_at) else ""
    result = "발송" if verified == 1 else ("차단" if verified == 0 else "?")
    route = _route_label(sa, gate)
    kw = ("  키워드:[" + ",".join(kws) + "]") if kws else ""
    return f"[{idx:2d}] {run_id}  {date} {hhmm}  결과:{result}  경로:{route}{kw}"


def list_mode(mode: str, limit: int, write) -> None:
    rows = _fetch_2nd_runs()
    titles = {
        "all": "2차 호출한",
        "keyword": "키워드 게이트로 2차 호출한",
        "local": "로컬 모델(should_alert=true)로 2차 호출한",
    }
    write(f"=== {titles[mode]} 최근 {limit} run (전체 2차 호출 {len(rows)} run 중) ===")
    write("(상세: python tools/llm_check.py 1 3 10  ← 줄 앞 [번호] 띄어쓰기로 여러 개 / run_id 직접도 가능)\n")
    shown = 0
    collected: list[str] = []
    for r in rows:
        if shown >= limit:
            break
        sa = _should_alert(r["raw_response"])
        if mode == "local" and not sa:
            continue
        # 모든 모드에서 게이트를 계산해 '로컬+키워드' 교집합까지 표시한다.
        gate, kws = _keyword_gate(r["run_id"])
        if mode == "keyword" and not gate:
            continue
        shown += 1
        collected.append(r["run_id"])
        write(_fmt_run(shown, r["run_id"], r["created_at"], r["cloud_verified"], sa, gate, kws))
    _save_cache(mode, collected)
    write("\n(해당 없음)" if shown == 0 else f"\n총 {shown} run  ·  번호로 상세: llm_check.py <번호 ...>")


def _fetch_all_fields(run_id: str):
    """local_llm_runs 의 모든 컬럼을 dict 로 반환. 없으면 None."""
    with sqlite3.connect(str(config.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM local_llm_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def _dump_db_fields(run_id: str, write, show_thinking: bool = False) -> None:
    """local_llm_runs DB 스키마의 세부 필드 값을 모두 출력(저장된 값 그대로, 재계산 없음).

    reporter_count 는 발송 시점에 main 이 계산해 저장한 cloud_reporter_count 컬럼을 그대로 읽는다.
    1차 raw_response(thinking 포함 원문)는 기본 생략하고 --thinking 시에만 전체 출력(DB에는 항상 저장됨).
    """
    d = _fetch_all_fields(run_id)
    if d is None:
        return
    write("\n" + "─" * 70)
    write("[DB 필드] local_llm_runs (전체)")
    write(f"  status={d['status']}  error={d['error']}")
    write(f"  message_count={d['message_count']}  new_message_count={d['new_message_count']}")
    write(f"  context_window_start={d['context_window_start']}")
    write(f"  window_start={d['window_start']}  window_end={d['window_end']}")
    write(f"  [1차 판정] has_possible_issue={d['has_possible_issue']}")
    write(
        f"  [1차 토큰] prompt={d['llm_prompt_tokens']} cached={d['llm_cached_prompt_tokens']} "
        f"completion={d['llm_completion_tokens']} reasoning={d['llm_reasoning_tokens']} "
        f"output={d['llm_output_tokens']} total={d['llm_total_tokens']}"
    )
    write(
        f"  [1차 chars] prompt={d['llm_prompt_chars']} response={d['llm_response_chars']} "
        f"thinking={d['llm_thinking_chars']}"
    )
    write(
        f"  [2차 결과] cloud_verify_status={d['cloud_verify_status']} "
        f"cloud_verified={d['cloud_verified']} cloud_reporter_count={d['cloud_reporter_count']}"
    )
    # A 채널 발송 조건(저장값 기준): 2차 호출됐고 confirmed & reporter_count>=임계.
    if d["cloud_verify_status"] is not None:
        rc = d["cloud_reporter_count"]
        _min = config.SLACK_CHANNEL_A_MIN_REPORTERS
        a_ok = (d["cloud_verified"] == 1) and (rc is not None and rc >= _min)
        write(f"  [A채널 조건] cloud_verified=1 & cloud_reporter_count>={_min} ⇒ {a_ok}")
    write(f"  [2차 reason] {d['cloud_verify_reason']}")
    write(
        f"  [2차 토큰] prompt={d['cloud_prompt_tokens']} "
        f"completion={d['cloud_completion_tokens']} total={d['cloud_total_tokens']}"
    )
    # cloud_raw_json(2차 응답 전체)에서 idx 목록만 참고 표시(메시지 매핑/재계산 없음).
    craw = d.get("cloud_raw_json")
    if not craw:
        write(
            "  [2차 응답 JSON] cloud_raw_json 없음 "
            "(2차 미호출 또는 cloud_raw_json 컬럼 추가(2026-06-15) 이전 run)"
        )
    else:
        try:
            cv = json.loads(craw)
            rep = sorted(set(cv.get("reporter_message_ids") or []))
            ev = sorted(set(cv.get("evidence_message_ids") or []))
            write(
                f"  [2차 응답 JSON] confirmed={cv.get('confirmed')} "
                f"reporter_message_ids={rep} evidence_message_ids={ev}"
            )
            if cv.get("error"):
                write(f"     error={cv.get('error')}")
        except Exception as exc:  # noqa: BLE001
            write(f"  [2차 응답 JSON] 파싱 실패: {exc}")
    write(f"  [llm_token_usage_json] {d['llm_token_usage_json']}")
    raw = d["raw_response"] or ""
    if show_thinking:
        write("  [1차 raw_response] (thinking 포함 전체)")
        write(str(raw))
    else:
        write(
            f"  [1차 raw_response] {len(raw)}자 생략 "
            "(thinking 포함 원문 — 전체 보기: --thinking)"
        )


def _dump_normalized(run_id: str, write, show_thinking: bool = False) -> None:
    info = _run_db_info(run_id)
    if info is None:
        write(f"[DB에 run 없음] run_id={run_id}")
        before, after = nearest_runs(run_id)
        if before:
            write("  ↑ 이전(가까운 순): " + ", ".join(before))
        if after:
            write("  ↓ 이후(가까운 순): " + ", ".join(after))
        if not before and not after:
            write("  (DB에 run 기록이 없습니다)")
        return
    if not info["cws"] or not info["we"]:
        write(f"[window 정보 없음] run_id={run_id}")
        return
    # 스냅샷 있으면 normalized, 없으면 카카오/인게임 원본에서 재구성(둘 다 is_new 포함).
    msgs, source = reconstruct(run_id, info["cws"], info["we"])
    if not msgs:
        write(f"[메시지 없음] run_id={run_id} (입력={source} · skipped_empty 가능)")
        return
    new_cnt = sum(1 for m in msgs if m.get("is_new"))
    gate, kws = _gate_from_msgs(msgs)
    src_label = "스냅샷" if source == "snapshot" else "⚠ 재구성(스냅샷 만료 → 카카오/인게임 원본 복원)"
    write(f"=== run_id={run_id} | 입력={src_label} | 메시지 {len(msgs)}건 (NEW {new_cnt}건) ===")
    head = f"[2차 호출 경로] {_route_label(info['sa'], gate)}"
    if kws:
        head += f"  키워드:[{','.join(kws)}]"
    if info["called"]:
        head += f"  |  2차 결과: {'발송' if info['verified'] == 1 else ('차단' if info['verified'] == 0 else '?')}"
    else:
        head += "  |  2차 미호출"
    write(head)
    write("형식: 번호 [NEW/   ] timestamp | source_id | sender: text")
    for i, m in enumerate(msgs, 1):
        flag = "NEW" if m.get("is_new") else "   "
        write(f"{i:3d} [{flag}] {m['timestamp']} | {m['source_id']} | {m['sender']}: {m['text']}")
    _dump_db_fields(run_id, write, show_thinking)


def _dump_raw(run_id: str, write) -> None:
    raw_root = SNAP / run_id / "raw"
    if not raw_root.exists():
        write(f"[raw 없음] {raw_root}")
        return
    files = sorted(p for p in raw_root.rglob("*") if p.is_file())
    write(f"=== run_id={run_id} | raw 원본 파일 {len(files)}개 (윈도우 필터 전 원문) ===")
    for fp in files:
        write(f"\n----- {fp.relative_to(raw_root)} ({fp.stat().st_size} bytes) -----")
        try:
            write(fp.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            write(fp.read_text(encoding="cp949", errors="replace"))


def show_indices(idxs: list[int], raw: bool, write, show_thinking: bool = False) -> None:
    """직전 목록(캐시)의 번호들에 해당하는 run 상세를 차례로 출력."""
    cache = _load_cache()
    if not cache or not cache.get("ids"):
        write("[캐시 없음] 먼저 목록(all/keyword/local)을 한 번 실행한 뒤 번호로 조회하세요.")
        return
    ids = cache["ids"]
    write(f"(직전 '{cache.get('mode')}' 목록 기준 · 총 {len(ids)} run · 선택 {len(idxs)}개)")
    for n in idxs:
        write("\n" + "=" * 70)
        if 1 <= n <= len(ids):
            write(f"[{n}] {ids[n - 1]}")
            if raw:
                _dump_raw(ids[n - 1], write)
            else:
                _dump_normalized(ids[n - 1], write, show_thinking)
        else:
            write(f"[{n}] 범위 밖 (유효 1~{len(ids)})")


def show_run(token: str, raw: bool, write, show_thinking: bool = False) -> None:
    # 정확한 run_id 형식이면 스냅샷 유무와 무관하게 처리(없으면 _dump_normalized가 원본 재구성).
    if RUNID_RE.match(token):
        if raw:
            _dump_raw(token, write)
        else:
            _dump_normalized(token, write, show_thinking)
        return
    # prefix(부분 입력): 스냅샷 폴더에서 매칭(스냅샷 있는 run만).
    matches = (
        sorted((p.name for p in SNAP.iterdir() if p.is_dir() and p.name.startswith(token)), reverse=True)
        if SNAP.exists() else []
    )
    if not matches:
        write(f"[매칭 없음] '{token}' — 스냅샷 prefix 매칭 없음. 정확한 run_id를 입력하면 만료된 run도 재구성합니다.")
        return
    if len(matches) > 1:
        write(f"=== '{token}' 매칭 {len(matches)}개 — 정확한 run_id 를 지정하세요 ===")
        for m in matches:
            write(m)
        return
    if raw:
        _dump_raw(matches[0], write)
    else:
        _dump_normalized(matches[0], write, show_thinking)


def main() -> None:
    ap = argparse.ArgumentParser(description="2차 검증 호출 분석 + 1차 입력 메시지 조회")
    ap.add_argument("targets", nargs="*", help="all|keyword|local / 번호(여러 개) / run_id(여러 개)")
    ap.add_argument("--raw", action="store_true", help="raw 원본 파일도 덤프")
    ap.add_argument(
        "--thinking", action="store_true",
        help="1차 raw_response(thinking 포함 원문) 전체 출력(기본은 길이만 표시)",
    )
    ap.add_argument("--limit", type=int, default=50, help="목록 표시 개수(기본 50)")
    ap.add_argument("--out", help="결과를 UTF-8 파일로 저장")
    args = ap.parse_args()

    buf: list[str] = []

    def write(line: str = "") -> None:
        buf.append(str(line))

    MODES = ("all", "keyword", "local")
    targets = args.targets

    def _is_runlike(t: str) -> bool:
        return bool(RUNID_RE.match(t)) or (
            SNAP.exists() and any(p.is_dir() and p.name.startswith(t) for p in SNAP.iterdir())
        )

    if not targets:
        list_mode("all", args.limit, write)
    elif len(targets) == 1 and targets[0] in MODES:
        list_mode(targets[0], args.limit, write)
    elif all(t.isdigit() for t in targets):
        show_indices([int(t) for t in targets], args.raw, write, args.thinking)
    elif all(_is_runlike(t) for t in targets):
        for i, t in enumerate(targets):
            if i > 0:
                write("\n" + "=" * 70)
            show_run(t, args.raw, write, args.thinking)
    else:
        write(f"[알 수 없는 인자] {targets} — all|keyword|local / 번호 / run_id 중 하나로 입력하세요.")

    text = "\n".join(buf)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[저장 완료] {args.out} ({len(buf)} 줄)")
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
        print(text)


if __name__ == "__main__":
    main()
