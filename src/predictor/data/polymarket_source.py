"""Polymarket Gamma API 数据源：活跃市场 → 分档筛选 → LLM 译中文 → 候选题。

防泄漏：只拉 closed=false 的活跃市场（未揭晓题），不碰历史题。
网络降级：单事件/单市场失败由调用方跳过，本模块只做纯拉取与纯筛选。
时区：endDate 为 UTC ISO（Z），closes_at 统一转北京时间入库（与题池一致）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
TZ_CN = timezone(timedelta(hours=8))

# 分档（天数闭区间）：短/中/长三档都铺题（用户拍板：各时段都要有）
HORIZON_TIERS: list[tuple[int, int, str]] = [(0, 14, "short"), (15, 45, "mid"), (46, 90, "long")]

# 去重归一化只通配「日期语境」数字（月+日 / 日,月年 / 纯年份），保留阈值类数字
# （$100k vs $150k、25bps vs 50bps 是不同市场，不得误杀）
_DATE_MONTHS = (
    r"(january|february|march|april|may|june|july|august|september|october"
    r"|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
)


def _norm_question(q: str) -> str:
    s = q.lower()
    s = re.sub(rf"{_DATE_MONTHS}[.]?\s+\d{{1,2}}(st|nd|rd|th)?", "#", s)  # "august 21"
    s = re.sub(r"\b\d{1,2},?\s+\d{4}\b", "#", s)  # "21, 2026"
    s = re.sub(r"\b\d{4}\b", "#", s)  # 纯年份 "2026"
    return s


@dataclass
class PMCandidate:
    market_id: str
    slug: str
    question: str  # 原文
    title: str  # 译文（翻译失败时=原文）
    closes_at: datetime  # 北京时间
    volume: float
    description: str
    url: str


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _is_closed_false(m: dict) -> bool:
    closed = m.get("closed")
    if isinstance(closed, str):
        return closed.strip().lower() in ("false", "0", "no")
    return not bool(closed)


# 体育/电竞/娱乐类市场标题硬过滤（与 autopick 的天气硬过滤同款纪律）：
# 短期赛事市场成交量碾压真事件题，不拦会占满 short 档且 B 端价值低
EXCLUDE_TITLE_KEYWORDS = (
    # 电竞
    "counter-strike", "cs2", "cs:go", "dota", "league of legends", "lol:", "valorant",
    "esports", "game 1 winner", "map 1 winner", "(bo3)", "(bo5)",
    # 体育赛事（对阵/联赛/每日赛果）
    " vs. ", " vs ", "spread:", "win on 20", "t20", "nfl", "nba", "mlb", "nhl",
    "premier league", "la liga", "serie a", "bundesliga", "ligue 1", "uefa",
    "champions league", "world cup", "super bowl", "world series", "stanley cup",
    "playoffs", "grand slam", "wimbledon", "men's us open", "women's us open",
    "atp ", "wta ", "formula 1", "f1 ", "ufc", "boxing", "olympic",
)

# /markets 分页 offset 上限（实测 offset=2100 起 422）：按成交量降序取头部足够选材
MAX_MARKET_OFFSET = 2000


def fetch_markets(
    client: httpx.Client,
    *,
    limit: int = 100,
    offset: int = 0,
    end_date_min: datetime | None = None,
    end_date_max: datetime | None = None,
) -> list[dict]:
    """活跃市场候选池（服务端按结束窗口过滤 + 成交量降序）。非 2xx 抛 httpx 异常，调用方降级。

    旧路径 `/events?order=endDate&ascending=true` 实测只会扫到 2025-11 起的**过期**事件
    （窗口内事件在 offset 2100 之外且翻页 422），导致 pm_fetch 连续多轮 0 候选——已废弃。
    """
    params: dict[str, Any] = {
        "closed": "false",
        "active": "true",
        "limit": limit,
        "offset": offset,
        "order": "volumeNum",
        "ascending": "false",
    }
    if end_date_min is not None:
        params["end_date_min"] = end_date_min.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if end_date_max is not None:
        params["end_date_max"] = end_date_max.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.get(f"{GAMMA_BASE}/markets", params=params, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def select_candidates(
    markets: list[dict],
    *,
    now: datetime,
    per_tier: int = 6,
    min_volume: float = 5000.0,
    tiers: list[tuple[int, int, str]] | None = None,
) -> list[PMCandidate]:
    """筛选「有价值 + 快速可判」市场并按 horizon 分档：档内按 volume 降序取前 per_tier。"""
    now_utc = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    tiers = tiers or HORIZON_TIERS
    picked: dict[str, list[dict]] = {name: [] for _, _, name in tiers}
    for m in markets:
        if not _is_closed_false(m):
            continue
        title_l = str(m.get("question") or "").lower()
        if any(kw in title_l for kw in EXCLUDE_TITLE_KEYWORDS):
            continue
        # 只取二值市场（outcomes 恰 2 个）：多选市场（'Who will win...'）二值预测无意义
        raw_outcomes = m.get("outcomes")
        if isinstance(raw_outcomes, str):
            try:
                outcomes = json.loads(raw_outcomes)
            except ValueError:
                outcomes = []
        else:
            outcomes = raw_outcomes or []  # 原生 list 直接采用
        if not isinstance(outcomes, list) or len(outcomes) != 2:
            continue
        end = _parse_utc(m.get("endDate") or m.get("endDateIso"))
        if end is None:
            continue
        days = (end - now_utc).total_seconds() / 86400.0
        tier_name = None
        for lo, hi, name in tiers:
            if lo <= days <= hi:
                tier_name = name
                break
        if tier_name is None:
            continue
        try:
            volume = float(m.get("volume") or 0.0)
        except (TypeError, ValueError):
            volume = 0.0
        if volume < min_volume:
            continue
        picked[tier_name].append(m)
    out: list[PMCandidate] = []
    # 同事件多市场（F1 冠军 20 个车手、Fed 决议 5 个档位）只留成交量最大的一个：
    # 同事件题高度相关，全入池会互相挤占 per_tier 名额、污染 Brier
    event_best: dict[str, dict] = {}
    for m in (mm for row in picked.values() for mm in row):
        eid = str((m.get("events") or [{}])[0].get("id") or m.get("_event_id") or m.get("id") or "")
        if float(m.get("volume") or 0.0) > float(event_best.get(eid, {}).get("volume") or -1.0):
            event_best[eid] = m
    keep_ids = {id(m) for m in event_best.values()}
    for name in picked:
        picked[name] = [m for m in picked[name] if id(m) in keep_ids]
    # 全局去重（跨档）：归一化题面（只通配日期语境数字）相同只保留最早结束的——
    # 防 "GPT-6 by Aug 21"（短档）与 "GPT-6 by Aug 31"（中档）这类跨档同主题题互相污染 Brier
    dedup_global: dict[str, dict] = {}
    for m in sorted(
        (mm for row in picked.values() for mm in row),
        key=lambda m: _parse_utc(m.get("endDate") or m.get("endDateIso")) or datetime.max,
    ):
        key = _norm_question(m.get("question") or "")
        if key in dedup_global:
            continue
        dedup_global[key] = m
        m["_norm_key"] = key  # 归位标记，供档内重建
    for _, _, name in tiers:
        # 档内按 volume 降序取前 per_tier（只取未在去重中被淘汰的）
        picked[name] = [m for m in picked[name] if m.get("_norm_key") in dedup_global]
        picked[name] = sorted(
            picked[name], key=lambda m: float(m.get("volume") or 0.0), reverse=True
        )[:per_tier]
        for m in picked[name]:
            qid = str(m.get("id") or "")
            slug = str(m.get("slug") or "")
            question = str(m.get("question") or "").strip()
            if not qid or not question:
                continue
            out.append(
                PMCandidate(
                    market_id=qid,
                    slug=slug,
                    question=question,
                    title=question,  # 译文由 translate 覆盖
                    # naive 北京时间：与题池口径一致（expand_question_pool "09:00 北京"），
                    # DuckDB TIMESTAMP 无 tz 转换，aware 值入库会被直接剥离 tzinfo
                    closes_at=_parse_utc(m.get("endDate") or m.get("endDateIso"))
                    .astimezone(TZ_CN)
                    .replace(tzinfo=None),
                    volume=float(m.get("volume") or 0.0),
                    description=str(m.get("description") or "")[:2000],
                    url=f"https://polymarket.com/market/{slug}" if slug else "",
                )
            )
    return out


def translate_title(llm_client: Any, question: str) -> str:
    """LLM 译中文；失败（异常/格式非法）返回原文，不阻塞拉题轮。"""
    try:
        out = llm_client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "把预测市场问题翻译成简洁中文，保留关键主体/数字/日期，"
                        '输出 JSON：{"title": "译文"}'
                    ),
                },
                {"role": "user", "content": question},
            ]
        )
        title = str(out.get("title", "")).strip()
        return title or question
    except Exception:
        return question
