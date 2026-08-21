"""共享 fixtures。环境变量在 import 前注入，避免真实 key 泄漏。
注意：此处不 import predictor.config（Task 2 才创建），配置 fixture 留空，
后续任务需要时再补。"""

import os

import pytest


@pytest.fixture(scope="session")
def _env():
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    return os.environ
