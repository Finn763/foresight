from dataclasses import dataclass
from typing import Any

from predictor.llm.prompts import SUPERFORECASTER_SYSTEM

FORECAST_PROMPT = (
    "预测下面事件成立的概率。流程：\n"
    "1. 先看【统计基线】（真实历史数据算出的客观频率）作为外部视角起点；无基线时凭领域知识给基准率；\n"
    "2. 再看【历史数据上下文】中的趋势/位置（距高点距离、年度涨跌、波动水平）；\n"
    "3. 最后按证据摘要调整（当前信息如何改变基线）。\n"
    '输出 JSON：{{"probability": 0.0-1.0, "rationale": "..."}}\n'
    "问题：{title}\n"
    "{baseline_block}"
    "{history_block}"
    "证据摘要：\n{summaries}"
    "{prior_block}"
)


@dataclass
class ForecastResult:
    probability: float
    rationale: str
    model: str


def forecast(
    title: str,
    summaries: list[str],
    client: Any,
    *,
    prior: float | None = None,
    model: str = "deepseek-chat",
    historical_context: str = "",
    baseline: dict | None = None,
) -> ForecastResult:
    prior_block = (
        f"\n参考：预测市场当前隐含概率 {prior:.0%}，注意其偏差，仅作锚点。" if prior else ""
    )
    baseline_block = ""
    if baseline and baseline.get("base_rate") is not None:
        baseline_block = (
            f"【统计基线】{baseline.get('method', '历史统计')}："
            f"{baseline['base_rate']:.1%}（样本 {baseline.get('n_obs', '?')} 个窗口）。"
            "以它为起点，除非当前证据明确偏离。\n"
        )
    history_block = f"【历史数据上下文】\n{historical_context}\n" if historical_context else ""
    for attempt in range(2):
        try:
            # format 在 try 内：题目标题含字面 { } 时 str.format 抛 KeyError
            # （predict_cli 等入口可注入任意标题），异常收敛为 ValueError → skip 单题
            prompt = FORECAST_PROMPT.format(
                title=title,
                summaries="\n".join(f"- {s}" for s in summaries),
                prior_block=prior_block,
                baseline_block=baseline_block,
                history_block=history_block,
            )
            # 采样温度 0.5：单模型多次采样产生模型内分歧（替代多模型集成的近似）；
            # 确定性步骤（搜索词/过滤/摘要）保持默认 0.0
            out = client.chat_json(
                [
                    {"role": "system", "content": SUPERFORECASTER_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
            )
            p = float(out["probability"])
            return ForecastResult(
                probability=max(0.0, min(1.0, p)),
                rationale=str(out.get("rationale", "")),
                model=model,
            )
        except Exception:
            if attempt == 1:
                raise ValueError("forecast 两次解析失败")
    raise ValueError("unreachable")
