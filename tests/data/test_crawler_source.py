"""Task 23：CrawlerSource（MediaCrawler 中文社交数据源）与 crawl_social.py 测试。"""

import importlib.util
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from predictor.data.crawler_source import CrawlerSource, _parse_create_time

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_crawl_social():
    """scripts/ 非包目录，用 importlib 加载（同 test_resolve_cli.py 的做法）。"""
    spec = importlib.util.spec_from_file_location(
        "crawl_social_mod", _PROJECT_ROOT / "scripts" / "crawl_social.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(tmp_path: Path, name: str, payload, mtime: float | None = None) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _rec(
    title="美联储 9月议息",
    desc="美联储主席讨论加息路径",
    create_time=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    nickname="测试用户甲",
    user_id="88888888",
    url="https://weibo.com/note/1",
    extra: dict | None = None,
):
    r = {
        "title": title,
        "desc": desc,
        "create_time": create_time,
        "nickname": nickname,
        "user_id": user_id,
        "note_url": url,
    }
    if extra:
        r.update(extra)
    return r


# ---------- CrawlerSource ----------


def test_keyword_filter_and_document_fields(tmp_path):
    _write(
        tmp_path,
        "a.json",
        [
            _rec(title="美联储 9月议息", desc="美联储主席讨论加息", url="https://weibo.com/note/1"),
            _rec(title="央行操作", desc="降息预期升温，美联储表态", url="https://weibo.com/note/2"),
            _rec(title="苹果发布会", desc="新品发布", url="https://weibo.com/note/3"),
        ],
    )
    src = CrawlerSource(tmp_path)
    docs = src.fetch("美联储")
    assert len(docs) == 2
    d = docs[0]
    assert d.source == "crawler"
    assert d.title == "美联储 9月议息"
    assert "美联储主席讨论加息" in d.content  # title+desc 进 content
    assert d.url == "https://weibo.com/note/1"
    assert d.published_at is not None
    assert d.fetched_at is not None
    # 与 retrieve.py 同口径：published_at 为 naive datetime
    assert d.published_at.tzinfo is None


def test_nested_wrapper_and_single_record_parsed(tmp_path):
    _write(tmp_path, "wrapped.json", {"data": [_rec(url="https://weibo.com/note/w1")]})
    _write(tmp_path, "single.json", _rec(url="https://weibo.com/note/s1"))
    docs = CrawlerSource(tmp_path).fetch("美联储")
    assert {d.url for d in docs} == {"https://weibo.com/note/w1", "https://weibo.com/note/s1"}


def test_jsonl_and_xhs_time_field(tmp_path):
    """MediaCrawler 默认导出 .jsonl（逐行 JSON）；xhs 笔记发布时间字段为 time（ms 时间戳）。"""
    p = tmp_path / "search_note_20260811.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "title": "美联储 9月议息",
                        "desc": "xhs 笔记",
                        "time": int(time.time() * 1000),
                        "nickname": "测试用户乙",
                        "user_id": "999",
                        "note_url": "https://www.xiaohongshu.com/explore/a1",
                    }
                ),
                json.dumps(
                    {
                        "title": "无关话题",
                        "desc": "不匹配",
                        "time": int(time.time() * 1000),
                        "note_url": "https://www.xiaohongshu.com/explore/a2",
                    }
                ),
                "not json at all",
            ]
        ),
        encoding="utf-8",
    )
    docs = CrawlerSource(tmp_path).fetch("美联储")
    assert len(docs) == 1
    d = docs[0]
    assert d.url == "https://www.xiaohongshu.com/explore/a1"
    assert "测试用户乙" not in f"{d.title} {d.content}"
    assert d.published_at is not None and d.published_at.tzinfo is None


def test_future_timestamp_rejected(tmp_path):
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    _write(
        tmp_path,
        "f.json",
        [
            _rec(create_time=future, url="https://weibo.com/note/future"),
            _rec(
                create_time=(datetime.now() + timedelta(days=30)).timestamp() * 1000,  # ms epoch
                url="https://weibo.com/note/future2",
            ),
        ],
    )
    assert CrawlerSource(tmp_path).fetch("美联储") == []


