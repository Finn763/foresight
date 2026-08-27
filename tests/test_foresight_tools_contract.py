"""foresight-tools.ts 契约单测。

覆盖两层契约（CC 评审 §1.2「第三条调用通道无契约」+ §4.2 predict 超时）：
  1. predict 工具超时必须 ≥ 引擎最坏时长（原有 3 例，§4.2）；
  2. BRIDGE_CONTRACT 单一契约常量不被破坏：版本、通道清单、参数白名单、
     超时值、第三条通道（9 个内联只读体）pyApi 白名单、Python 拉起单漏斗、
     错误语义、YOLO 关闭时 bash/edit/write 拦截护栏。

引擎最坏时长计算依据（与 TS 注释同源，改任何一侧先读这里）：
  - 单次采样最坏 = responses API 缺省 timeout 120s × 3 次尝试（max_retries=2）
    + 指数退避 1s+2s ≈ 363s（src/predictor/llm/client.py）；
  - n_samples=2 采样已 asyncio.gather 并发 → 采样阶段最坏 ≈ 363s（不再 ×2）；
  - 加序列拉取（历史基线 fetch_series_map，实测 ~20s）→ 并发后引擎最坏 ≈ 383s ≈ 6.4 分钟；
  - 并发修复前串行最坏 ≈ 2×363 + 20 ≈ 746s ≈ 12.5 分钟（180s 工具超时必然先触发 →
    python 被 kill → agent 重试 → TUI 挂起）。
工具超时 ≥ 15 分钟（900_000ms）对并发最坏 ≈2.3× 余量，且完整覆盖串行最坏（防回退）。

桥契约背景（CC 评审 §1.2 / §5.3）：
  扩展桥三条通道（predict=scripts/predict_cli.py / leaderboard=内联只读 / resolve=
  scripts/resolve.py）之外存在「第三条通道」——9 个只读工具经 runReadJson 内联
  Python 直调 predictor.data.storage / predictor.ops.* 内部 API，绕过引擎公开入口
  契约，历史上零测试覆盖。修复：BRIDGE_CONTRACT 单一常量（22 通道清单 + 参数
  白名单 + 超时 + pyApi 白名单）+ 注册门 contractTool + 内联体门 readBody +
  完备门 assertBridgeContract，本文件断言契约不被破坏。
"""

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_TS = _PROJECT_ROOT / ".foresight" / "extensions" / "foresight-tools.ts"
CLIENT_PY = _PROJECT_ROOT / "src" / "predictor" / "llm" / "client.py"

# 并发后引擎最坏：单次采样 363s（并发，不再 ×2）+ 序列拉取 20s
ENGINE_WORST_MS = (363 + 20) * 1000
MIN_TIMEOUT_MS = 15 * 60 * 1000  # 任务要求：≥ 15 分钟

# 22 条通道清单（与 BRIDGE_CONTRACT.channels 一一对应；改任何一侧先改这里）
# key=工具名；值=(kind, backend, 参数白名单[(name, required)...], 静态超时ms 或 None, entry 或 None)
EXPECTED_CHANNELS = {
    "questions": ("read", "inline_json", [("status", False), ("q", False), ("limit", False)], 60_000, None),
    "question": ("read", "inline_json", [("qid", True)], 60_000, None),
    "leaderboard": ("read", "inline_json", [], 60_000, None),
    "scoreboard": ("read", "inline_json", [], 60_000, None),
    "system": ("read", "inline_json", [], 60_000, None),
    "calibration": ("read", "inline_json", [("limit", False)], 60_000, None),
    "health": ("read", "inline_json", [("refresh", False)], 150_000, None),
    "events": ("read", "inline_json", [("types", False), ("limit", False), ("before_id", False)], 60_000, None),
    "logs": ("read", "inline_json", [("name", True), ("lines", False)], 60_000, None),
    "probe_quotes": ("read", "script", [], 90_000, "scripts/probe_quotes.py"),
    "backtest_report": ("read", "fs_json", [], None, None),
    "predict": ("write", "script", [("question", True), ("closes", False)], 900_000, "scripts/predict_cli.py"),
    "resolve": ("write", "script", [("qid", True), ("outcome", True), ("source", True)], 60_000, "scripts/resolve.py"),
    "publish": ("write", "script", [("qid", True)], 120_000, None),
    "run_round": ("write", "script", [("round", True)], 7_200_000, None),
    "schedule_questions": ("write", "script", [("week", False)], 300_000, None),
    "pm_fetch": (
        "write",
        "script",
        [("dry_run", False), ("public", False), ("max_events", False), ("per_tier", False), ("min_volume", False), ("no_translate", False)],
        900_000,
        None,
    ),
    "pm_resolve": ("write", "script", [("dry_run", False)], 300_000, None),
    # crawl_social 为动态超时：n 组合 × perComboMs + overheadMs
    "crawl_social": ("write", "script", [("keywords", True), ("platforms", True), ("limit", False)], None, None),
    "backtest": ("write", "script", [("sample", False)], 1_800_000, None),
    "compare_backtest": ("write", "script", [("sample", False)], 1_800_000, None),
    "fb_dry_run": ("write", "script", [("limit", False)], 1_800_000, None),
}

