"""验证从 forecastbench-datasets 真实结构 JSON 解析出 BenchQuestion 列表。"""

import httpx

from predictor.data.benchmarks import BenchQuestion, fetch_forecastbench_questions


def test_parse_questions_from_real_dataset_shape():
    # 模拟 forecastbench question_sets 真实结构（2026-08-11 实测字段）
    sample = {
        "forecast_due_date": "2025-06-08",
        "question_set": "2025-06-08-llm",
        "questions": [
            {
                "id": "ZxGMjG8U4zDigZh8zcPo",
                "source": "manifold",
                "question": "Will the US CPI for September 2026 be above 3.0%?",
                "resolution_criteria": "Resolves to...",
                "url": "https://manifold.markets/example",
                "freeze_datetime": "2025-06-08T00:00:00+00:00",
            },
            # combination question：id 为数组 → 应被丢弃
            {
                "id": ["a", "b"],
                "source": "manifold",
                "question": "Combination of two questions",
                "freeze_datetime": "2025-06-08T00:00:00+00:00",
            },
        ],
    }
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=sample, headers={"Content-Type": "application/json"})
    )
    questions = fetch_forecastbench_questions(limit=10, _transport=transport)
    assert len(questions) == 1
    q = questions[0]
    assert isinstance(q, BenchQuestion)
    assert q.title.startswith("Will the US CPI")
    assert q.id == "ZxGMjG8U4zDigZh8zcPo"
    assert q.source == "manifold"
    assert q.url.startswith("https://manifold")
    # 官方题集无 resolution → outcome=None（答案待外部源），resolved=False
    assert q.outcome is None
    assert q.resolved is False


def test_parse_outcome_when_resolution_present():
    sample = {
        "questions": [
            {
                "id": "q1",
                "question": "Will X happen?",
                "resolution": "YES",
                "freeze_datetime": "2025-06-08T00:00:00+00:00",
            },
        ]
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=sample))
    questions = fetch_forecastbench_questions(limit=5, _transport=transport)
    assert questions[0].outcome is True
    assert questions[0].resolved is True


def test_local_seed_fallback():
    """网络失败时降级本地 seed（data/fb_seed/ 已从官方仓库整包落盘）。"""
    from predictor.data.benchmarks import _load_local_seed

    questions = _load_local_seed(limit=5)
    assert len(questions) >= 1
    assert all(isinstance(q, BenchQuestion) for q in questions)
