"""CLI 非交互路径与存储行为的单元测试。

不需要网络：main 的 --publish 路径不碰 LLM/网络；无 title 且无 --publish 直接报错退出。
"""

from datetime import datetime, timedelta

import pytest

from predictor.data.storage import Storage


class TestPredictOnceGuards:
    def test_past_closes_rejected_without_creating_question(self):
        """对抗测试：过去日期建题被拒绝，不落库（防过期题污染未揭晓列表）。"""
        from predictor.cli import predict_once
        from predictor.config import Settings

        st = Storage(":memory:")
        st.create_schema()
        r = predict_once(
            "昨天上证涨没涨", datetime.now() - timedelta(days=1), "classic", False, st, Settings()
        )
        assert r["ok"] is False
        assert "过去" in r["reason"]
        assert st._conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
        st._conn.close()


class TestMainNoTitleNonTty:
    def test_no_title_without_publish_errors(self, monkeypatch, capsys):
        """无 title 且无 --publish → argparse usage 错误，退出码 2（JSON 契约不受影响）。"""
        import predictor.cli as cli

        monkeypatch.setattr("sys.argv", ["foresight"])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 2
        assert "需要提供预测问题文本，或用 --publish <id> 转公开" in capsys.readouterr().err


class TestDefaultDbFallback:
    def test_running_outside_project_uses_project_db(self, monkeypatch, tmp_path, capsys):
        """任意目录启动：默认 db 相对路径在 cwd 下不存在 → 回落项目根库，不在 cwd 建新库。"""
        import predictor.cli as cli

        monkeypatch.setattr("sys.argv", ["foresight", "--publish", "99999"])
        monkeypatch.chdir(tmp_path)
        cli.main()
        out = capsys.readouterr().out
        assert '"ok": false' in out  # 打开了库并执行了查询
        assert not (tmp_path / "data").exists()  # 没有在 cwd 下建新库


class TestStorageAutoMkdir:
    def test_storage_creates_missing_parent_dirs(self, tmp_path):
        db = tmp_path / "deep" / "nested" / "t.db"
        st = Storage(str(db))
        st.create_schema()
        st.add_question("深层目录测试", datetime(2026, 9, 17))
        st._conn.close()
        assert db.exists()