def test_missing_or_garbage_timestamp_rejected(tmp_path):
    no_key = _rec(url="https://weibo.com/note/missing")
    del no_key["create_time"]  # 真缺字段（默认参数会带上 create_time，需删掉）
    _write(
        tmp_path,
        "g.json",
        [
            _rec(extra={"create_time": None}, url="https://weibo.com/note/none"),
            _rec(extra={"create_time": "不是时间"}, url="https://weibo.com/note/garbage"),
            no_key,
        ],
    )
    assert CrawlerSource(tmp_path).fetch("美联储") == []


def test_nickname_user_id_not_stored(tmp_path):
    _write(
        tmp_path,
        "n.json",
        [
            _rec(
                title="美联储 9月议息",
                desc="@测试用户甲 转发：美联储主席谈加息，详情 https://example.com/long/tail 全文见链接",
                nickname="测试用户甲",
                user_id="88888888",
                extra={"avatar": "https://example.com/avatar.jpg", "uid": "abc123"},
            )
        ],
    )
    docs = CrawlerSource(tmp_path).fetch("美联储")
    assert len(docs) == 1
    d = docs[0]
    blob = f"{d.title} {d.content} {d.url}"
    assert "测试用户甲" not in blob  # 昵称不落库
    assert "88888888" not in blob  # 用户 ID 不落库
    assert "abc123" not in blob  # 其他 ID 字段也不落库
    assert "https://example.com" not in d.content  # 链接尾巴被清洗（url 字段本身保留）
    assert len(d.content) <= 2000


def test_old_files_skipped(tmp_path):
    """只扫最近 24h 落盘的 JSON；48h 前的文件不读。"""
    _write(
        tmp_path,
        "old.json",
        [_rec(url="https://weibo.com/note/old")],
        mtime=time.time() - 48 * 3600,
    )
    _write(tmp_path, "new.json", [_rec(url="https://weibo.com/note/new")], mtime=time.time() - 3600)
    docs = CrawlerSource(tmp_path).fetch("美联储")
    assert [d.url for d in docs] == ["https://weibo.com/note/new"]


def test_missing_dir_and_empty_keyword(tmp_path):
    assert CrawlerSource(tmp_path / "nope").fetch("美联储") == []
    _write(tmp_path, "a.json", [_rec()])
    assert CrawlerSource(tmp_path).fetch("") == []


def test_parse_create_time_variants():
    assert _parse_create_time(None) is None
    assert _parse_create_time("") is None
    assert _parse_create_time("不是时间") is None
    # xhs 毫秒时间戳 / 秒时间戳 → UTC naive
    dt = _parse_create_time(1714538000000)
    assert dt is not None and dt.tzinfo is None
    assert _parse_create_time(1714538000) == dt
    # 北京时间字符串 → UTC（北京 08:00 = UTC 00:00）
    assert _parse_create_time("2026-08-10 08:00:00") == datetime(2026, 8, 10, 0, 0, 0)
    assert _parse_create_time("2026-08-10T08:00:00") == datetime(2026, 8, 10, 0, 0, 0)
    assert _parse_create_time("2026-08-10") == datetime(2026, 8, 9, 16, 0, 0)


# ---------- scripts/crawl_social.py ----------


