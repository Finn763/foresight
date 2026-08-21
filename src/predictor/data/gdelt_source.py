# gdelt_source.py：GDELT 2.0 DOC API（免费、无 key、英文为主）
import time
from datetime import UTC, datetime

import httpx

from predictor.data.sources import Document


def _parse_gdelt_date(raw: str) -> datetime | None:
    """GDELT seendate 存在 YYYYMMDDTHHMMSSZ / ISO8601 / YYYYMMDDHHMMSS 三种形态，逐格式尝试。
    注意：%Y 占 2 个格式字符但消费 4 个数据字符，不能按 len(fmt) 截断 raw，
    直接对完整串尝试解析；带 Z 的串同时尝试去掉 Z 的变体。"""
    s = raw.strip()
    candidates = [s]
    if s.endswith("Z"):
        candidates.append(s[:-1])
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y%m%d%H%M%S"):
        for cand in candidates:
            try:
                return datetime.strptime(cand, fmt)
            except ValueError:
                continue
    return None


class GDELTSource:
    name = "gdelt"

    def __init__(
        self,
        *,
        maxrecords: int = 5,
        start: datetime | None = None,
        end: datetime | None = None,
        _transport=None,
    ):
        self.maxrecords = maxrecords
        self.start, self.end = start, end  # 回测用历史窗口；缺省实时取最近 7 天
        self._transport = _transport

    def fetch(self, search_term: str) -> list[Document]:
        # 2026-08-11 实测：本网络 https(443) TLS 握手被干扰而 http(80) 正常——
        # GDELT 是公开数据 API（无鉴权、官方支持 http），走 http 规避 TLS 干扰
        url = "http://api.gdeltproject.org/api/v2/doc/doc"
        params = {
            "query": search_term,
            "mode": "artlist",
            "maxrecords": self.maxrecords,
            "format": "json",
        }
        if self.start and self.end:
            params["startdatetime"] = self.start.strftime("%Y%m%d%H%M%S")
            params["enddatetime"] = self.end.strftime("%Y%m%d%H%M%S")
        else:
            params["timespan"] = "7d"
        try:
            # GDELT 免费公共 API 不稳定（429 限流常见），重试 2 次、间隔递增
            arts = []
            for attempt in range(3):
                try:
                    with httpx.Client(transport=self._transport, timeout=10.0) as c:
                        r = c.get(url, params=params)
                        r.raise_for_status()
                        arts = r.json().get("articles", [])
                    break
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and attempt < 2:
                        time.sleep(3 * (attempt + 1))
                        continue
                    raise
                except httpx.TransportError:
                    if attempt < 2:
                        time.sleep(3 * (attempt + 1))
                        continue
                    raise
        except (httpx.HTTPError, ValueError):
            return []
        out = []
        for a in arts:
            pub = _parse_gdelt_date(a.get("seendate", ""))
            out.append(
                Document(
                    source="gdelt",
                    url=a.get("url", ""),
                    title=a.get("title", ""),
                    content=a.get("content", "")[:2000],
                    published_at=pub,
                    fetched_at=datetime.now(UTC),
                )
            )
        return out
