"""Storage 真实 DuckDB 跑 CRUD + 揭晓回填 Brier + 分桶统计。"""

from datetime import datetime, timedelta

import pytest

from predictor.data.storage import Storage


@pytest.fixture()
def storage() -> Storage:
    st = Storage(":memory:")
    st.create_schema()
    return st


def test_add_and_resolve_question_updates_brier(storage):
    # 动态未来日期：硬编码 2026-9-10 在 closes+7 天后（2026-09-17 起）会命中
    # 延迟归档不写 brier（时间炸弹，同 test_storage_readonly 8-27 教训）
    qid = storage.add_question("中国9月CPI同比会高于8月吗", datetime.now() + timedelta(days=14))
    storage.add_prediction(qid, 0.7, evidence_ids=[1], model_runs={"deepseek-chat": 0.7})
    storage.add_document(qid, "gdelt", "http://x", "t", "c", published_at=datetime(2026, 8, 1))
    storage.resolve_question(qid, True, "国家统计局 9/10 公布")
    q = storage.get_question(qid)
    assert q.outcome is True
    assert storage.brier_latest(qid) == pytest.approx((0.7 - 1.0) ** 2)
    # 在线权重：resolve 后 model_stats 有该模型记录（EMA = 首次 Brier）
    stats = storage.model_stats()
    assert stats and stats[0]["model_name"] == "deepseek-chat"
    assert stats[0]["predictions"] == 1
    assert stats[0]["brier_ema"] == pytest.approx((0.7 - 1.0) ** 2)
    assert "last_updated" in stats[0]  # 系统面板「更新时间」列数据契约


def test_evidence_required_for_prediction(storage):
    qid = storage.add_question("Q", datetime(2026, 10, 1))
    with pytest.raises(ValueError):
        storage.add_prediction(qid, 0.5, evidence_ids=[], model_runs={})


def test_horizon_buckets_only_count_resolved_public(storage):
    # 短周期题（3 天后揭晓）
    q1 = storage.add_question("短", datetime.now() + timedelta(days=3))
    storage.add_prediction(q1, 0.9, evidence_ids=[1], model_runs={})
    storage.resolve_question(q1, True, "s")
    # 长周期题（60 天后揭晓）
    q2 = storage.add_question("长", datetime.now() + timedelta(days=60))
    storage.add_prediction(q2, 0.2, evidence_ids=[1], model_runs={})
    storage.resolve_question(q2, False, "s")
    buckets = {b["bucket"]: b for b in storage.brier_by_horizon_bucket()}
    assert buckets["<=7"]["n"] == 1
    assert buckets["30-90"]["n"] == 1


def test_resolve_scores_only_latest_prediction(storage):
    qid = storage.add_question("多预测题", datetime.now() + timedelta(days=5))
    storage.add_prediction(qid, 0.9, evidence_ids=[1], model_runs={"deepseek-chat": 0.9})
    storage.add_prediction(
        qid, 0.1, evidence_ids=[1], model_runs={"deepseek-chat": 0.1}
    )  # 更新预测
    assert storage.last_prediction_at(qid) is not None
    storage.resolve_question(qid, True, "s")
    # 只给最后一条（0.1）计分：(0.1-1)^2=0.81；旧行 0.9 作废不计入
    assert storage.brier_latest(qid) == pytest.approx((0.1 - 1.0) ** 2)


def test_resolve_scores_only_baseline_arm(storage):
    """I2：计分只认生产臂（baseline/websearch）——predict_round 先写臂 A 后写臂 B，
    一旦 P1 注册候选杠杆，最后一条恒是 experiment，若不隔离会把实验臂当对外战绩
    （污染 scoreboard 且配对 ΔBrier 地基缺失）。"""
    qid = storage.add_question("臂隔离题", datetime.now() + timedelta(days=5))
    storage.add_prediction(qid, 0.7, evidence_ids=[1], model_runs={"deepseek-chat": 0.7})
    storage.add_prediction(
        qid, 0.9, evidence_ids=[1], model_runs={"deepseek-chat": 0.9}, arm="experiment", arm_group=1
    )
    storage.resolve_question(qid, True, "s")
    rows = storage._conn.execute(
        "SELECT arm, brier_score FROM predictions WHERE question_id = ? ORDER BY id", [qid]
    ).fetchall()
    by_arm = {arm: brier for arm, brier in rows}
    # 只有 baseline（0.7，先写）有分；experiment（0.9，后写）不得被计分
    assert by_arm["baseline"] == pytest.approx((0.7 - 1.0) ** 2)
    assert by_arm["experiment"] is None
    assert storage.brier_latest(qid) == pytest.approx((0.7 - 1.0) ** 2)