def _deploy_fake_mediacrawler(tmp_path: Path) -> Path:
    """伪造 data/mediacrawler 部署（含 main.py），返回 MC_DIR。"""
    mc = tmp_path / "mediacrawler"
    (mc / ".venv" / "Scripts").mkdir(parents=True)
    (mc / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (mc / "main.py").write_text("", encoding="utf-8")
    return mc


def test_crawl_social_graceful_when_not_deployed(tmp_path, monkeypatch, capsys):
    cs = _load_crawl_social()
    monkeypatch.setattr(cs, "MC_DIR", tmp_path / "nope")  # 未部署
    assert cs.main(["--keywords", "美联储"]) == 0  # 优雅退出，不崩溃
    err = capsys.readouterr().err
    assert "MediaCrawler 未部署" in err


def test_crawl_social_invokes_cli_and_merges_output(tmp_path, monkeypatch):
    cs = _load_crawl_social()
    mc = _deploy_fake_mediacrawler(tmp_path)
    monkeypatch.setattr(cs, "MC_DIR", mc)
    monkeypatch.setattr(cs, "OUT_DIR", tmp_path / "crawler")

    called: list[list[str]] = []

    def fake_run(cmd, cwd=None, **kw):
        called.append(cmd)
        # 模拟 MediaCrawler 把结果写到 save_data_path 下嵌套目录（wb/json/...）
        out = tmp_path / "crawler" / "wb" / "json" / "search_note_20260811.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                [_rec(url="https://weibo.com/note/1"), _rec(url="https://weibo.com/note/2")],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cs.main(["--keywords", "美联储", "--platforms", "weibo", "--limit", "10"]) == 0

    assert len(called) == 1
    cmd = called[0]
    assert cmd[0].endswith("python.exe") and "main.py" in cmd
    assert cmd[cmd.index("--platform") + 1] == "wb"  # 用户平台名映射为 CLI 枚举
    assert cmd[cmd.index("--type") + 1] == "search"
    assert cmd[cmd.index("--keywords") + 1] == "美联储"
    assert cmd[cmd.index("--crawler_max_notes_count") + 1] == "10"
    assert cmd[cmd.index("--save_data_path") + 1] == str(tmp_path / "crawler")
    assert cmd[cmd.index("--save_data_option") + 1] == "json"
    assert cmd[cmd.index("--get_comment") + 1] == "no"

    merged = list((tmp_path / "crawler").glob("mediacrawler_*.json"))
    assert len(merged) == 1
    payload = json.loads(merged[0].read_text(encoding="utf-8"))
    assert payload["count"] == 2 and len(payload["items"]) == 2
    assert payload["keywords"] == ["美联储"] and payload["platforms"] == ["weibo"]
    assert "fetched_at" in payload


def test_crawl_social_unknown_platform_rejected(tmp_path, monkeypatch, capsys):
    cs = _load_crawl_social()
    mc = _deploy_fake_mediacrawler(tmp_path)
    monkeypatch.setattr(cs, "MC_DIR", mc)
    monkeypatch.setattr(cs, "OUT_DIR", tmp_path / "crawler")
    assert cs.main(["--keywords", "美联储", "--platforms", "notaplatform"]) == 1
    assert "未知平台" in capsys.readouterr().err


def test_crawl_social_limit_clamped_to_20(tmp_path, monkeypatch):
    cs = _load_crawl_social()
    mc = _deploy_fake_mediacrawler(tmp_path)
    monkeypatch.setattr(cs, "MC_DIR", mc)
    monkeypatch.setattr(cs, "OUT_DIR", tmp_path / "crawler")
    called: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, cwd=None, **kw: (
            called.append(cmd),
            subprocess.CompletedProcess(cmd, 0, "", ""),
        )[1],
    )
    assert cs.main(["--keywords", "美联储", "--limit", "999"]) == 0
    assert called[0][called[0].index("--crawler_max_notes_count") + 1] == "20"


def test_crawl_social_12h_frequency_guard(tmp_path, monkeypatch):
    """同 平台+关键词 12h 内已抓取 → 拒绝再抓（每天 ≤2 次护栏）。"""
    cs = _load_crawl_social()
    mc = _deploy_fake_mediacrawler(tmp_path)
    out = tmp_path / "crawler"
    out.mkdir()
    payload = {
        "fetched_at": (datetime.now() - timedelta(hours=1)).isoformat(),
        "platforms": ["weibo"],
        "keywords": ["美联储"],
        "items": [],
    }
    (out / "mediacrawler_20260811_000000.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(cs, "MC_DIR", mc)
    monkeypatch.setattr(cs, "OUT_DIR", out)

    def boom(*a, **kw):
        raise AssertionError("频率护栏应拦截，不应再调 MediaCrawler")

    monkeypatch.setattr(subprocess, "run", boom)
    assert cs.main(["--keywords", "美联储", "--platforms", "weibo"]) == 0
    assert len(list(out.glob("mediacrawler_*.json"))) == 1  # 未写新合并文件
