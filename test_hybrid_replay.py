"""과거 7개 run의 원본 대화를 재구성해 하이브리드 파이프라인(1차 로컬 → 2차 OpenAI)으로 재실행.

- 입력: 각 run의 window 메시지를 main.py와 동일 파서로 재구성
  (스냅샷 있으면 normalized 사용, 없으면 카카오 llm/rooms + 인게임 output에서 window 필터)
- 1차: judge_messages(로컬), 2차: verify_alert_cloud(OpenAI)
- 2차 confirmed=true(또는 2차 장애 fallback)면 실제 Slack 알림 발송
- 기대값 비교 없음: 실제 판정/발송 결과를 그대로 출력

실행: (.venv) python test_hybrid_replay.py
"""
import sqlite3
import json
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, ".")
import config
from parsers.kakao_parser import parse_kakao_file
from parsers.ingame_parser import parse_ingame_file
from core.time_utils import ensure_kst, to_iso_kst
from llm.judge import (
    judge_messages,
    verify_alert_cloud,
    issue_candidate_sender_count,
    detect_issue_candidates,
)
from alerts.slack import send_slack_notification
from main import _parse_alert_category

KROOMS = Path(config.KAKAO_BASE_DIR)
INGAME = Path(config.INGAME_BASE_DIR) / "llm_messages.jsonl"
KFILES = [
    ("kakao_a", "모바일게임_원조_커뮤니티(비번).txt"),
    ("kakao_b", "모바일게임_정보&소통방_ver.2.txt"),
]

RUNS = [
    "20260604_145002",
    "20260604_021503",
    "20260604_011503",
    "20260601_214006",
    "20260601_213506",
    "20260529_225417",
    "20260529_144917",
]


def _part(hour: int) -> str:
    return "1_0000-1059" if hour < 11 else ("2_1100-1859" if hour < 19 else "3_1900-2359")


_ingame_cache = None


def _load_ingame():
    global _ingame_cache
    if _ingame_cache is None:
        if INGAME.exists():
            _ingame_cache, _ = parse_ingame_file(INGAME, "ingame")
        else:
            _ingame_cache = []
    return _ingame_cache


def reconstruct(rid: str, cws: str, we: str):
    """run의 window 메시지를 재구성. (messages, source) 반환."""
    snap = config.SNAPSHOT_DIR / rid / "normalized" / "messages.jsonl"
    if snap.exists():
        msgs = []
        with open(snap, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if cws <= str(o.get("timestamp", "")) <= we:
                    msgs.append(
                        {
                            "source_id": o.get("source_id", ""),
                            "timestamp": o.get("timestamp", ""),
                            "sender": o.get("sender", ""),
                            "text": o.get("text", ""),
                        }
                    )
        msgs.sort(key=lambda m: (m["timestamp"], m["source_id"]))
        return msgs, "snapshot"

    # 스냅샷 만료 → 카카오 llm/rooms + 인게임 output에서 재구성
    cwt = ensure_kst(datetime.fromisoformat(cws))
    wet = ensure_kst(datetime.fromisoformat(we))
    day = cws[:10]
    folders = {f"{day}_{_part(int(cws[11:13]))}", f"{day}_{_part(int(we[11:13]))}"}
    parsed_msgs = []
    for folder in folders:
        for sid, fn in KFILES:
            p = KROOMS / folder / fn
            if p.exists():
                pm, _ = parse_kakao_file(p, sid)
                parsed_msgs += pm
    parsed_msgs += _load_ingame()
    fil = [m for m in parsed_msgs if cwt <= ensure_kst(m.timestamp) <= wet]
    seen = set()
    uniq = []
    for m in fil:
        key = (m.source_id, to_iso_kst(m.timestamp), m.sender, m.text)
        if key not in seen:
            seen.add(key)
            uniq.append(
                {
                    "source_id": m.source_id,
                    "timestamp": to_iso_kst(m.timestamp),
                    "sender": m.sender,
                    "text": m.text,
                }
            )
    uniq.sort(key=lambda x: (x["timestamp"], x["source_id"]))
    return uniq, "reconstructed"


def main() -> None:
    conn = sqlite3.connect("data/issue_monitor.sqlite3")
    conn.row_factory = sqlite3.Row
    for rid in RUNS:
        r = conn.execute(
            "SELECT context_window_start, window_end, message_count "
            "FROM local_llm_runs WHERE run_id=?",
            (rid,),
        ).fetchone()
        if r is None:
            print(f"[{rid}] DB에 run 없음 — 스킵")
            continue
        msgs, src = reconstruct(rid, r["context_window_start"], r["window_end"])
        print("=" * 78)
        print(f"[{rid}] 입력={src} {len(msgs)}건 (원본 MC {r['message_count']})")

        # --- 1차: 로컬 ---
        jr = judge_messages(msgs)
        parsed = jr.parsed_response or {}
        should_alert = bool(parsed.get("should_alert", False))
        category = _parse_alert_category(parsed)
        content = str(parsed.get("content") or jr.error or "")
        # 키워드 게이트(main.py와 동일): 이슈 키워드를 서로 다른 2명 이상이 언급하면 9B 무관 2차로
        keyword_gate = issue_candidate_sender_count(msgs) >= 2
        # evidence만 추출 (main.py와 동일: 전체 메시지가 아니라 신고 idx만)
        ev_ids = set(parsed.get("evidence_message_ids") or [])
        evidence_rows = [m for i, m in enumerate(msgs, start=1) if i in ev_ids]
        if not evidence_rows and keyword_gate:
            cand_ids = {i for i, _ in detect_issue_candidates(msgs)}
            evidence_rows = [m for i, m in enumerate(msgs, start=1) if i in cand_ids]
        print(f"  [1차 로컬] status={jr.status} should_alert={should_alert} keyword_gate={keyword_gate} category={category}")
        print(f"            evidence {sorted(ev_ids)} → {len(evidence_rows)}건")
        print(f"            {content[:130]}")

        if jr.status != "ok" or not (should_alert or keyword_gate):
            print("  → 2차 미호출 (1차 alert/게이트 아님)")
            continue
        if not should_alert and keyword_gate:
            print("  [키워드 게이트 발동] 9B=false지만 이슈 키워드 다수 → 2차 호출")

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

        # 2차 confirmed 시, 2차가 재선정한 evidence를 우선 사용 (없으면 1차 evidence 유지)
        if cv_status == "ok" and bool(cv.get("confirmed")):
            cloud_ev = set(cv.get("evidence_message_ids") or [])
            if cloud_ev:
                cloud_rows = [m for i, m in enumerate(msgs, start=1) if i in cloud_ev]
                if cloud_rows:
                    evidence_rows = cloud_rows
                    print(f"            [2차 evidence 재선정] {sorted(cloud_ev)} → {len(evidence_rows)}건")

        if send:
            ok = send_slack_notification(
                title=f"[재구성테스트 {rid}] issue_monitor",
                should_alert=True,
                content=content,
                fields={
                    "run_id": rid,
                    "category": category,
                    "decision": note,
                    "cloud_confirmed": cv.get("confirmed"),
                    "cloud_status": cv_status,
                },
                evidence_messages=evidence_rows,
            )
            print(f"  → Slack 발송({note}): sent={ok}")
        else:
            print(f"  → 발송 안 함 ({note})")
    conn.close()
    print("\n=== 완료 ===")


if __name__ == "__main__":
    main()