def test_resolve_scores_websearch_arm(storage):
    """生产臂计分：websearch 臂是 daily/evolve/pm 题的统一生产入口，最后一条
    websearch 预测必须被计分（旧口径只认 baseline 会让生产战绩永远隐形）。"""
    qid = storage.add_question("websearch 臂题", datetime.now() + timedelta(days=5))
    storage.add_prediction(
        qid,
        0.3,
        evidence_ids=[1],
        model_runs={"deepseek-flash-websearch": [0.3]},
        arm="websearch",
    )
    storage.add_prediction(
        qid,
        0.4,
        evidence_ids=[1],
        model_runs={"deepseek-flash-websearch": [0.4]},
        arm="websearch",
    )
    storage.resolve_question(qid, True, "s")
    # 只给最后一条（0.4）计分
    assert storage.brier_latest(qid) == pytest.approx((0.4 - 1.0) ** 2)
    rows = storage._conn.execute(
        "SELECT arm, brier_score FROM predictions WHERE question_id = ? ORDER BY id", [qid]
    ).fetchall()
    assert rows[0][1] is None  # 旧行 0.3 作废
    assert rows[1][1] == pytest.approx((0.4 - 1.0) ** 2)


def test_late_resolve_gt_7d_not_scored(storage):
    """I3：人工延迟揭晓（resolved_at - closes_at > 7 天）不写 brier_score——
    题保留在库中但无战绩，天然不进技能桶（spec §5 延迟 >7 天独立归档）。"""
    qid = storage.add_question("延迟揭晓题", datetime.now() - timedelta(days=8))
    storage.add_prediction(qid, 0.7, evidence_ids=[1], model_runs={})
    storage.resolve_question(qid, True, "s")
    row = storage._conn.execute(
        "SELECT brier_score FROM predictions WHERE question_id = ?", [qid]
    ).fetchone()
    assert row[0] is None
    assert storage.brier_latest(qid) is None
    assert storage.brier_by_horizon_bucket() == []  # 不进技能桶


def test_late_resolve_within_7d_scored(storage):
    """I3 对照：7 天内揭晓正常计分入桶。"""
    qid = storage.add_question("正常揭晓题", datetime.now() - timedelta(days=3))
    storage.add_prediction(qid, 0.7, evidence_ids=[1], model_runs={"deepseek-chat": 0.7})
    storage.resolve_question(qid, True, "s")
    assert storage.brier_latest(qid) == pytest.approx((0.7 - 1.0) ** 2)
    buckets = {b["bucket"]: b for b in storage.brier_by_horizon_bucket()}
    assert buckets["<=7"]["n"] == 1


def test_source_market_ids_dedup(storage):
    """拉题判重：resolution_spec.source 匹配时收集 market_id；损坏 JSON 忽略不崩。"""
    storage.add_question(
        "pm 题 1",
        datetime(2026, 9, 10),
        resolution_class="B",
        resolution_spec={"source": "polymarket", "market_id": "101"},
    )
    storage.add_question(
        "pm 题 2",
        datetime(2026, 9, 10),
        resolution_class="B",
        resolution_spec={"source": "polymarket", "market_id": "102"},
    )
    storage.add_question(
        "自有题",
        datetime(2026, 9, 10),
        resolution_spec={"class": "A", "symbol": "sp500"},
    )
    assert storage.source_market_ids("polymarket") == {"101", "102"}
    assert storage.source_market_ids("other") == set()


# ---- CC §2.7①：证据表 (question_id,url) 唯一约束（2026-08-27）----


def test_add_document_duplicate_url_ignored(storage):
    """同 (question_id,url) 重复插入被唯一索引忽略，返回既有行 id（7 天重预测防重复）。"""
    qid = storage.add_question("去重题", datetime.now() + timedelta(days=5))
    id1 = storage.add_document(qid, "gdelt", "http://x/1", "标题", "正文", published_at=None)
    id2 = storage.add_document(qid, "gdelt", "http://x/1", "标题", "正文", published_at=None)
    assert id2 == id1
    n = storage._conn.execute(
        "SELECT COUNT(*) FROM source_documents WHERE question_id = ?", [qid]
    ).fetchone()[0]
    assert n == 1


def test_add_document_same_url_different_question_ok(storage):
    """同一 URL 可作不同题的证据（唯一性只约束 (question_id,url) 组合）。"""
    q1 = storage.add_question("题一", datetime.now() + timedelta(days=5))
    q2 = storage.add_question("题二", datetime.now() + timedelta(days=5))
    i1 = storage.add_document(q1, "gdelt", "http://x/2", "t", "c", published_at=None)
    i2 = storage.add_document(q2, "gdelt", "http://x/2", "t", "c", published_at=None)
    assert i1 != i2


def test_add_document_null_url_not_deduped(storage):
    """url IS NULL 不参与唯一性（DuckDB 唯一索引视 NULL 互异），可重复入库。"""
    qid = storage.add_question("无URL题", datetime.now() + timedelta(days=5))
    i1 = storage.add_document(qid, "crawler", None, "t", "c", published_at=None)
    i2 = storage.add_document(qid, "crawler", None, "t", "c", published_at=None)
    assert i1 != i2


def test_add_document_not_null_violation_still_raises(storage):
    """INSERT OR IGNORE 只吞唯一性冲突：source NOT NULL 违反仍抛异常（防静默丢证据）。"""
    qid = storage.add_question("坏数据题", datetime.now() + timedelta(days=5))
    with pytest.raises(Exception):
        storage.add_document(qid, None, "http://x/3", "t", "c", published_at=None)
