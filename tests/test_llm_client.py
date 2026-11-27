"""LLMClient 单测：responses_create 端点/参数/重试/超时（httpx.MockTransport，零网络）。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

from predictor.llm.client import LLMClient, LLMError


def _client(handler, **kw):
    kw.setdefault("base_url", "https://api.deepseek.com")
    kw.setdefault("api_key", "k")
    kw.setdefault("model", "deepseek-v4-flash")
    kw.setdefault("max_retries", 2)
    kw.setdefault("timeout", 10.0)
    return LLMClient(**kw, _transport=httpx.MockTransport(handler))


def test_responses_create_posts_to_v1_responses_with_tools():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "r1",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "w1",
                        "results": [{"url": "https://x/1", "title": "t"}],
                    },
                    {
                        "type": "output_text",
                        "id": "o1",
                        "content": '{"outcome": true, "confidence": 0.9, "citations": ["https://x/1"]}',
                    },
                ],
            },
        )

    out = _client(handler).responses_create(
        input="题目：明天北京最高气温会超过 35°C 吗",
        instructions="判定并输出 JSON",
        tools=[{"type": "web_search"}],
        tool_choice={"type": "web_search"},
        json_format=True,
        temperature=0.3,
        timeout=30.0,
    )
    assert seen["url"] == "https://api.deepseek.com/v1/responses"
    assert seen["json"]["tools"] == [{"type": "web_search"}]
    assert seen["json"]["tool_choice"] == {"type": "web_search"}
    assert seen["json"]["text"] == {"format": {"type": "json_object"}}
    assert seen["json"]["temperature"] == 0.3
    assert out["output"][-1]["content"].startswith("{")
    # json_format=False 时不带 text 参数
    out2 = _client(
        lambda r: httpx.Response(200, json={"output": [{"type": "output_text", "content": "x"}]})
    ).responses_create(input="a", instructions="b", tools=[], tool_choice="none")
    assert out2["output"][0]["content"] == "x"


def test_responses_create_retries_then_raises():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"output": [{"type": "output_text", "content": "ok"}]})

    out = _client(handler).responses_create(
        input="a", instructions="b", tools=[], tool_choice="none"
    )
    assert calls["n"] == 3 and out["output"][0]["content"] == "ok"


def test_responses_create_accepts_message_final_answer_item():
    # 真实结构（8-13 试点实测）：最终答案条目为 type="message" phase="final_answer"，
    # 文本嵌在 content 列表的 output_text 块——顶层无 output_text item
    real_item = {
        "type": "message",
        "id": "m1",
        "status": "completed",
        "role": "assistant",
        "phase": "final_answer",
        "content": [
            {
                "type": "output_text",
                "text": '{"outcome": false, "confidence": 0.98, "citations": ["https://www.nmc.cn/"]}',
            }
        ],
    }
    out = _client(lambda r: httpx.Response(200, json={"output": [real_item]})).responses_create(
        input="a",
        instructions="b",
        tools=[{"type": "web_search"}],
        tool_choice={"type": "web_search"},
    )
    assert out["output"][0]["type"] == "message"


def test_responses_create_all_failures_raise_llm_error():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(LLMError):
        _client(handler).responses_create(input="a", instructions="b", tools=[], tool_choice="none")


def test_responses_create_no_output_text_retries_with_bigger_tokens():
    seen = {}

    def handler(request):
        payload = json.loads(request.content)
        seen[len(seen)] = payload["max_output_tokens"]
        if len(seen) == 3:
            return httpx.Response(200, json={"output": [{"type": "output_text", "content": "ok"}]})
        return httpx.Response(
            200, json={"output": [{"type": "web_search_call", "id": "w"}]}
        )  # 无 output_text = 截断签名

    _client(handler).responses_create(
        input="a",
        instructions="b",
        tools=[{"type": "web_search"}],
        tool_choice={"type": "web_search"},
    )
    assert seen[0] == 4096 and seen[1] == 8192 and seen[2] == 16384


def test_responses_create_explicit_truncation_jumps_to_ceiling_and_bounds_reasoning():
    # 显式截断（status=incomplete / incomplete_details.reason=max_output_tokens）
    # → 直接跳到上限 + 压低 reasoning effort，而非逐次翻倍。
    seen = {}

    def handler(request):
        payload = json.loads(request.content)
        seen[len(seen)] = (payload["max_output_tokens"], payload.get("reasoning"))
        if len(seen) == 3:
            return httpx.Response(200, json={"output": [{"type": "output_text", "content": "ok"}]})
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "reasoning", "id": "r"}],
            },
        )

    _client(handler).responses_create(
        input="a",
        instructions="b",
        tools=[{"type": "web_search"}],
        tool_choice={"type": "web_search"},
    )
    assert seen[0] == (4096, None)
    assert seen[1] == (65536, {"effort": "low"})
    assert seen[2] == (65536, {"effort": "low"})


class _CountingCloseTransport(httpx.AsyncBaseTransport):
    """记录 aclose 次数：AsyncClient 每重建一次就会 aclose 一次 transport
    （httpx.AsyncClient.aclose → transport.aclose，含外部传入的 transport）。"""

    def __init__(self, handler):
        self._handler = handler
        self.requests = 0
        self.closes = 0

    async def handle_async_request(self, request):
        self.requests += 1
        return self._handler(request)

    async def aclose(self):
        self.closes += 1


def test_responses_create_reuses_async_client_across_retries():
    """CC §4.2：重试不再重建 AsyncClient——3 次尝试只建/关 1 个 client
    （旧实现每尝试重建 → closes=3）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"output": [{"type": "output_text", "content": "ok"}]})

    transport = _CountingCloseTransport(handler)
    client = LLMClient(
        base_url="https://api.deepseek.com",
        api_key="k",
        model="deepseek-v4-flash",
        max_retries=2,
        timeout=10.0,
        _transport=transport,
    )
    out = client.responses_create(input="a", instructions="b", tools=[], tool_choice="none")
    assert calls["n"] == 3
    assert out["output"][0]["content"] == "ok"
    assert transport.requests == 3
    assert transport.closes == 1  # 全程一个 client（旧实现 = 3）


