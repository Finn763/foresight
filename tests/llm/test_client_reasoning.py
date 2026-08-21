# tests/llm/test_client_reasoning.py
import json

import httpx
import pytest

from predictor.llm.client import LLMClient, LLMError


class _FakeTransport(httpx.AsyncBaseTransport):
    """按调用序号返回：空 content（截断）→ 正常回复。"""

    def __init__(self, responses: list[dict]):
        self._responses = responses
        self._i = 0

    async def handle_async_request(self, request):
        self._i += 1
        return httpx.Response(200, json=self._responses[self._i - 1])


def _client(responses):
    return LLMClient(
        base_url="https://x",
        api_key="k",
        model="m",
        max_retries=1,
        _transport=_FakeTransport(responses),
    )


def test_empty_content_truncation_retries_then_succeeds():
    truncated = {
        "choices": [
            {"message": {"role": "assistant", "content": "", "reasoning_content": "thinking..."}}
        ]
    }
    ok = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
    c = _client([truncated, ok])
    assert c.chat(None, [{"role": "user", "content": "hi"}]) == "OK"


def test_persistent_truncation_raises_llm_error():
    truncated = {
        "choices": [
            {"message": {"role": "assistant", "content": "", "reasoning_content": "thinking..."}}
        ]
    }
    c = _client([truncated, truncated])
    with pytest.raises(LLMError):
        c.chat(None, [{"role": "user", "content": "hi"}])


def test_include_reasoning_returns_json_string():
    resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "0.6",
                    "reasoning_content": "base rate 60%",
                }
            }
        ]
    }
    c = _client([resp])
    out = json.loads(c.chat(None, [{"role": "user", "content": "hi"}], include_reasoning=True))
    assert out["content"] == "0.6"
    assert "base rate" in out["reasoning"]
