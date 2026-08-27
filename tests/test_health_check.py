"""health_check 自检脚本单测（subprocess 驱动，断言退出码与告警落盘）。"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
HC = ROOT / "scripts" / "health_check.py"


def _env():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PATH"] = str(ROOT / ".venv" / "Scripts") + os.pathsep + env.get("PATH", "")
    return env


def run_hc(db: Path, now: datetime, cwd: Path, extra=None) -> subprocess.CompletedProcess:
    cmd = [str(PY), HC, "--db", str(db), "--now", now.isoformat(), "--no-notify"]
    return subprocess.run(
        cmd + (extra or []), cwd=cwd, env=_env(), capture_output=True, timeout=120
    )


def _seed_rounds(st, now):
    """当日三轮 completed（干净库）→ assess 无 error。"""
    import sys

    sys.path.insert(0, str(ROOT))
    for r in ("daily_predict", "evolve_predict", "evolve_resolve"):
        st.log_evolution("round_started", json.dumps({"round": r}))
        st.log_evolution("round_completed", json.dumps({"round": r}))


def _mkdb(tmp_path):
    sys.path.insert(0, str(ROOT))
    from predictor.data.storage import Storage

    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    db = d / "t.db"
    st = Storage(str(db))
    st.create_schema()
    return st, db


def _close(st):
    try:
        st._conn.close()  # subprocess 要独占打开同一 db 文件
    except Exception:
        pass


def test_clean_rounds_silent(tmp_path):
    st, db = _mkdb(tmp_path)
    now = datetime(2026, 8, 14, 10, 0)
    _seed_rounds(st, now)
    # scoreboard 今日 → 避免战绩快照 warn（warn 不告警，但保持干净）
    (tmp_path / "data" / "latest_scoreboard.json").write_text(
        json.dumps({"date": "2026-08-14"}), encoding="utf-8"
    )
    _close(st)
    r = run_hc(db, now, tmp_path)
    assert r.returncode == 0, r.stdout.decode("utf-8", errors="ignore") + r.stderr.decode(
        "utf-8", errors="ignore"
    )
    assert not (tmp_path / "data" / "alerts").exists()


def test_llm_resolve_failed_alerts(tmp_path):
    st, db = _mkdb(tmp_path)
    now = datetime(2026, 8, 14, 10, 0)
    _seed_rounds(st, now)
    st.log_evolution(
        "llm_resolve_failed",
        json.dumps({"qid": 68, "detail": "api_error: LLM call failed"}, ensure_ascii=False),
    )
    _close(st)
    r = run_hc(db, now, tmp_path)
    assert r.returncode == 1
    alerts = list((tmp_path / "data" / "alerts").glob("*.md"))
    assert alerts, "应生成告警文件"
    text = alerts[0].read_text(encoding="utf-8")
    assert "LLM 揭晓失败" in text


def test_auth_failure_single_event_alerts(tmp_path):
    st, db = _mkdb(tmp_path)
    now = datetime(2026, 8, 14, 10, 0)
    _seed_rounds(st, now)
    st.log_evolution(
        "prediction_skipped",
        json.dumps({"qid": 70, "detail": "LLM call failed: 401 Unauthorized"}, ensure_ascii=False),
    )
    _close(st)
    r = run_hc(db, now, tmp_path)
    assert r.returncode == 1
    text = list((tmp_path / "data" / "alerts").glob("*.md"))[0].read_text(encoding="utf-8")
    assert "认证失败" in text


def test_skip_storm_threshold(tmp_path):
    st, db = _mkdb(tmp_path)
    now = datetime(2026, 8, 14, 10, 0)
    _seed_rounds(st, now)
    for i in range(4):  # 4 条 < 5 阈值 → 静默
        st.log_evolution("prediction_skipped", json.dumps({"qid": i, "detail": "no evidence"}))
    _close(st)
    r = run_hc(db, now, tmp_path)
    assert r.returncode == 0
    from predictor.data.storage import Storage

    st = Storage(str(db))
    st.log_evolution("prediction_skipped", json.dumps({"qid": 9, "detail": "no evidence"}))
    _close(st)
    r = run_hc(db, now, tmp_path)
    assert r.returncode == 1
    text = list((tmp_path / "data" / "alerts").glob("*.md"))[0].read_text(encoding="utf-8")
    assert "预测跳过 5 题" in text


def test_missing_rounds_today_alerts(tmp_path):
    st, db = _mkdb(tmp_path)
    now = datetime(2026, 8, 14, 18, 0)  # 18:00 已过全部宽限 → 缺席轮次 warn→resolve error
    _close(st)
    r = run_hc(db, now, tmp_path)
    assert r.returncode == 1
    text = list((tmp_path / "data" / "alerts").glob("*.md"))[0].read_text(encoding="utf-8")
    assert "揭晓轮" in text


def _seed_clean(tmp_path) -> tuple[Path, datetime]:
    """照抄 test_clean_rounds_silent 的造数：干净轮次 + 今日 scoreboard。"""
    st, db = _mkdb(tmp_path)
    now = datetime(2026, 8, 14, 10, 0)
    _seed_rounds(st, now)
    (tmp_path / "data" / "latest_scoreboard.json").write_text(
        json.dumps({"date": "2026-08-14"}), encoding="utf-8"
    )
    _close(st)
    return db, now


def test_waits_for_live_lock_then_fallback_alert(tmp_path):
    """锁被存活进程持有（测试进程自身 pid）→ 排队到 --lock-wait 上限 → 兜底告警落盘；
    health 未接管锁（锁文件仍在），且从未打开 DB（无撞库重试痕迹）。"""
    db, now = _seed_clean(tmp_path)
    lock = tmp_path / "data" / "evolve.lock"
    lock.write_text(f"{os.getpid()}|{time.time()}", encoding="utf-8")
    r = run_hc(db, now, tmp_path, extra=["--lock-wait", "2", "--lock-poll", "1"])
    assert r.returncode == 1
    alerts = list((tmp_path / "data" / "alerts").glob("*.md"))
    assert alerts, "等锁超时应落兜底告警"
    text = alerts[0].read_text(encoding="utf-8")
    assert "等锁超时" in text
    assert lock.exists()  # health 未接管，锁仍由原持有者


def test_dead_pid_lock_taken_over_and_cleaned(tmp_path):
    """持锁进程已死（不存在的 pid）→ 立即接管；finally unlink 清理锁；干净库静默 0。"""
    db, now = _seed_clean(tmp_path)
    lock = tmp_path / "data" / "evolve.lock"
    lock.write_text(f"99999999|{time.time()}", encoding="utf-8")
    r = run_hc(db, now, tmp_path)
    assert r.returncode == 0, r.stdout.decode("utf-8", errors="ignore") + r.stderr.decode(
        "utf-8", errors="ignore"
    )
    assert not lock.exists()  # 接管后 finally unlink 清理
    assert not (tmp_path / "data" / "alerts").exists()


def test_corrupt_lock_content_taken_over(tmp_path):
    """锁内容损坏（无法解析 pid|ts）→ 视为 stale 接管；锁文件被清理；静默 0。"""
    db, now = _seed_clean(tmp_path)
    lock = tmp_path / "data" / "evolve.lock"
    lock.write_text("garbage", encoding="utf-8")
    r = run_hc(db, now, tmp_path)
    assert r.returncode == 0, r.stdout.decode("utf-8", errors="ignore") + r.stderr.decode(
        "utf-8", errors="ignore"
    )
    assert not lock.exists()  # 接管后 finally unlink 清理
    assert not (tmp_path / "data" / "alerts").exists()
