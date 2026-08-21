"""predict_cli 的轻量冒烟：无证据源时必须拒绝出预测而非崩溃。

（完整 JSON 结构由真实运行验证，见实施计划 Task 17 Step 5。）
不需要网络：LLM 用 stub client，publish 路径不碰 LLM/网络。
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from predictor.data.storage import Storage
from predictor.pipeline import run_prediction

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "predict_cli.py"


class FakeClient:
    """与 tests/test_pipeline.py 同款 stub：不出网，返回固定搜索词。"""

    def chat_json(self, messages, **kw):
        return {"terms": ["term"]}

    def chat(self, messages, **kw):
        return '{"summary": "s"}'


def test_no_evidence_rejects_prediction():
    st = Storage(":memory:")
    st.create_schema()
    qid = st.add_question("美联储9月会加息吗", datetime(2026, 9, 17))
    client = FakeClient()
    pred = run_prediction(qid, st, client, [], now=None)  # 无数据源
    assert pred is None  # 无证据 → 拒绝出预测，不崩溃


def test_cli_publish_draft_roundtrip(tmp_path):
    """--publish <id> 把草稿转公开；不存在的 id 返回 ok=false。"""
    db = tmp_path / "t.db"
    st = Storage(str(db))
    st.create_schema()
    qid = st.add_question("草稿题", datetime(2026, 9, 17), is_public=False)
    assert not st.get_question(qid).is_public
    st._conn.close()  # Windows 文件锁：subprocess 打开同一 DB 前先释放

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run(
        [sys.executable, str(CLI), "--publish", str(qid), "--db", str(db)],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out == {"ok": True, "published": qid, "reason": None}
    st2 = Storage(str(db))
    st2.create_schema()
    assert st2.get_question(qid).is_public  # 已转公开
    st2._conn.close()

    r2 = subprocess.run(
        [sys.executable, str(CLI), "--publish", "99999", "--db", str(db)],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=60,
    )
    out2 = json.loads(r2.stdout)
    assert out2["ok"] is False