def test_usage_counts_truncated_retry_attempts(monkeypatch):
    """截断/空响应重试同样消耗 token：每个 200 都要记账，不能只记成功那次。"""
    from datetime import date

    from predictor.llm import client as client_mod

    with client_mod._DAILY_USAGE_LOCK:
        client_mod._DAILY_USAGE.pop(date.today().isoformat(), None)
    calls = {"n": 0}

    def handler(request: httpx.Request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": ""}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    assert _client(handler).chat(None, [{"role": "user", "content": "hi"}]) == "ok"
    rec = client_mod._DAILY_USAGE[date.today().isoformat()]
    assert rec["prompt"] == 200
    assert rec["completion"] == 60
    assert rec["calls"] == 2


# ============================================================================
# Fetch API（/v1/tools/fetch）：请求形状、返回、重试语义
# ============================================================================


def test_fetch_posts_url_and_returns_page():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"title": "T", "content": "# body"})

    page = _client(handler).fetch("https://example.com/a")
    assert seen["url"] == "https://api.deepseek.com/v1/tools/fetch"
    assert seen["json"] == {"url": "https://example.com/a"}
    assert page == {"title": "T", "content": "# body"}


def test_fetch_missing_fields_default_to_empty_strings():
    page = _client(lambda r: httpx.Response(200, json={})).fetch("https://example.com/a")
    assert page == {"title": "", "content": ""}


def test_fetch_does_not_retry_deterministic_4xx():
    """403/404 是确定性失败：立即抛 LLMError，不重复计费。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            404, json={"error": {"message": "failed to fetch URL", "type": "not_found_error"}}
        )

    with pytest.raises(LLMError) as ei:
        _client(handler).fetch("https://example.com/missing")
    assert calls["n"] == 1
    assert "404" in str(ei.value) and "failed to fetch URL" in str(ei.value)


def test_fetch_retries_retryable_status_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"title": "T", "content": "body"})

    page = _client(handler, max_retries=1).fetch("https://example.com/a")
    assert calls["n"] == 2
    assert page["content"] == "body"


def test_fetch_rejects_non_http_url_without_request():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"title": "", "content": ""})

    with pytest.raises(LLMError):
        _client(handler).fetch("file:///etc/passwd")
    assert calls["n"] == 0
