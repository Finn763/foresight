"""告警落盘治理单测：同日同类合并去重 + 30 天清理 + 确认后复发（评审 §3.5）。"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
HC = SCRIPTS / "health_check.py"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))
import health_check as hc  # noqa: E402


def _alert_lines(now: datetime, bullets: list[str]) -> list[str]:
    lines = ["# Foresight 健康告警", "", f"检出时间：{now:%Y-%m-%d %H:%M:%S}", "", "## 告警", ""]
    lines += [f"- {b}" for b in bullets]
    lines += ["", "## 全量检查", "", "- [ok] 全绿"]
    return lines


def test_same_day_same_type_merged(tmp_path):
    out = tmp_path / "alerts"
    now = datetime(2026, 8, 27, 9, 35)
    lines = _alert_lines(now, ["LLM 揭晓失败 2 次（24h，api_error/护栏）"])
    f1, merged1 = hc.write_alert_file(out, now, lines, is_error=False)
    f2, merged2 = hc.write_alert_file(out, now, lines, is_error=False)
    assert not merged1 and merged2 and f2 == f1
    assert len(list(out.glob("*.md"))) == 1


def test_count_change_same_type_merged_content_refreshed(tmp_path):
    out = tmp_path / "alerts"
    now = datetime(2026, 8, 27, 9, 35)
    f1, _ = hc.write_alert_file(
        out, now, _alert_lines(now, ["LLM 揭晓失败 2 次（24h，api_error/护栏）"]), is_error=False
    )
    lines2 = _alert_lines(now, ["LLM 揭晓失败 5 次（24h，api_error/护栏）"])
    f2, merged = hc.write_alert_file(out, now, lines2, is_error=False)
    assert merged and f2 == f1
    assert len(list(out.glob("*.md"))) == 1
    assert "5 次" in f1.read_text(encoding="utf-8")  # 合并后内容刷新为最新


def test_different_type_same_day_two_files(tmp_path):
    out = tmp_path / "alerts"
    now = datetime(2026, 8, 27, 9, 35)
    hc.write_alert_file(out, now, _alert_lines(now, ["LLM 揭晓失败 1 次（24h）"]), is_error=False)
    hc.write_alert_file(
        out, now, _alert_lines(now, ["预测跳过 6 题（24h，疑似 key 失效）"]), is_error=False
    )
    assert len(list(out.glob("*.md"))) == 2


def test_cross_day_same_type_not_merged(tmp_path):
    out = tmp_path / "alerts"
    d1 = datetime(2026, 8, 27, 9, 35)
    d2 = datetime(2026, 8, 28, 9, 35)
    lines = _alert_lines(d1, ["LLM 揭晓失败 1 次（24h）"])
    hc.write_alert_file(out, d1, lines, is_error=False)
    hc.write_alert_file(out, d2, lines, is_error=False)
    assert len(list(out.glob("*.md"))) == 2


def test_acked_file_not_merged_recurrence_new_file(tmp_path):
    out = tmp_path / "alerts"
    now = datetime(2026, 8, 27, 9, 35)
    lines = _alert_lines(now, ["LLM 揭晓失败 1 次（24h）"])
    f1, _ = hc.write_alert_file(out, now, lines, is_error=False)
    acked = f1.with_name(f1.name[: -len(".md")] + ".ack.md")
    f1.replace(acked)
    f2, merged = hc.write_alert_file(out, now, lines, is_error=False)
    assert not merged and f2 != acked  # 确认后复发按新告警落盘
    assert len(list(out.glob("*.md"))) == 2


def test_error_type_dedup_by_heading(tmp_path):
    out = tmp_path / "alerts"
    now = datetime(2026, 8, 27, 9, 35)
    lines = [
        "# Foresight 健康自检异常（等待轮次锁超时）",
        "",
        f"检出时间：{now:%Y-%m-%d %H:%M:%S}",
        "",
        "等锁超时。",
    ]
    hc.write_alert_file(out, now, lines, is_error=True)
    hc.write_alert_file(out, now, lines, is_error=True)
    assert len(list(out.glob("*.md"))) == 1
    lines2 = ["# Foresight 健康自检异常（未完成检测）", "", f"检出时间：{now:%Y-%m-%d %H:%M:%S}", "", "撞库。"]
    hc.write_alert_file(out, now, lines2, is_error=True)
    assert len(list(out.glob("*.md"))) == 2  # 不同 error 类别不合并


def test_error_vs_alert_not_merged(tmp_path):
    out = tmp_path / "alerts"
    now = datetime(2026, 8, 27, 9, 35)
    hc.write_alert_file(out, now, _alert_lines(now, ["LLM 揭晓失败 1 次（24h）"]), is_error=False)
    err_lines = [
        "# Foresight 健康自检异常（未完成检测）",
        "",
        f"检出时间：{now:%Y-%m-%d %H:%M:%S}",
        "",
        "撞库。",
    ]
    hc.write_alert_file(out, now, err_lines, is_error=True)
    assert len(list(out.glob("*.md"))) == 2


def test_cleanup_removes_over_30_days_keeps_fresh(tmp_path):
    out = tmp_path / "alerts"
    out.mkdir()
    now = datetime(2026, 8, 27, 10, 0)
    stale = out / "alert-20260701-093500.md"
    stale.write_text("# Foresight 健康告警\n", encoding="utf-8")
    acked_stale = out / "alert-20260702-093500.ack.md"  # 已确认的过期文件同样清理
    acked_stale.write_text("# Foresight 健康告警\n", encoding="utf-8")
    fresh = out / "alert-20260826-093500.md"
    fresh.write_text("# Foresight 健康告警\n", encoding="utf-8")
    removed = hc.cleanup_stale_alerts(out, now)
    assert sorted(p.name for p in removed) == [
        "alert-20260701-093500.md",
        "alert-20260702-093500.ack.md",
    ]
    assert fresh.exists()


def test_cleanup_missing_dir_safe(tmp_path):
    assert hc.cleanup_stale_alerts(tmp_path / "nope", datetime.now()) == []


def test_cli_same_day_alert_merged(tmp_path):
    """端到端：同一天两次巡检命中同一告警 → 只落一个文件（第二次合并）。"""
    from predictor.data.storage import Storage

    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    db = d / "t.db"
    st = Storage(str(db))
    st.create_schema()
    now = datetime(2026, 8, 14, 10, 0)
    for r in ("daily_predict", "evolve_predict", "evolve_resolve"):
        st.log_evolution("round_started", json.dumps({"round": r}))
        st.log_evolution("round_completed", json.dumps({"round": r}))
    st.log_evolution(
        "llm_resolve_failed", json.dumps({"qid": 1, "detail": "api_error"}, ensure_ascii=False)
    )
    (d / "latest_scoreboard.json").write_text(
        json.dumps({"date": "2026-08-14"}), encoding="utf-8"
    )
    st._conn.close()

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [
        str(PY), "-E", "-X", "utf8", str(HC), "--db", str(db), "--now", now.isoformat(),
        "--no-notify",
    ]
    r1 = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, timeout=120)
    r2 = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, timeout=120)
    assert r1.returncode == 1 and r2.returncode == 1
    alerts = list((d / "alerts").glob("*.md"))
    assert len(alerts) == 1, alerts
    assert "LLM 揭晓失败" in alerts[0].read_text(encoding="utf-8")
    assert "已合并" in r2.stdout.decode("utf-8", errors="ignore")
