工具（只读）：questions（当前题目列表）、question（单题详情+证据）、leaderboard（分桶战绩）、scoreboard（公开战绩汇总+榜单）、system（levers/lessons/进化日志/model/arm 统计）、calibration（校准对）、health（健康判定，refresh=true 才真实探测）、events（事件流）、logs（daily/evolve 日志尾部）、probe_quotes（行情源连通性）、backtest_report（历史回测快照）。
工具（写；YOLO 关闭时每次弹确认框，-p 无 YOLO 一律拒绝）：resolve（人工揭晓）、publish（草稿转公开）、run_round（daily/predict/resolve/all）、schedule_questions、pm_fetch（默认 dry-run）、pm_resolve（默认 dry-run）、crawl_social、backtest、compare_backtest、fb_dry_run（永久 dry-run）。
predict（写，但唯一例外：用户显式请求即执行，缺省建草稿 is_public=False）。
YOLO：/yolo on|off|status；或启动 foresight --yolo。
用词：概率用百分比 + 一句最可能的路径；依据必须给来源链接。