# 第三条通道：9 个 inline_json 只读工具（内联 Python 直调引擎内部 API，受 pyApi 白名单约束）
INLINE_READ_TOOLS = [
    "questions",
    "question",
    "leaderboard",
    "scoreboard",
    "system",
    "calibration",
    "health",
    "events",
    "logs",
]


def _tools_src() -> str:
    return TOOLS_TS.read_text(encoding="utf-8")


def _contract_channels_body() -> str:
    """BRIDGE_CONTRACT.channels 数组正文（条目缩进 4 空格起，闭合 `],` 缩进 2 空格）。"""
    m = re.search(r"const BRIDGE_CONTRACT[\s\S]*?channels:\s*\[\n([\s\S]*?)\n  \],", _tools_src())
    assert m, "BRIDGE_CONTRACT.channels 数组未找到（契约常量被删/改名？）"
    return m.group(1)


def _channel_blocks() -> dict:
    """通道条目 → 正文（`{` 换行开头、`name: "x"` 在下一行的条目按 name 切分）。"""
    body = _contract_channels_body()
    parts = re.split(r"\{\s*\n\s*name:\s*\"(\w+)\"", body)
    blocks: dict = {}
    for i in range(1, len(parts), 2):
        blocks[parts[i]] = parts[i + 1]
    return blocks


def _block_of(name: str) -> str:
    blocks = _channel_blocks()
    assert name in blocks, f"契约通道「{name}」缺失（现有：{sorted(blocks)}）"
    return blocks[name]


def _block_params(block: str) -> list:
    return [(n, r == "true") for n, r in re.findall(r'\{ name: "(\w+)", required: (true|false) \}', block)]


def _block_timeout(block: str):
    m = re.search(r"timeoutMs:\s*(\d[\d_]*)", block)
    return int(m.group(1).replace("_", "")) if m else None


def _block_pyapi(block: str) -> list:
    m = re.search(r"pyApi:\s*\[([^\]]*)\]", block)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


# ============================================================================
# 原有 §4.2：predict 工具超时 ≥ 引擎最坏时长（3 例）
# ============================================================================


def test_predict_tool_timeout_constant_at_least_15min():
    # TS 数字可用 _ 分隔（900_000），正则取整段再剥下划线
    m = re.search(r"PREDICT_TOOL_TIMEOUT_MS\s*=\s*(\d[\d_]*)\s*;", _tools_src())
    assert m, "foresight-tools.ts 应定义 PREDICT_TOOL_TIMEOUT_MS 常量"
    value_ms = int(m.group(1).replace("_", ""))
    assert (
        value_ms >= MIN_TIMEOUT_MS
    ), f"predict 工具超时 {value_ms}ms < {MIN_TIMEOUT_MS}ms（引擎最坏 {ENGINE_WORST_MS}ms）"
    assert (
        value_ms >= ENGINE_WORST_MS
    ), f"predict 工具超时 {value_ms}ms < 引擎最坏 {ENGINE_WORST_MS}ms，工具层会先于引擎完成触发"


