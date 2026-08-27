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
