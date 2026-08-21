"""registry 单测：B 类缺 key/构造失败 → get_resolver 返回 None（pending，不每日烧 401）。

用 monkeypatch _default_client 断言两个分支（不触 Settings/.env/conftest 环境变量，
本地与 CI 均稳定；I-1 修复前此两分支均会泄漏出带空 key 或 None client 的 resolver）。
"""

import pytest

from predictor.resolution import registry


class _NoKeyClient:
    api_key = ""


class _KeyClient:
    api_key = "test-key"


def test_registry_b_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(registry, "_default_client", lambda: _NoKeyClient())
    assert registry.get_resolver("B") is None


def test_registry_b_returns_none_on_client_construction_failure(monkeypatch):
    monkeypatch.setattr(registry, "_default_client", lambda: None)
    assert registry.get_resolver("B") is None


def test_registry_b_returns_resolver_with_api_key(monkeypatch):
    monkeypatch.setattr(registry, "_default_client", lambda: _KeyClient())
    assert registry.get_resolver("B") is not None


def test_registry_a_and_c_unchanged(monkeypatch):
    # A 类不触 client；C 类恒 None（I-1 修复不回归 A/C 分支）
    monkeypatch.setattr(registry, "_default_client", lambda: _KeyClient())
    assert registry.get_resolver("A") is not None
    assert registry.get_resolver("C") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
