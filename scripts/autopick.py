#!/usr/bin/env python3
"""Foresight 自主选题引擎原型（autopick）。

WSL 侧独立运行：只读参考 Windows 项目（schema/style），只写输出文件，
绝不连接/写入 data/foresight.db，绝不修改项目代码与 .venv。

流程
----
1. 扫新闻（RSS 聚合去重，48h 窗口）
2. 硬规则初筛（天气/气温/气候事件按标题关键词剔除）
3. LLM 初筛打分（可证伪 / 揭晓时间 <90 天 / 公众关注度 / B 端价值）
4. LLM 出题（1-2 道，questions 表 schema + 可查证 resolution_spec）
5. 落盘（只写文件，幂等）：
   data/autopick/<date>-<slug>.json      新题 JSON
   data/autopick/candidates-<date>.json  候选清单（含 LLM 打分）
   data/autopick/registry.jsonl          建题注册表（判重依据）
   data/daily-brief.md                   当日简报（选题、理由、预测概率）

运行
----
cd /root/foresight-autopick
.venv/bin/python autopick.py --dry-run        # 试跑，不落盘
.venv/bin/python autopick.py                  # 正式跑
.venv/bin/python autopick.py --max-picks 1    # 只选 1 题
"""

import argparse
import email.utils
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx

# ---------------------------------------------------------------- 配置常量

# 配置常量（并入项目后：默认路径跟随脚本位置，Windows/WSL 通用）
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent if _SCRIPT_DIR.name == "scripts" else _SCRIPT_DIR
DEFAULT_ENV_FILE = str(_PROJECT_ROOT / ".env")
DEFAULT_OUT_DIR = str(_PROJECT_ROOT / "data")

# 新闻源：Google News RSS 多主题 + 主流媒体公开 RSS（WSL 实测 2026-08-27 全部可达）
NEWS_SOURCES = {
    # Google News RSS：通用面
    "google_world": "https://news.google.com/rss/search?q=world&hl=en-US&gl=US&ceid=US:en",
    "google_business": "https://news.google.com/rss/search?q=business&hl=en-US&gl=US&ceid=US:en",
    # Google News RSS：事件密度高的定向 query（并购/监管/央行/选举/IPO/财报/法律裁决）
    "google_fed": "https://news.google.com/rss/search?q=Fed+rate+decision&hl=en-US&gl=US&ceid=US:en",
    "google_centralbank": "https://news.google.com/rss/search?q=central+bank+interest+rate&hl=en-US&gl=US&ceid=US:en",
    "google_merger": "https://news.google.com/rss/search?q=merger+OR+acquisition+deal&hl=en-US&gl=US&ceid=US:en",
    "google_antitrust": "https://news.google.com/rss/search?q=antitrust+ruling&hl=en-US&gl=US&ceid=US:en",
    "google_ipo": "https://news.google.com/rss/search?q=IPO+filing&hl=en-US&gl=US&ceid=US:en",
    "google_earnings": "https://news.google.com/rss/search?q=earnings+report+guidance&hl=en-US&gl=US&ceid=US:en",
    "google_election": "https://news.google.com/rss/search?q=election+vote&hl=en-US&gl=US&ceid=US:en",
    "google_launch": "https://news.google.com/rss/search?q=rocket+launch+OR+chip+launch&hl=en-US&gl=US&ceid=US:en",
    "google_upcoming": "https://news.google.com/rss/search?q=set+to+announce+OR+expected+to+decide&hl=en-US&gl=US&ceid=US:en",
    "google_deadline": "https://news.google.com/rss/search?q=deadline+OR+due+to+vote&hl=en-US&gl=US&ceid=US:en",
    "google_tech": "https://news.google.com/rss/search?q=technology&hl=en-US&gl=US&ceid=US:en",
    # 中文源（B 端中文客户视角）
    "google_zh_headline": "https://news.google.com/rss/search?q=%E8%A6%81%E9%97%BB&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    "google_zh_biz": "https://news.google.com/rss/search?q=%E8%B4%A2%E7%BB%8F&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    # 主流媒体公开 RSS
    "bbc_world": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "bbc_business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "npr_top": "https://feeds.npr.org/1001/rss.xml",
    "guardian_world": "https://www.theguardian.com/world/rss",
    "guardian_business": "https://www.theguardian.com/business/rss",
}