def test_predict_tool_run_python_uses_contract_timeout():
    # predict 工具的 runPython 调用必须经 channelTimeout 从契约取超时（防改回魔法数而绕过校验）
    src = _tools_src()
    m = re.search(
        r'runPython\(\s*pi,\s*ctx\.cwd,\s*\["scripts/predict_cli\.py",\s*\.\.\.args\],\s*'
        r'channelTimeout\("predict"\),\s*signal,',
        src,
    )
    assert m, 'predict 工具的 runPython 调用应使用 channelTimeout("predict") 取契约超时'
    # 单源链：契约 predict 通道 timeoutMs 必须引用 PREDICT_TOOL_TIMEOUT_MS 常量
    assert re.search(
        r"timeoutMs:\s*PREDICT_TOOL_TIMEOUT_MS", _block_of("predict")
    ), "BRIDGE_CONTRACT 的 predict 通道 timeoutMs 应引用 PREDICT_TOOL_TIMEOUT_MS"


def test_engine_worst_case_basis_unchanged():
    """计算依据护栏：responses 缺省 timeout 120s、max_retries 缺省 2。

    这两处若被改，TS 超时的推导依据失效——本测试在此报警，提醒重新核对。"""
    src = CLIENT_PY.read_text(encoding="utf-8")
    assert "timeout or 120.0" in src, "responses API 缺省 timeout 120s 被改，需重新核对工具层超时"
    assert "max_retries: int = 2" in src, "LLMClient max_retries 缺省被改，需重新核对工具层超时"


# ============================================================================
# 桥契约（§1.2）：BRIDGE_CONTRACT 单一常量不被破坏
# ============================================================================


def test_contract_constant_exists_and_version():
    src = _tools_src()
    assert re.search(r"const BRIDGE_CONTRACT: BridgeContract = \{", src), "BRIDGE_CONTRACT 常量应存在"
    assert re.search(r"version:\s*1,", src), "契约 version 应为 1（结构变更需升版并同步本测试）"


def test_contract_channel_inventory_matches_registered_tools():
    """通道清单：契约登记的 22 条 ⇔ 实际经 contractTool 注册的 22 条，双向一致。"""
    blocks = _channel_blocks()
    assert set(blocks) == set(EXPECTED_CHANNELS), (
        f"契约通道清单与预期不一致：契约多={set(blocks) - set(EXPECTED_CHANNELS)}，"
        f"契约缺={set(EXPECTED_CHANNELS) - set(blocks)}"
    )
    registered = re.findall(r"contractTool\(\s*pi,\s*defineTool\(\{\s*name:\s*\"(\w+)\"", _tools_src())
    assert sorted(registered) == sorted(EXPECTED_CHANNELS), (
        f"经 contractTool 注册的工具集与契约不一致：注册多={set(registered) - set(EXPECTED_CHANNELS)}，"
        f"注册缺={set(EXPECTED_CHANNELS) - set(registered)}"
    )


def test_contract_all_registrations_go_through_gate():
    """无契约通道不得注册：pi.registerTool 只允许出现在 contractTool 门内（全文件恰 1 处）。"""
    assert _tools_src().count("pi.registerTool(") == 1, (
        "pi.registerTool 应只在 contractTool 内部出现一次；"
        "新工具必须经 contractTool 注册并先登记 BRIDGE_CONTRACT"
    )


def test_contract_param_whitelist():
    """参数白名单：每个通道的参数名与必填标记必须与契约一致。"""
    for name, (kind, backend, params, _timeout, _entry) in EXPECTED_CHANNELS.items():
        block = _block_of(name)
        assert re.search(r'kind:\s*"(\w+)"', block).group(1) == kind, f"{name} kind 应为 {kind}"
        assert re.search(r'backend:\s*"(\w+)"', block).group(1) == backend, f"{name} backend 应为 {backend}"
        actual = _block_params(block)
        assert actual == params, f"{name} 参数白名单不一致（契约={params}，实际={actual}）"
        if backend == "script":
            assert re.search(r'entry:\s*"', block), f"{name} script 通道缺 entry"
        if backend == "inline_json":
            assert _block_pyapi(block), f"{name} inline_json 通道缺 pyApi 白名单（第三条通道必须契约化）"


