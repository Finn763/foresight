"""B 类 LLM 揭晓器：Responses API 内置 web_search 搜证据 → 双采样判定 + 四护栏。

判定依据 = 题目标题文本（B spec 空壳 `{"class": "B"}` 无需回填）；与 MarketResolver
同构接口，成功返回 3-tuple（extra 携带 confidence/citations，由 auto_resolve 合并进
resolution_ok detail 供时间线审计）。
护栏：no_evidence（无搜索结果/无引用）/ low_confidence（任一采样 <0.7）/
disagreement（双采样不一致）/ api_error（调用或解析失败）→ None + llm_resolve_failed
事件（storage 为 None 时静默跳过，试点脚本用）。
"""

import json
from datetime import datetime, timedelta

from predictor.llm.client import LLMError

_JUDGE_INSTRUCTIONS = (
    "你是预测判定员。根据题目与搜索工具返回的证据，判定该事件在题目给定的时间窗口内是否成立。\n"
    "规则：\n"
    "1. 只依据搜索返回的引用证据判定；题目问的是事件在【截止时间】前是否发生/成立，"
    "禁止用当前状态代替当时状态（如'未来 7 天内突破 X'须判定截止前是否曾突破）。\n"
    "2. citations 必须真实来自搜索结果的 URL，禁止编造。\n"
    '3. 输出 JSON：{{"outcome": true/false, "confidence": 0.0-1.0, '
    '"citations": ["url", ...]}}\n'
    "outcome=true 表示事件成立（题目答案为是）；confidence 是你对判定的把握度。\n"
    "题目：{title}\n截止时间：{closes_iso}\n当前判定时刻：{now_iso}"
)

_CONFIDENCE_FLOOR = 0.7


