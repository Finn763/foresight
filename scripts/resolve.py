"""python scripts/resolve.py --db data/foresight.db --outcomes data/resolutions.csv"""

import argparse
import csv
import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.config import Settings
from predictor.data.storage import Storage
from predictor.resolution.spec import validate_resolution_spec


def load_resolutions_csv(path: Path, *, text: str | None = None) -> list[tuple[int, bool, str]]:
    content = text if text is not None else Path(path).read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(content))
    missing = {"id", "outcome", "source"} - set(reader.fieldnames or [])
    if missing:
        # 对抗审计（2026-08-15）：坏 csv 静默 0 揭晓会让人误以为揭晓成功
        raise ValueError(f"resolutions csv 缺少列: {sorted(missing)}（需 id,outcome,source）")
    rows = []
    for line in reader:
        outcome = {"1": True, "0": False, "true": True, "false": False}.get(
            line["outcome"].strip().lower()
        )
        if outcome is None:
            continue
        rows.append((int(line["id"]), outcome, line["source"]))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=Settings().db_path)
    ap.add_argument("--outcomes", required=True)
    args = ap.parse_args()
    st = Storage(args.db)
    st.create_schema()
    try:
        rows = load_resolutions_csv(Path(args.outcomes))
    except ValueError as e:
        ap.error(str(e))
    resolved = 0
    for qid, outcome, source in rows:
        try:
            q = st.get_question(qid)
        except KeyError:
            print(f"跳过 #{qid}: 题不存在")
            continue
        if q.outcome is not None:
            # 重复揭晓会覆盖 outcome 且 model_stats 重复自增（8-14 预演前对抗审计）
            print(f"跳过 #{qid}: 已揭晓")
            continue
        try:
            spec = st.question_resolution(qid)
        except Exception:
            spec = None
        if spec and spec.get("class") == "A" and not validate_resolution_spec(spec):
            # 合法 A 类未到数据窗口（美股 T+1 / A 股当日收盘前）→ 拒绝人工填表：
            # 防止把自动题在行情出现前判死、16:30 自动揭晓被跳过
            lo = (
                q.closes_at + timedelta(days=1)
                if spec.get("close_timezone") != "Asia/Shanghai"
                else q.closes_at
            )
            if datetime.now() < lo:
                print(
                    f"跳过 #{qid}: A 类自动题未到可判定时点（{lo:%Y-%m-%d %H:%M}），"
                    f"交 16:30 自动揭晓"
                )
                continue
        st.resolve_question(qid, outcome, source)
        resolved += 1
    print(f"已揭晓 {resolved} 题")
    if resolved > 0:
        # 校准闭环：揭晓回填后重新 fit 校准器落盘（预测侧下次加载生效）。
        # 刷新失败只降级（保持旧校准器/identity），不阻塞揭晓轮其余输出。
        try:
            from predictor.calibration.calibrate import (
                DEFAULT_CALIBRATOR_PATH,
                refresh_from_storage,
            )

            if refresh_from_storage(st):
                print(f"校准器已刷新：{DEFAULT_CALIBRATOR_PATH}")
            else:
                print("校准器未刷新（已揭晓样本不足 30，保持 identity）")
        except Exception as e:
            print(f"校准器刷新失败（降级，不阻塞）：{e}")
    print("分桶战绩：")
    for b in st.brier_by_horizon_bucket():
        flag = " (样本不足,不可靠)" if b["unreliable"] else ""
        print(f"  {b['bucket']}: n={b['n']} Brier={b['brier_mean']:.4f}{flag}")


if __name__ == "__main__":
    main()
