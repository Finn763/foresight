"""把模糊 C 类题迁移为 B 类（LLM 先网络搜索揭晓，护栏判不了再降级人工）。

用法：python scripts/migrate_c_to_b.py --db data/foresight.db

背景（2026-08-14 用户拍板）：C 类题到期时也应先让 LLM 搜证据尝试揭晓，
搜不到权威证据/置信不足/双采样分歧时 LLMResolver 护栏自动拒判 → 降级人工。
B 类机制已含此语义，故直接把存量模糊题迁为 B（spec 空壳 {"class": "B"}）。
"""

import argparse

from predictor.data.storage import Storage

B_SPEC = {"class": "B"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/foresight.db")
    args = ap.parse_args()

    st = Storage(args.db)
    rows = st._conn.execute(
        """
        SELECT id, title, resolution_class FROM questions
        WHERE outcome IS NULL AND resolution_class = 'C'
        ORDER BY id
        """
    ).fetchall()

    migrated = []
    for qid, title, cls in rows:
        st.set_resolution(qid, "B", B_SPEC)
        migrated.append(qid)
        print(f"migrated #{qid}: {title[:55]}")

    print(f"迁移 {len(migrated)} 道 C→B")
    dist = st._conn.execute(
        "SELECT resolution_class, COUNT(*) FROM questions WHERE outcome IS NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    print("迁移后未揭晓题分布:", dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
