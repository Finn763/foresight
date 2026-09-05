"""TruthSet 构建：把生产库已揭晓题冻结成 data/truthset/v1.jsonl（全量重写，幂等）。

只读生产库（read_only 短连接），绝不写回；输出是 replay 的地面真值快照。
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb

FIELDS = ("qid", "title", "opens_at", "closes_at", "resolved_at",
          "resolution_spec", "resolution_source", "outcome")


def snapshot_rows(db_path: Path) -> list[dict]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT id, title, opens_at, closes_at, resolved_at, "
            "resolution_spec, resolution_source, outcome "
            "FROM questions WHERE outcome IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        spec = row[5]
        out.append(dict(zip(FIELDS, (
            int(row[0]),
            row[1],
            row[2].isoformat(),
            row[3].isoformat(),
            row[4].isoformat() if row[4] else None,
            json.loads(spec) if isinstance(spec, str) and spec else (spec or None),
            row[6],
            bool(row[7]),
        ))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/foresight.db")
    ap.add_argument("--out", default="data/truthset/v1.jsonl")
    args = ap.parse_args()
    rows = snapshot_rows(Path(args.db))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    print(json.dumps({"n": len(rows), "out": str(out_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
