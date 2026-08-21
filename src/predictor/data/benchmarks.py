"""ForecastBench 题集加载器（2026-08-11 按官方数据实测适配）。

真实数据源：forecastingresearch/forecastbench-datasets（GitHub 公开 JSON，CC-BY-SA-4.0）。
官方仓库结构（已实测确认）：
  datasets/question_sets/YYYY-MM-DD-llm.json  每两周一期，含 latest-llm.json 指针
  raw 结构：{"forecast_due_date": "2025-06-08", "question_set": "...", "questions": [...]}
  每题字段：id, source, question, resolution_criteria, background,
            market_info_open_datetime, market_info_close_datetime, url, freeze_datetime, ...
  注意：题内无 resolution（答案）字段——已揭晓结果需外部答案源（manifold API 等）补齐；
        2025-10-26 前的旧题集含 combination questions（id 为数组），解析时丢弃。

网络降级：本机对 raw.githubusercontent.com 直连被掐，模块支持本地 seed
（data/fb_seed/*.json，已从官方仓库整包落盘 36 期）——拉取失败自动读本地。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from predictor.llm.client import LLMError

BENCH_RAW_URL = (
    "https://raw.githubusercontent.com/forecastingresearch/"
    "forecastbench-datasets/main/datasets/question_sets/latest-llm.json"
)
BENCH_SEED_DIR = Path(__file__).resolve().parents[3] / "data" / "fb_seed"


@dataclass
class BenchQuestion:
    id: str
    title: str
    closes_at: datetime
    resolved: bool
    outcome: bool | None
    category: str
    url: str = ""
    source: str = ""


def _parse_item(item: dict) -> BenchQuestion | None:
    title = item.get("question") or item.get("title")
    if not title:
        return None
    qid = item.get("id")
    if isinstance(qid, list) or qid is None:
        return None  # combination questions（id 为数组）无法单题判定，丢弃
    # 题目自带 resolution 时解析；官方题集无此字段 → outcome=None（答案待外部源补）
    outcome = _parse_outcome(item.get("resolution"))
    date_s = (
        item.get("freeze_datetime")
        or item.get("market_info_close_datetime")
        or item.get("date")
        or item.get("closes_at")
    )
    try:
        closes_at = datetime.fromisoformat(str(date_s))
    except (ValueError, TypeError):
        closes_at = datetime.now(UTC)
    if closes_at.tzinfo is None:
        closes_at = closes_at.replace(tzinfo=UTC)
    return BenchQuestion(
        id=str(qid),
        title=title,
        closes_at=closes_at,
        resolved=outcome is not None,
        outcome=outcome,
        category=item.get("category") or item.get("source") or "unknown",
        url=str(item.get("url") or ""),
        source=str(item.get("source") or ""),
    )


def _parse_outcome(resolution: str | None) -> bool | None:
    if resolution is None:
        return None
    up = resolution.upper()
    if up in ("YES", "TRUE", "1", "YES "):
        return True
    if up in ("NO", "FALSE", "0"):
        return False
    return None  # AMBIGUOUS 等 → 无法客观判定


def _load_local_seed(limit: int) -> list[BenchQuestion]:
    """本地 seed 降级：data/fb_seed/ 里取最新的题集文件（latest 优先）。"""
    if not BENCH_SEED_DIR.exists():
        return []
    candidates = sorted(BENCH_SEED_DIR.glob("*.json"), key=lambda p: p.name, reverse=True)
    for path in candidates:
        try:
            raw = __import__("json").loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = raw if isinstance(raw, list) else raw.get("questions", [])
        out = []
        for item in items:
            q = _parse_item(item)
            if q is not None:
                out.append(q)
            if len(out) >= limit:
                return out
        if out:
            return out
    return []


def fetch_forecastbench_questions(
    limit: int = 200, *, timeout: float = 30.0, _transport=None
) -> list[BenchQuestion]:
    """拉取 ForecastBench 题集（最新一期）。网络失败自动降级本地 seed。"""
    raw = None
    try:
        with httpx.Client(transport=_transport, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(BENCH_RAW_URL)
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError):
        raw = None  # 网络不可用 → 降级本地
    if raw is None:
        local = _load_local_seed(limit)
        if local:
            return local
        raise LLMError(f"benchmark 数据集拉取失败（网络 + 本地 seed 均不可用）: {BENCH_RAW_URL}")
    items = raw if isinstance(raw, list) else raw.get("questions", [])
    out = []
    for item in items:
        q = _parse_item(item)
        if q is not None:
            out.append(q)
        if len(out) >= limit:
            break
    return out