# 硬规则（用户拍板 2026-08-13）：气温/气候/天气类事件一律禁出。
# 标题命中即剔除（强信号）；描述命中留给 LLM 按语义裁决（气候政策类政策节点不算天气）。
WEATHER_TITLE_KEYWORDS = [
    "气温", "天气", "气候", "台风", "飓风", "高温", "低温", "暴雨", "暴雪",
    "洪水", "干旱", "降雨", "降雪", "寒潮", "热浪", "龙卷风", "沙尘暴",
    "heatwave", "heat wave", "hurricane", "typhoon", "cyclone", "tornado",
    "blizzard", "snowstorm", "monsoon", "rainfall", "temperature", "forecast",
    "flood", "drought", "wildfire", "bushfire", "storm", "landslide", "avalanche",
]

MAX_ITEMS = 60          # 进入 LLM 初筛的候选上限
WINDOW_HOURS = 48       # 只收 48h 内的新闻
STAGE1_CHUNK = 20       # LLM 初筛每批条数
MIN_SCORE = 7.0         # 入选最低分（0-10）
MAX_PICKS = 2           # 每日最多建题数
MAX_HORIZON_DAYS = 90   # 揭晓时间上限
GRACE_DAYS = 3

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) ForesightAutopick/0.1"

STAGE1_PROMPT = """你是预测市场选题编辑。今天是 {today}。下面给出新闻候选（JSON 数组，每项含 key/title/source/pubtime/summary/url）。

任务：逐项评估是否值得做成一道「二元预测题」（binary，Yes/No）。
【reject 只有这几种情况】
1. 气温/气候/天气类事件（台风、飓风、高温、降雨、山火、温度预测等）。
   气候政策类（碳关税、COP 决议等具体可查证政策节点）不算天气类，可正常评估。
2. 纯评论/分析/科普/榜单/教程类软文（没有事件）。
3. 泛市场涨跌题（"某指数/个股明天涨还是跌"）——系统已有固定题族，一律 reject。
4. 本地市政小事、无公众关注度的琐碎新闻。

【falsifiable 判定】
- 事件含具体节点（投票、裁决、决议会议、发布日期、发射窗口、截止日等）→ true；
  谈判/战争/争端等持续过程，仅当存在明确近期节点（deadline/会议/裁决日）才 true。
- 过去时新闻但有自然的前向节点（如下次会议/下一轮裁决/原定日程）→ true。
- 事件发生前有官方日程（Jackson Hole、FOMC 日历、发射窗口、财报日等）→ true。
- 确无任何可预期节点的纯回顾报道 → false。
【horizon_days】事件自然揭晓日距今天的天数（1-90）；
文本无日期但有自然后续节点时，按官方日程保守估计填（≤90），在 reason 里注明依据；
确无后续节点才填 null。
【public_interest】1-5：主流媒体覆盖面与公众关心度。
【b2b_value】1-5：对投资/供应链/政策/科技决策的参考价值（宏观数据、利率决议、
监管裁决、并购、大公司关键节点、科技里程碑、地缘经济等）；
体育转会/娱乐八卦给 ≤2。
【reason】一句话说清（含关键节点/日期）。

输出 JSON：{{"items":[{{"key":"...","falsifiable":true,"horizon_days":30,
"public_interest":4,"b2b_value":4,"reject":false,"reason":"..."}}]}}
key 必须与输入完全一致，逐项返回，不得漏项。"""

