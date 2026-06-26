"""현재 남아있는 스냅샷 중 cloud_verified=1(2차 응답 true)인 것을
data/snapshots_true/ 에 복사한다. 이미 복사된 것은 스킵."""
import shutil
import sqlite3
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = BASE_DIR / "data" / "snapshots"
TRUE_DIR = BASE_DIR / "data" / "snapshots_true"
TRUE_DIR.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(str(BASE_DIR / "data" / "issue_monitor.sqlite3"))
conn.row_factory = sqlite3.Row

# cloud_verified=1 run_id 목록
rows = conn.execute(
    "SELECT run_id FROM local_llm_runs WHERE cloud_verified = 1 ORDER BY run_id"
).fetchall()
conn.close()

run_ids = [r["run_id"] for r in rows]
print(f"DB cloud_verified=1: {len(run_ids)}건")

copied = skipped_no_src = skipped_exists = 0
for rid in run_ids:
    src = SNAPSHOT_DIR / rid
    dst = TRUE_DIR / rid
    if not src.exists():
        skipped_no_src += 1
        continue
    if dst.exists():
        skipped_exists += 1
        continue
    shutil.copytree(str(src), str(dst))
    print(f"  copied: {rid}")
    copied += 1

print(f"\n결과: 복사={copied} / 스냅샷없음={skipped_no_src} / 이미존재={skipped_exists}")
