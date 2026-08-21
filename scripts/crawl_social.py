#!/usr/bin/env python3
"""crawl_social.py：调用 MediaCrawler 抓取中文社交平台数据，JSON 落地 data/crawler/。

用法：
  python scripts/crawl_social.py --keywords "美联储,降息" --platforms weibo,xhs --limit 20

护栏（实施计划 Task 23，强制执行）：
  - 每关键词每天 ≤2 次抓取：同 平台+关键词 12h 内重复抓取直接拒绝（exit 0）
  - 每次抓取 ≤20 条：--limit 超限自动钳制到 20
  - 只抓公开内容；昵称/用户 ID 不落库（CrawlerSource 读取时不取身份字段，见其 docstring）
  - 仅实时题使用；回测脚本不得引用

MediaCrawler 未部署（data/mediacrawler 缺失）时打印提示并正常退出（exit 0），不阻塞管线。
抓取结果由 predict_cli.py / daily.py 的检索链路入库（CrawlerSource 读取本目录 JSON）。
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MC_DIR_PRO = ROOT / "data" / "mediacrawler-pro"  # Pro 版（付费授权，2026-08-11 起主力）
MC_DIR = MC_DIR_PRO if (MC_DIR_PRO / "main.py").exists() else ROOT / "data" / "mediacrawler"
MC_PYTHON = MC_DIR / ".venv" / "Scripts" / "python.exe"
OUT_DIR = ROOT / "data" / "crawler"

MAX_LIMIT = 20  # 护栏：每次抓取 ≤20 条
MIN_INTERVAL_H = 12  # 护栏：同 平台+关键词 每天 ≤2 次（12h 内不重复）
MC_TIMEOUT_S = 1800  # MediaCrawler 慢（含登录等待），给足超时

# 用户友好平台名 → MediaCrawler CLI 枚举值（2026-08 实测：weibo 为 wb、bilibili 为 bili）
PLATFORM_MAP = {
    "weibo": "wb",
    "xhs": "xhs",
    "xiaohongshu": "xhs",
    "bilibili": "bili",
    "bili": "bili",
    "douyin": "dy",
    "dy": "dy",
    "kuaishou": "ks",
    "ks": "ks",
    "tieba": "tieba",
    "zhihu": "zhihu",
    "reddit": "reddit",
}


def _is_pro() -> bool:
    """Pro 版特征：--hot_search_max_items 参数存在（原版无）。"""
    return MC_DIR == MC_DIR_PRO


def _iter_json_records(path: Path):
    """宽松解析 MediaCrawler 导出的 JSON/JSONL（列表 / {"data": [...]} / 单条 / 逐行）。"""
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
            yield data


def _recent_crawls(platform: str, keyword: str, now: datetime) -> list[Path]:
    """OUT_DIR 下同 平台+关键词 且抓取时间距今 <12h 的合并文件（频率护栏）。"""
    recent: list[Path] = []
    if not OUT_DIR.is_dir():
        return recent
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    for p in OUT_DIR.glob("mediacrawler_*.json"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        try:
            fetched_dt = datetime.fromisoformat(meta.get("fetched_at") or "")
        except (TypeError, ValueError):
            continue
        if fetched_dt.tzinfo is not None:
            fetched_dt = fetched_dt.replace(tzinfo=None)
        if now_naive - fetched_dt >= timedelta(hours=MIN_INTERVAL_H):
            continue
        if platform in (meta.get("platforms") or []) and keyword in (meta.get("keywords") or []):
            recent.append(p)
    return recent


def _run_mediacrawler(
    platform: str, keyword: str, limit: int, crawler_type: str = "search", log=print
) -> list[dict]:
    """调 MediaCrawler（Pro 优先）CLI 抓取，收集本次运行新落盘的 JSON 记录。"""
    python = str(MC_PYTHON) if MC_PYTHON.exists() else sys.executable
    mc_platform = PLATFORM_MAP[platform]
    if _is_pro():
        # Pro 版（2026-08-11 实测 CLI）：--max_notes_count / --no-enable_comments /
        # SAVE_DATA_OPTION=json（导出到其项目根 data/<platform>/json/）
        cmd = [
            python,
            "main.py",
            "--platform",
            mc_platform,
            "--type",
            crawler_type,
            "--no-enable_comments",
        ]
        if crawler_type == "hot_search":
            cmd += ["--hot_search_max_items", str(limit)]
        else:
            cmd += ["--keywords", keyword, "--max_notes_count", str(limit)]
        env = {
            k: v for k, v in dict(__import__("os").environ).items() if k != "PYTHONPATH"
        }  # 清掉 Hermes 终端注入的 PYTHONPATH，避免污染子进程 import
        env["SAVE_DATA_OPTION"] = "json"
    else:
        cmd = [
            python,
            "main.py",
            "--platform",
            mc_platform,
            "--type",
            crawler_type,
            "--keywords",
            keyword,
            "--crawler_max_notes_count",
            str(limit),
            "--save_data_path",
            str(OUT_DIR),
            "--save_data_option",
            "json",
            "--get_comment",
            "no",
        ]
        env = None
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(MC_DIR), capture_output=True, text=True, timeout=MC_TIMEOUT_S, env=env
        )
    except FileNotFoundError:
        log(f"[crawl_social] 找不到解释器 {python}，跳过 {platform}/{keyword}", file=sys.stderr)
        return []
    except subprocess.TimeoutExpired:
        log(
            f"[crawl_social] MediaCrawler 超时（>{MC_TIMEOUT_S}s），跳过 {platform}/{keyword}",
            file=sys.stderr,
        )
        return []
    if proc.stdout:
        log(proc.stdout)
    if proc.stderr:
        log(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        log(
            f"[crawl_social] MediaCrawler 退出码 {proc.returncode}（可能需要配置 cookie/启动签名服务，见其 README）",
            file=sys.stderr,
        )
        return []
    records: list[dict] = []
    # Pro 版输出在 MC_DIR/data/<platform>/json/；原版在 OUT_DIR——两处都扫
    scan_dirs = [MC_DIR / "data", OUT_DIR] if _is_pro() else [OUT_DIR]
    for base in scan_dirs:
        if not base.is_dir():
            continue
        for p in base.rglob("*.json"):
            try:
                if p.stat().st_mtime < t0:
                    continue
            except OSError:
                continue
            records.extend(_iter_json_records(p))
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--keywords", default="", help="关键词，多个用英文逗号分隔（hot_search 模式可省略）"
    )
    ap.add_argument(
        "--platforms",
        default="weibo",
        help="平台：weibo/xhs/bilibili（亦支持 douyin/kuaishou/tieba/zhihu/reddit），逗号分隔",
    )
    ap.add_argument(
        "--type",
        default="search",
        choices=["search", "hot_search"],
        help="抓取类型：search=关键词搜索（默认）；hot_search=热搜榜（Pro 版，舆情信号直采）",
    )
    ap.add_argument(
        "--limit", type=int, default=20, help=f"每关键词抓取条数（护栏上限 {MAX_LIMIT}）"
    )
    ap.add_argument(
        "--db",
        default=str(ROOT / "data" / "foresight.db"),
        help="兼容参数：抓取结果由 predict_cli/daily 入库，本脚本只落地 JSON",
    )
    args = ap.parse_args(argv)

    if not (MC_DIR / "main.py").exists():
        print(
            f"[crawl_social] MediaCrawler 未部署（{MC_DIR} 不存在），跳过抓取。"
            "部署方式见实施计划 Task 23 Step 1（git clone + venv + requirements）。",
            file=sys.stderr,
        )
        return 0

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
    crawler_type = args.type
    if crawler_type != "hot_search" and (not keywords or not platforms):
        print("[crawl_social] search 模式需要至少一个关键词和一个平台", file=sys.stderr)
        return 1
    if not platforms:
        print("[crawl_social] 需要至少一个平台", file=sys.stderr)
        return 1
    unknown = [p for p in platforms if p not in PLATFORM_MAP]
    if unknown:
        print(
            f"[crawl_social] 未知平台 {unknown}，支持：{', '.join(sorted(PLATFORM_MAP))}",
            file=sys.stderr,
        )
        return 1
    limit = max(1, min(args.limit, MAX_LIMIT))
    if args.limit > MAX_LIMIT:
        print(
            f"[crawl_social] --limit {args.limit} 超过护栏上限 {MAX_LIMIT}，钳制为 {limit}",
            file=sys.stderr,
        )

    now = datetime.now()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    crawler_type = args.type
    all_records: list[dict] = []
    if crawler_type == "hot_search" and not _is_pro():
        print(
            "[crawl_social] hot_search 模式需要 MediaCrawlerPro（原版不支持），降级为 search",
            file=sys.stderr,
        )
        crawler_type = "search"
    for platform in platforms:
        if crawler_type == "hot_search":
            print(f"[crawl_social] 抓取 {platform} 热搜榜 hot_search_max_items={limit} ...")
            records = _run_mediacrawler(platform, "", limit, crawler_type="hot_search")
            print(f"[crawl_social] {platform} 热搜得到 {len(records)} 条")
            all_records.extend(records)
            continue
        for keyword in keywords:
            if _recent_crawls(platform, keyword, now):
                print(
                    f"[crawl_social] {platform}/{keyword} 12h 内已抓取过（每天 ≤2 次护栏），跳过",
                    file=sys.stderr,
                )
                continue
            print(f"[crawl_social] 抓取 {platform} 关键词「{keyword}」limit={limit} ...")
            records = _run_mediacrawler(platform, keyword, limit)
            print(f"[crawl_social] {platform}/{keyword} 得到 {len(records)} 条")
            all_records.extend(records)

    if all_records:
        ts = now.strftime("%Y%m%d_%H%M%S")
        out_path = OUT_DIR / f"mediacrawler_{ts}.json"
        payload = {
            "fetched_at": now.isoformat(),
            "platforms": platforms,
            "keywords": keywords,
            "count": len(all_records),
            "items": all_records,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[crawl_social] 合并 {len(all_records)} 条 → {out_path}")
    else:
        print("[crawl_social] 无新记录（可能 MediaCrawler 需要 cookie/登录，见其 README）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
