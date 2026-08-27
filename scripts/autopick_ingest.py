"""scripts/autopick_ingest.py — 当日 autopick 新题入库（观察期 is_public=False）。

前置：scripts/autopick.py 已把当日新题落盘 data/autopick/<today>-*.json（不含 candidates-*）。
本脚本只入库、不建题、不写揭晓数据。调度：schtasks 08:45（Foresight-AutoPickIngest），
赶在 09:00 预测轮前让新题进题池；08:30 的 Foresight-AutoPick 负责跑 autopick.py。

安全设计：
- 判重双查：title 精确 + resolution_spec.event_key 串匹配，已存在跳过（幂等）；
- wait_acquire 排队等 evolve.lock（health_check 同款协议），绝不硬撞 DuckDB 独占锁；
- 每文件独立 try/except，单文件坏数据不中断批次；
- 入库后 read_only 读回验证（id/title/event_key），拿不到即报错退出；
- is_public=False：观察期新题不进对外榜单，但仍进预测轮；
- 时间戳去 tzinfo（DuckDB TIMESTAMP 不吃 aware datetime）。

运行：
  手动: .venv\\Scripts\\python.exe -E -X utf8 scripts\\autopick_ingest.py
  调度: pythonw + run_silent.py（见 foresight 任务列表 Foresight-AutoPickIngest）
"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from predictor.config import Settings  # noqa: E402
from predictor.data.storage import Storage  # noqa: E402
from predictor.ops.lock import LockWaitTimeout, wait_acquire  # noqa: E402

LOCK_WAIT_SECONDS = 600  # 08:45 入场，09:00 预测轮前必须结束；等不到锁即告警退出
OBSERVE_IS_PUBLIC = False  # 观察期（2026-08-27 定）：新题不进对外榜单


def _parse_dt(s: str) -> datetime:
    """ISO 8601（含 +00:00）→ naive UTC（DuckDB TIMESTAMP 不接受 tzinfo）。"""
    return datetime.fromisoformat(s).replace(tzinfo=None)


def _event_key(spec) -> str:
    if isinstance(spec, dict):
        return str(spec.get("event_key", ""))
    return ""


def main() -> int:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    autopick_dir = ROOT / "data" / "autopick"
    db_path = Path(Settings().db_path)
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    qfiles = sorted(
        f for f in autopick_dir.glob(f"{today}-*.json")
        if not f.name.startswith("candidates-")
    )
    if not qfiles:
        print(f"[autopick-ingest] {today}: 无当日新题文件，正常退出。")
        return 0

    print(f"[autopick-ingest] {today}: 发现 {len(qfiles)} 个题文件，排队等锁（上限 {LOCK_WAIT_SECONDS}s）...")
    lock_path = db_path.parent / "evolve.lock"
    try:
        return _ingest(storage_db=db_path, qfiles=qfiles, lock_path=lock_path)
    except LockWaitTimeout:
        print(f"[autopick-ingest] 错误：等锁超时（>{LOCK_WAIT_SECONDS}s），轮次疑似挂死或异常超长，请人工确认。")
        return 1


def _ingest(storage_db, qfiles, lock_path) -> int:
    with wait_acquire(lock_path, timeout_seconds=LOCK_WAIT_SECONDS, poll_seconds=20):
        st = Storage(str(storage_db))
        try:
            st.create_schema()
            added, skipped = 0, 0
            for qf in qfiles:
                try:
                    payload = json.loads(qf.read_text(encoding="utf-8"))
                    kw = (payload.get("proposed_insert") or {}).get("kwargs") or {}
                    title = (kw.get("title") or payload.get("title") or "").strip()
                    if not title:
                        print(f"  跳过（无 title）: {qf.name}")
                        skipped += 1
                        continue
                    spec = kw.get("resolution_spec") or {}
                    ek = _event_key(spec)
                    dup = st._conn.execute(
                        "SELECT count(*) FROM questions WHERE title = ?", [title]
                    ).fetchone()[0]
                    if not dup and ek:
                        # resolution_spec 是 DuckDB JSON 列：用 json_extract_string 精确匹配 event_key
                        dup = st._conn.execute(
                            "SELECT count(*) FROM questions WHERE "
                            "json_extract_string(resolution_spec, '$.event_key') = ?",
                            [ek],
                        ).fetchone()[0]
                    if dup:
                        print(f"  跳过（判重命中）: {title[:60]}")
                        skipped += 1
                        continue
                    closes_at = _parse_dt(kw["closes_at"])
                    opens_at = _parse_dt(kw.get("opens_at") or kw["closes_at"])
                    qid = st.add_question(
                        title,
                        closes_at,
                        opens_at=opens_at,
                        outcome_type=kw.get("outcome_type", "binary"),
                        is_public=OBSERVE_IS_PUBLIC,
                        resolution_class=kw.get("resolution_class"),
                        resolution_spec=spec,
                    )
                    # 读回验证（写后立即验，防止静默失败）
                    row = st._conn.execute(
                        "SELECT id, title, is_public, resolution_spec FROM questions WHERE id = ?",
                        [qid],
                    ).fetchone()
                    if row is None or row[1] != title:
                        print(f"  错误：入库 #{qid} 读回验证失败，中止。")
                        return 1
                    print(f"  入库 #{qid}（public={row[2]}）: {title[:60]}")
                    added += 1
                except Exception as e:  # 单文件失败不中断批次
                    print(f"  跳过（异常 {type(e).__name__}: {str(e)[:120]}）: {qf.name}")
                    skipped += 1
        finally:
            st.close()
    print(f"[autopick-ingest] 完成：新增 {added} / 跳过 {skipped} / 扫描 {len(qfiles)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