STAGE2_PROMPT = """你是预测市场出题编辑。今天是 {today}。基于下面这条新闻，为 Foresight 引擎出一道中文二元预测题。

【新闻】标题: {news_title}
来源: {news_source}  发布时间: {news_pubtime}
摘要: {news_summary}
URL: {news_url}

要求：
1. title：中文题面（专有名词保留英文），必须包含「具体主体 + 可证伪主张 + 明确时间窗口」，
   截止日期具体到日且 ≤90 天。参考风格：「美联储会在 2026 年 9 月 FOMC 会议宣布降息 25 个基点吗」。
   时间窗口只能取自新闻文本；文本未给具体日期时，采用最近的官方日程或保守截止日，
   并在 probability_reason 里注明推定依据。
   禁止出泛市场涨跌题（"某指数/个股明天涨还是跌"）——必须绑定新闻里的具体事件节点
   （投票/裁决/会议/发布/发射/截止日等）。
2. closes_at：ISO 8601 UTC 时间，必须取整点或整半点（如 2026-09-30T21:00:00+00:00，
   不得出现 "08:43:37" 这类运行时刻秒数），取事件实际发生/宣布时刻后 24 小时内，
   不得晚于 {max_close}。
3. title 与 resolution_criteria 里的时间窗口一律用自然语言日期
   （如「2026 年 11 月 24 日前」「2026-11-24 23:59 UTC（含）」），
   禁止把 ISO 时间戳写进 title。
4. resolution_criteria：写清"什么事实出现即揭晓为 Yes"，必须具体可查证：
   含可核验的数值/名称/官宣渠道（机构名+官网域名，如 federalreserve.gov）。
   禁止含糊表述（如"有明显进展""大幅上升"）。
5. primary_source：官方宣布渠道（机构官网域名；不确定具体页面就写官网首页域名，
   注明"以官方发布为准"）。
6. evidence_urls：只能从我提供的新闻 URL 里选，禁止编造 URL。
7. probability：0.05-0.95 的先验概率估计（今天视角），配合一句话理由。
8. event_key：英文小写 slug（如 nvidia-q2-earnings），供判重。

输出 JSON：
{{"title":"...","event_key":"...","category":"...","closes_at":"...",
"resolution_criteria":"...","primary_source":"...","evidence_urls":["..."],
"probability":0.6,"probability_reason":"...","b2b_value_note":"..."}}"""


# ---------------------------------------------------------------- 工具函数

