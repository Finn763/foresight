"""evolve.py 编排测试：文件锁排他/stale 接管/predict_round 产题族/超时兜底降级（T7 Step 1）。

注：brief 原测试 import 了不存在的 ReleaseLock（brief Step 3 代码块只有
acquire_lock 上下文管理器），落地为仅导入实际存在的符号。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.data.storage import Storage
from scripts.evolve import acquire_lock, predict_round, resolve_round, write_weekly_review


def test_lock_exclusive(tmp_path):
    lock_path = tmp_path / "evolve.lock"
    with acquire_lock(lock_path) as a:
        assert a
        # brief 原写法 `pytest.raises(SystemExit): acquire_lock(lock_path)` 是裸调用
        # （contextmanager 函数体不执行），必须嵌套 with 进入才触发拒绝
        with pytest.raises(SystemExit):
            with acquire_lock(lock_path):
                pass


def test_lock_stale_takeover(tmp_path):
    lock_path = tmp_path / "evolve.lock"
    lock_path.write_text("999999|2020-01-01T00:00:00")  # 6 年前 → stale
    with acquire_lock(lock_path, stale_seconds=6 * 3600) as a:
        assert a


def test_lock_takeover_when_pid_dead(tmp_path):
    """原生崩溃（0xc0000005）接管：锁文件新（未过 stale）但持有进程已死 →
    pid 存活检查接管，不等 6h——备援轨道（evolve 09:05）才能及时补上预测。"""
    import time

    lock_path = tmp_path / "evolve.lock"
    lock_path.write_text(f"99999999|{time.time()}")  # 不存在的 pid、新鲜时间戳
    with acquire_lock(lock_path) as a:
        assert a


def test_resolve_question_brier_failure_logs_not_raises():
    """resolve_question 的计分段异常（model_runs 损坏等）→ 记 resolution_brier_failed
    不向上抛：outcome 已落库，抛异常会让揭晓轮崩溃且重跑时该题被跳过、brier 永久缺失。"""
    st = Storage(":memory:")
    st.create_schema()
    qid = st.add_question("题", datetime.now() - timedelta(days=1))
    st.add_prediction(qid, 0.5, evidence_ids=[1], model_runs=None)  # 损坏 model_runs
    st.resolve_question(qid, True, "test")  # 不抛
    assert st.get_question(qid).outcome is True
    evs = st._conn.execute("SELECT event_type FROM evolution_log").fetchall()
    assert [e[0] for e in evs] == ["resolution_brier_failed"]


def test_predict_round_survives_pipeline_exception(tmp_path, monkeypatch):
    """每题兜底契约（预测轮侧）：predict_with_websearch 抛异常 → skip 单题记日志，
    整轮继续（题族补充/战绩快照照常）。"""
    import scripts.daily as daily

    st = Storage(str(tmp_path / "e3.db"))
    st.create_schema()
    st.add_question("会崩的题", datetime(2026, 8, 20, 9, 0))

    def boom(*a, **kw):
        raise RuntimeError("storage write failed")

    monkeypatch.setattr(daily, "predict_with_websearch", boom)
    stats = predict_round(
        st, now=datetime(2026, 8, 17, 9, 5), client=object(), sources=[], base_rates={}
    )
    assert stats["predicted"] == 0
    assert stats["skipped"] == 1
    evs = st._conn.execute("SELECT event_type FROM evolution_log").fetchall()
    assert "prediction_skipped" in [e[0] for e in evs]


def test_resolve_round_manual_includes_b_and_excludes_valid_auto_a(tmp_path, monkeypatch):
    """8-14 预演前对抗审计：人工清单收 B 类（resolver 不可用/client 构造失败时
    仍留人工兜底，不永久悬挂）与非法 spec 题；合法 A 类留给 auto_resolve
    （防提前人工判死自动题）。"""
    from predictor.resolution import auto_resolve as ar

    st = Storage(str(tmp_path / "e4.db"))
    st.create_schema()
    now = datetime(2026, 8, 14, 16, 30)
    valid_a = {
        "class": "A",
        "instrument": "spx",
        "source_primary": "sina",
        "compare_symbol": "gb_$inx",
        "source_backup": "tencent",
        "condition": "gt_prev_close",
        "close_timezone": "America/New_York",
        "grace_days": 3,
        "degrade_to": "C",
    }
    qa = st.add_question(
        "自动A", now - timedelta(days=1), resolution_class="A", resolution_spec=valid_a
    )
    qb = st.add_question(
        "B题", now - timedelta(days=1), resolution_class="B", resolution_spec={"class": "B"}
    )
    monkeypatch.setattr(ar, "get_resolver", lambda cls, storage=None: None)  # A 类不触网
    stats = resolve_round(st, now=now, data_dir=tmp_path)
    assert stats["manual_c"] == 1
    tmpl = (tmp_path / "resolutions.template.csv").read_text(encoding="utf-8")
    assert str(qb) in tmpl and str(qa) not in tmpl
    # 揭晓轮即时刷新战绩快照（8-14 16:30 揭晓后不再等次日 09:05）
    assert (tmp_path / "latest_scoreboard.json").exists()


def test_predict_round_creates_families(tmp_path):
    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    now = datetime(2026, 8, 17, 9, 5)  # 周一
    # base_rates={} 显式传：跳过真实历史数据层网络拉取（生产 None→_build_base_rates
    # 尽力组装、失败同降级为空 dict；此处不依赖网络，测试封闭）
    stats = predict_round(st, now=now, client=None, sources=[], base_rates={})
    # 空库周一产出：超短 3（标普+上证+道琼斯）+ 7d 黄金 + 30d 黄金/人民币/布伦特 + 60d 布伦特 = 8 道。
    # brief 原断言上界 3 早于 T5 修复轮（+7d 黄金、+60d 布伦特），按现状放宽为 1..8。
    assert stats["families_added"] >= 1
    assert stats["families_added"] <= 8


def test_resolve_round_timeout_degrades_class_b(tmp_path, monkeypatch):
    """B 类超宽限（grace 缺省 3 天）→ 降级 C 进人工清单（spec 4.1④）。"""
    from predictor.resolution import auto_resolve as ar

    st = Storage(str(tmp_path / "e5.db"))
    st.create_schema()
    now = datetime(2026, 8, 20, 16, 30)
    qid = st.add_question(
        "B类过期题", now - timedelta(days=10), resolution_class="B", resolution_spec={"class": "B"}
    )
    monkeypatch.setattr(ar, "get_resolver", lambda cls, storage=None: None)  # 不触网
    stats = resolve_round(st, now=now, data_dir=tmp_path)
    assert stats["timeouts"] == 1
    assert st.question_resolution(qid)["class"] == "C"
    assert stats["manual_c"] == 1


def test_resolve_round_timeout_degrades_to_manual_once(tmp_path):
    """T4 遗留兜底：宽限过期仍无法揭晓的 A 类 → 标 C 降级人工，每题只记一次
    resolution_timeout（后续轮次不再刷日志）。"""
    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    now = datetime(2026, 8, 20, 9, 5)  # 周四（不触发周报分支）
    spec = {
        "class": "A",
        "instrument": "usdcnh",
        "source_primary": "sina",
        "compare_symbol": "fx_susdcnh",
        "condition": "lt_threshold",
        "value": 6.75,
        "close_timezone": "Asia/Shanghai",
        "grace_days": 3,
        "degrade_to": "C",
    }
    qid = st.add_question(
        "未来 7 天内 离岸人民币兑美元 会升破 6.75 吗",
        now - timedelta(days=10),  # closes 8-10，宽限 3 天（8-13）已过
        resolution_class="A",
        resolution_spec=spec,
    )
    stats = resolve_round(st, now=now)
    assert stats["timeouts"] == 1
    assert st.question_resolution(qid)["class"] == "C"  # 降级人工
    assert stats["manual_c"] == 1  # 进入待人工揭晓清单
    stats2 = resolve_round(st, now=now)
    assert stats2["timeouts"] == 0  # 幂等：第二轮不再判定超时
    events = st._conn.execute(
        "SELECT COUNT(*) FROM evolution_log WHERE event_type = 'resolution_timeout'"
    ).fetchone()[0]
    assert events == 1  # 每题只记一次
    # T7 审查裁定：超时降级同轮补记归档事件（resolution_archived），语义 = 延迟
    # >7 天未揭晓题由宽限降级人工覆盖，未揭晓题不进技能桶（brier 只计 IS NOT NULL）
    archived = st._conn.execute(
        "SELECT COUNT(*) FROM evolution_log WHERE event_type = 'resolution_archived'"
    ).fetchone()[0]
    assert archived == 1
    row = st._conn.execute(
        "SELECT detail FROM evolution_log WHERE event_type = 'resolution_archived'"
    ).fetchone()[0]
    assert "timeout_manual_degrade" in row and f'"qid": {qid}' in row


def test_resolve_round_broken_grace_days_does_not_crash(tmp_path):
    """T7 审查 Minor 1 崩溃防护：grace_days 非数值（损坏 spec）不中断整轮，
    回退默认 3 天仍进宽限超时降级路径（对称防护同 auto_resolve 的 try/except）。"""
    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    now = datetime(2026, 8, 20, 9, 5)  # 周四（不触发周报分支）
    spec = {
        "class": "A",
        "instrument": "usdcnh",
        "source_primary": "sina",
        "compare_symbol": "fx_susdcnh",
        "condition": "lt_threshold",
        "value": 6.75,
        "close_timezone": "Asia/Shanghai",
        "grace_days": "abc",
        "degrade_to": "C",
    }
    qid = st.add_question(
        "未来 7 天内 离岸人民币兑美元 会升破 6.75 吗",
        now - timedelta(days=10),  # closes 8-10，回退默认 3 天也已过期
        resolution_class="A",
        resolution_spec=spec,
    )
    stats = resolve_round(st, now=now)  # 不崩溃（修复前 int("abc") 抛 ValueError）
    assert stats["timeouts"] == 1  # 回退默认宽限后仍进超时降级路径
    assert st.question_resolution(qid)["class"] == "C"
    types = [
        r[0]
        for r in st._conn.execute("SELECT event_type FROM evolution_log ORDER BY id").fetchall()
    ]
    assert "resolution_timeout" in types and "resolution_archived" in types


def test_write_weekly_review_monday(tmp_path):
    """周一 resolve 轮生成周报骨架（分桶战绩/基线矩阵/待人工揭晓清单）。"""
    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    now = datetime(2026, 8, 17, 9, 5)  # 周一
    write_weekly_review(st, now, data_dir=tmp_path)
    iso = now.isocalendar()
    out = tmp_path / "weekly_review" / f"{iso.year}-W{iso.week:02d}.md"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "## 分桶战绩" in text and "## 基线矩阵" in text and "## 待人工揭晓" in text


def test_predict_round_predicts_newly_added_family_immediately(tmp_path):
    """出题即预测：超短题 closes=次日，predict_round 循环快照不含当轮新题——
    不立即预测则下轮 closes 已过、题永远无预测（8-13 预演发现）。
    （2026-08-13 起预测走 websearch 引擎，fake client 提供 responses_create。）"""
    import json

    def _resp(prob=0.55):
        return {
            "output": [
                {"type": "web_search_call", "action": {"type": "search", "queries": ["q"]}},
                {
                    "type": "message",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {"probability": prob, "rationale": "r", "citations": ["http://e"]}
                            ),
                        }
                    ],
                },
            ]
        }

    class FakeClient:
        def responses_create(self, **kw):
            return _resp()

        async def aresponses_create(self, **kw):
            # 2026-08-27 采样并发化后 websearch_predict 走异步入口（CC §4.2）
            return _resp()

    st = Storage(str(tmp_path / "e2.db"))
    st.create_schema()
    now = datetime(2026, 8, 17, 9, 5)  # 周一
    stats = predict_round(st, now=now, client=FakeClient(), sources=[], base_rates={})
    assert stats["families_added"] >= 1
    assert stats["predicted"] >= 1, "新增族题应立即预测（超短题错过本轮将永远无预测）"
    n_no_pred = st._conn.execute("""
        SELECT COUNT(*) FROM questions q
        LEFT JOIN predictions p ON p.question_id = q.id
        WHERE p.id IS NULL
    """).fetchone()[0]
    assert n_no_pred == 0, "所有新增族题应有预测"
    # F2：题族循环写 question_added 事件（spec §4.1；daily 缺席的备援日时间线不缺出题事件）
    qid = st._conn.execute("SELECT id FROM questions ORDER BY id").fetchone()[0]
    qa_rows = st._conn.execute(
        "SELECT detail FROM evolution_log WHERE event_type = 'question_added'"
    ).fetchall()
    assert qa_rows, "新增族题应写 question_added 事件"
    assert any(f'"qid": {qid}' in r[0] for r in qa_rows), "事件 detail 应含新增题 qid"


def test_main_logs_round_events_per_subround(tmp_path, monkeypatch):
    """evolve main 获锁后按子轮写 round_started/round_completed（round=evolve_resolve）。"""
    import json

    import scripts.evolve as ev

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "e.db"
    st0 = Storage(str(db))
    st0.create_schema()
    st0.close()
    monkeypatch.setattr("sys.argv", ["evolve.py", "resolve", "--db", str(db)])
    monkeypatch.setattr(
        ev,
        "resolve_round",
        lambda st, **kw: {"resolved": 0, "degraded": 0, "pending": 0, "manual_c": 0, "timeouts": 0},
    )
    ev.main()
    st = Storage(str(db))
    rows = st._conn.execute("SELECT event_type, detail FROM evolution_log ORDER BY id").fetchall()
    pairs = [(r[0], json.loads(r[1]).get("round")) for r in rows]
    assert pairs[0] == ("round_started", "evolve_resolve")
    assert pairs[-1] == ("round_completed", "evolve_resolve")


def test_build_base_rates_canonical_titles(monkeypatch):
    """CC §2.2 修复接线：bare key（"标普"）无窗口措辞 → compute_baseline 宁缺毋滥
    返回 None；evolve 用规范题面（未来7天内标普500会创新高吗）计算族基线。"""
    import random

    import scripts.evolve as ev
    from predictor.stats import historical

    rng = random.Random(42)
    price, d = 2000.0, datetime(2020, 1, 1)
    rows = []
    while len(rows) < 600:
        if d.weekday() < 5:
            price *= 1 + 0.0003 + rng.gauss(0, 0.006)
            rows.append(
                {
                    "date": d.strftime("%Y-%m-%d"),
                    "open": price,
                    "close": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                }
            )
        d += timedelta(days=1)
    monkeypatch.setattr(
        historical,
        "fetch_series_map",
        lambda now=None: {
            "sp500": rows,
            "usdcnh": [],
            "gold": [],
            "brent": [],
            "shanghai": [],
            "dow": [],
            "cpi_cn": [],
            "ffr": [],
            "wti_price": [],
            "wti_stock": [],
        },
    )
    rates = ev._build_base_rates(now=datetime(2026, 8, 27, 9, 0))
    assert "标普" in rates
    assert 0.0 < rates["标普"] < 1.0