def test_contract_timeouts():
    """超时值：静态超时与契约一致；crawl_social 动态超时参数一致。"""
    for name, (_kind, _backend, _params, timeout, _entry) in EXPECTED_CHANNELS.items():
        block = _block_of(name)
        if name == "predict":
            # predict 的 timeoutMs 引用 PREDICT_TOOL_TIMEOUT_MS 常量（单源链，§4.2 测试校验值本身）
            assert re.search(r"timeoutMs:\s*PREDICT_TOOL_TIMEOUT_MS", block), "predict 通道 timeoutMs 应引用常量"
            continue
        if name == "crawl_social":
            assert _block_timeout(block) is None, "crawl_social 不应有静态 timeoutMs"
            m = re.search(r"dynamicTimeout:\s*\{\s*perComboMs:\s*(\d[\d_]*),\s*overheadMs:\s*(\d[\d_]*)", block)
            assert m, "crawl_social 缺 dynamicTimeout"
            per_combo = int(m.group(1).replace("_", ""))
            overhead = int(m.group(2).replace("_", ""))
            assert per_combo == 1_800_000, f"crawl_social perComboMs={per_combo} 应为 30 分钟"
            assert overhead == 120_000, f"crawl_social overheadMs={overhead}"
            # 执行点必须从契约取动态超时（不得绕过）
            assert re.search(
                r'keywords\.length \* platforms\.length \* dynamic\.perComboMs \+ dynamic\.overheadMs',
                _tools_src(),
            ), "crawl_social 执行点应使用 channelDynamicTimeout 取契约动态超时"
            continue
        if timeout is None:
            assert _block_timeout(block) is None, f"{name} 不应有静态 timeoutMs（fs_json 不经 Python）"
        else:
            actual = _block_timeout(block)
            assert actual == timeout, f"{name} 超时契约={timeout}ms，实际={actual}ms"


def test_contract_inline_bodies_hoisted_through_readbody_gate():
    """第三条通道收紧：9 个 inline_json 只读体的构建必须经 readBody 门（模块级，加载期校验）。"""
    src = _tools_src()
    hoisted = dict(re.findall(r"const READ_BODY_(\w+) = readBody\(\"(\w+)\"", src))
    for tool in INLINE_READ_TOOLS:
        assert tool in hoisted.values(), f"只读工具「{tool}」内联体未经 readBody 门构建（第三条通道失管）"


def test_contract_third_channel_import_whitelist():
    """第三条通道 pyApi 白名单：每个内联体的全部 import 必须落在该通道 pyApi 前缀内。"""
    src = _tools_src()
    for tool in INLINE_READ_TOOLS:
        block = _block_of(tool)
        pyapi = _block_pyapi(block)
        assert pyapi, f"{tool} 契约缺 pyApi 白名单"
        m = re.search(rf"readBody\(\"{tool}\",\s*\[\n([\s\S]*?)\n\]\);", src)
        assert m, f"{tool} 内联体未找到"
        mods = []
        for mod_a, mod_b in re.findall(
            r"^\s*\"?(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", m.group(1), re.M
        ):
            mods.append(mod_a or mod_b)
        assert mods, f"{tool} 内联体未发现任何 import（体可能被改空）"
        for mod in mods:
            covered = any(a == mod or a.startswith(mod + ".") for a in pyapi)
            assert covered, (
                f"{tool} 内联体 import {mod} 不在其 pyApi 白名单 {pyapi}——"
                "直调引擎内部 API 必须先登记契约（第三条通道已收紧）"
            )


