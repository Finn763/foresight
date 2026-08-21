import pytest

from predictor.inference.forecast import ForecastResult, forecast


class GoodClient:
    def __init__(self):
        self.kw, self.msg = None, None

    def chat_json(self, messages, **kw):
        self.kw, self.msg = kw, messages
        return {"probability": 0.62, "rationale": "base rate 60%, 上调"}


def test_forecast_returns_structured():
    client = GoodClient()
    r = forecast("Q", ["s1"], client)
    assert isinstance(r, ForecastResult)
    assert 0.0 <= r.probability <= 1.0


def test_forecast_sampling_temperature():
    client = GoodClient()
    forecast("Q", ["s1"], client)
    assert client.kw.get("temperature") == 0.5  # 采样温度：单模型模型内分歧


def test_forecast_prior_block_injected_when_given():
    client = GoodClient()
    forecast("Q", ["s1"], client, prior=0.7)
    assert "70%" in client.msg[-1]["content"]  # 先验块真实注入提示词


def test_forecast_system_prompt_used():
    client = GoodClient()
    forecast("Q", ["s1"], client)
    assert client.msg[0]["role"] == "system"


def test_forecast_clamps_probability():
    class WildClient:
        def chat_json(self, messages, **kw):
            return {"probability": 3.0, "rationale": "r"}

    assert forecast("Q", ["s"], WildClient()).probability == 1.0


def test_forecast_raises_after_two_failures():
    class BoomClient:
        def chat_json(self, messages, **kw):
            raise RuntimeError("always fails")

    with pytest.raises(ValueError):
        forecast("Q", ["s"], BoomClient())
