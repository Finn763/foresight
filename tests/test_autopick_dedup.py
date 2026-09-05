"""autopick 判重/建题/校验纯函数单测（不触网、不写盘、不连生产库）。"""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import autopick  # noqa: E402


def _write_reg(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def test_load_registry_indexes(tmp_path):
    reg_path = tmp_path / "registry.jsonl"
    _write_reg(reg_path, [
        {"event_key": "fed-cut", "title": "美联储会在 9 月降息吗", "news_url": "https://x/1"},
    ])
    reg_path.open("a", encoding="utf-8").write("not-json\n")
    reg = autopick.load_registry(reg_path)
    assert list(reg["by_key"]) == ["fed-cut"]
    assert "美联储会在9月降息吗" in reg["by_title"]  # norm_title 去空格标点
    assert reg["by_url"]["https://x/1"]["event_key"] == "fed-cut"
    assert len(reg["rows"]) == 1


def test_is_duplicate_key_title_url(tmp_path):
    reg_path = tmp_path / "registry.jsonl"
    _write_reg(reg_path, [
        {"event_key": "fed-cut", "title": "美联储会降息吗", "news_url": "https://x/a"},
    ])
    reg = autopick.load_registry(reg_path)
    assert autopick.is_duplicate(reg, "另一道题", "fed-cut", tmp_path)
    assert autopick.is_duplicate(reg, "美联储会降息吗", "other-key", tmp_path)
    assert autopick.is_duplicate(reg, "另一道题", "other-key", tmp_path, news_url="https://x/a")
    assert not autopick.is_duplicate(reg, "完全无关的题", "other-key", tmp_path)


def test_is_duplicate_similarity_and_slug_file(tmp_path):
    reg_path = tmp_path / "registry.jsonl"
    _write_reg(reg_path, [
        {"event_key": "old", "title": "美联储会在九月宣布降息吗", "news_url": "https://x/0"},
    ])
    reg = autopick.load_registry(reg_path)
    (tmp_path / "2026-09-04-nvidia-earnings.json").write_text("{}", encoding="utf-8")
    assert autopick.is_duplicate(reg, "美联储会在九月宣布降息么", "k2", tmp_path)
    assert autopick.is_duplicate(reg, "别的题", "nvidia-earnings", tmp_path)
    assert not autopick.is_duplicate(reg, "英国大选将于七月举行", "k3", tmp_path)


def test_build_question_shape():
    now = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    cand = {
        "title": "美联储会在 2026 年 9 月 FOMC 会议宣布降息吗",
        "event_key": "fed-sep-cut",
        "category": "macro",
        "closes_at": "2026-09-18T21:00:00+00:00",
        "resolution_criteria": "FOMC 声明显示降息",
        "primary_source": "federalreserve.gov",
        "evidence_urls": ["https://x/1"],
        "probability": 0.6,
        "probability_reason": "通胀回落",
    }
    item = {"url": "https://x/1", "title": "美联储将开会议息", "pubtime": "2026-09-04 08:00 UTC"}
    q = autopick.build_question(cand, item, now)
    assert q["title"] == cand["title"]
    assert q["closes_at"] == cand["closes_at"]
    assert q["outcome"] is None and q["resolution_source"] is None
    assert q["resolution_spec"]["class"] == "B"
    assert q["resolution_spec"]["source"] == "autopick"
    assert q["resolution_spec"]["news_url"] == item["url"]
    assert q["proposed_insert"]["kwargs"]["closes_at"] == cand["closes_at"]


def test_validate_candidate():
    now = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    item = {"url": "https://x/1"}
    good = {
        "title": "t",
        "resolution_criteria": "c",
        "primary_source": "s",
        "closes_at": (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "probability": 0.6,
        "evidence_urls": ["https://x/1", "https://fake/2"],
    }
    assert autopick.validate_candidate(good, item, now) == []
    assert good["evidence_urls"] == ["https://x/1"]  # 编造 URL 被清洗
    bad = {"title": "", "closes_at": "junk"}
    errs = autopick.validate_candidate(bad, item, now)
    assert any("title" in e for e in errs)
    assert any("closes_at" in e for e in errs)
    bad2 = {**good, "title": "", "probability": 1.5}
    errs2 = autopick.validate_candidate(bad2, item, now)
    assert any("probability" in e for e in errs2)
