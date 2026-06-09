"""테스트(test_hybrid_replay)에 사용되는 입력 메시지를 원문 그대로 출력한다.

사용법:
  (.venv) python tools/show_test_messages.py                # RUNS 7종 전체
  (.venv) python tools/show_test_messages.py 20260529_144917  # 특정 run 1개
  (.venv) python tools/show_test_messages.py 20260529_144917 20260604_011503  # 여러 개

출력: [idx] timestamp | source_id | sender | text  (축약·가공 없이 reconstruct 결과 그대로)
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 프로젝트 루트(config 등)
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools(test_hybrid_replay)
import config
from test_hybrid_replay import reconstruct, RUNS


def show(rid: str) -> None:
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT context_window_start, window_end, message_count "
        "FROM local_llm_runs WHERE run_id=?",
        (rid,),
    ).fetchone()
    conn.close()
    if r is None:
        print(f"\n[{rid}] DB에 run 없음")
        return
    msgs, src = reconstruct(rid, r["context_window_start"], r["window_end"])
    print("=" * 90)
    print(f"[{rid}] 입력={src} {len(msgs)}건 (원본 message_count={r['message_count']})")
    print(f"  window: {r['context_window_start']} ~ {r['window_end']}")
    print("-" * 90)
    for i, m in enumerate(msgs, 1):
        print(f"  [{i:2d}] {m['timestamp']} | {m['source_id']} | {m['sender']} | {m['text']}")


def main() -> None:
    targets = sys.argv[1:] or list(RUNS)
    for rid in targets:
        show(rid)


if __name__ == "__main__":
    main()
