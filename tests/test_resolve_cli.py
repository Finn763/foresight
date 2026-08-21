import importlib.util
from pathlib import Path

import pytest


def _load_resolve():
    spec = importlib.util.spec_from_file_location("resolve_mod", Path("scripts/resolve.py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_load_resolutions_csv(tmp_path):
    resolve = _load_resolve()
    rows = resolve.load_resolutions_csv(tmp_path / "r.csv", text="id,outcome,source\n1,1,统计局")
    assert rows == [(1, True, "统计局")]


def test_load_resolutions_csv_bad_header_raises():
    """对抗测试：缺 id/outcome/source 列的 csv 必须报错，不得静默 0 揭晓。"""
    resolve = _load_resolve()
    with pytest.raises(ValueError, match="缺少列"):
        resolve.load_resolutions_csv(None, text="bad,header,only\n1,0,src")


def test_main_skips_premature_auto_a_and_resolved(tmp_path, monkeypatch):
    """8-14 预演前对抗审计防护：① 合法 A 类未到数据窗口（美股 T+1 前）拒绝人工填表
    （防把自动题在行情出现前判死）；② 已揭晓题跳过（防 outcome 覆盖 + model_stats 重复自增）。"""
    from datetime import datetime, timedelta

    from predictor.data.storage import Storage

    resolve = _load_resolve()
    db = tmp_path / "r.db"
    st = Storage(str(db))
    st.create_schema()
    spec = {
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
    q1 = st.add_question(
        "自动题", datetime.now() - timedelta(hours=1), resolution_class="A", resolution_spec=spec
    )
    q2 = st.add_question(
        "人工C题",
        datetime.now() - timedelta(days=1),
        resolution_class="C",
        resolution_spec={"class": "C"},
    )
    st.close()
    csvf = tmp_path / "out.csv"
    csvf.write_text(f"id,outcome,source\n{q1},1,x\n{q2},0,y\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["resolve.py", "--db", str(db), "--outcomes", str(csvf)])
    resolve.main()
    st2 = Storage(str(db))
    assert st2.get_question(q1).outcome is None  # 未到可判定时点 → 拒绝人工填表
    assert st2.get_question(q2).outcome is False  # 人工题正常揭晓
    st2.close()
