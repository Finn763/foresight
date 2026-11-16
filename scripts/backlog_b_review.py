"""python scripts/backlog_b_review.py [--limit N] [--out CSV]
过期 B 类题批量复核：LLM 双采样判定 + 四护栏，**只输出审阅 CSV，不写库**。

用途：清理历史积压（2026-09→10 停摆期间到期的 B 类题）。逐题调
`LLMResolver.resolve(..., ignore_grace=True)`（绕过硬宽限，仅复核）；低置信/
分歧/无证据一律留空，由人工复核。入库请另行编辑 data/resolutions.csv 后跑
`scripts/resolve.py`。

成本：每题 2 次搜索 + 2 次 Responses 采样；默认题间 sleep 0.5s。中断后重跑会
重新判定（不做断点续跑，输出文件按 qid 覆盖写）。
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predictor.config import Settings  # noqa: E402
from predictor.data.storage import Storage  # noqa: E402
from predictor.llm.client import LLMClient  # noqa: E402
from predictor.resolution.llm_resolver import LLMResolver  # noqa: E402


def _snapshot(db_path: str, limit: int | None) -> list[dict]:
    """一次性读出待复核题后关闭连接（不长时间持有 DuckDB 读锁）。"""
    st = Storage(db_path, read_only=True)
    try:
        sql = (
            "SELECT id, title, closes_at, resolution_spec FROM questions "
            "WHERE outcome IS NULL AND resolution_class = 'B' "
            "AND closes_at < CURRENT_TIMESTAMP ORDER BY closes_at, id"
        )
        rows = st._conn.execute(sql).fetchall()
    finally:
        st.close()
    out = []
    for qid, title, closes_at, spec_raw in rows:
        try:
            spec = json.loads(spec_raw) if spec_raw else {}
        except Exception:
            spec = {}
        spec.setdefault("class", "B")
        out.append({"id": qid, "title": title, "closes_at": closes_at, "spec": spec})
    return out[:limit] if limit else out


def main() -> int:
    ap = argparse.ArgumentParser(description="过期 B 类题批量复核（只读，不写库）")
    ap.add_argument("--db", default=Settings().db_path)
    ap.add_argument("--limit", type=int, default=0, help="只复核前 N 题（0=全部）")
    ap.add_argument("--out", default="data/backlog-b-review-20261116.csv")
    ap.add_argument("--sleep", type=float, default=0.5, help="题间休眠秒数")
    args = ap.parse_args()

    questions = _snapshot(args.db, args.limit or None)
    if not questions:
        print("无过期 B 类题")
        return 0
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "qid", "title", "closes_at", "outcome", "confidence", "citations", "reviewed_at", "note",
    ]
    client = LLMClient(**Settings().llm_client_kwargs)
    resolver = LLMResolver(client, storage=None)

    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, row in enumerate(questions, 1):
            q = SimpleNamespace(
                id=row["id"], title=row["title"], closes_at=row["closes_at"]
            )
            note = ""
            outcome = ""
            confidence = ""
            citations = ""
            try:
                verdict = resolver.resolve(
                    q, row["spec"], datetime.now(), ignore_grace=True
                )
            except Exception as e:  # 单题失败不中断批次
                verdict = None
                note = f"{type(e).__name__}: {str(e)[:120]}"
            if verdict is not None:
                outcome = "true" if verdict[0] else "false"
                extra = verdict[2] if len(verdict) > 2 and isinstance(verdict[2], dict) else {}
                confidence = extra.get("confidence", "")
                citations = ";".join(extra.get("citations") or [])
            else:
                note = note or "no_verdict（低置信/分歧/无证据，需人工）"
            writer.writerow(
                {
                    "qid": row["id"],
                    "title": row["title"],
                    "closes_at": row["closes_at"].isoformat(timespec="seconds"),
                    "outcome": outcome,
                    "confidence": confidence,
                    "citations": citations,
                    "reviewed_at": datetime.now().isoformat(timespec="seconds"),
                    "note": note,
                }
            )
            f.flush()
            print(
                f"[{i}/{len(questions)}] #{row['id']} -> {outcome or 'NO_VERDICT'}"
                f"{(' conf=' + str(confidence)) if confidence != '' else ''}",
                flush=True,
            )
            if args.sleep and i < len(questions):
                time.sleep(args.sleep)
    print(f"审阅结果已写入 {out_path}（未写库；入库请人工复核后走 resolve.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