def parse_env_file(path: Path) -> dict:
    """极简 .env 解析（KEY=VALUE），不依赖第三方库。"""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def norm_title(t: str) -> str:
    """标题归一化（去媒体后缀、去标点空白、小写）用于去重。"""
    t = re.sub(r"\s*[-|–—]\s*[^|–—-]{2,40}$", "", t)  # " - The Guardian"
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t.lower())
    return t


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or hashlib.sha1(s.encode()).hexdigest()[:12]


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&amp;", "&", s)
    s = re.sub(r"&lt;", "<", s)
    s = re.sub(r"&gt;", ">", s)
    s = re.sub(r"&quot;", '"', s)
    s = re.sub(r"&#39;|&apos;", "'", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_pubdate(raw: str, fallback: datetime) -> datetime:
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except Exception:
            return fallback


def fetch_news(http: httpx.Client, now: datetime) -> tuple[list[dict], dict]:
    """聚合各 RSS 源 → 去重 → 48h 窗口。返回 (items, 源统计)。"""
    items: dict[str, dict] = {}
    stats = {}
    lo = now - timedelta(hours=WINDOW_HOURS)
    hi = now + timedelta(hours=1)
    for name, url in NEWS_SOURCES.items():
        try:
            resp = http.get(url, headers={"User-Agent": USER_AGENT}, timeout=20.0)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
            n = 0
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not title or not link:
                    continue
                raw_pub = item.findtext("pubDate") or item.findtext("published") or ""
                pub = parse_pubdate(raw_pub, now)
                if not (lo <= pub <= hi):
                    continue
                desc = strip_html(item.findtext("description") or "")[:300]
                key = hashlib.sha1(norm_title(title).encode()).hexdigest()[:16]
                if key not in items:
                    items[key] = {
                        "key": key, "title": title, "url": link,
                        "summary": desc, "source": name,
                        "pubtime": pub.strftime("%Y-%m-%d %H:%M UTC"),
                        "pub_ts": pub.timestamp(),
                    }
                n += 1
            stats[name] = {"ok": True, "items": n}
        except Exception as e:
            stats[name] = {"ok": False, "error": str(e)[:120]}
    rows = sorted(items.values(), key=lambda x: -x["pub_ts"])[:MAX_ITEMS]
    return rows, stats


def weather_excluded(items: list[dict]) -> tuple[list[dict], list[dict]]:
    kept, excluded = [], []
    for it in items:
        hit = next((kw for kw in WEATHER_TITLE_KEYWORDS
                    if kw.lower() in it["title"].lower()), None)
        # 标题命中关键词 → 直接剔除；仅摘要命中 → 保留由 LLM 语义裁决
        if hit:
            it = dict(it)
            it["exclude_reason"] = f"weather_keyword:{hit}"
            excluded.append(it)
        else:
            kept.append(it)
    return kept, excluded


class LLM:
    """OpenAI 兼容 Chat Completions 客户端（DeepSeek）。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        if not api_key:
            raise RuntimeError("缺少 LLM API key（检查 .env 或 --env-file）")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") or "https://api.deepseek.com"
        self.model = model or "deepseek-v4-flash"

    def chat_json(self, prompt: str, *, temperature: float, max_tokens: int = 8192) -> dict:
        url = f"{self.base_url}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        last_err = None
        with httpx.Client(timeout=120.0) as http:
            for attempt in range(3):
                try:
                    resp = http.post(url, json=payload, headers=headers)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
                    msg = resp.json()["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    if not content:
                        # 推理模型 reasoning 吃光 max_tokens → 翻倍重试
                        payload["max_tokens"] = min(payload.get("max_tokens", 8192) * 2, 32768)
                        raise RuntimeError("LLM content empty (reasoning truncated)")
                    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
                    return json.loads(content)
                except Exception as e:
                    last_err = e
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"LLM call failed: {last_err}")


# ---------------------------------------------------------------- 流水线步骤

def stage1_score(llm: LLM, items: list[dict], today: str) -> list[dict]:
    """LLM 初筛打分，返回逐项评分结果（与输入 key 对齐）。"""
    scored: dict[str, dict] = {}
    for i in range(0, len(items), STAGE1_CHUNK):
        chunk = items[i:i + STAGE1_CHUNK]
        payload = [{"key": it["key"], "title": it["title"], "source": it["source"],
                    "pubtime": it["pubtime"], "summary": it["summary"],
                    "url": it["url"]} for it in chunk]
        prompt = STAGE1_PROMPT.format(today=today) + "\n\n候选：\n" + json.dumps(
            payload, ensure_ascii=False)
        out = llm.chat_json(prompt, temperature=0.0)
        for row in out.get("items", []):
            k = str(row.get("key", ""))
            try:
                pi = float(row.get("public_interest") or 0)
                b2b = float(row.get("b2b_value") or 0)
            except (TypeError, ValueError):
                pi = b2b = 0.0
            # score 由代码计算（0.45*关注 + 0.55*B端价值，满分 10），消除 LLM 自评分漂移
            row["score"] = round((0.45 * pi + 0.55 * b2b) * 2, 1)
            if k not in scored or row["score"] > scored[k].get("score", 0):
                scored[k] = row
    merged = []
    for it in items:
        row = scored.get(it["key"], {"error": "no_llm_row"})
        merged.append({**it, **row})
    return merged


def stage2_question(llm: LLM, item: dict, today: str, max_close: str) -> dict:
    prompt = STAGE2_PROMPT.format(
        today=today,
        news_title=item["title"],
        news_source=item["source"],
        news_pubtime=item["pubtime"],
        news_summary=item["summary"],
        news_url=item["url"],
        max_close=max_close,
    )
    return llm.chat_json(prompt, temperature=0.3)


def title_similarity(a: str, b: str) -> float:
    """字符 bigram Jaccard 相似度（中英文均可用），用于本轮近似重复题判重。"""
    def bigrams(s: str) -> set:
        s = re.sub(r"[^\w一-鿿]", "", s.lower())
        return {s[i:i + 2] for i in range(max(len(s) - 1, 0))} or {s}
    sa, sb = bigrams(a), bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def load_registry(reg_path: Path) -> dict:
    """返回 {event_key: row, norm_title: row, news_url: row}。"""
    by_key, by_title, by_url = {}, {}, {}
    if reg_path.exists():
        for line in reg_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            by_key[row.get("event_key", "")] = row
            by_title[norm_title(row.get("title", ""))] = row
            if row.get("news_url"):
                by_url[row["news_url"]] = row
    return {"by_key": by_key, "by_title": by_title, "by_url": by_url,
            "rows": list(by_key.values())}


def is_duplicate(reg: dict, title: str, event_key: str, autopick_dir: Path,
                 *, news_url: str = "", sim_threshold: float = 0.7) -> bool:
    """判重：注册表 event_key / 注册表归一标题（含相似题面）/ 新闻 URL / 落盘文件 slug。"""
    if event_key and event_key in reg["by_key"]:
        return True
    nt = norm_title(title)
    if nt in reg["by_title"]:
        return True
    # LLM 复写题面不会逐字相同 → 与注册表标题做相似度判重（防次日重跑重复建题）
    if any(title_similarity(nt, reg_title) > sim_threshold
           for reg_title in reg["by_title"]):
        return True
    if news_url and news_url in reg["by_url"]:
        return True
    slug = slugify(event_key)
    if slug and list(autopick_dir.glob(f"*-{slug}.json")):
        return True
    return False


def build_question(cand: dict, item: dict, now: datetime) -> dict:
    """组装符合 questions 表 schema 的完整新题 JSON。"""
    spec = {
        "class": "B",
        "source": "autopick",
        "event_key": cand["event_key"],
        "category": cand.get("category", "news"),
        "resolution_criteria": cand["resolution_criteria"],
        "primary_source": cand["primary_source"],
        "evidence_urls": cand["evidence_urls"],
        "news_url": item["url"],
        "news_title": item["title"],
        "news_published_at": item["pubtime"],
        "probability_prior": cand["probability"],
        "probability_reason": cand["probability_reason"],
        "grace_days": GRACE_DAYS,
        "degrade_to": "C",
    }
    q = {
        "title": cand["title"],
        "opens_at": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "closes_at": cand["closes_at"],
        "outcome_type": "binary",
        "outcome": None,
        "resolved_at": None,
        "resolution_source": None,
        "is_public": True,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "resolution_class": "B",
        "resolution_spec": spec,
        "proposed_insert": {
            "table": "questions",
            "method": "Storage.add_question",
            "kwargs": {
                "title": cand["title"],
                "opens_at": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "closes_at": cand["closes_at"],
                "outcome_type": "binary",
                "is_public": True,
                "resolution_class": "B",
                "resolution_spec": spec,
            },
        },
    }
    return q


def validate_candidate(cand: dict, item: dict, now: datetime) -> list[str]:
    """出题结果机器校验，返回问题列表（空 = 通过）。"""
    errs = []
    if not cand.get("title"):
        errs.append("title 为空")
    if not cand.get("resolution_criteria"):
        errs.append("resolution_criteria 为空")
    if not cand.get("primary_source"):
        errs.append("primary_source 为空")
    try:
        closes = datetime.fromisoformat(cand["closes_at"])
    except Exception:
        errs.append(f"closes_at 不可解析: {cand.get('closes_at')}")
        return errs
    if closes <= now:
        errs.append("closes_at 早于当前时间")
    if closes > now + timedelta(days=MAX_HORIZON_DAYS):
        errs.append(f"closes_at 超过 {MAX_HORIZON_DAYS} 天上限")
    p = cand.get("probability")
    if not isinstance(p, (int, float)) or not (0.05 <= p <= 0.95):
        errs.append(f"probability 非法: {p}")
    # evidence_urls 只允许来自输入新闻（防编造）
    valid = {item["url"]}
    ev = [u for u in cand.get("evidence_urls", []) if u in valid]
    if not ev:
        ev = [item["url"]]
    cand["evidence_urls"] = ev
    return errs


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="Foresight 自主选题引擎原型")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help=f"输出根目录（默认 {DEFAULT_OUT_DIR}）")
    ap.add_argument("--max-picks", type=int, default=MAX_PICKS)
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--dry-run", action="store_true", help="只跑流程不落盘")
    args = ap.parse_args()

    now = datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir)
    autopick_dir = out_dir / "autopick"
    reg_path = autopick_dir / "registry.jsonl"

    env = parse_env_file(Path(args.env_file))
    os_env = __import__("os").environ
    api_key = os_env.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY", "")
    base_url = os_env.get("DEEPSEEK_BASE_URL") or env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os_env.get("DEEPSEEK_MODEL") or env.get("DEEPSEEK_MODEL", "deepseek-chat")

    print(f"[1/5] 扫新闻（48h 窗口，{len(NEWS_SOURCES)} 个源）...")
    with httpx.Client(follow_redirects=True) as http:
        items, src_stats = fetch_news(http, now)
    if len(items) < 10:
        print(f"错误：候选不足 10 条（仅 {len(items)}），退出。")
        print(json.dumps(src_stats, ensure_ascii=False, indent=2))
        return 2
    print(f"      聚合去重后 {len(items)} 条")
    kept, excluded = weather_excluded(items)
    print(f"      天气类剔除 {len(excluded)} 条，进入 LLM 初筛 {len(kept)} 条")

    print("[2/5] LLM 初筛打分...")
    llm = LLM(api_key, base_url, model)
    scored = stage1_score(llm, kept, today)
    bad = [s for s in scored if s.get("error") == "no_llm_row"]
    if bad:
        print(f"      警告：{len(bad)} 条未获 LLM 评分")
    passes = [s for s in scored
              if s.get("error") != "no_llm_row"
              and not s.get("reject")
              and s.get("falsifiable")
              and 1 <= (s.get("horizon_days") or 0) <= MAX_HORIZON_DAYS
              and (s.get("public_interest") or 0) >= 3
              and (s.get("b2b_value") or 0) >= 3
              and (s.get("score") or 0) >= args.min_score]
    passes.sort(key=lambda x: -(x.get("score") or 0))
    print(f"      达标候选 {len(passes)} 条（score≥{args.min_score}，public/b2b≥3）")

    reg = load_registry(reg_path)
    fresh = [s for s in passes
             if not is_duplicate(reg, s["title"], s.get("event_key", ""), autopick_dir,
                                 news_url=s["url"])]
    # 每日配额：注册表里今天已建题数，占掉当日额度 → 同日重跑不会超额补题（幂等）
    daily_done = sum(1 for r in reg["rows"] if r.get("date") == today)
    quota = max(0, args.max_picks - daily_done)
    print(f"      判重后新候选 {len(fresh)} 条；今日已建 {daily_done} 道，剩余配额 {quota}")

    print(f"[3/5] LLM 出题（配额 {quota} 道）...")
    max_close = (now + timedelta(days=MAX_HORIZON_DAYS)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    picks = []
    for s in fresh:
        if len(picks) >= quota:
            break
        try:
            cand = stage2_question(llm, s, today, max_close)
            cand.setdefault("event_key", slugify(s.get("title", s["key"])))
            # 出题后二次判重：event_key/题面/新闻 URL 与注册表、本轮已建题都不重
            if is_duplicate(reg, cand.get("title", s["title"]), cand["event_key"],
                            autopick_dir, news_url=s["url"]):
                print(f"      跳过（出题后判重命中）: {cand.get('title', s['title'])[:60]}")
                continue
            if any(cand["event_key"] == p["candidate"]["event_key"] for p in picks):
                continue
            # 本轮近似重复题判重（同一事件不同新闻源/不同措辞）
            if any(title_similarity(cand["title"], p["question"]["title"]) > 0.55
                   for p in picks):
                print(f"      跳过（本轮相似题）: {cand['title'][:60]}")
                continue
            errs = validate_candidate(cand, s, now)
            if errs:
                print(f"      跳过（校验失败 {errs}）: {s['title'][:60]}")
                continue
            q = build_question(cand, s, now)
            picks.append({"question": q, "news": s, "candidate": cand})
            print(f"      [OK] #{len(picks)} {cand['title']}（closes {cand['closes_at']}，p={cand['probability']}）")
        except Exception as e:
            print(f"      跳过（出题异常）: {s['title'][:60]} — {type(e).__name__}: {str(e)[:120]}")
    if not picks:
        if quota == 0:
            print("今日配额已用完，本轮不建题（幂等）")
        else:
            print("警告：本轮无新题产出（可能全被判重或校验不过）")

    print("[4/5] 落盘...")
    if not args.dry_run:
        autopick_dir.mkdir(parents=True, exist_ok=True)
        brief_lines = []
        created_files = []
        for p in picks:
            q, s, cand = p["question"], p["news"], p["candidate"]
            slug = slugify(cand["event_key"])
            qfile = autopick_dir / f"{today}-{slug}.json"
            if qfile.exists():
                print(f"      已存在，跳过: {qfile.name}")
                continue
            qfile.write_text(json.dumps(q, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
            reg_row = {"date": today, "event_key": cand["event_key"],
                       "title": cand["title"], "closes_at": cand["closes_at"],
                       "file": qfile.name, "news_url": s["url"],
                       "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+00:00")}
            with reg_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(reg_row, ensure_ascii=False) + "\n")
            created_files.append(qfile)
            brief_lines.append({
                "title": cand["title"], "file": qfile.name,
                "closes_at": cand["closes_at"],
                "probability": cand["probability"],
                "probability_reason": cand["probability_reason"],
                "resolution_criteria": cand["resolution_criteria"],
                "primary_source": cand["primary_source"],
                "news": f"{s['source']}: {s['title']} ({s['url']})",
                "b2b_value_note": cand.get("b2b_value_note", ""),
            })
            print(f"      {qfile}")

        # 候选清单（含 LLM 打分 + 天气剔除记录）
        cand_file = autopick_dir / f"candidates-{today}.json"
        cand_file.write_text(json.dumps({
            "date": today,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "llm": {"model": model, "base_url": base_url},
            "sources": src_stats,
            "raw_count": len(items),
            "weather_excluded": excluded,
            "scored": scored,
            "passed": passes,
            "fresh": fresh,
            "picks": [p["candidate"] for p in picks],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"      {cand_file}")

        # daily-brief.md
        md = [f"# Foresight 每日简报 — {today}", ""]
        if brief_lines:
            md.append(f"## 今日选题（{len(brief_lines)} 道）")
            for i, b in enumerate(brief_lines, 1):
                md += [
                    f"### {i}. {b['title']}",
                    f"- **建题文件**: `data/autopick/{b['file']}`",
                    f"- **揭晓时间**: {b['closes_at']}（≤90 天）",
                    f"- **预测概率**: {b['probability']:.2f} — {b['probability_reason']}",
                    f"- **揭晓条件（resolution_criteria）**: {b['resolution_criteria']}",
                    f"- **官宣渠道**: {b['primary_source']}",
                    f"- **B 端价值**: {b['b2b_value_note']}",
                    f"- **新闻依据**: {b['news']}",
                    "",
                ]
        else:
            md.append("## 今日无新选题（候选未达标或已建过）")
            md.append("")
        md += [
            "## 候选池",
            f"- 原始新闻 {len(items)} 条 → 天气类剔除 {len(excluded)} 条 → LLM 达标 {len(passes)} 条 → 判重后 {len(fresh)} 条",
            f"- 候选清单: `data/autopick/candidates-{today}.json`",
            "",
            "## 运行信息",
            f"- 模型: {model}",
            f"- 运行时间: {now.strftime('%Y-%m-%d %H:%M UTC')}",
            f"- 新闻源: {', '.join(NEWS_SOURCES.keys())}",
            "",
        ]
        brief_path = out_dir / "daily-brief.md"
        # 幂等：本轮无新题且简报已存在 → 保留上次简报（避免重跑把有效简报覆盖成"无选题"）
        if not brief_lines and brief_path.exists():
            print(f"      [保留] {brief_path}（本轮无新题，不覆盖）")
        else:
            brief_path.write_text("\n".join(md), encoding="utf-8")
            print(f"      {brief_path}")
    else:
        print("      [dry-run] 不落盘")
        for p in picks:
            print(json.dumps(p["question"], ensure_ascii=False, indent=2))

    print("[5/5] 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