def test_contract_read_envelope_semantics():
    """readEnvelope 错误语义：内联体必须产 _out（readBody 门强制），runReadJson 异常缺省 error 信封。"""
    src = _tools_src()
    assert 'body.includes("_out")' in src, "readBody 门必须强制 _out 产出（readEnvelope 契约）"
    assert "_out = {'error': f'{type(_e).__name__}:" in src, "runReadJson 异常缺省 error 信封被改"
    for tool in INLINE_READ_TOOLS:
        m = re.search(rf"readBody\(\"{tool}\",\s*\[\n([\s\S]*?)\n\]\);", src)
        assert "_out" in m.group(1), f"{tool} 内联体未产 _out（readEnvelope 契约）"


def test_contract_python_invocation_single_funnel():
    """Python 拉起单漏斗：pi.exec 全文件仅 runPython/runWriteScript 两处，且都带 -E -X utf8。"""
    src = _tools_src()
    assert src.count("pi.exec(") == 2, (
        "pi.exec 应只在 runPython 与 runWriteScript 两个 spawn 点出现；"
        "新增 Python 调用通道必须走这两个封装（契约 python.envFlags 统一生效）"
    )
    assert src.count('["-E", "-X", "utf8", ...args]') == 2, "两个 spawn 点都必须带 -E -X utf8"
    # 只读工具参数必须经 sys.argv[1] JSON 传入（防注入），不得拼进 -c 代码
    assert '"print(json.dumps(_out, ensure_ascii=False, default=str))"' in src
    assert '["-c", code, JSON.stringify(payload)]' in src, "runReadJson 必须以 argv[1] JSON 传参"


def test_contract_error_semantics_nonzero_exit_throws():
    """nonzeroExit 错误语义：两个 spawn 封装对 exit!=0 / killed 一律抛 Error（含 stdout/stderr）。"""
    src = _tools_src()
    assert src.count("r.code !== 0 || r.killed") == 2, "runPython/runWriteScript 都必须抛非零退出错误"
    assert "throw new Error(" in src


def test_contract_yolo_off_blocks_agent_shell_escape():
    """第三条通道相邻护栏：YOLO 关闭时 tool_call 钩子必须拦截内置 bash/edit/write。

    bash 是 agent 唯一可绕过工具定义直接触达 Python 引擎的宿主通道（内置工具清单已核：
    bash/edit/write/find/grep/read），YOLO 关闭时被硬拦截；YOLO 开启为显式信任模式（放行）。
    本测试确保拦截器本身不被静默拆除。"""
    src = _tools_src()
    m = re.search(r'pi\.on\("tool_call",[\s\S]*?\n  \}\)', src)
    assert m, "tool_call 钩子缺失"
    hook = m.group(0)
    for tool in ("bash", "edit", "write"):
        assert f'event.toolName === "{tool}"' in hook, f"tool_call 钩子必须拦截 {tool}"
    assert "block: true" in hook, "拦截必须返回 block: true"


def test_contract_clip_and_sequential_invariants():
    """输出截断与写串行不变量：OUTPUT_CLIP=20_000 与契约 errorSemantics.clipBytes 一致；
    写工具执行模式为 sequential（DuckDB 单写者）。"""
    src = _tools_src()
    assert re.search(r"const OUTPUT_CLIP = 20_000;", src), "OUTPUT_CLIP 应为 20_000"
    assert re.search(r"clipBytes:\s*20_000,", src), "契约 errorSemantics.clipBytes 应与 OUTPUT_CLIP 一致"
    for name, (kind, _b, _p, _t, _e) in EXPECTED_CHANNELS.items():
        if kind != "write":
            continue
        m = re.search(rf'defineTool\(\{{\s*name: "{name}",([\s\S]*?)(?=contractTool\(|readBody\(|\Z)', src)
        assert m, f"写工具 {name} 的 defineTool 注册块未找到"
        assert "executionMode: SEQUENTIAL" in m.group(1), (
            f"写工具 {name} 应声明 executionMode: SEQUENTIAL（DuckDB 单写者纪律）"
        )
