"""python scripts/pm_fetch.py --dry-run
从 Polymarket Gamma API 拉活跃市场 → horizon 三档筛选 → LLM 译中文 → 入题池。

用法：
  python scripts/pm_fetch.py                     # 拉取并入库（分档：短≤14/中≤45/长≤90 天）
  python scripts/pm_fetch.py --dry-run           # 只打印候选，不落库不翻译
  python scripts/pm_fetch.py --per-tier 8 --min-volume 10000
  python scripts/pm_fetch.py --max-events 300    # 分页扫描事件数上限（保护）

题面：英文原题 LLM 译中文（失败回退英文）；closes_at = 市场 endDate（北京时间）；
resolution_spec 记 polymarket 来源（market_id 判重 + pm_resolve 揭晓溯源）。
默认 is_public=False（内部样本积累；对外公开性后议，--public 翻转）。
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
import httpx

from predictor.config import Settings
from predictor.data.polymarket_source import (
    _parse_utc,
    fetch_event_markets,
    fetch_events,
    select_candidates,
    translate_title,
)
from predictor.data.storage import Storage
from predictor.llm.client import LLMClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=Settings().db_path)
    ap.add_argument("--max-events", type=int, default=500, help="分页扫描事件数上限（保护）")
    ap.add_argument("--per-tier", type=int, default=6, help="每档（短/中/长）最多入库题数")
    ap.add_argument("--min-volume", type=float, default=1000.0, help="市场累计成交额门槛（美元）")
    ap.add_argument("--dry-run", action="store_true", help="只打印候选，不翻译不落库")
    ap.add_argument("--public", action="store_true", help="入库为公开题（默认内部题）")
    ap.add_argument("--no-translate", action="store_true", help="跳过翻译，保留英文题面")

    def _error(msg: str) -> None:
        # argparse 用法错误默认 exit 2 → 调度侧统一为 1（只有 0/1 语义）
        print(msg, file=sys.stderr)
        sys.exit(1)

    ap.error = _error  # type: ignore[method-assign]
    args = ap.parse_args()

    settings = Settings()
    try:
        st = Storage(args.db)
        st.create_schema()
        known = st.source_market_ids("polymarket")
    except Exception as e:
        # DuckDB 跨进程独占锁：daily/evolve 轮持锁时连接即 IOException。优雅退出
        # exit 1（LastTaskResult 非零可查），不裸 traceback；下一轮自动重试。
        print(f"DB 初始化失败（可能被其他轮持锁，下一轮自动重试）：{e}")
        return 1

    horizon_end = datetime.now(UTC) + timedelta(days=90)  # 只扫长档窗口内的事件
    with httpx.Client() as http:
        try:
            events: list[dict] = []
            offset = 0
            while offset < args.max_events:
                page = fetch_events(http, limit=100, offset=offset)
                if not page:
                    break
                events.extend(page)
                # 升序 = 最早结束在前：本页最早事件的 endDate 已超 90 天窗口则停
                earliest = min(
                    (_parse_utc(e.get("endDate")) for e in page if e.get("endDate")),
                    default=None,
                )
                if earliest is None or earliest > horizon_end:
                    break
                offset += 100
            if offset >= args.max_events:
                print(
                    f"警告：事件扫描达上限 {args.max_events}，90 天窗口可能未扫完（长档候选不完整），"
                    f"建议提高 --max-events"
                )
        except Exception as e:
            # 网络失败整轮终止：可读消息 + exit 1（LastTaskResult 非零可查），
            # 不裸 traceback；幂等语义不受影响（本轮未入库任何题）。
            print(f"事件列表拉取失败（网络降级，本轮终止）：{e}")
            return 1
        # 并发拉各事件的 markets（8 线程；单事件失败跳过，不阻塞整轮）
        markets: list[dict] = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {
                ex.submit(fetch_event_markets, http, str(ev.get("id") or "")): str(
                    ev.get("id") or ""
                )
                for ev in events
                if ev.get("id")
            }
            for fut in as_completed(futs):
                eid = futs[fut]
                try:
                    for m in fut.result():
                        m["_event_id"] = eid  # 供同主题去重
                        markets.append(m)
                except Exception:
                    continue  # 单事件失败跳过，不阻塞整轮
    print(f"事件 {len(events)} 个 → 市场 {len(markets)} 个")

    candidates = select_candidates(
        markets, now=datetime.now(UTC), per_tier=args.per_tier, min_volume=args.min_volume
    )
    fresh = [c for c in candidates if c.market_id not in known]
    print(f"候选 {len(candidates)}（去重后新增 {len(fresh)}）")

    llm = None
    if not args.dry_run and not args.no_translate:
        try:
            llm = LLMClient(**settings.llm_client_kwargs)
        except Exception as e:
            print(f"警告：LLM 客户端构造失败（{e}），本轮跳过翻译保留英文题面")

    added = 0
    for c in fresh:
        title = c.title
        if not args.dry_run and not args.no_translate and llm is not None:
            title = translate_title(llm, c.question)
        if args.dry_run:
            print(f"  [dry] {c.closes_at:%m-%d} vol=${c.volume:,.0f} {title}")
            continue
        spec = {
            "source": "polymarket",
            "market_id": c.market_id,
            "slug": c.slug,
            "url": c.url,
            "resolution_criteria": c.description,
        }
        try:
            qid = st.add_question(
                title,
                c.closes_at,
                is_public=args.public,
                resolution_class="B",  # 混合揭晓：市场决议优先，B 类 LLM 兜底
                resolution_spec=spec,
            )
        except Exception as e:
            # 并发锁/DB 异常只降级本题（与 pm_resolve 同纪律），不击垮整轮；
            # 已入库题不受影响，未入库题 market_id 判重保证下一轮重试不重复。
            print(f"  入库失败（降级，待下一轮）：{e}（{title}）")
            continue
        print(f"  #{qid} [{c.closes_at:%Y-%m-%d}] vol=${c.volume:,.0f} {title}")
        added += 1
    print(f"本轮入库 {added} 题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
