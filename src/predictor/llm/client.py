"""OpenAI 兼容 LLM 客户端：deepseek 等。统一入口，禁止散落直连。"""

import asyncio
import json
import threading
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx

# 截断重试的输出 token 上限。deepseek-v4-flash 推理 token 可占输出 86-99%
# （8-14 实测 reasoning_tokens=1315-2458 对 ~200 token 的最终答案），逐次翻倍
# 到 16384 仍可能被长推理吃光（qid 68 实测失败）；显式截断信号出现时直接跳到上限。
_MAX_OUTPUT_TOKENS_CEILING = 65536

# 进程内 token 记账：按天累积 prompt/completion tokens 与调用次数。
# usage 取不到时记 0 并计入 unknown（不阻断调用，仅记账）。
_DAILY_USAGE_LOCK = threading.Lock()
_DAILY_USAGE: dict[str, dict[str, int]] = {}


def _record_usage(data) -> None:
    usage = data.get("usage") if isinstance(data, dict) else None
    prompt = 0
    completion = 0
    unknown = 0
    if isinstance(usage, dict):
        p = usage.get("prompt_tokens")
        if p is None:
            p = usage.get("input_tokens")
        c = usage.get("completion_tokens")
        if c is None:
            c = usage.get("output_tokens")
        if isinstance(p, int) and isinstance(c, int):
            prompt = p
            completion = c
        else:
            unknown = 1
    else:
        unknown = 1
    day = date.today().isoformat()
    with _DAILY_USAGE_LOCK:
        rec = _DAILY_USAGE.setdefault(day, {"prompt": 0, "completion": 0, "calls": 0, "unknown": 0})
        rec["prompt"] += prompt
        rec["completion"] += completion
        rec["calls"] += 1
        rec["unknown"] += unknown


def dump_daily_usage(log_path) -> None:
    """把当日汇总（日期/总 prompt/总 completion/调用次数）追加一行到 log_path。"""
    day = date.today().isoformat()
    with _DAILY_USAGE_LOCK:
        rec = _DAILY_USAGE.get(day, {"prompt": 0, "completion": 0, "calls": 0})
        prompt, completion, calls = rec["prompt"], rec["completion"], rec["calls"]
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[usage]\t{day}\tprompt={prompt}\tcompletion={completion}\tcalls={calls}\n")


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


# Fetch API 只在这些状态码上重试（官方错误表：408/429/5xx 可重试）；
# 403/404 等确定性失败立即报错，不为同一个抓不到的 URL 重复计费。
_FETCH_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _error_detail(resp: httpx.Response) -> str:
    """取 OpenAI 兼容错误体里的 message；取不到就回退截断原文。"""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    err = data.get("error") if isinstance(data, dict) else None
    message = err.get("message") if isinstance(err, dict) else None
    return str(message)[:200] if message else resp.text[:200]


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
            # 每个 200 响应都记账：截断/空响应重试同样消耗 token，只在成功分支记会漏账。
            _record_usage(data)
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
                    pl["max_tokens"] = min(
                        pl.get("max_tokens", 4096) * 2, _MAX_OUTPUT_TOKENS_CEILING
                    )
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

    async def asearch(
        self,
        query: str,
        *,
        count: int = 10,
        need_content: bool = False,
        ori_question: str | None = None,
    ) -> list[dict]:
        """DeepSeek Search API（POST /v1/tools/search，2026-10 官方文档）：results 原样列表。

        这是独立 REST 接口，不是 /v1/responses 的服务端工具：后者请求会被受理但不再
        真正执行搜索（output 无 web_search_call，模型自述无搜索工具）——2026-09-09
        起预测/揭晓全灭即此因。结果按请求次数计费（5元/千次，空结果不计费）。
        """
        payload: dict[str, Any] = {
            "query": query[:128],
            "count": max(1, min(50, count)),
            "need_content": need_content,
        }
        if ori_question:
            payload["ori_question"] = ori_question[:128]
        url = f"{self.base_url}/v1/tools/search"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            data = await _post_with_retry(client, url, payload, headers, self.max_retries, None)
        results = data.get("results") if isinstance(data, dict) else None
        return results if isinstance(results, list) else []

    def search(self, query: str, **kw) -> list[dict]:
        """同步封装（揭晓器等同步调用点用）。"""
        return asyncio.run(self.asearch(query, **kw))

    async def afetch(self, url: str) -> dict:
        """DeepSeek Fetch API（POST /v1/tools/fetch）：单页 → Markdown 正文。

        与 asearch 同一 Bearer 鉴权；按成功请求计费（5元/千次），失败/无正文不计费。
        错误表（2026-11 官方文档）：403=抓取被安全策略拒绝，404=拿不到可提取正文，
        408/429/5xx 可等待后重试——其余 4xx 不重试，避免重复计费与空转。
        """
        target = url.strip()
        parts = urlsplit(target)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise LLMError(f"Fetch 需要含 host 的 http(s) URL：{target[:200]}")
        payload: dict[str, Any] = {"url": target}
        url_ = f"{self.base_url}/v1/tools/fetch"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_err: Exception | None = None
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.post(url_, json=payload, headers=headers)
                except httpx.HTTPError as e:
                    last_err = e
                else:
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                        except ValueError as e:
                            raise LLMError(f"Fetch 返回非法 JSON: {resp.text[:200]}") from e
                        if not isinstance(data, dict):
                            raise LLMError(f"Fetch 返回非 JSON 对象: {str(data)[:200]}")
                        return {
                            "title": str(data.get("title") or ""),
                            "content": str(data.get("content") or ""),
                        }
                    last_err = LLMError(f"Fetch HTTP {resp.status_code}: {_error_detail(resp)}")
                    if resp.status_code not in _FETCH_RETRY_STATUS:
                        raise last_err
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
        raise LLMError(f"Fetch failed after retries: {last_err}")

    def fetch(self, url: str) -> dict:
        """同步封装（扩展内联体等同步调用点用）。"""
        return asyncio.run(self.afetch(url))

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
        """Responses API 采样（2026-10 起不再承担搜索：搜索走 `asearch`/`search`）。

        POST {base_url}/v1/responses（OpenAI 兼容惯例路径）；timeout 缺省 120s；
        重试/退避与 chat 同机制；output 中无 output_text/message 条目视为截断签名，
        重试时倍增 max_output_tokens（4096→8192→16384 封顶）。返回完整响应 JSON
        （output items 由调用方解析）。注：8-13 试点实测最终答案为 type=\"message\"
        （phase=\"final_answer\"）条目，而非顶层 output_text。"""
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

        async with httpx.AsyncClient(transport=self._transport, timeout=timeout or 120.0) as client:
            return await _post_with_retry(client, url, payload, headers, self.max_retries, _check)
