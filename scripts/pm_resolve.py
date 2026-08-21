"""python scripts/pm_resolve.py
Polymarket 题混合揭晓（用户拍板：市场优先 + LLM 兜底）：

1. 枚举 resolution_spec.source='polymarket' 且未揭晓的到期题
2. 市场决议优先：拉 Gamma API /markets/{id}，outcomePrices 翻转为 ["1","0"]→Yes /
   ["0","1"]→No 即回填（resolution source = polymarket gamma api）
3. 市场未决议：交 B 类 LLM 揭晓器（LLMResolver，内部有 grace 3 天护栏；超宽限或
   护栏失败返回 None → 打印待人工，不阻塞）
4. 有揭晓 → 重新 fit 校准器落盘（与 resolve.py 同链路）

用法：python scripts/pm_resolve.py [--db data/foresight.db] [--dry-run]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx

from predictor.calibration.calibrate import refresh_from_storage
from predictor.config import Settings
from predictor.data.polymarket_source import GAMMA_BASE
from predictor.data.storage import Storage
from predictor.llm.client import LLMClient
from predictor.resolution.llm_resolver import LLMResolver


def market_outcome(http: httpx.Client, market_id: str) -> bool | None:
    """拉市场详情，已决议返回 True/False，未决议或请求失败返回 None。

    揭晓方向按 outcomes 顺序判断（极少数市场为 ["No","Yes"]，固定 index0=Yes
    会反转揭晓）：outcomePrices[i] == 1 表示 outcomes[i] 获胜。
    """
    try:
        r = http.get(f"{GAMMA_BASE}/markets/{market_id}", timeout=30.0)
        r.raise_for_status()
        m = r.json()
        prices = [float(p) for p in (m.get("outcomePrices") or [])]  # 字符串/浮点归一化
        if len(prices) != 2 or sorted(prices) != [0.0, 1.0]:
            return None  # 未决议（非 0/1 组合）
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except ValueError:
                outcomes = None
        win_idx = prices.index(1.0)
        # 默认二值市场 index0=Yes；仅显式 ["No","Yes"] 顺序时反转
        yes_idx = 1 if isinstance(outcomes, list) and len(outcomes) == 2 and outcomes[0] == "No" else 0
        return win_idx == yes_idx
    except (TypeError, ValueError):
        return None
    except Exception:
        return None


def should_fallback(now: datetime, closes_at: datetime, *, window_days: int = 3) -> bool:
    """市场决议独占窗口判定：closes 后 window_days 内只等市场，超窗才允许 LLM 兜底。"""
    return now - closes_at >= timedelta(days=window_days)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=Settings().db_path)
    ap.add_argument("--dry-run", action="store_true", help="只打印判定结果，不回填")
    args = ap.parse_args()

    settings = Settings()
    st = Storage(args.db)
    st.create_schema()
    now = datetime.now()
    qids = [q for q in st.source_question_ids("polymarket")]
    llm_client = None
    resolved = 0
    with httpx.Client() as http:
        for qid in qids:
            q = st.get_question(qid)
            if q.outcome is not None or now < q.closes_at:
                continue  # 已揭晓 / 未到期
            spec = st.question_resolution(qid) or {}
            mid = str(spec.get("market_id") or "")
            outcome = market_outcome(http, mid)
            if outcome is not None:
                if args.dry_run:
                    print(f"[dry] #{qid} 市场决议 {outcome}（market={mid}）")
                else:
                    try:
                        st.resolve_question(qid, outcome, f"polymarket gamma api market={mid}")
                        print(f"#{qid} 市场决议 {outcome}：{q.title}")
                    except Exception as e:
                        # 并发锁/DB 异常只降级本题（与 auto_resolve 同纪律），不击垮整轮
                        print(f"#{qid} 市场决议回填失败（降级）：{e}")
                        continue
                resolved += 1
                continue
            # 市场未决议：closes 后 3 天为市场决议独占窗口（事件结果常需数日公开报道，
            # 立即 LLM 兜底会抢先揭晓并永久屏蔽市场决议）——窗口内只等市场
            if not should_fallback(now, q.closes_at):
                print(f"#{qid} 市场未决议，独占窗口内等待（{q.closes_at.date()}）：{q.title}")
                continue
            # 超窗 → B 类 LLM 兜底。LLMResolver 内部 grace 护栏（默认 3 天）会与独占
            # 窗口叠加成零宽窗口，故 grace 推导传入：独占 window_days + 兜底宽限 3 天
            if llm_client is None:
                try:
                    llm_client = LLMClient(**settings.llm_client_kwargs)
                except Exception as e:
                    print(f"LLM 客户端构造失败（{e}），本轮兜底跳过，全部待人工/下一轮")
                    break
            verdict = LLMResolver(llm_client, storage=st).resolve(
                q, {**spec, "grace_days": 3 + 3}, now
            )
            if verdict is None:
                print(f"#{qid} 市场未决议且 LLM 兜底不可判（待人工/下一轮）：{q.title}")
                continue
            if args.dry_run:
                print(f"[dry] #{qid} LLM 兜底 {verdict[0]}：{q.title}")
            else:
                try:
                    st.resolve_question(qid, verdict[0], f"llm fallback ({verdict[1]})")
                    print(f"#{qid} LLM 兜底 {verdict[0]}：{q.title}")
                except Exception as e:
                    print(f"#{qid} LLM 兜底回填失败（降级）：{e}")
                    continue
            resolved += 1
    if resolved > 0 and not args.dry_run:
        try:
            if refresh_from_storage(st):
                print("校准器已刷新")
            else:
                print("校准器未刷新（已揭晓样本不足 30，保持 identity）")
        except Exception as e:
            print(f"校准器刷新失败（降级）：{e}")
    print(f"本轮揭晓 {resolved} 题")


if __name__ == "__main__":
    main()
