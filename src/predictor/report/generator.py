"""可解释报告：结论 → 推理 → 统计基线 → 依据（带链接）→ 模型分歧。"""

import statistics

from predictor.data.sources import Document


def generate_report(
    question_title: str,
    probability: float,
    rationale: str,
    summaries: list[str],
    evidence: list[Document],
    prior: float | None = None,
    runs: list | None = None,
    baseline: dict | None = None,
) -> str:
    lines = [
        f"# 预测报告：{question_title}",
        "",
        f"**结论概率：{probability:.0%}**",
        "",
        f"**推理：** {rationale}",
        "",
        "## 依据",
        "",
    ]
    for s, d in zip(summaries, evidence):
        lines.append(f"- {s}  [来源]({d.url})")
    if baseline and baseline.get("base_rate") is not None:
        lines += [
            "",
            f"**统计基线：** {baseline['base_rate']:.0%} （{baseline.get('method', '历史统计')}）",
        ]
    if prior is not None:
        lines += ["", f"**先验参考：** 市场隐含 {prior:.0%}"]
    if runs:
        ps = [r.probability for r in runs]
        lines += [
            "",
            f"**模型分歧：** {min(ps):.0%} ~ {max(ps):.0%}"
            f"（{len(ps)} 次采样，中位 {statistics.median(ps):.0%}）",
        ]
    lines += ["", "> 本预测基于揭晓前可得公开信息；概率非事实陈述。"]
    return "\n".join(lines)
