"""스냅샷이 만료된 run_id의 메시지를 원본에서 재구성해 data/snapshots_true/{run_id}/normalized/ 에 저장."""
import json
import sqlite3
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config
from tools._replay_core import reconstruct

TRUE_DIR = config.BASE_DIR / "data" / "snapshots_true"
TRUE_DIR.mkdir(parents=True, exist_ok=True)

TARGETS = ["20260617_170355", "20260615_100449", "20260613_184452"]

conn = sqlite3.connect(str(config.DB_PATH))
conn.row_factory = sqlite3.Row

for rid in TARGETS:
    r = conn.execute(
        "SELECT context_window_start, window_end, message_count FROM local_llm_runs WHERE run_id=?",
        (rid,)
    ).fetchone()
    if not r:
        print(f"[{rid}] DB 없음 — 스킵")
        continue

    msgs, src = reconstruct(rid, r["context_window_start"], r["window_end"])
    print(f"[{rid}] 재구성={src} {len(msgs)}건 (원래 {r['message_count']}건)")

    if not msgs:
        print(f"  → 원본 파일 없음, 재구성 불가 — 스킵")
        continue

    dst_dir = TRUE_DIR / rid / "normalized"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_file = dst_dir / "messages.jsonl"
    with open(dst_file, "w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    print(f"  → 저장: {dst_file.relative_to(config.BASE_DIR)} ({len(msgs)}건)")

conn.close()
