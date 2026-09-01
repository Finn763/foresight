"""外部探测：行情源 / LLM / 任务计划器。TTL 300s 内存缓存，后台刷新。

探测失败只影响健康页状态，永不抛出（各函数自身 try/except）。
LLM 探测用纯 chat（无 json_mode——DeepSeek 要求 prompt 含 "json" 字样否则恒 400），
max_tokens ≥512（推理模型 reasoning 吃 token，小值触发截断重试后假阳，8-13 实测）。
"""

import csv
import io
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
    fresh = bool(_cache.get("ts")) and time.time() - _cache["ts"] < _TTL
    return {k: _cache.get(k) if fresh else None for k in ("quotes", "llm", "scheduler", "checked_at")} | {"refreshing": _refreshing}


def refresh_probes() -> None:
    """后台刷新（FastAPI BackgroundTasks 调用）；并发调用幂等（进行中标记）。"""
    global _refreshing
    if _refreshing:
        return
    if not _lock.acquire(blocking=False):
        return
    try:
        if _refreshing:
            return
        _refreshing = True
        quotes, llm, scheduler = _probe_quotes(), _probe_llm(), _probe_scheduler()
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
        _refreshing = False
        _lock.release()


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
        r = subprocess.run(
            ["schtasks", "/query", "/fo", "csv", "/v"], capture_output=True, timeout=20
        )
        text = r.stdout.decode("gbk", errors="replace")
        if not text.strip():
            raise ValueError("empty schtasks output")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValueError("empty schtasks output")
        # header normalization: DictReader keeps original header strings (with quotes handled)
        # row keys are exactly header cells
        problems = []
        disabled = []
        for row in reader:
            name = (row.get("任务名") or row.get("TaskName") or "").strip().strip('"').lstrip("\\").lower()
            if not any(name.startswith(t) for t in _TASKS):
                continue
            status = (row.get("计划任务状态") or row.get("Scheduled Task State") or row.get("状态") or row.get("Status") or row.get("模式") or "").strip()
            result_raw = (row.get("上次结果") or row.get("Last Result") or "").strip()
            if status.lower() in ("disabled", "已禁用"):
                disabled.append(name)
            else:
                try:
                    result = int(result_raw.strip('"'))
                except ValueError:
                    result = None
                if result == 267011:
                    problems.append(f"{name} 从未运行过")
                elif result not in (None, 0):
                    problems.append(f"{name} 上次结果={result}")
        # 若所有行均未匹配到 _TASKS 且无 disabled/problems，视为解析失败？但测试用例中小表头只有 4 列也需命中
        # 若 fieldnames 缺少关键列，上面 loop 自然不会产生 disabled/problems，但测试不应误判为正常
        # 额外护栏：若 header 缺少任务名列，提前抛错（与旧 _col 检测等价）
        needed = any(k in reader.fieldnames for k in ("任务名", "TaskName"))
        if not needed:
            raise ValueError("columns not found")
        # similarly require status/result column presence when platform is win32; 旧逻辑要求三列同时存在
        has_status = any(k in reader.fieldnames for k in ("计划任务状态", "Scheduled Task State", "状态", "Status", "模式"))
        has_result = any(k in reader.fieldnames for k in ("上次结果", "Last Result"))
        if not (has_status and has_result):
            raise ValueError("columns not found")
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
