"""스냅샷(있으면) 또는 원본에서 특정 run의 window 입력 메시지를 재구성하는 공용 로직.

llm_replay(재실행)·llm_check(조회)가 함께 사용한다. Slack 등 무거운 의존 없이
config·parsers만 import하므로 조회 도구에서 가볍게 불러 쓸 수 있다.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core.time_utils import ensure_kst, to_iso_kst  # noqa: E402
from parsers.ingame_parser import parse_ingame_file  # noqa: E402
from parsers.kakao_parser import parse_kakao_file  # noqa: E402

KROOMS = Path(config.KAKAO_BASE_DIR)
INGAME = Path(config.INGAME_BASE_DIR) / "llm_messages.jsonl"
KFILES = [tuple(item) for item in getattr(config, "KAKAO_REPLAY_FILES", [])]

# llm_replay 인자 없이 실행할 때의 회귀 세트(과거 대표 run 7종).
RUNS = [
    "20260604_145002",
    "20260604_021503",
    "20260604_011503",
    "20260601_214006",
    "20260601_213506",
    "20260529_225417",
    "20260529_144917",
]

_ingame_cache = None


def _part(hour: int) -> str:
    return "1_0000-1059" if hour < 11 else ("2_1100-1859" if hour < 19 else "3_1900-2359")


def _load_ingame():
    global _ingame_cache
    if _ingame_cache is None:
        _ingame_cache = parse_ingame_file(INGAME, "ingame")[0] if INGAME.exists() else []
    return _ingame_cache


def nearest_runs(run_id: str, n: int = 3):
    """입력 run_id 기준 이전/이후로 가장 가까운 실제 run_id를 n개씩(가까운 순) 반환.

    run_id 문자열(YYYYMMDD_HHMMSS)은 사전식 정렬이 곧 시간순이라 부등호 비교로 앞뒤를 찾는다.
    llm_check·llm_replay가 '존재하지 않는 run_id' 입력 시 후보 안내에 공통으로 쓴다.
    """
    with sqlite3.connect(str(config.DB_PATH)) as conn:
        before = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM local_llm_runs WHERE run_id < ? ORDER BY run_id DESC LIMIT ?",
                (run_id, n),
            ).fetchall()
        ]
        after = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM local_llm_runs WHERE run_id > ? ORDER BY run_id ASC LIMIT ?",
                (run_id, n),
            ).fetchall()
        ]
    return before, after


def reconstruct(rid: str, cws: str, we: str):
    """run의 window 메시지를 재구성. (messages, source) 반환.

    - messages 각 항목: source_id, timestamp, sender, text, is_new
    - source: 'snapshot'(스냅샷 normalized 사용) | 'reconstructed'(스냅샷 만료 → 원본 재구성)
    - is_new: 스냅샷이면 저장값, 재구성이면 window_end - NEW_WINDOW_MINUTES 기준 재계산.
    """
    new_cut = ensure_kst(datetime.fromisoformat(we)) - timedelta(minutes=config.NEW_WINDOW_MINUTES)

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
                            "is_new": bool(o.get("is_new")),
                        }
                    )
        msgs.sort(key=lambda m: (m["timestamp"], m["source_id"]))
        return msgs, "snapshot"

    # 스냅샷 만료 → 카카오 llm/rooms + 인게임 output에서 window 재구성
    cwt = ensure_kst(datetime.fromisoformat(cws))
    wet = ensure_kst(datetime.fromisoformat(we))
    day = cws[:10]
    folders = {f"{day}_{_part(int(cws[11:13]))}", f"{day}_{_part(int(we[11:13]))}"}
    parsed_msgs = []
    for folder in folders:
        for sid, fn in KFILES:
            p = KROOMS / folder / fn
            if p.exists():
                parsed_msgs += parse_kakao_file(p, sid)[0]
    parsed_msgs += _load_ingame()
    fil = [m for m in parsed_msgs if cwt <= ensure_kst(m.timestamp) <= wet]
    seen = set()
    uniq = []
    for m in fil:
        ts = to_iso_kst(m.timestamp)
        key = (m.source_id, ts, m.sender, m.text)
        if key not in seen:
            seen.add(key)
            uniq.append(
                {
                    "source_id": m.source_id,
                    "timestamp": ts,
                    "sender": m.sender,
                    "text": m.text,
                    "is_new": ensure_kst(m.timestamp) >= new_cut,
                }
            )
    uniq.sort(key=lambda x: (x["timestamp"], x["source_id"]))
    return uniq, "reconstructed"
