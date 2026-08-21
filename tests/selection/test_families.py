"""题族生成器测试（T5）：四时间档 × 配额纪律（族≤30%、同日≤3）× 难度分档 × 全产出 spec 合法。"""

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from predictor.data.storage import Storage
from predictor.resolution.spec import validate_resolution_spec
from predictor.selection.families import difficulty_tier, generate_families


def _st(tmp_path: Path) -> Storage:
    st = Storage(str(tmp_path / "f.db"))
    st.create_schema()
    return st


def _validate_spec_ok(spec) -> bool:
    return validate_resolution_spec(spec) == []


def test_ultra_short_weekday_three_questions(tmp_path):
    st = _st(tmp_path)
    now = datetime(2026, 8, 17, 9, 0)  # 周一
    specs = generate_families(st, now)
    ultra = [s for s in specs if (s.closes_at - now) <= timedelta(days=3)]
    assert len(ultra) == 3
    for s in ultra:
        assert s.resolution_spec["class"] == "A"
        assert _validate_spec_ok(s.resolution_spec)


def test_ultra_short_friday_weekend_skipped(tmp_path):
    """C1 回归：周五/周末超短不出题——次日 closes 落休市日（周五→周六、周六→周日），
    "周六收盘 vs 周五收盘"事件不存在，行情 API 周末只返回"周五收盘 vs 周四收盘"，
    出题即错日错判永久入账；closes 落周六的族必须被跳过（周五起全部跳过）。"""
    for now in (
        datetime(2026, 8, 14, 9, 0),  # 周五
        datetime(2026, 8, 15, 9, 0),  # 周六
        datetime(2026, 8, 16, 9, 0),
    ):  # 周日
        st = _st(tmp_path)
        specs = generate_families(st, now)
        ultra = [s for s in specs if (s.closes_at - now) <= timedelta(days=3)]
        assert ultra == [], f"{now.date()}: 不应生成 closes 落休市日的超短题"


def test_ultra_short_no_saturday_close_any_day(tmp_path):
    """C1 硬约束：全周任一天生成的所有超短题 closes 都不得落在周六。"""
    for offset in range(7):
        now = datetime(2026, 8, 10 + offset, 9, 0)  # 8/10(一) ~ 8/16(日)
        st = _st(tmp_path)
        for s in generate_families(st, now):
            if (s.closes_at - now) <= timedelta(days=3):
                assert s.closes_at.date().weekday() != 5, f"{s.title} closes 落周六（错日错判风险）"


def test_ultra_short_pool_clean_no_brent_gold(tmp_path):
    """I1 回归：超短档只留标普+上证+道琼斯——布伦特/黄金 hf_ 期货无双源验证昨收
    （gt_prev_close 恒失败 → resolution_failed 刷屏后降级 C，"A 类自动揭晓"落空），
    且 ICE/COMEX 周六无结算，P0 移出超短池；周一~周四照常 3 道且全为合法 A 类。"""
    for now in (
        datetime(2026, 8, 17, 9, 0),  # 周一
        datetime(2026, 8, 18, 9, 0),  # 周二（原黄金轮换日）
        datetime(2026, 8, 19, 9, 0),  # 周三（原布伦特轮换日）
        datetime(2026, 8, 20, 9, 0),
    ):  # 周四
        st = _st(tmp_path)
        specs = generate_families(st, now)
        ultra = [s for s in specs if (s.closes_at - now) <= timedelta(days=3)]
        assert len(ultra) == 3
        for s in ultra:
            assert s.resolution_spec["class"] == "A"
            assert _validate_spec_ok(s.resolution_spec)
            assert not any(k in s.title for k in ("布伦特", "黄金"))


def test_all_generated_specs_valid(tmp_path):
    """全产出 spec 过 validate_resolution_spec（覆盖 7d/30d/60d 阈值题）。"""
    st = _st(tmp_path)
    for now in (
        datetime(2026, 8, 16, 9, 0),  # 周日
        datetime(2026, 8, 17, 9, 0),  # 周一
        datetime(2026, 8, 19, 9, 0),
    ):  # 周三（黄金超短日）
        specs = generate_families(st, now)
        assert specs, f"无产出 at {now}"
        for s in specs:
            errs = validate_resolution_spec(s.resolution_spec)
            assert errs == [], f"{s.title}: {errs}"


def test_no_more_than_3_same_day_close(tmp_path):
    st = _st(tmp_path)
    specs = generate_families(st, datetime(2026, 8, 17, 9, 0))
    counts = Counter(s.closes_at.date() for s in specs)
    assert max(counts.values()) <= 3


def test_family_quota_30pct(tmp_path):
    st = _st(tmp_path)
    now = datetime(2026, 8, 17, 9, 0)
    # 预置 6 道未揭晓标普题（>30% 题池）→ 本批标普新题恒 0（族配额 <3 全挡）
    for i in range(6):
        st.add_question(
            f"未来7天内标普500会创新高吗{i}", now + timedelta(days=7), resolution_class="A"
        )
    specs = generate_families(st, now)
    titles = [s.title for s in specs]
    spx = sum(1 for t in titles if "标普" in t)
    assert spx == 0  # 6 题已占 >30% → 本批标普超短题被族配额挡住，恒 0


def test_difficulty_quota_each_tier(tmp_path):
    st = _st(tmp_path)
    # key 为中文子串（与族 key 同构）：标普→易(0.30)、上证/布伦特→中；
    # 人民币/黄金/道琼斯无基线 → 盲档（平衡由调用方注入 base_rates 负责，生成器不内置兜底）
    base = {"标普": 0.30, "上证": 0.45, "布伦特": 0.55}
    specs = generate_families(st, datetime(2026, 8, 17, 9, 0), base_rates=base)
    tiers = [difficulty_tier(s.title, base) for s in specs]
    c = Counter(tiers)
    # 周一 8 道：标普超短→easy；上证超短+布伦特 30d+布伦特 60d→medium；
    #           人民币 30d+黄金 7d+黄金 30d+道琼斯超短（无基线）→blind
    assert c["easy"] == 1
    assert c["medium"] == 3
    assert c["blind"] == 4
