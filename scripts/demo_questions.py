"""演示案例集：演示题初始化。

用法：
  python scripts/demo_questions.py --init        # 5 道演示题入库（is_public=TRUE）
  python scripts/demo_questions.py --list        # 列出演示题

选题原则：正在进行、客观可判定、揭晓日错开（≤3 题/天）、覆盖短/中/长周期；
避开政治敏感与个股；判定来源均为公开惯例/官方发布（见 docs/官方日历.md）。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predictor.config import Settings
from predictor.data.storage import Storage

# (标题, closes_at, 判定来源)
# 选题铁律：预测窗口内事件必须有变动可能（2026-08-11 哪吒票房废题教训——已下映电影票房
# 是定格数字，预测"未来突破"是伪命题，禁止出题）。以下均为窗口内仍有变数的未来事件。
DEMO_QUESTIONS = [
    (
        "未来 14 天内标普 500 会创历史新高吗",
        datetime(2026, 8, 26, 9, 0),
        "标普 500 每日收盘（公开行情）",
    ),
    (
        "2026 年 9 月 FOMC 会议会维持利率不变吗",
        datetime(2026, 9, 17, 9, 0),
        "美联储决议公布（federalreserve.gov）",
    ),
    (
        "2026 年苹果秋季发布会会在 9 月 30 日前举行吗",
        datetime(2026, 9, 30, 9, 0),
        "苹果官方新闻稿（惯例 9 月）",
    ),
    (
        "2026 年 10 月中国 CPI 同比会高于 9 月吗",
        datetime(2026, 11, 12, 9, 0),
        "国家统计局 10 月数据发布",
    ),
    (
        "2026 年底前人民币兑美元会升破 7.0 吗",
        datetime(2026, 12, 31, 9, 0),
        "中国外汇交易中心中间价",
    ),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="演示题入库（is_public=TRUE）")
    ap.add_argument("--list", action="store_true", help="列出演示题")
    ap.add_argument("--db", default=Settings().db_path)
    args = ap.parse_args()

    st = Storage(args.db)
    st.create_schema()

    if args.list or not args.init:
        print("演示题清单（--init 入库）：")
        for title, closes, source in DEMO_QUESTIONS:
            print(f"  [{closes.date()}] {title}  （判定：{source}）")
        if not args.init:
            return

    for title, closes, source in DEMO_QUESTIONS:
        qid = st.add_question(title, closes, is_public=True)
        print(f"#{qid} {title} (揭晓 {closes.date()}) 来源: {source}")
    print(f"\n共入库 {len(DEMO_QUESTIONS)} 道演示题。揭晓日错开（同日 ≤3 题 ✓）。")


if __name__ == "__main__":
    main()
