"""探测模块单测：行情/LLM/schtasks 解析、TTL 缓存、刷新幂等。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import predictor.ops.probes as probes


def _reset():
    probes._cache.clear()
    probes._refreshing = False


def test_probe_quotes_flags_failure(monkeypatch):
    from predictor.resolution.quotes import QuoteError

    def fake_close(provider, symbol):
        if provider == "sina":
            raise QuoteError("down")
        return 100.0

    monkeypatch.setattr(probes, "fetch_close", fake_close)
    out = probes._probe_quotes()
    assert out["ok"] is False and '"sina": false' in out["detail"]


def test_probe_llm_calls_plain_chat(monkeypatch):
    calls = {}

    class FakeLLM:
        def __init__(self, **kw):
            pass

        def chat(self, model, messages, **kw):
            calls["messages"] = messages
            calls["max_tokens"] = kw.get("max_tokens")
            return "pong"

    monkeypatch.setattr(probes, "LLMClient", FakeLLM)
    out = probes._probe_llm()
    assert out["ok"] is True
    assert calls["max_tokens"] >= 512  # 推理模型 reasoning 会吃 token，防假阳
    # 不用 json_mode（DeepSeek 要求 prompt 含 "json" 字样，8-13 实测 400）
    assert "response_format" not in calls


def test_probe_scheduler_parses_chinese_csv(monkeypatch):
    import subprocess

    class FakeDone:
        stdout = (
            '"任务名","状态","上次运行时间","上次结果"\n'
            '"\\foresight-daily","就绪","2026/8/13 9:00:00","0"\n'
            '"\\Foresight-Predict","就绪","2026/8/13 9:05:00","0"\n'
            '"\\Foresight-Resolve","已禁用","N/A","267011"\n'
        ).encode("gbk")
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeDone())
    out = probes._probe_scheduler()
    assert out["ok"] is False and out["level"] == "error"  # 有任务被禁用


def test_probe_scheduler_last_result_nonzero_warns(monkeypatch):
    import subprocess

    class FakeDone:
        stdout = (
            '"TaskName","Status","Last Run Time","Last Result"\n'
            '"\\foresight-daily","Ready","2026/8/13 9:00:00","0"\n'
            '"\\Foresight-Predict","Ready","2026/8/13 9:05:00","1"\n'
            '"\\Foresight-Resolve","Ready","2026/8/13 16:30:00","0"\n'
        ).encode("gbk")
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeDone())
    out = probes._probe_scheduler()
    # 实现把任务名转小写做匹配，detail 里也用小写（实现契约，勿按输入大小写断言）
    assert out["ok"] is False and out["level"] == "warn" and "foresight-predict" in out["detail"]


def test_probe_scheduler_verbose_csv_real_header(monkeypatch):
    """真实 schtasks /v CSV（29 列中文表头，8-13 实机捕获）——非 verbose 无「上次结果」列。"""
    import subprocess

    class FakeDone:
        stdout = (
            '"主机名","任务名","下次运行时间","模式","登录状态","上次运行时间","上次结果","创建者","要运行的任务",'
            '"起始于","注释","计划任务状态","空闲时间","电源管理","作为用户运行","删除没有计划的任务",'
            '"如果运行了 X 小时 X 分钟，停止任务","计划","计划类型","开始时间","开始日期","结束日期","天","月",'
            '"重复: 每","重复: 截止: 时间","重复: 截止: 持续时间","重复: 如果还在运行，停止"\n'
            '"DESKTOP-PBMT45K","\\foresight-daily","2026/8/14 9:00:00","就绪","只使用交互方式","2026/8/13 9:00:01","0","Administrator","","","","已启用","","","","","","",'
            '"每日","每天","9:00","2026/8/12","N/A","","","",""\n'
            '"DESKTOP-PBMT45K","\\Foresight-Predict","2026/8/14 9:05:00","就绪","只使用交互方式","2026/8/13 9:05:00","0","","","","","已启用","","","","","","",'
            '"每日","每天","9:05","2026/8/12","N/A","","","",""\n'
            '"DESKTOP-PBMT45K","\\Foresight-Resolve","2026/8/14 16:30:00","就绪","只使用交互方式","N/A","267011","","","","","已禁用","","","","","","",'
            '"每日","每天","16:30","2026/8/12","N/A","","","",""\n'
        ).encode("gbk")
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeDone())
    out = probes._probe_scheduler()
    assert out["ok"] is False and out["level"] == "error"  # Resolve 已禁用
    assert "Foresight-Resolve" in out["detail"] or "foresight-resolve" in out["detail"]


def test_cache_ttl_and_refresh_idempotent(monkeypatch):
    _reset()
    probes._cache.update({"quotes": {"ok": True, "detail": ""}, "ts": time.time()})
    assert probes.get_probes()["quotes"]["ok"] is True
    t0 = (
        time.time()
    )  # 先捕获真实时间：patch 的是共享 time 模块属性，lambda 内再调 time.time 会自递归
    monkeypatch.setattr(probes.time, "time", lambda: t0 + 1000)
    assert probes.get_probes()["quotes"] is None  # TTL 过期 → 未检测
    # 刷新进行中 → refresh 幂等返回
    _reset()
    probes._refreshing = True
    probes.refresh_probes()
    assert probes._cache == {}
    _reset()
