"""OpenAI 兼容 LLM 客户端：deepseek 等。统一入口，禁止散落直连。"""

import asyncio
import json
from typing import Any

import httpx

# 截断重试的输出 token 上限。deepseek-v4-flash 推理 token 可占输出 86-99%
# （8-14 实测 reasoning_tokens=1315-2458 对 ~200 token 的最终答案），逐次翻倍
# 到 16384 仍可能被长推理吃光（qid 68 实测失败）；显式截断信号出现时直接跳到上限。
_MAX_OUTPUT_TOKENS_CEILING = 65536


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        max_retries: int = 2,
        timeout: float = 60.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout
        self._transport = _transport

    def chat(
        self,
        model: str | None,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        json_mode: bool = False,
        max_tokens: int = 4096,  # 改：推理模型 reasoning 占 token，2048 会被吃光
        include_reasoning: bool = False,  # 改：可选捕获 reasoning（归因用）
    ) -> str:
        """同步封装：内部用 asyncio.run。测试传 _transport 以 mock。"""
        return asyncio.run(
            self._achat(
                model,
                messages,
                temperature=temperature,
                json_mode=json_mode,
                max_tokens=max_tokens,
                include_reasoning=include_reasoning,
            )
        )

    async def _achat(
        self, model, messages, *, temperature, json_mode, max_tokens, include_reasoning
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"{self.base_url}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_err: Exception | None = None
        # AsyncClient 复用：单次调用全程一个连接池（重试不再重建，省 TCP/TLS 握手）。
        # payload 在重试间就地变更（max_tokens 倍增），连接池与请求参数解耦。
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code >= 400:
                        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
                    msg = resp.json()["choices"][0]["message"]
                    content = msg.get("content") or ""
                    reasoning = msg.get("reasoning_content") or ""
                    if not content:
                        # 推理模型：max_tokens 被 reasoning 吃光 → content 空 = 截断，不可静默返回。
                        # 重试时倍增 max_tokens（4096→8192→16384 封顶）：同参数重试在
                        # temperature 0 确定性输出下不收敛（对抗审计 P2-2）
                        if attempt < self.max_retries:
                            payload["max_tokens"] = min(payload.get("max_tokens", 4096) * 2, 16384)
                        raise LLMError("LLM content empty (reasoning truncated): retrying")
                    if include_reasoning:
                        return json.dumps(
                            {"content": content, "reasoning": reasoning}, ensure_ascii=False
                        )
                    return content
                except (httpx.HTTPError, KeyError, ValueError, LLMError) as e:
                    # ValueError 覆盖 resp.json() 的 JSONDecodeError（200 + 非 JSON 响应体，
                    # 如中间代理返回 HTML）——原实现在此路径一次都不重试直接穿透
                    last_err = e
                    if attempt < self.max_retries:
                        await asyncio.sleep(2**attempt)  # 指数退避
        raise LLMError(f"LLM call failed after retries: {last_err}")

    def chat_json(self, messages: list[dict], **kw) -> dict:
        """json_mode 且解析 JSON；解析失败抛 LLMError。"""
        raw = self.chat(None, messages, json_mode=True, **kw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM 返回非法 JSON: {raw[:200]}") from e

    def responses_create(
        self,
        *,
        input: str,
        instructions: str,
        tools: list[dict],
        tool_choice: dict | str,
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
        json_format: bool = False,
        timeout: float | None = None,
    ) -> dict:
        """Responses API（内置 web_search 工具，服务端执行；2026-08 官方文档）。

        POST {base_url}/v1/responses（OpenAI 兼容惯例路径）；timeout 缺省 120s
        （服务端搜索慢于普通 chat）；重试/退避与 chat 同机制；output 中无
        output_text/message 条目视为截断签名，重试时倍增 max_output_tokens
        （4096→8192→16384 封顶）。返回完整响应 JSON（output items 由调用方解析）。
        注：8-13 试点实测最终答案为 type="message"（phase="final_answer"）条目，
        而非顶层 output_text。"""
        return asyncio.run(
            self.aresponses_create(
                input=input,
                instructions=instructions,
                tools=tools,
                tool_choice=tool_choice,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                json_format=json_format,
                timeout=timeout,
            )
        )

    async def aresponses_create(
        self,
        *,
        input,
        instructions,
        tools,
        tool_choice,
        max_output_tokens,
        temperature,
        json_format,
        timeout,
    ) -> dict:
        """Responses API 异步入口（2026-08-27 公开）：并发采样（websearch_predictor
        asyncio.gather）直接 await 本方法，不经同步包装（asyncio.run 不可嵌套）。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "input": input,
            "instructions": instructions,
            "tools": tools,
            "tool_choice": tool_choice,
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
        }
        if json_format:
            payload["text"] = {"format": {"type": "json_object"}}
        url = f"{self.base_url}/v1/responses"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_err: Exception | None = None
        # AsyncClient 复用：单次调用全程一个连接池（重试不再重建，省 TCP/TLS 握手）；
        # 并发采样时各自持有一个 client，httpx 连接池各自独立，无跨调用串扰。
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout or 120.0
        ) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code >= 400:
                        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
                    data = resp.json()
                    if not isinstance(data, dict):
                        # 非对象响应（list/null 等）不可解析 → 纳入重试（M-4）
                        raise LLMError(f"responses: 非对象响应 {type(data).__name__}")

                    # 截断护栏目的：无内容即重试。8-13 试点实测最终答案条目是
                    # type="message"（phase="final_answer"，JSON 嵌其 content 列表的
                    # output_text 块），顶层无 output_text——message 条目须含非空
                    # output_text 块才算有内容（M-3：空 message 条目照旧重试）。
                    def _has_answer(items):
                        for item in items:
                            if item.get("type") == "output_text":
                                return True
                            if item.get("type") == "message" and any(
                                isinstance(b, dict)
                                and b.get("type") == "output_text"
                                and b.get("text")
                                for b in (item.get("content") or [])
                            ):
                                return True
                        return False

                    # 显式截断信号：status="incomplete" 或 incomplete_details.reason=
                    # "max_output_tokens"。比「无答案」启发式更准——模型把整段输出预算
                    # 烧在 reasoning 上时，HTTP 仍是 200 且 output 只剩 reasoning 条目。
                    truncated = data.get("status") == "incomplete" or (
                        (data.get("incomplete_details") or {}).get("reason")
                        == "max_output_tokens"
                    )
                    if not _has_answer(data.get("output", [])) or truncated:
                        if attempt < self.max_retries:
                            if truncated:
                                # 截断 = 预算硬不足：直接跳到上限，而非逐次翻倍
                                payload["max_output_tokens"] = _MAX_OUTPUT_TOKENS_CEILING
                            else:
                                payload["max_output_tokens"] = min(
                                    payload["max_output_tokens"] * 2, _MAX_OUTPUT_TOKENS_CEILING
                                )
                            # 重试时压低推理努力度，抑制长推理吃预算（约 -17%，实测）
                            payload["reasoning"] = {"effort": "low"}
                        raise LLMError(
                            f"responses: {'max_output_tokens 截断' if truncated else 'no output_text/message item'}"
                            " (reasoning truncated?): retrying"
                        )
                    return data
                except (httpx.HTTPError, KeyError, ValueError, LLMError) as e:
                    last_err = e
                    if attempt < self.max_retries:
                        await asyncio.sleep(2**attempt)  # 指数退避
        raise LLMError(f"LLM call failed after retries: {last_err}")
