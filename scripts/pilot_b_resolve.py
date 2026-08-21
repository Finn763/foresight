"""python scripts/pilot_b_resolve.py --qid 68
B 类 LLM 揭晓器试点：真实 Responses API web_search 判定单题——只打印不入库，
结果交人工审阅后决定（spec §5 Go/No-Go 门槛）。"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.config import Settings
from predictor.data.storage import Storage
from predictor.llm.client import LLMClient
from predictor.resolution.llm_resolver import LLMResolver


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qid", type=int, required=True)
    ap.add_argument("--db", default=Settings().db_path)
    args = ap.parse_args()
    st = Storage(args.db, read_only=True)
    q = st.get_question(args.qid)
    spec = st.question_resolution(args.qid) or {"class": "B"}
    now = datetime.now()
    client = LLMClient(**Settings().llm_client_kwargs)
    verdict = LLMResolver(client, storage=None).resolve(q, spec, now)
    print(
        json.dumps(
            {
                "qid": q.id,
                "title": q.title,
                "closes": q.closes_at.isoformat(),
                "now": now.isoformat(timespec="seconds"),
                "verdict": verdict,
                "note": "只打印不入库；verdict=None 需看 llm_resolve_failed 或降级路径",
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
