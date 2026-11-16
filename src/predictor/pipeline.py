"""编排：question → [搜索词]→[检索+硬时间戳]→[过滤]→[摘要]→[预测]→[集成+校准]→入库→报告。"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from predictor.calibration.calibrate import (
    DEFAULT_CALIBRATOR_PATH,
    apply_calibrator,
    load_calibrator,
)
from predictor.calibration.isotonic import IsotonicCalibrator
from predictor.calibration.postprocess import clamp, extremize
from predictor.inference.ensemble import ensemble
from predictor.inference.filter import filter_relevant
from predictor.inference.forecast import ForecastResult, forecast
from predictor.inference.retrieve import retrieve_and_store
from predictor.inference.search_terms import generate_search_terms
from predictor.inference.summarize import summarize_documents
from predictor.report.generator import generate_report

_UNSET = object()


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
    calibrator: IsotonicCalibrator | None | object = _UNSET,
) -> Prediction | None:
    try:
        q = storage.get_question(question_id)
    except KeyError:
        return None
    if calibrator is _UNSET:
        # 生产调用点默认加载落盘校准器；回测/对照实验必须显式传 None（避免未来信息）
        calibrator = load_calibrator(DEFAULT_CALIBRATOR_PATH)
    now = now or datetime.now()
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
        try:
            storage.log_evolution("prediction_skipped", json.dumps({"qid": question_id, "detail": f"search terms LLM failed: {e}"}, ensure_ascii=False))
        except Exception:
            pass
        return None
    docs = retrieve_and_store(question_id, q.title, terms, sources, storage, now=now)
    if not docs:
        return None
    relevant = filter_relevant(q.title, docs, client, top_k=5)
    summaries = summarize_documents(relevant, client)
    model_name = getattr(client, "model", None) or "unknown"
    runs: list[ForecastResult] = []
    for i in range(n_samples):
        try:
            runs.append(forecast(q.title, summaries, client, prior=prior, model=model_name, historical_context=historical_context, baseline=baseline))
        except ValueError:
            continue
    if not runs:
        try:
            storage.log_evolution("prediction_skipped", json.dumps({"qid": question_id, "detail": "forecast all samples failed (LLM/format)"}, ensure_ascii=False))
        except Exception:
            pass
        return None
    # ponytail: single-model path trimmed → equal-weight ensemble (weights_from_model_stats deleted)
    raw_prob = extremize(ensemble(runs), alpha=alpha if alpha is not None else 0.2)
    prob = clamp(apply_calibrator(raw_prob, calibrator))
    evidence_ids = [d.id for d in relevant if d.id is not None]
    if not evidence_ids:
        return None
    sample_probs = [r.probability for r in runs]
    model_runs = {model_name: sample_probs}
    # 报告/理由取最接近集成值的样本（不是 runs[0]）；websearch 路径同款。
    chosen = min(runs, key=lambda r: abs(r.probability - raw_prob))
    pid = storage.add_prediction(
        question_id,
        prob,
        evidence_ids=evidence_ids,
        model_runs=model_runs,
        arm=arm,
        arm_group=arm_group,
        raw_probability=raw_prob,
    )
    report = generate_report(q.title, prob, chosen.rationale, summaries, relevant, prior, runs=runs, baseline=baseline)
    return Prediction(id=pid, question_id=question_id, probability=prob, rationale=chosen.rationale, evidence_ids=evidence_ids, model_runs=model_runs, report_md=report)
