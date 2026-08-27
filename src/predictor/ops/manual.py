"""人工揭晓候选清单：到期待揭晓题中无法自动揭晓的部分。

从 scripts/daily.py 下沉（P2 包边界修复）：facts.py（健康事实）与 evolve 揭晓轮
共用该清单逻辑——下沉后 src 不再反向依赖 scripts/，wheel 可独立运行。
"""

from predictor.resolution.spec import validate_resolution_spec


def manual_candidates(st, now) -> list:
    """到期待揭晓题中需要人工处理的部分：无 spec / class B / class C / A 类 spec 非法
    （自动揭晓必失败）。合法 A 类由 16:30 auto_resolve 自动揭晓——不进人工清单，
    防止把自动题提前人工判死（8-14 预演前对抗审计：daily 09:00 清单曾列出 A 类 #67，
    照提示在美股收盘前填写即永久错判，16:30 自动揭晓被跳过）。"""
    out = []
    for q in st.list_open_questions(by=now):
        try:
            spec = st.question_resolution(q.id)
        except Exception:
            out.append(q)  # spec JSON 损坏 → 无法自动 → 人工（与 auto_resolve 对称防护）
            continue
        if spec is None or spec.get("class") != "A":
            out.append(q)
            continue
        if validate_resolution_spec(spec):
            out.append(q)  # 非法 A spec → 自动揭晓必失败 → 人工
    return out
