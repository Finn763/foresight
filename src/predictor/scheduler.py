"""每周短周期客观题生成。公布日程为模板示例，执行 agent 按官方日历维护真实日期。

排期纪律（强制执行）：
① 每周题的 closes_at 错开——同一天到期 ≤3 题（单人人工揭晓的极限）；
② 官方公布日历开工前先收集：国家统计局发布日程 / FOMC 决议日历 / EIA 周报日历
   （URL 记入 docs/官方日历.md）；
③ closes_at 统一按北京时间 09:00 记录，美东事件按当日换算
   （时区坑：FOMC 是美东时间，别用本地时间直接写）。
④ 选题铁律（2026-08-13 用户拍板）：气温/气候/天气类事件一律禁出——
   天气预报已成熟且可查，预测"明天北京超 35°C"对 B 端客户无增量价值；
   （2026-08-13 已删除遗留题 #68；此前超短档气温模板已于 8-13 final review 移除）。
"""

from datetime import datetime, timedelta


def build_weekly_questions(week: datetime) -> list[dict]:
    """从模板生成当周题。所有 closes_at 为客观揭晓日（官方公布/固定时点）。

    频次纪律（2026-08-12 修）：
    - 只生成周频事件题（EIA 每周三 / 标普 7 天 / 汇率 30 天滚动），每周各自独立窗口；
    - CPI（月度）：schedule_questions.py 按"中国*月 CPI"前缀去重，同月数据不重复建题；
    - FOMC（非固定周期）：不在此生成——按 docs/官方日历.md 的真实决议日人工出题
      （2026-08-12 教训：模板 week+14 与真实决议日脱节会产出 closes 晚于事件的废题）。
    """
    month = week.strftime("%m")
    return [
        {
            "title": f"中国{month}月 CPI 同比会高于上月吗",
            "closes_at": week + timedelta(days=12),
            "is_public": True,
        },
        {
            "title": "本周 EIA 原油库存会下降吗",
            # EIA WPSR 每周三 10:30 ET（北京 22:30 夏令时）公布；原 closes=week+2d
            # 落周三 00:00、早于公布约 22.5 小时——#9（closes 8-19 00:00）在数据
            # 发布前即闭题，B 类 LLM 揭晓时只能凭旧数据判定（低置信被护栏拦下，
            # 属"宁缺毋滥"但题面时点设计错误）。2026-08-20 修：closes 移至周四
            # 00:00（week+3d），保证揭晓时事件已发生。
            "closes_at": week + timedelta(days=3),
            "is_public": True,
        },
        {
            "title": "未来 7 天内标普 500 会创新高吗",
            "closes_at": week + timedelta(days=7),
            "is_public": True,
        },
        {
            "title": "未来 30 天内人民币兑美元会升破 7.0 吗",
            "closes_at": week + timedelta(days=30),
            "is_public": True,
        },
    ]
