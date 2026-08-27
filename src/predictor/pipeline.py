"""编排：question → [搜索词]→[检索+硬时间戳]→[过滤]→[摘要]→[预测]→[集成+校准]→入库→报告。"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from predictor.calibration.calibrate import apply_calibrator
from predictor.calibration.isotonic import IsotonicCalibrator
from predictor.calibration.postprocess import extremize
from predictor.inference.ensemble import ensemble, weights_from_model_stats
from predictor.inference.filter import filter_relevant
from predictor.inference.forecast import ForecastResult, forecast
from predictor.inference.retrieve import retrieve_and_store
from predictor.inference.search_terms import generate_search_terms
from predictor.inference.summarize import summarize_documents
from predictor.report.generator import generate_report


@dataclass
class Prediction:
    id: int
    question_id: int
    probability: float
    rationale: str
    evidence_ids: list[int]
    model_runs: dict
    report_md: str


def run_prediction(
    question_id: int,
    storage: Any,
    client: Any,
    sources: list[Any],
    *,
    now: datetime | None = None,
    prior: float | None = None,
    n_samples: int = 3,
    arm: str = "baseline",
    arm_group: int | None = None,
    alpha: float | None = None,
    calibrator: IsotonicCalibrator | None = None,
) -> Prediction | None:
    # 注：prior（市场先验）M3 前恒为 None——Polymarket/Kalshi 接入是 M3 后任务，
    # 参数仅为接口预留，外部调用方勿依赖。
    # 注：calibrator 默认 None = 不校准（classic 管线只服务回测/历史题，回测题
    # 用生产校准器会污染对比）；生产入口 websearch_predict 自动加载落盘校准器。
    # 更新语义：同一题可再次调用本函数 = 预测更新（re-predict，新证据→新 prediction 行）；
    # resolve 只给最后一条计分（Task 3），旧行作废不入战绩。
    try:
        q = storage.get_question(question_id)
    except KeyError:
        return None  # get_question 查无此 id 时抛 KeyError（永不返回 None）
    now = now or datetime.now()
    # 历史数据层（方案 A）：真实序列摘要 + 统计基线 → 注入 forecast（失败降级为空，不阻塞）
    historical_context = ""
    baseline = None
    try:
        from predictor.stats.baselines import compute_baseline
        from predictor.stats.historical import build_series_context, fetch_series_map

        sm = fetch_series_map(now=now)
        historical_context = build_series_context(sm, now=now)
        baseline = compute_baseline(q.title, sm, now=now, closes_at=q.closes_at)
    except Exception:
        pass
    try:
        terms = generate_search_terms(q.title, client)
    except Exception as e:
        # LLM 故障兜底（P1）：记 evolution_log 后 skip 单题，不击垮整个 predict 轮。
        # 与"无证据拒绝"纪律同构——搜不到词的题不出预测，留待下轮。
        try:
            storage.log_evolution(
                "prediction_skipped",
                json.dumps(
                    {"qid": question_id, "detail": f"search terms LLM failed: {e}"},
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
        return None
    docs = retrieve_and_store(question_id, q.title, terms, sources, storage, now=now)
    if not docs:
        return None  # 无证据 → 拒绝出预测（可溯源纪律）
    relevant = filter_relevant(q.title, docs, client, top_k=5)
    summaries = summarize_documents(relevant, client)
    runs: list[ForecastResult] = []
    for i in range(n_samples):
        try:
            runs.append(
                forecast(
                    q.title,
                    summaries,
                    client,
                    prior=prior,
                    model=client.model if hasattr(client, "model") else "deepseek-chat",
                    historical_context=historical_context,
                    baseline=baseline,
                )
            )
        except ValueError:
            continue
    if not runs:
        # forecast 全灭（LLM 故障/格式非法）≠ 无证据：记日志区分，避免调用方误判
        # 为"无证据拒绝"，运维排障时能在 evolution_log 看到真实原因
        try:
            storage.log_evolution(
                "prediction_skipped",
                json.dumps(
                    {"qid": question_id, "detail": "forecast all samples failed (LLM/format)"},
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
        return None
    # 在线权重：历史揭晓题的 Brier EMA 决定模型话语权；无统计（stub/无有效行/读失败）时等权
    weights = None
    try:
        stats_fn = getattr(storage, "model_stats", None)
        weights = weights_from_model_stats(stats_fn()) if stats_fn else None
    except Exception:
        pass  # 权重读取失败不阻塞预测（与 historical 降级纪律一致）
    prob = ensemble(runs, weights=weights)
    prob = extremize(prob, alpha=alpha if alpha is not None else 0.2)  # 杠杆②后处理参数化
    prob = apply_calibrator(prob, calibrator)  # 校准层（fit 自历史揭晓题；None 时 identity）
    evidence_ids = [d.id for d in relevant if d.id is not None]  # Task 7 已把真实 id 填进 doc
    if not evidence_ids:
        return None  # 无证据 id → 拒绝出预测（可溯源纪律）
    sample_probs = [r.probability for r in runs]  # 存全部采样：模型内分歧可追溯
    pid = storage.add_prediction(
        question_id,
        prob,
        evidence_ids=evidence_ids,
        model_runs={"deepseek-chat": sample_probs},
        arm=arm,
        arm_group=arm_group,
    )
    report = generate_report(
        q.title, prob, runs[0].rationale, summaries, relevant, prior, runs=runs, baseline=baseline
    )
    return Prediction(
        id=pid,
        question_id=question_id,
        probability=prob,
        rationale=runs[0].rationale,
        evidence_ids=evidence_ids,
        model_runs={"deepseek-chat": sample_probs},
        report_md=report,
    )
