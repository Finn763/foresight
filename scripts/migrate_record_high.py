"""把标普"创历史新高/创新高"存量 C 类题迁移为 A 类 record_high（幂等，只动未揭晓题）。

用法：python scripts/migrate_record_high.py --db data/foresight.db

口径说明：创新高 = 窗口内（opens_at..closes_at）收盘最大值 > 窗口前历史最高收盘。
spec 不存 value——prior ATH 由揭晓时 fetch_kline（腾讯日K主源 + Yahoo 备源）动态计算。
"""

import argparse

from predictor.data.storage import Storage
from predictor.resolution.spec import validate_resolution_spec

RECORD_HIGH_SPEC = {
    "class": "A",
    "instrument": "spx",
    "source_primary": "tencent",
    "compare_symbol": "usINX",
    "source_backup": "yahoo",
    "backup_symbol": "^GSPC",
    "condition": "record_high",
    "close_timezone": "America/New_York",
    "grace_days": 3,
    "degrade_to": "C",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/foresight.db")
    args = ap.parse_args()

    errs = validate_resolution_spec(RECORD_HIGH_SPEC)
    if errs:
        raise SystemExit(f"spec 非法: {errs}")

    st = Storage(args.db)
    rows = st._conn.execute(
        """
        SELECT id, title, resolution_class, resolution_spec
        FROM questions
        WHERE outcome IS NULL
          AND title LIKE '%标普%'
          AND (title LIKE '%创历史新高%' OR title LIKE '%创新高%')
        ORDER BY id
        """
    ).fetchall()

    migrated, skipped = [], []
    for qid, title, cls, spec in rows:
        if cls == "A":
            skipped.append(qid)
            continue
        st.set_resolution(qid, "A", RECORD_HIGH_SPEC)
        migrated.append(qid)
        print(f"migrated #{qid}: {title[:50]}")

    print(f"迁移 {len(migrated)} 道，跳过已 A 类 {len(skipped)} 道")

    dist = st._conn.execute(
        "SELECT resolution_class, COUNT(*) FROM questions WHERE outcome IS NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    print("迁移后未揭晓题分布:", dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
