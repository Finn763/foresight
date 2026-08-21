# crawler_source.py：MediaCrawler 中文社交数据源（微博/小红书/B站）
#
# 抓取频率护栏（强制执行，实施计划 Task 23 用户拍板）：
#   每关键词每天 ≤2 次抓取、每次 ≤20 条 —— 由 scripts/crawl_social.py 落地时把关
#   （同 平台+关键词 12h 内重复抓取直接拒绝；--limit 钳制到 20）。
#   本模块只读 data/crawler/ 已落盘的 JSON，本身不触发任何抓取。
#
# 隐私护栏：
#   昵称/头像/用户 ID 一律不落库 —— 读取时只取 title/desc/链接/发布时间，
#   从不读取 nickname/user_id 等身份字段；content 中 @昵称 与链接尾巴一并清洗。
#
# 防泄漏纪律（与 retrieve.py 一致）：
#   create_time 缺失或无法解析的文档拒绝；发布时间晚于抓取时刻（未来时间戳）的文档拒绝；
#   只消费"预测时点之前已公开"的内容。published_at 只信平台发布时间，抓取时间≠发布时间。
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from predictor.data.sources import Document

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CRAWLER_DIR = _PROJECT_ROOT / "data" / "crawler"

_AT_MENTION_RE = re.compile(r"@[\w\u4e00-\u9fa5._-]+")  # @昵称（含中文/下划线/点）
_URL_RE = re.compile(r"https?://\S+")  # 链接尾巴
_WS_RE = re.compile(r"\s+")
_BEIJING = timezone(timedelta(hours=8))  # 平台字符串时间为北京时间


def _clean_text(s: str) -> str:
    """清洗内容：去掉 @昵称 与链接，折叠空白。"""
    s = _AT_MENTION_RE.sub(" ", s or "")
    s = _URL_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _parse_create_time(raw) -> datetime | None:
    """解析 MediaCrawler 导出时间 → naive UTC datetime。
    - **优先带时区字符串**（create_date_time '2026-08-11 21:03:54+08:00'，自解释无歧义）
    - 数字时间戳：按 **UTC** 解析（2026-08-11 实测：Pro 版微博 create_time 为 UTC unix 秒，
      与 create_date_time 相差 8 小时时以 create_date_time 为准；数字仅兜底——
      按 UTC 解析即使平台实际为北京秒也只会偏早 8h，不会放行未来文档，防泄漏更保守）
    - 无时区字符串：平台本地时间（北京时间）→ 换算 UTC
    解析失败返回 None（调用方按"时间戳缺失"拒绝）。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return _ts_to_utc(float(raw))
    s = str(raw).strip()
    if s.replace(".", "", 1).lstrip("-").isdigit():
        return _ts_to_utc(float(s))
    # 带时区字符串优先（自解释，无歧义）
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1))
        if dt.tzinfo is not None:
            return dt.astimezone(UTC).replace(tzinfo=None)
    except ValueError:
        pass
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    # 平台字符串时间为北京时间（UTC+8），换算为 UTC 后再转 naive（与 retrieve.py 入库口径一致）
    return dt.replace(tzinfo=_BEIJING).astimezone(UTC).replace(tzinfo=None)


def _ts_to_utc(ts: float) -> datetime | None:
    """unix 时间戳 → naive UTC。秒/毫秒自适应。
    按 UTC 解析（2026-08-11 实测 Pro 版微博 create_time=UTC 秒；即便个别平台是北京秒，
    偏早 8h 也只让文档更保守地通过防泄漏检查，不会放行未来信息）。"""
    if ts > 1e12:
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _iter_records(path: Path):
    """宽松解析 MediaCrawler 导出：
    - .jsonl：JSON Lines（默认导出格式），逐行解析
    - .json：顶层列表 / {"data"|"items"|"records": [...]} / 单条记录"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if path.suffix.lower() == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                yield item
        return
    try:
        data = json.loads(text)
    except ValueError:
        return
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                yield it
    elif isinstance(data, dict):
        for key in ("data", "items", "records"):
            val = data.get(key)
            if isinstance(val, list):
                for it in val:
                    if isinstance(it, dict):
                        yield it
                return
        if any(k in data for k in ("title", "desc", "content", "create_time", "time")):
            yield data  # 本身就是一条笔记


class CrawlerSource:
    name = "crawler"

    def __init__(self, crawler_dir: str | Path | None = None, *, now: datetime | None = None):
        self.crawler_dir = Path(crawler_dir) if crawler_dir else DEFAULT_CRAWLER_DIR
        self._now = now  # 测试注入；None 时每次 fetch 取当前时间

    def fetch(self, search_term: str) -> list[Document]:
        kw = (search_term or "").strip().lower()
        if not kw or not self.crawler_dir.is_dir():
            return []
        now = self._now or datetime.now(UTC)
        now_naive = now.replace(tzinfo=None) if now.tzinfo else now
        now_aware = now_naive.replace(tzinfo=UTC)
        cutoff_ts = (now_aware - timedelta(hours=24)).timestamp()  # 只扫最近 24h 落盘文件

        out: list[Document] = []
        for path in sorted(
            p for p in self.crawler_dir.rglob("*") if p.suffix.lower() in (".json", ".jsonl")
        ):
            try:
                if path.stat().st_mtime < cutoff_ts:
                    continue
            except OSError:
                continue
            for item in _iter_records(path):
                doc = self._to_document(item, kw, now_naive)
                if doc is not None:
                    out.append(doc)
        return out

    def _matches(self, text_lower: str, kw: str) -> bool:
        """宽松匹配：搜索词整体命中，或任一中文字节对（2 字窗口）/英文词（≥3 字母）命中。
        中文无空格，LLM 生成的搜索词与爬虫文本关键词空间不同，必须宽匹配；
        相关性由下游 filter_relevant（LLM 挑选）把关，此处宁多勿漏。"""
        if kw in text_lower:
            return True
        # 英文词 token（≥3 字母）
        for tok in re.findall(r"[a-zA-Z]{3,}", kw):
            if tok.lower() in text_lower:
                return True
        # 中文 2 字连续子串
        for i in range(len(kw) - 1):
            if kw[i : i + 2] in text_lower:
                return True
        return False

    def _to_document(self, item: dict, kw: str, now_naive: datetime) -> Document | None:
        title = _clean_text(str(item.get("title") or ""))
        desc = _clean_text(str(item.get("desc") or item.get("content") or ""))
        if not self._matches(f"{title} {desc}".lower(), kw):
            return None
        published = _parse_create_time(
            item.get("create_date_time") or item.get("create_time") or item.get("time")
        )
        # 注意：优先 create_date_time（带 +08:00 时区，自解释）；create_time 数字为 UTC 秒兜底
        if published is None:
            return None  # 时间戳缺失/无法解析 → 拒绝（防泄漏）
        if published > now_naive:
            return None  # 未来时间戳 → 拒绝（防泄漏）
        url = str(
            item.get("note_url")
            or item.get("video_url")
            or item.get("url")
            or item.get("link")
            or ""
        )
        content = _clean_text(f"{title} {desc}")[:2000]
        return Document(
            source=self.name,
            url=url,
            title=title,
            content=content,
            published_at=published,
            fetched_at=now_naive,
        )
