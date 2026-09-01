"""Storage 真实 DuckDB 跑 CRUD + 揭晓回填 Brier + 分桶统计。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import predictor.data.storage as storage_mod
from predictor.data.storage import Storage


@pytest.fixture(autouse=True)
def _pin_model_settings(monkeypatch):
    # P0 trim: canonical family removed, fixture is now no-op (kept for compatibility)
    pass


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


# ---- CC §2.3① 题族分桶 + §2.3② brier_ema 归属修正（2026-08-27）----


def test_brier_by_family_buckets_instrument_category_and_unknown(storage):
    """题族维度：instrument（A 类行情网格）→ category（autopick）→ unclassified 兜底；
    口径与 horizon 桶一致（is_public + outcome 非空），非公开题不进。"""
    q1 = storage.add_question(
        "标普题",
        datetime.now() + timedelta(days=3),
        resolution_class="A",
        resolution_spec={"class": "A", "instrument": "spx", "condition": "gt_prev_close"},
    )
    storage.add_prediction(q1, 0.9, evidence_ids=[1], model_runs={})
    storage.resolve_question(q1, True, "s")

    q2 = storage.add_question(
        "黄金题",
        datetime.now() + timedelta(days=3),
        resolution_class="A",
        resolution_spec={"class": "A", "instrument": "gold", "condition": "gt_threshold"},
    )
    storage.add_prediction(q2, 0.7, evidence_ids=[1], model_runs={})
    storage.resolve_question(q2, False, "s")

    q3 = storage.add_question(
        "autopick 题",
        datetime.now() + timedelta(days=3),
        resolution_class="B",
        resolution_spec={
            "class": "B",
            "source": "autopick",
            "event_key": "x-y-z",
            "category": "central_bank",
        },
    )
    storage.add_prediction(q3, 0.8, evidence_ids=[1], model_runs={})
    storage.resolve_question(q3, True, "s")

    q4 = storage.add_question(
        "旧 B 类题",
        datetime.now() + timedelta(days=3),
        resolution_class="B",
        resolution_spec={"class": "B"},  # 无 instrument/category → unclassified
    )
    storage.add_prediction(q4, 0.6, evidence_ids=[1], model_runs={})
    storage.resolve_question(q4, False, "s")

    q5 = storage.add_question("无 spec 题", datetime.now() + timedelta(days=3))
    storage.add_prediction(q5, 0.5, evidence_ids=[1], model_runs={})
    storage.resolve_question(q5, True, "s")

    # 非公开题（回测口径）：计分但绝不进对外族桶
    q6 = storage.add_question(
        "非公开道琼斯题",
        datetime.now() + timedelta(days=3),
        resolution_class="A",
        resolution_spec={"class": "A", "instrument": "dji"},
        is_public=False,
    )
    storage.add_prediction(q6, 0.5, evidence_ids=[1], model_runs={})
    storage.resolve_question(q6, True, "s")

    fams = {f["family"]: f for f in storage.brier_by_family()}
    assert fams["spx"]["n"] == 1
    assert fams["spx"]["brier_mean"] == pytest.approx((0.9 - 1.0) ** 2)
    assert fams["gold"]["n"] == 1
    assert fams["gold"]["brier_mean"] == pytest.approx((0.7 - 0.0) ** 2)
    assert fams["central_bank"]["n"] == 1
    assert fams["central_bank"]["brier_mean"] == pytest.approx((0.8 - 1.0) ** 2)
    assert fams["unclassified"]["n"] == 2  # 旧 B 类（无 instrument/category）+ 无 spec
    assert "dji" not in fams  # is_public=False 不进对外族桶
    assert sum(f["n"] for f in fams.values()) == 5  # 与已揭晓公开题数对账
    assert all(f["unreliable"] for f in fams.values())  # n<30 全部标 unreliable

    # scoreboard_summary 携带 families 维度（/api/scoreboard 对外契约，纯增量字段）
    summary = storage.scoreboard_summary()
    assert summary["resolved"] == 5
    assert {f["family"] for f in summary["families"]} == {
        "spx",
        "gold",
        "central_bank",
        "unclassified",
    }


def test_model_stats_brier_ema_single_owner(storage):
    """CC §2.3②：单模型 model_runs → brier_ema 归属该模型名（经配置化转名）。"""
    qid = storage.add_question("单模型题", datetime.now() + timedelta(days=5))
    storage.add_prediction(qid, 0.7, evidence_ids=[1], model_runs={"deepseek-chat": [0.7]})
    storage.resolve_question(qid, True, "s")
    stats = storage.model_stats()
    assert [s["model_name"] for s in stats] == ["deepseek-chat"]  # 钉死默认配置 → 原名
    assert stats[0]["predictions"] == 1
    assert stats[0]["brier_ema"] == pytest.approx((0.7 - 1.0) ** 2)


def test_model_stats_brier_ema_multi_model_goes_to_ensemble(storage):
    """CC §2.3② P0 trim: 多模型不再归 ensemble，逐键直写（单模型路径保留）。"""
    qid = storage.add_question("多模型题", datetime.now() + timedelta(days=5))
    storage.add_prediction(
        qid,
        0.8,
        evidence_ids=[1],
        model_runs={"model-a": [0.8], "model-b": [0.8]},
    )
    storage.resolve_question(qid, False, "s")
    stats = storage.model_stats()
    assert [s["model_name"] for s in stats] == ["model-a", "model-b"]
    for s in stats:
        assert s["predictions"] == 1
        assert s["brier_ema"] == pytest.approx((0.8 - 0.0) ** 2)


def test_model_stats_canonicalizes_legacy_hardcoded_names(storage, monkeypatch):
    """CC §2.3③ P0 trim: 已移除 canonical 映射，历史硬编码名原样保留不合并。"""
    q1 = storage.add_question("经典管线题", datetime.now() + timedelta(days=5))
    storage.add_prediction(q1, 0.7, evidence_ids=[1], model_runs={"deepseek-chat": [0.7]})
    storage.resolve_question(q1, True, "s")

    q2 = storage.add_question("websearch 题", datetime.now() + timedelta(days=5))
    storage.add_prediction(
        q2,
        0.3,
        evidence_ids=[1],
        model_runs={"deepseek-flash-websearch": [0.3]},
        arm="websearch",
    )
    storage.resolve_question(q2, True, "s")

    q3 = storage.add_question("自定义模型题", datetime.now() + timedelta(days=5))
    storage.add_prediction(q3, 0.5, evidence_ids=[1], model_runs={"my-model": [0.5]})
    storage.resolve_question(q3, True, "s")

    stats = {s["model_name"]: s for s in storage.model_stats()}
    # P0 trim 后不再合并到配置名，各硬编码名独立计数
    assert stats["deepseek-chat"]["predictions"] == 1
    assert stats["deepseek-chat"]["brier_ema"] == pytest.approx((0.7 - 1.0) ** 2)
    assert stats["deepseek-flash-websearch"]["predictions"] == 1
    assert stats["deepseek-flash-websearch"]["brier_ema"] == pytest.approx((0.3 - 1.0) ** 2)
    assert stats["my-model"]["predictions"] == 1
    assert stats["my-model"]["brier_ema"] == pytest.approx((0.5 - 1.0) ** 2)


def test_model_stats_empty_model_runs_not_recorded(storage):
    """model_runs 为空 → 不写 model_stats（现状基线，保持）。"""
    qid = storage.add_question("空 runs 题", datetime.now() + timedelta(days=5))
    storage.add_prediction(qid, 0.7, evidence_ids=[1], model_runs={})
    storage.resolve_question(qid, True, "s")
    assert storage.model_stats() == []


def test_model_stats_non_dict_model_runs_logged_not_raised(storage):
    """model_runs 非对象（JSON null）→ 既有降级契约：brier 照常落库、stats 跳过、
    evolution_log 记 resolution_brier_failed，不向上抛（揭晓轮不崩）。"""
    qid = storage.add_question("异常 runs 题", datetime.now() + timedelta(days=5))
    storage.add_prediction(qid, 0.7, evidence_ids=[1], model_runs=None)
    storage.resolve_question(qid, True, "s")  # 不得抛异常
    assert storage.brier_latest(qid) == pytest.approx((0.7 - 1.0) ** 2)
    assert storage.model_stats() == []
    evs = storage._conn.execute("SELECT event_type FROM evolution_log").fetchall()
    assert [e[0] for e in evs] == ["resolution_brier_failed"]
