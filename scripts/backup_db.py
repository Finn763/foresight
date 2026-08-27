"""scripts/backup_db.py — DuckDB 文件级备份（避开 Windows 独占锁）。

schtasks Foresight-Backup 每日 02:30 触发（避开全部预测/揭晓/巡检轮次）：
shutil.copy2 直接拷贝文件 data/foresight.db → data/backup/foresight-YYYYMMDD-HHMM.db，
**不连接 DB**——DuckDB 在 Windows 上跨进程排他访问，连 read_only 连接都会 IOException，
文件拷贝是唯一不受持锁轮次影响的方式（评审 §3.5）。

备份后自动清理保留窗口（默认 7 天）外的旧备份；清理按文件名日期判定
（copy2 保留源文件 mtime，mtime 是源库最后写入时刻而非备份时刻，不可作清理依据）。

用法：
  python scripts/backup_db.py [--db <路径>] [--backup-dir <目录>] [--keep-days N] [--now ISO]
"""

import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_KEEP_DAYS = 7

_ROOT = Path(__file__).resolve().parents[1]


def backup_db(db_path: Path, backup_dir: Path, now: datetime) -> Path:
    """文件拷贝备份：db_path → backup_dir/foresight-YYYYMMDD-HHMM.db。

    不连接数据库；DB 缺失或拷贝被拒（极端情况撞上持锁句柄）上抛，由 main 落
    exit 1（schtasks 记失败，宁可显式失败也不留「假成功」的空备份）。
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"DB 不存在：{db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"foresight-{now:%Y%m%d-%H%M}.db"
    shutil.copy2(db_path, dest)
    return dest


def prune_old(backup_dir: Path, now: datetime, keep_days: int = DEFAULT_KEEP_DAYS) -> list[Path]:
    """删除早于保留窗口的备份；按文件名日期判定（foresight-YYYYMMDD-*），解析失败跳过。"""
    if not backup_dir.is_dir():
        return []
    cutoff = now.date() - timedelta(days=keep_days)
    removed = []
    for f in backup_dir.glob("foresight-*.db"):
        try:
            day = datetime.strptime(f.name[10:18], "%Y%m%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                f.unlink()
                removed.append(f)
            except OSError:
                pass
    return removed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="DB 路径，默认 <项目根>/data/foresight.db")
    ap.add_argument("--backup-dir", default=None, help="备份目录，默认 <db 同目录>/backup")
    ap.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS, help="保留天数，默认 7")
    ap.add_argument("--now", default=None, help="测试注入 YYYY-MM-DDTHH:MM:SS")
    args = ap.parse_args(argv)

    db = Path(args.db) if args.db else _ROOT / "data" / "foresight.db"
    backup_dir = Path(args.backup_dir) if args.backup_dir else db.parent / "backup"
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()

    try:
        dest = backup_db(db, backup_dir, now)
    except (FileNotFoundError, OSError) as e:
        print(f"BACKUP-FAIL: {type(e).__name__}: {e}")
        return 1
    pruned = prune_old(backup_dir, now, args.keep_days)
    print(f"BACKUP: {db} -> {dest}")
    for p in pruned:
        print(f"  pruned: {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
