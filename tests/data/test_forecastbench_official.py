"""Task 22：ForecastBench 官方提交通道——离线测试。

已查证通道（2026-08-11）：邮件注册 + GCP bucket 上传 Forecast Set（非 Metaculus API）。
fetch 读本地 question set（HTTP 降级用 MockTransport mock）；submit 生成 forecast set JSON；
本地记账（ledger）逻辑单独测。全部离线，无网络/token 依赖。
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from predictor.data.forecastbench_official import (
    LEDGER_SOURCE_TAG,
    fetch_open_questions,
    load_ledger,
    record_submissions,
    submit_predictions,
)
from predictor.llm.client import LLMError

DUE = "2026-08-02"


def _set_file(*, path, questions=None, due=DUE):
    """写一份官方 question set 形状的 JSON 到指定路径。"""
    payload = {
        "forecast_due_date": due,
        "question_set": "test-set",
        "questions": questions
        if questions is not None
        else [
            {
                "id": "q_001",
                "source": "manifold",
                "question": "Will X happen?",
                "freeze_datetime": "2026-07-23T00:00:00+00:00",
                "market_info_close_datetime": "2525-01-01T05:00:00+00:00",
                "resolution_criteria": "yes/no",
                "background": "",
                "url": "https://x",
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _q(**over) -> dict:
    base = {
        "id": "q_001",
        "source": "manifold",
        "question": "Will X happen?",
        "freeze_datetime": "2026-07-23T00:00:00+00:00",
        "resolution_criteria": "yes/no",
        "background": "",
        "url": "https://x",
    }
    base.update(over)
    return base


# ---- fetch_open_questions：本地 question set ----
def test_fetch_parses_local_seed_and_filters(tmp_path):
    _set_file(
        path=tmp_path / "latest-llm.json",
        questions=[
            _q(),  # 正常 → 保留
            _q(id="c_001"),  # 正常 → 保留
            _q(id=["a", "b"], question="Combination"),  # combination → 丢
            _q(id="r_001", resolution="yes"),  # 已揭晓 → 丢
            _q(id="t_001", question=""),  # 缺文本 → 丢
        ],
    )
    questions = fetch_open_questions(limit=20, seed_dir=tmp_path)
    assert [q.id for q in questions] == ["q_001", "c_001"]
    q = questions[0]
    assert q.resolved is False
    assert q.outcome is None
    assert q.closes_at == datetime(2026, 7, 23, tzinfo=UTC)  # freeze_datetime
    assert q.closes_at.tzinfo is not None
    assert q.category == "manifold"


def test_fetch_prefers_latest_pointer_over_dated(tmp_path):
    _set_file(path=tmp_path / "latest-llm.json", questions=[_q(id="latest_q")])
    _set_file(path=tmp_path / "2026-08-02-llm.json", questions=[_q(id="dated_q")])
    questions = fetch_open_questions(limit=20, seed_dir=tmp_path)
    assert [q.id for q in questions] == ["latest_q"]


def test_fetch_limit(tmp_path):
    _set_file(path=tmp_path / "latest-llm.json", questions=[_q(id=f"q_{i}") for i in range(5)])
    assert len(fetch_open_questions(limit=2, seed_dir=tmp_path)) == 2


def test_fetch_close_fallback_to_due_date(tmp_path):
    _set_file(
        path=tmp_path / "latest-llm.json",
        questions=[_q(id="x", freeze_datetime=None, market_info_close_datetime=None)],
    )
    q = fetch_open_questions(limit=1, seed_dir=tmp_path)[0]
    assert q.closes_at == datetime(2026, 8, 2, tzinfo=UTC)


# ---- fetch_open_questions：HTTP 降级（MockTransport）----
def test_fetch_http_fallback_with_mock(tmp_path):
    payload = {
        "forecast_due_date": DUE,
        "question_set": "s",
        "questions": [_q(), _q(id=["a", "b"], question="Combination")],
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    questions = fetch_open_questions(limit=20, seed_dir=tmp_path, _transport=transport)
    assert [q.id for q in questions] == ["q_001"]


def test_fetch_http_error_raises(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(LLMError):
        fetch_open_questions(limit=2, seed_dir=tmp_path, _transport=transport)


def test_fetch_malformed_local_seed_raises(tmp_path):
    (tmp_path / "latest-llm.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LLMError):
        fetch_open_questions(limit=2, seed_dir=tmp_path)


# ---- submit_predictions：生成 Forecast Set ----
def test_submit_writes_forecast_set(tmp_path):
    n = submit_predictions(
        [
            {"question_id": "q_001", "probability": 0.7},
            {"id": "q_002", "probability": 1.2},
        ],  # id 别名可用；概率被夹到 [0,1]
        api_token="unused",
        forecast_due_date=DUE,
        out_dir=tmp_path,
    )
    assert n == 2
    path = tmp_path / f"{DUE}_forecast_set.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["forecast_due_date"] == DUE
    assert payload["forecasts"] == [
        {"id": "q_001", "probability": 0.7},
        {"id": "q_002", "probability": 1.0},
    ]


def test_submit_empty_returns_zero(tmp_path):
    assert submit_predictions([], api_token="", out_dir=tmp_path) == 0
    assert not list(tmp_path.iterdir())


def test_submit_bad_entry_raises(tmp_path):
    with pytest.raises(LLMError):
        submit_predictions([{"question_id": "q_001"}], api_token="", out_dir=tmp_path)
    with pytest.raises(LLMError):
        submit_predictions([{"probability": 0.5}], api_token="", out_dir=tmp_path)


# ---- 本地记账 ----
def test_ledger_roundtrip_and_dedupe(tmp_path):
    p = tmp_path / "ledger.json"
    e1 = {
        "question_id": "q_001",
        "local_question_id": 1,
        "title": "A",
        "probability": 0.7,
        "closes_at": "2026-07-23",
        "submitted_at": "2026-08-11T00:00:00",
        "source": LEDGER_SOURCE_TAG,
        "resolved": False,
        "outcome": None,
    }
    e2 = dict(e1, question_id="q_002", local_question_id=2, probability=0.4)

    assert record_submissions([e1, e2], path=p) == 2
    assert record_submissions([e1, e2], path=p) == 0  # 精确重复 → 去重
    assert record_submissions([dict(e1, probability=0.8)], path=p) == 1  # 更新概率 → 新行

    entries = load_ledger(p)
    assert len(entries) == 3
    assert {e["question_id"] for e in entries} == {"q_001", "q_002"}
    assert all(e["source"] == LEDGER_SOURCE_TAG for e in entries)
    assert all(e["resolved"] is False for e in entries)
    probs = [e["probability"] for e in entries if e["question_id"] == "q_001"]
    assert sorted(probs) == [0.7, 0.8]  # append-only，预测更新可追溯


def test_ledger_missing_file_returns_empty(tmp_path):
    assert load_ledger(tmp_path / "nope.json") == []
