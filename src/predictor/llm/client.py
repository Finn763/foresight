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


def _has_answer(items) -> bool:
    for item in items:
        if item.get("type") == "output_text":
            return True
        if item.get("type") == "message" and any(
            isinstance(b, dict) and b.get("type") == "output_text" and b.get("text")
            for b in (item.get("content") or [])
        ):
            return True
    return False


async def _post_with_retry(client, url, payload, headers, max_retries, check):
    """通用重试循环：check(data, payload, attempt) -> True=命中直接返回, False=需重试(已就地改 payload)."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                last_err = LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                break
            data = resp.json()
            if check is not None and not check(data, payload, attempt):
                # check 内部已按需改 payload 并约定 last_err
                last_err = LLMError("retrying: empty/truncated")
                if attempt < max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                break
            return data
        except (httpx.HTTPError, KeyError, ValueError) as e:
            last_err = e
            if attempt < max_retries:
                await asyncio.sleep(2**attempt)
                continue
            break
    raise LLMError(f"LLM call failed after retries: {last_err}")


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
        max_tokens: int = 4096,
        include_reasoning: bool = False,
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

        def _check(data, pl, attempt):
            msg = data["choices"][0]["message"]
            if not (msg.get("content") or ""):
                if attempt < self.max_retries:
                    pl["max_tokens"] = min(pl.get("max_tokens", 4096) * 2, _MAX_OUTPUT_TOKENS_CEILING)
                return False
            return True

        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            data = await _post_with_retry(client, url, payload, headers, self.max_retries, _check)
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        if include_reasoning:
            return json.dumps({"content": content, "reasoning": reasoning}, ensure_ascii=False)
        return content

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
        注：8-13 试点实测最终答案为 type=\"message\"（phase=\"final_answer\"）条目，
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
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
        json_format: bool = False,
        timeout: float | None = None,
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

        def _check(data, pl, attempt):
            if not isinstance(data, dict):
                return False
            truncated = data.get("status") == "incomplete" or (
                (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
            )
            if not _has_answer(data.get("output", [])) or truncated:
                if attempt < self.max_retries:
                    if truncated:
                        pl["max_output_tokens"] = _MAX_OUTPUT_TOKENS_CEILING
                    else:
                        pl["max_output_tokens"] = min(
                            pl["max_output_tokens"] * 2, _MAX_OUTPUT_TOKENS_CEILING
                        )
                    pl["reasoning"] = {"effort": "low"}
                return False
            return True

        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout or 120.0
        ) as client:
            return await _post_with_retry(client, url, payload, headers, self.max_retries, _check)
