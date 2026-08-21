"""python scripts/schedule_questions.py --week 2026-09-01
生成当周短周期客观题并入库存。"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.config import Settings
from predictor.data.storage import Storage
from predictor.scheduler import build_weekly_questions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=Settings().db_path)
    ap.add_argument("--week", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    st = Storage(args.db)
    st.create_schema()
    week = datetime.fromisoformat(args.week)
    # 去重（2026-08-12）：CPI 为月度事件，同月数据同标题未揭晓已存在则跳过（防同事件重复题）。
    # 周频题（EIA/标普/汇率）标题固定但每周窗口独立，不去重。
    existing = {
        t[0]
        for t in st._conn.execute("SELECT title FROM questions WHERE outcome IS NULL").fetchall()
    }
    for q in build_weekly_questions(week):
        if q["title"].startswith("中国") and q["title"] in existing:
            print(f"跳过(已存在): {q['title']}")
            continue
        qid = st.add_question(q["title"], q["closes_at"], is_public=q["is_public"])
        existing.add(q["title"])
        print(f"#{qid} {q['title']} (揭晓 {q['closes_at'].date()})")


if __name__ == "__main__":
    main()
