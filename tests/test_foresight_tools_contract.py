"""foresight-tools.ts 契约单测：predict 工具超时必须 ≥ 引擎最坏时长（CC 评审 §4.2）。

引擎最坏时长计算依据（与 TS 注释同源，改任何一侧先读这里）：
  - 单次采样最坏 = responses API 缺省 timeout 120s × 3 次尝试（max_retries=2）
    + 指数退避 1s+2s ≈ 363s（src/predictor/llm/client.py）；
  - n_samples=2 采样已 asyncio.gather 并发 → 采样阶段最坏 ≈ 363s（不再 ×2）；
  - 加序列拉取（历史基线 fetch_series_map，实测 ~20s）→ 并发后引擎最坏 ≈ 383s ≈ 6.4 分钟；
  - 并发修复前串行最坏 ≈ 2×363 + 20 ≈ 746s ≈ 12.5 分钟（180s 工具超时必然先触发 →
    python 被 kill → agent 重试 → TUI 挂起）。
工具超时 ≥ 15 分钟（900_000ms）对并发最坏 ≈2.3× 余量，且完整覆盖串行最坏（防回退）。
"""

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_TS = _PROJECT_ROOT / ".foresight" / "extensions" / "foresight-tools.ts"
CLIENT_PY = _PROJECT_ROOT / "src" / "predictor" / "llm" / "client.py"

# 并发后引擎最坏：单次采样 363s（并发，不再 ×2）+ 序列拉取 20s
ENGINE_WORST_MS = (363 + 20) * 1000
MIN_TIMEOUT_MS = 15 * 60 * 1000  # 任务要求：≥ 15 分钟


def _tools_src() -> str:
    return TOOLS_TS.read_text(encoding="utf-8")


def test_predict_tool_timeout_constant_at_least_15min():
    # TS 数字可用 _ 分隔（900_000），正则取整段再剥下划线
    m = re.search(r"PREDICT_TOOL_TIMEOUT_MS\s*=\s*(\d[\d_]*)", _tools_src())
    assert m, "foresight-tools.ts 应定义 PREDICT_TOOL_TIMEOUT_MS 常量"
    value_ms = int(m.group(1).replace("_", ""))
    assert (
        value_ms >= MIN_TIMEOUT_MS
    ), f"predict 工具超时 {value_ms}ms < {MIN_TIMEOUT_MS}ms（引擎最坏 {ENGINE_WORST_MS}ms）"
    assert (
        value_ms >= ENGINE_WORST_MS
    ), f"predict 工具超时 {value_ms}ms < 引擎最坏 {ENGINE_WORST_MS}ms，工具层会先于引擎完成触发"


def test_predict_tool_run_python_uses_timeout_constant():
    # predict 工具的 runPython 调用必须用常量（防改回魔法数 180_000 而绕过校验）
    src = _tools_src()
    m = re.search(
        r'runPython\(\s*pi,\s*ctx\.cwd,\s*\["scripts/predict_cli\.py",\s*\.\.\.args\],\s*'
        r"PREDICT_TOOL_TIMEOUT_MS,\s*signal\)",
        src,
    )
    assert m, "predict 工具的 runPython 调用应使用 PREDICT_TOOL_TIMEOUT_MS 常量"


def test_engine_worst_case_basis_unchanged():
    """计算依据护栏：responses 缺省 timeout 120s、max_retries 缺省 2。

    这两处若被改，TS 超时的推导依据失效——本测试在此报警，提醒重新核对。"""
    src = CLIENT_PY.read_text(encoding="utf-8")
    assert "timeout or 120.0" in src, "responses API 缺省 timeout 120s 被改，需重新核对工具层超时"
    assert "max_retries: int = 2" in src, "LLMClient max_retries 缺省被改，需重新核对工具层超时"
