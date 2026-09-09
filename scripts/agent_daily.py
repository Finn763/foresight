"""python scripts/agent_daily.py [--timeout 1200] [--dry-run]

每日 agent 运维轮（由计划任务触发）：
  以 foresight CLI（--mode json --ops）拉起 agent，让它自己看健康/事件/题池，
  并跑自主选题（autopick 工具，非 dry-run）。

边界：只做「看 + 选题 + 汇报」，不跑预测轮——预测仍由 scripts/daily.py 09:00 兜底。
      --ops 只放行注册写工具，内置 bash/edit/write 仍被扩展拦截。
stdout 由 run_silent 重定向到 data/agent-daily.log。
退出码：0=完成；1=失败/超时（当日 Python 任务不受影响）。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_common import run_agent  # noqa: E402

PROMPT = """你是 Foresight 的每日运维轮（无人值守，{date}）。按顺序执行，每步如实汇报，不要编造数字：

1) 用 health 工具（refresh=false）看系统健康；再用 events 工具查最近 24h 的
   prediction_skipped / llm_resolve_failed 事件。有异常就在汇报里点名。
2) 用 questions 工具看当前未揭晓题数，以及最近 3 天到期的题。
3) 用 autopick 工具跑自主选题（dry_run=false，max_picks=4）。
   若提示配额已用完或没有达标候选，如实说明，不要硬凑。
4) 最后输出一份简短日报，三段：
   ①健康与 24h 异常；②本轮新选题（标题 + 概率）或配额/供给说明；③需要人工处理的事项。
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=1200, help="agent 轮超时秒数")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt，不执行")
    args = ap.parse_args()

    prompt = PROMPT.format(date=datetime.now().strftime("%Y-%m-%d"))
    if args.dry_run:
        print(f"[dry-run] prompt {len(prompt)} 字，前 120 字：{prompt[:120]}")
        return 0

    print(f"agent 轮启动 {datetime.now():%Y-%m-%d %H:%M:%S}（timeout={args.timeout}s）", flush=True)
    rc, tools, failures, text, _ = run_agent(prompt, timeout=args.timeout, ops=True)
    print(f"agent 工具调用：{tools or '无'}")
    if failures:
        print(f"工具报错：{', '.join(failures)}")
    print("--- agent 日报 ---")
    print((text or "(无文本输出)")[:4000])
    if rc != 0:
        print(f"agent 轮失败 rc={rc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
