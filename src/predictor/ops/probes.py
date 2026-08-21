"""外部探测：行情源 / LLM / 任务计划器。TTL 300s 内存缓存，后台刷新。

探测失败只影响健康页状态，永不抛出（各函数自身 try/except）。
LLM 探测用纯 chat（无 json_mode——DeepSeek 要求 prompt 含 "json" 字样否则恒 400），
max_tokens ≥512（推理模型 reasoning 吃 token，小值触发截断重试后假阳，8-13 实测）。
"""

import json
import subprocess
import sys
import threading
import time
from datetime import datetime

from predictor.config import Settings
from predictor.llm.client import LLMClient
from predictor.resolution.quotes import fetch_close

_TTL = 300.0
_cache: dict = {}
_refreshing = False
_lock = threading.Lock()


def get_probes() -> dict:
    """读缓存；过期/未检测 → 各探测 None（前端提示「尚未检测」）。"""
    ts = _cache.get("ts")
    fresh = bool(ts) and time.time() - ts < _TTL
    return {
        "quotes": _cache.get("quotes") if fresh else None,
        "llm": _cache.get("llm") if fresh else None,
        "scheduler": _cache.get("scheduler") if fresh else None,
        "checked_at": _cache.get("checked_at") if fresh else None,
        "refreshing": _refreshing,
    }


def refresh_probes() -> None:
    """后台刷新（FastAPI BackgroundTasks 调用）；并发调用幂等（进行中标记）。"""
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        quotes, llm, scheduler = _probe_quotes(), _probe_llm(), _probe_scheduler()
        with _lock:
            _cache.update(
                {
                    "quotes": quotes,
                    "llm": llm,
                    "scheduler": scheduler,
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                    "ts": time.time(),
                }
            )
    finally:
        with _lock:
            _refreshing = False


def _probe_quotes() -> dict:
    """新浪/腾讯各拉一价（生产揭晓路径同款函数）。"""
    out = {}
    for provider, symbol in (("sina", "gb_$inx"), ("tencent", "usINX")):
        try:
            fetch_close(provider, symbol)
            out[provider] = True
        except Exception:
            out[provider] = False
    return {"ok": all(out.values()), "detail": json.dumps(out, ensure_ascii=False)}


def _probe_llm() -> dict:
    try:
        s = Settings()
        LLMClient(**s.llm_client_kwargs).chat(
            None, [{"role": "user", "content": "回复 pong"}], max_tokens=512
        )
        return {"ok": True, "detail": "LLM 可达"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:120]}"}


_TASKS = ("foresight-daily", "foresight-predict", "foresight-resolve")


def _probe_scheduler() -> dict:
    """schtasks CSV 解析（中/英文表头容错）。level: error=有任务禁用, warn=上次结果非 0/解析失败。"""
    if sys.platform != "win32":
        return {"ok": None, "detail": "非 Windows，跳过", "level": "info"}
    try:
        # /v（verbose）：权威列为「计划任务状态=已启用/已禁用」与「上次结果」。
        # 非 verbose CSV 仅 3 列（任务名/下次运行时间/模式），无状态与上次结果列，
        # 解析恒失败（8-13 实机验证发现）。
        r = subprocess.run(
            ["schtasks", "/query", "/fo", "csv", "/v"], capture_output=True, timeout=20
        )
        lines = r.stdout.decode("gbk", errors="replace").splitlines()
        if len(lines) < 2:
            raise ValueError("empty schtasks output")
        cols = [c.strip().strip('"') for c in lines[0].split(",")]
        i_name = _col(cols, ("TaskName", "任务名"))
        i_status = _col(cols, ("计划任务状态", "Scheduled Task State", "状态", "Status", "模式"))
        i_result = _col(cols, ("上次结果", "Last Result"))
        if None in (i_name, i_status, i_result):
            raise ValueError("columns not found")
        problems = []
        disabled = []
        for line in lines[1:]:
            cells = [c.strip().strip('"') for c in line.split(",")]
            if len(cells) <= max(i_name, i_status, i_result):
                continue
            name = cells[i_name].lstrip("\\").lower()
            if not any(name.startswith(t) for t in _TASKS):
                continue
            status = cells[i_status]
            if status.lower() in ("disabled", "已禁用"):
                disabled.append(name)
            else:
                try:
                    result = int(cells[i_result])
                except ValueError:
                    result = None
                if result == 267011:
                    problems.append(f"{name} 从未运行过")
                elif result not in (None, 0):
                    problems.append(f"{name} 上次结果={result}")
        if disabled:
            return {"ok": False, "level": "error", "detail": f"任务已禁用: {disabled}"}
        if problems:
            return {"ok": False, "level": "warn", "detail": "; ".join(problems)}
        return {"ok": True, "detail": "三任务正常"}
    except Exception as e:
        return {
            "ok": False,
            "level": "warn",
            "detail": f"无法读取任务器状态: {type(e).__name__}: {str(e)[:80]}",
        }


def _col(cols, names):
    """按候选名顺序匹配（names 在前者优先）：/v 表头同时含「模式」(Ready) 与
    「计划任务状态」(Enabled/Disabled)，必须让 计划任务状态 优先——
    若按列序匹配，模式 列在前恒命中，禁用任务永远漏报（F3 实机验证）。"""
    for name in names:
        if name in cols:
            return cols.index(name)
    return None
