"""Foresight 预测引擎 CLI 入口（`python -m predictor.cli`，等价于 scripts/predict_cli.py）。

两种使用方式：
  1. 单次模式（机器可解析，输出恒为单行 JSON）：
       python -m predictor.cli "美联储9月会加息吗" --closes 2026-09-17
       python -m predictor.cli --publish 3          # 草稿转公开（审核门）
  2. python scripts/predict_cli.py "标题"           # Pi Extension 经 pi.exec 调用同一入口

建题缺省草稿 is_public=False；--public 建为公开题（进公开战绩）。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from predictor.config import Settings
from predictor.data.gdelt_source import GDELTSource
from predictor.data.newsapi_source import NewsAPISource
from predictor.data.storage import Storage
from predictor.llm.client import LLMClient
from predictor.pipeline import run_prediction
from predictor.selection.dedup import find_duplicate_question

# CrawlerSource 稍后才落地（Task 稍后）：现在缺失时静默跳过，装上后自动启用。
try:
    from predictor.data.crawler_source import CrawlerSource
except ImportError:
    CrawlerSource = None


def build_sources() -> list:
    """按 FORESIGHT_DISABLE_SOURCES 开关组装数据源（gdelt,newsapi[,crawler]）。"""
    disabled = {
        s.strip() for s in os.getenv("FORESIGHT_DISABLE_SOURCES", "").split(",") if s.strip()
    }
    sources: list = []
    if "gdelt" not in disabled:
        sources.append(GDELTSource())
    if "newsapi" not in disabled:
        sources.append(NewsAPISource())
    if CrawlerSource is not None and "crawler" not in disabled:
        sources.append(CrawlerSource())
    return sources


def predict_once(
    title: str,
    closes: datetime,
    engine: str,
    is_public: bool,
    st: Storage,
    settings: Settings,
) -> dict[str, Any]:
    """建题（去重）→ 跑管线 → 返回 JSON 契约 dict（单次模式）。"""
    now = datetime.now()
    if closes <= now:
        # 对抗审计（2026-08-15）：过去日期建题会永远挂在未揭晓列表，直接拒绝
        return {
            "ok": False,
            "question_id": None,
            "is_public": is_public,
            "reason": "揭晓日期已过去，拒绝建题（不预测过去事件）",
        }
    client = LLMClient(**settings.llm_client_kwargs)
    # 建题去重（2026-08-12 精确标题 → 2026-08-20 扩为事件签名近似判重）：未揭晓同题
    # 已存在 → 复用（防调试/误操作/措辞略异产生重复题，如 #97/#98、#93/#94）
    try:
        dup_id = find_duplicate_question(st, title)
    except Exception:
        # 兜底：签名判重模块异常时退回精确标题查重，不阻塞建题
        dup = st._conn.execute(
            "SELECT id FROM questions WHERE title = ? AND outcome IS NULL ORDER BY id LIMIT 1",
            [title],
        ).fetchone()
        dup_id = dup[0] if dup else None
    if dup_id is not None:
        return {
            "ok": True,
            "question_id": dup_id,
            "is_public": is_public,
            "reused": True,
            "note": "同题（精确或近似事件签名）未揭晓题已存在，复用",
        }
    qid = st.add_question(title, closes, is_public=is_public)
    try:
        # 审计：agent 建题同样记 question_added（与 daily/evolve 题族事件格式一致）
        st.log_evolution(
            "question_added",
            json.dumps(
                {"qid": qid, "title": title, "closes": closes.isoformat()}, ensure_ascii=False
            ),
        )
    except Exception:
        pass  # 审计日志失败不阻塞建题（与 daily._log_event 同纪律）
    now = datetime.now()
    try:
        if engine == "websearch":
            # 历史数据层（方案 A，同 classic 管线）：失败降级为空，不阻塞
            baseline = None
            historical_context = ""
            try:
                from predictor.stats.baselines import compute_baseline
                from predictor.stats.historical import build_series_context, fetch_series_map

                sm = fetch_series_map(now=now)
                historical_context = build_series_context(sm, now=now)
                baseline = compute_baseline(title, sm, now=now, closes_at=closes)
            except Exception:
                pass
            from predictor.websearch_predictor import websearch_predict

            pred = websearch_predict(
                qid,
                title,
                closes,
                now,
                client,
                st,
                baseline=baseline,
                historical_context=historical_context,
            )
        else:
            pred = run_prediction(qid, st, client, build_sources(), now=now)
    except Exception as e:  # noqa: BLE001 —— CLI 边界：任何失败都输出可解析 JSON，不裸抛
        return {
            "ok": False,
            "question_id": qid,
            "is_public": is_public,
            "reason": f"管线失败: {e}",
        }
    if pred is None:
        return {
            "ok": False,
            "question_id": qid,
            "is_public": is_public,
            "reason": "无可用证据或管线失败",
        }
    return {
        "ok": True,
        "question_id": qid,
        "is_public": is_public,
        "probability": pred.probability,
        "rationale": pred.rationale,
        "report_md": pred.report_md,
        "evidence_ids": pred.evidence_ids,
    }


def publish_question(qid: int, st: Storage) -> dict[str, Any]:
    """草稿题转公开（审核门）。"""
    # 注意：duckdb 的 UPDATE rowcount 恒为 -1，不能用它判断是否命中 → UPDATE 后 SELECT 验证
    st._conn.execute("UPDATE questions SET is_public = TRUE WHERE id = ?", [qid])
    row = st._conn.execute("SELECT is_public FROM questions WHERE id = ?", [qid]).fetchone()
    ok = row is not None and bool(row[0])
    return {"ok": ok, "published": qid, "reason": None if ok else "题目不存在"}


def _project_root() -> Path:
    """源码树场景（editable 安装/脚本）返回项目根；wheel 安装时返回上层目录（无 .env 自动跳过）。"""
    return Path(__file__).resolve().parents[2]


def _load_project_env() -> None:
    """在任意目录启动也能读项目根 .env（API key 等）；系统环境变量优先于 .env。"""
    env_file = _project_root() / ".env"
    if env_file.exists():
        from dotenv import load_dotenv

        load_dotenv(env_file)


def main() -> None:
    ap = argparse.ArgumentParser(description="Foresight 预测引擎 CLI")
    ap.add_argument("title", nargs="*", help="预测问题文本")
    ap.add_argument("--closes", default=None, help="揭晓日期 YYYY-MM-DD，默认 30 天后")
    ap.add_argument(
        "--public",
        action="store_true",
        help="建为公开题（进公开战绩）。缺省草稿：人工审核后转公开，垃圾题不污染战绩",
    )
    ap.add_argument(
        "--publish", type=int, default=None, help="把草稿题转公开：--publish <id>（审核门）"
    )
    ap.add_argument(
        "--engine",
        choices=["websearch", "classic"],
        default="websearch",
        help="预测引擎：websearch=LLM 原生服务端搜索（默认，实时题）；"
        "classic=Halawi 自建检索管线（回测/历史题专用，防泄漏）",
    )
    ap.add_argument("--db", default=None, help="DB 路径，默认 Settings().db_path")
    args = ap.parse_args()

    _load_project_env()
    settings = Settings()
    db_arg = args.db
    if db_arg is None:
        # 默认相对路径在 cwd 下不存在时回落项目根，支持在任意目录启动 foresight
        p = Path(settings.db_path)
        if not p.is_absolute() and not p.exists():
            proj = _project_root() / p
            if proj.exists() or proj.parent.is_dir():
                p = proj
        db_arg = str(p)
    st = Storage(db_arg)
    st.create_schema()

    if args.publish is not None:
        print(json.dumps(publish_question(args.publish, st), ensure_ascii=False))
        return

    if not args.title:
        ap.error("需要提供预测问题文本，或用 --publish <id> 转公开")

    title = " ".join(args.title)
    closes = (
        datetime.fromisoformat(args.closes) if args.closes else datetime.now() + timedelta(days=30)
    )
    print(
        json.dumps(
            predict_once(title, closes, args.engine, args.public, st, settings),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
