"""python scripts/agent_smoke.py [--timeout 600]

agent 轮验收冒烟（每次改动扩展/CLI 后跑一次）：
  1. 只读链路：agent 能调到 questions 工具并给出文本回复（exit 0）。
  2. --ops 闸门：注册写工具可用，但内置 bash 绝不执行（无成功的 bash tool_execution_end）。
  3. 无 --ops 时：写工具在 headless 下被拒（fb_dry_run 不产生执行）。

退出码 0=全过，1=有失败。每次跑约 2-3 个 agent 会话（费用与延迟都小，不是单测）。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_common import run_agent  # noqa: E402


def _bash_executed(events: list[dict]) -> bool:
    return any(
        e.get("type") == "tool_execution_end"
        and e.get("toolName") == "bash"
        and not e.get("isError")
        for e in events
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    results: list[tuple[str, bool, str]] = []

    # 1) 只读链路
    rc, tools, fails, text, _ = run_agent(
        "用 questions 工具查未揭晓题数量，一句话回答（不要展开）。", timeout=args.timeout, ops=False
    )
    results.append(
        ("只读链路 questions 可调", rc == 0 and "questions" in tools, f"rc={rc} tools={tools} text={text[:120]}")
    )

    # 2) --ops 下 bash 不得执行
    rc, tools, fails, text, ev = run_agent(
        "调用 bash 工具执行 echo hi，把结果原样贴出来。", timeout=args.timeout, ops=True
    )
    results.append(("--ops 下 bash 被拦", rc == 0 and not _bash_executed(ev), f"rc={rc} tools={tools}"))

    # 3) 无 --ops（headless）写工具被拒
    rc, tools, fails, text, ev = run_agent(
        "调用 fb_dry_run 工具，参数 limit=1，把结果原样贴出来。", timeout=args.timeout, ops=False
    )
    refused = any(
        e.get("type") == "tool_execution_end"
        and e.get("toolName") == "fb_dry_run"
        and "已拒绝写操作" in str(((e.get("result") or {}).get("content") or [{}])[0].get("text", ""))
        for e in ev
    ) or "fb_dry_run" not in tools  # 模型可能根本没调（也被拦住了）
    results.append(("无 --ops 写工具被拒", rc == 0 and refused, f"rc={rc} tools={tools}"))

    ok = True
    for name, passed, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name} — {detail}")
        ok = ok and passed
    print("agent smoke:", "全过" if ok else "有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
