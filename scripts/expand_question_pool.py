"""题库扩充：按短/中/长期三类生成真实事件题入库（2026-08-12，方案 A 后）。

- 短期 7-14 天：周频事件（EIA/标普/热搜/票房周榜）
- 中期 30-90 天：月度/季度事件（CPI/FOMC/汇率/发布会/档期）
- 长期 180-365 天：年度目标（汇率/CPI/科技产品/体育/文化）

选题铁律：窗口内事件必须有变动可能；政治敏感/博彩/个股禁止；
判定来源全部客观可查；与现有未揭晓题按标题去重。

用法：python scripts/expand_question_pool.py [--db data/foresight.db] [--list]
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predictor.config import Settings
from predictor.data.storage import Storage

TODAY = datetime(2026, 8, 12)


def _d(days: int, hour: int = 9) -> datetime:
    """今日 + days 天，09:00 北京。"""
    return (TODAY + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


# (标题, closes_at, 判定来源)
SHORT_TERM = [
    ("未来 14 天内标普 500 会创历史新高吗", _d(14), "标普 500 每日收盘"),
    ("未来 7 天内微博热搜会出现新的体育冠军话题吗", _d(7), "微博热搜榜（赛事公开结果）"),
    ("未来 14 天内苹果公司会发布新设备吗", _d(14), "苹果官方新闻稿"),
    ("未来 7 天内 离岸人民币兑美元 会升破 6.75 吗", _d(7), "中国外汇交易中心"),
    ("未来 14 天内 国际金价（COMEX）会突破 3500 美元/盎司吗", _d(14), "COMEX 期货行情"),
]

MEDIUM_TERM = [
    ("2026 年 9 月美国 CPI 同比会高于 8 月吗", _d(45), "美国劳工统计局（10 月中公布 9 月数据）"),
    ("2026 年 10 月 FOMC 会议会维持利率不变吗", _d(75), "美联储决议公告"),
    ("2026 年 10 月底前 布伦特原油价格会突破 90 美元/桶吗", _d(75), "ICE 布伦特行情"),
    ("2026 年国庆档电影总票房会突破 40 亿元吗", _d(55), "猫眼/灯塔专业版（10/8 后揭晓）"),
    ("2026 年 11 月 11 日前 淘宝双 11 全网成交额会创历史新高吗", _d(90), "平台官方战报"),
    ("2026 年 9 月底前 中国官方制造业 PMI 会重回荣枯线上方吗", _d(50), "国家统计局（月末公布）"),
    ("2026 年 12 月 31 日前 比特币价格会突破 15 万美元吗", _d(140), "主流交易所行情"),
]

LONG_TERM = [
    ("2027 年春节档电影总票房会突破 100 亿元吗", _d(365), "猫眼/灯塔专业版"),
    ("2027 年 6 月 30 日前 人民币兑美元会升破 6.5 吗", _d(320), "中国外汇交易中心"),
    ("2027 年 6 月 30 日前 OpenAI 会发布 GPT-6 吗", _d(320), "OpenAI 官方公告"),
    ("2027 年 1 月 31 日前 标普 500 会首次站上 8500 点吗", _d(170), "标普 500 每日收盘"),
    ("2026 年 12 月 31 日前 中国新能源汽车年销量会突破 2000 万辆吗", _d(140), "中汽协月度数据"),
    ("2027 年 6 月 30 日前 美联储会降息至 2.5% 以下吗", _d(320), "美联储决议公告"),
]

ALL = SHORT_TERM + MEDIUM_TERM + LONG_TERM


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=Settings().db_path)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for name, items in (
            ("短期(7-14天)", SHORT_TERM),
            ("中期(30-90天)", MEDIUM_TERM),
            ("长期(180-365天)", LONG_TERM),
        ):
            print(f"\n=== {name} ===")
            for title, closes, src in items:
                print(f"  [{closes.date()}] {title}（判定：{src}）")
        return

    st = Storage(args.db)
    st.create_schema()
    existing = {
        t[0]
        for t in st._conn.execute("SELECT title FROM questions WHERE outcome IS NULL").fetchall()
    }
    added = skipped = 0
    for name, items in (("短期", SHORT_TERM), ("中期", MEDIUM_TERM), ("长期", LONG_TERM)):
        for title, closes, src in items:
            if title in existing:
                print(f"跳过(已存在): [{name}] {title}")
                skipped += 1
                continue
            qid = st.add_question(title, closes, is_public=True)
            existing.add(title)
            print(f"#{qid} [{name}] {title}（揭晓 {closes.date()}）")
            added += 1
    print(f"\n新增 {added} 题，跳过 {skipped} 题")


if __name__ == "__main__":
    main()