class LLMResolver:
    """client 注入（测试用 fake）；生产由 registry 惰性构造，storage 注入用于护栏日志。"""

    def __init__(self, client, storage=None):
        self._client = client
        self._storage = storage

    def resolve(self, question, spec: dict, now: datetime):
        if now < question.closes_at:
            return None  # 截止未到
        try:
            grace = int(spec.get("grace_days", 3))
        except (TypeError, ValueError):
            grace = 3
        if now > question.closes_at + timedelta(days=grace):
            return None  # 宽限已过 → resolve_round 超时分支接管降级
        samples = []
        for _ in range(2):
            try:
                samples.append(self._judge(question, now))
            except LLMError as e:
                self._log(question.id, "api_error", str(e)[:120])
                return None
            except Exception as e:
                self._log(question.id, "api_error", f"parse failed: {type(e).__name__}")
                return None
        s1, s2 = samples
        guard = self._guard(s1, s2)
        if guard:
            self._log(question.id, *guard)
            return None
        conf = min(s1["confidence"], s2["confidence"])
        citations = sorted(set(s1["citations"]) | set(s2["citations"]))
        return (bool(s1["outcome"]), "llm_websearch", {"confidence": conf, "citations": citations})

    def _judge(self, question, now) -> dict:
        instructions = _JUDGE_INSTRUCTIONS.format(
            title=question.title,
            closes_iso=question.closes_at.isoformat(timespec="seconds"),
            now_iso=now.isoformat(timespec="seconds"),
        )
        raw = self._client.responses_create(
            input="请判定：该事件在截止时间前是否成立？输出 JSON：{\"outcome\": true/false, \"confidence\": 0-1, \"citations\": []}",
            instructions=instructions,
            tools=[{"type": "web_search"}],
            tool_choice={"type": "web_search"},
            temperature=0.3,
            json_format=True,
        )
        # 最终判定文本双路径（8-13 试点实测）：
        # ① 主路径：最后一个 type="message" 条目（优先 phase 含 final，任意 phase
        #    兜底），JSON 文本嵌在 content 列表 output_text 块的 text 字段——真实
        #    响应顶层无 output_text item
        # ② 回退：顶层 output_text 条目的 content 字段（兼容既有测试与 API 波动；
        #    OpenAI 兼容形态 content 是 block 列表 → 取首块 text，再变则 None → 降级）
        text = None
        last_msg_text = None
        final_msg_text = None
        for item in raw.get("output", []):
            if item.get("type") != "message":
                continue
            for block in item.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    t = block.get("text")
                    if t:
                        last_msg_text = t
                        if "final" in str(item.get("phase") or "").lower():
                            final_msg_text = t
        text = final_msg_text if final_msg_text is not None else last_msg_text
        if text is None:
            for item in raw.get("output", []):
                if item.get("type") == "output_text":
                    content = item.get("content")
                    if isinstance(content, list):
                        content = (
                            content[0].get("text")
                            if content and isinstance(content[0], dict)
                            else None
                        )
                    text = content
        verdict = json.loads(text or "{}")
        # 引用三来源合并去重（实测结构 web_search_call 无 results 字段）：
        # ① verdict JSON 的 citations（模型被 prompt 要求输出，主来源；M-1：非
        #    list-of-str（字符串/含 dict 元素）整体作废 → 空，由 no_evidence 护栏拦下）
        # ② message 条目的 citations/urls/annotations 字段（容错解析，实测样本无则忽略）
        # ③ web_search_call action.open_page 的 url；action.search 的 queries 不是引用
        cites = verdict.get("citations") or []
        if not isinstance(cites, list) or not all(isinstance(u, str) for u in cites):
            cites = []  # citations 非法 → 空（由 no_evidence 护栏拦下，而非崩溃）
        citations = []
        for u in cites:
            if u and u not in citations:
                citations.append(u)
        for item in raw.get("output", []):
            if item.get("type") != "message":
                continue
            for u in item.get("citations") or item.get("urls") or []:
                u = u.get("url") if isinstance(u, dict) else u
                if u and str(u) not in citations:
                    citations.append(str(u))
            for ann in item.get("annotations") or []:
                if isinstance(ann, dict):
                    u = ann.get("url") or ann.get("citation")
                    if u and str(u) not in citations:
                        citations.append(str(u))
        for item in raw.get("output", []):
            if item.get("type") != "web_search_call":
                continue
            action = item.get("action") or {}
            if isinstance(action, dict) and action.get("type") == "open_page":
                u = action.get("url")
                if u and str(u) not in citations:
                    citations.append(str(u))
        raw_outcome = verdict.get("outcome")
        if type(raw_outcome) is not bool:
            raise ValueError(f"outcome 非布尔: {raw_outcome!r}")  # 缺失/字符串/None 一律拒
        try:
            confidence = float(verdict.get("confidence"))
        except (TypeError, ValueError):
            raise ValueError(f"confidence 非法: {verdict.get('confidence')!r}")
        if not any(item.get("type") == "web_search_call" for item in raw.get("output", [])):
            # M-2：搜索从未发生则引用必为幻觉（spec 4.2④ 后半句的可实现代理）→ api_error
            raise ValueError("web_search_call 条目缺失（搜索未发生，引用不可信）")
        return {
            "outcome": raw_outcome,
            "confidence": confidence,
            "citations": sorted(set(citations)),
        }

    def _guard(self, s1, s2):
        if not s1["citations"] or not s2["citations"]:
            # 任一采样无引用即证据不足（引用强制是每题采样的门槛，spec 4.2④）
            return ("no_evidence", "web_search 无结果或引用为空")
        if s1["confidence"] < _CONFIDENCE_FLOOR or s2["confidence"] < _CONFIDENCE_FLOOR:
            return (
                "low_confidence",
                f"置信度不足（{s1['confidence']:.2f}/{s2['confidence']:.2f} < 0.7）",
            )
        if s1["outcome"] != s2["outcome"]:
            return ("disagreement", "双采样判定不一致")
        return None

    def _log(self, qid: int, category: str, detail: str) -> None:
        if self._storage is None:
            return
        try:
            self._storage.log_evolution(
                "llm_resolve_failed",
                json.dumps({"qid": qid, "detail": f"{category}: {detail}"}, ensure_ascii=False),
            )
        except Exception:
            pass
