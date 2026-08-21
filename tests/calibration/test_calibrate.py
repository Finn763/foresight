"""校准闭环测试：fit_pairs 收集 → build_calibrator 样本护栏 → save/load 落盘。"""

from datetime import datetime, timedelta

from predictor.calibration.calibrate import (
    build_calibrator,
    load_calibrator,
    save_calibrator,
)
from predictor.data.storage import Storage


def _resolved_pairs(n: int) -> Storage:
    """造 n 道已揭晓题：概率 0.1~0.9 均匀分布，outcome 与概率正相关（outcome=True
    当 prob>=0.5），保证 fit 出的校准器单调。"""
    st = Storage(":memory:")
    st.create_schema()
    now = datetime(2026, 8, 1)
    for i in range(n):
        qid = st.add_question(f"测试题 {i}", now + timedelta(days=30), is_public=False)
        prob = 0.1 + 0.8 * i / max(n - 1, 1)
        doc = st.add_document(
            qid,
            source="test",
            url="http://x",
            title="t",
            content="c",
            published_at=now,
        )
        st.add_prediction(
            qid, prob, evidence_ids=[doc], model_runs={"deepseek-chat": [prob]}, arm="baseline"
        )
        st.resolve_question(qid, prob >= 0.5, "test", force_score=True)
    return st


def test_build_calibrator_below_min_samples_returns_none():
    st = _resolved_pairs(2)
    assert build_calibrator(st) is None


def test_build_calibrator_with_enough_samples_fits():
    st = _resolved_pairs(35)
    cal = build_calibrator(st)
    assert cal is not None
    assert len(cal.steps) >= 2


def test_calibrator_apply_monotonic_and_clamped():
    st = _resolved_pairs(35)
    cal = build_calibrator(st)
    assert cal is not None
    prev = 0.0
    for p in (0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95):
        out = cal.apply(p)
        assert 0.0 <= out <= 1.0
        assert out >= prev  # 单调不减
        prev = out


def test_save_load_roundtrip(tmp_path):
    st = _resolved_pairs(35)
    cal = build_calibrator(st)
    assert cal is not None
    path = tmp_path / "calibrator.json"
    save_calibrator(cal, path)
    loaded = load_calibrator(path)
    assert loaded is not None
    assert loaded.steps == cal.steps


def test_load_missing_file_returns_none(tmp_path):
    assert load_calibrator(tmp_path / "nope.json") is None


def test_load_corrupt_file_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_calibrator(path) is None


def test_calibration_pairs_uses_last_prediction_any_arm():
    """口径=系统最终输出：同一题先写 baseline 臂再写 websearch 臂（生产臂 brier_score
    恒 NULL），fit 对必须取最后一条 websearch 概率，否则校准器学不到生产分布。"""
    st = Storage(":memory:")
    st.create_schema()
    now = datetime(2026, 8, 1)
    for i in range(35):
        qid = st.add_question(f"测试题 {i}", now + timedelta(days=30), is_public=False)
        doc = st.add_document(
            qid, source="test", url="http://x", title="t", content="c", published_at=now
        )
        # 第一条 baseline 臂概率（旧口径会计入）；第二条 websearch 臂（生产口径）
        st.add_prediction(
            qid, 0.1, evidence_ids=[doc], model_runs={"deepseek-chat": [0.1]}, arm="baseline"
        )
        st.add_prediction(
            qid,
            0.9,
            evidence_ids=[doc],
            model_runs={"deepseek-flash-websearch": [0.9]},
            arm="websearch",
        )
        st.resolve_question(qid, True, "test", force_score=True)
    pairs = st.calibration_pairs()
    assert len(pairs) == 35
    assert all(p == 0.9 for p, _ in pairs)  # 取的是 websearch 臂（最后一条），非 baseline 臂


def test_refresh_from_storage_writes_file(tmp_path):
    from predictor.calibration.calibrate import refresh_from_storage

    st = _resolved_pairs(35)
    path = tmp_path / "cal.json"
    assert refresh_from_storage(st, path=path) is True
    assert path.exists()


def test_refresh_from_storage_below_min_samples(tmp_path):
    from predictor.calibration.calibrate import refresh_from_storage

    st = _resolved_pairs(2)
    path = tmp_path / "cal.json"
    assert refresh_from_storage(st, path=path) is False
    assert not path.exists()
