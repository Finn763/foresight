from datetime import datetime, timedelta
from pathlib import Path

import pytest

from predictor.data.storage import Storage


def _new_db(tmp_path: Path) -> Storage:
    st = Storage(str(tmp_path / "t.db"))
    st.create_schema()
    return st


def test_new_schema_has_evolution_columns(tmp_path):
    st = _new_db(tmp_path)
    st.add_question(
        "明天标普会涨吗",
        datetime.now() + timedelta(days=1),
        resolution_class="A",
        resolution_spec={"class": "A", "instrument": "spx"},
    )
    cols = st._conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='questions'"
    ).fetchall()
    assert ("resolution_class",) in cols
    assert ("resolution_spec",) in cols


def test_add_prediction_arm_and_group(tmp_path):
    st = _new_db(tmp_path)
    qid = st.add_question("测试题", datetime.now() + timedelta(days=1))
    st.add_prediction(
        qid, 0.6, evidence_ids=[1], model_runs={"m": [0.6]}, arm="baseline", arm_group=1
    )
    st.add_prediction(
        qid, 0.7, evidence_ids=[1], model_runs={"m": [0.7]}, arm="experiment", arm_group=1
    )
    rows = st._conn.execute(
        "SELECT arm, arm_group FROM predictions WHERE question_id = ? ORDER BY id", [qid]
    ).fetchall()
    assert rows == [("baseline", 1), ("experiment", 1)]


def test_brier_first_latest_avg(tmp_path):
    st = _new_db(tmp_path)
    qid = st.add_question("测试题", datetime.now() + timedelta(days=1))
    st.add_prediction(qid, 0.9, evidence_ids=[1], model_runs={})  # first
    st.add_prediction(qid, 0.3, evidence_ids=[1], model_runs={})  # latest
    st.resolve_question(qid, True, "test")
    # first=0.9 → (0.9-1)^2=0.01; latest=0.3 → (0.3-1)^2=0.49; avg=0.25
    assert st.brier_latest(qid) == pytest.approx(0.49)
    assert st.brier_first(qid) == pytest.approx(0.01)
    assert st.brier_avg(qid) == pytest.approx(0.25)
    both = st.brier_question_both(qid)
    assert both == pytest.approx({"first": 0.01, "latest": 0.49})


def test_migrate_script_idempotent_on_old_schema(tmp_path):
    # 模拟旧库：只建旧表（不含新列/新表），跑迁移后补全
    import duckdb

    old = tmp_path / "old.db"
    conn = duckdb.connect(str(old))
    conn.execute("""
        CREATE SEQUENCE seq_questions START 1;
        CREATE SEQUENCE seq_predictions START 1;
        CREATE SEQUENCE seq_documents START 1;
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY DEFAULT nextval('seq_questions'),
            title TEXT NOT NULL,
            opens_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            closes_at TIMESTAMP NOT NULL,
            outcome_type TEXT NOT NULL DEFAULT 'binary',
            outcome BOOLEAN,
            resolved_at TIMESTAMP,
            resolution_source TEXT,
            is_public BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY DEFAULT nextval('seq_predictions'),
            question_id INTEGER NOT NULL,
            probability DOUBLE NOT NULL,
            brier_score DOUBLE,
            evidence_ids JSON NOT NULL,
            model_runs JSON,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE source_documents (
            id INTEGER PRIMARY KEY DEFAULT nextval('seq_documents'),
            question_id INTEGER,
            source TEXT NOT NULL,
            url TEXT, title TEXT, content TEXT,
            published_at TIMESTAMP,
            fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE model_stats (
            model_name TEXT PRIMARY KEY, predictions INTEGER NOT NULL DEFAULT 0,
            brier_ema DOUBLE, last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO questions (title, opens_at, closes_at) VALUES
            ('旧题1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL 1 DAY);
        INSERT INTO predictions (question_id, probability, evidence_ids, model_runs) VALUES
            (1, 0.6, '[1]', '{"m": 0.6}');
    """)
    conn.close()
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "scripts/migrate_schema.py", "--db", str(old)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    st = Storage(str(old))
    st.create_schema()  # 幂等：迁移后再 create_schema 不报错
    cols = st._conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='predictions'"
    ).fetchall()
    assert ("arm",) in cols
    # 存量回填
    rows = st._conn.execute("SELECT arm FROM predictions").fetchall()
    assert rows == [("baseline",)]
