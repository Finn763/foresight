"""agent 轮公共入口：以 foresight CLI（pi fork）跑一次非交互会话并解析 stream-json。

被 scripts/agent_daily.py（每日运维轮）与 scripts/agent_smoke.py（验收冒烟）共用。
"""

import json
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "shell" / "pi" / "packages" / "coding-agent" / "dist" / "cli.js"


def run_agent(
    prompt: str, *, timeout: int = 1200, ops: bool = True
) -> tuple[int, list[str], list[str], str, list[dict]]:
    """跑一次 headless agent 会话。

    返回 (returncode, 工具名列表, 报错工具列表, 最后一段 assistant 文本, 全部事件)。
    returncode=-1 表示环境缺失（node/cli 不在），-2 表示超时。
    """
    node = shutil.which("node")
    if node is None or not CLI.exists():
        return -1, [], [], f"环境缺失：node={node or '未找到'}，cli 存在={CLI.exists()}", []
    cmd = [node, str(CLI), "-p", prompt, "--mode", "json", "--no-session"]
    if ops:
        cmd.append("--ops")
    try:
        r = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return -2, [], [], f"agent 会话超时（>{timeout}s）", []

    events: list[dict] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue

    tools: list[str] = []
    failures: list[str] = []
    texts: list[str] = []
    for e in events:
        if e.get("type") == "tool_execution_end":
            name = str(e.get("toolName"))
            tools.append(name)
            if e.get("isError"):
                failures.append(name)
        elif e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant":
            for c in e["message"].get("content") or []:
                if c.get("type") == "text":
                    texts.append(c["text"])
    return r.returncode, tools, failures, (texts[-1] if texts else ""), events
