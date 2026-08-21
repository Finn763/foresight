"""python scripts/migrate_schema.py [--db path]
对已存在旧库幂等迁移：questions/predictions 加列、新建 5 张表、存量预测回填 arm='baseline'。
新库直接 create_schema() 即可（create_schema 幂等）。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.config import Settings
from predictor.data.storage import Storage


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=Settings().db_path)
    args = ap.parse_args()
    st = Storage(args.db)
    st.create_schema()  # 含所有 ALTER TABLE IF NOT EXISTS + CREATE TABLE IF NOT EXISTS
    st._conn.execute("UPDATE predictions SET arm = 'baseline' WHERE arm IS NULL")
    print(f"迁移完成: {args.db}")


if __name__ == "__main__":
    main()
