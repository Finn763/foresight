"""LLMClient 用 mock httpx 验证：URL/headers/body/json_mode/重试。"""

import httpx
import pytest

from predictor.llm.client import LLMClient, LLMError


def _mock_transport(responses: list[dict], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=responses.pop(0))

    return httpx.MockTransport(handler)


def test_chat_posts_to_openai_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "0.6"}}]})

    client = LLMClient(
        base_url="https://api.deepseek.com",
        api_key="k",
        model="deepseek-chat",
        _transport=httpx.MockTransport(handler),
    )
    out = client.chat("deepseek-chat", [{"role": "user", "content": "hi"}])
    assert out == "0.6"
    # 请求路径必须是 {base_url}/v1/chat/completions，且带 Bearer 认证
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["auth"] == "Bearer k"


def test_json_mode_sets_response_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = LLMClient(
        base_url="https://api.deepseek.com",
        api_key="k",
        model="deepseek-chat",
        _transport=httpx.MockTransport(handler),
    )
    client.chat("deepseek-chat", [], json_mode=True)
    assert '"response_format"' in seen["body"]
    assert '"json_object"' in seen["body"]


def test_retries_then_raises_on_500():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={})

    client = LLMClient(
        base_url="https://api.deepseek.com",
        api_key="k",
        model="deepseek-chat",
        max_retries=2,
        _transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        client.chat("deepseek-chat", [])
    assert calls["n"] == 3  # 初始 1 次 + 重试 2 次
