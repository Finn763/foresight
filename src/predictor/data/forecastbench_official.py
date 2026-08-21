"""ForecastBench 官方提交通道（已查证，详见 docs/forecastbench提交渠道调研.md）。

官方机制（2026-08-11 实测查证——**不是** Metaculus bot API）：
  1. 邮件注册 forecastbench@forecastingresearch.org → 官方分配 GCP bucket 文件夹；
  2. 每两周一期 Question Set（JSON：forecast_due_date + questions[]，均为未解决题）；
  3. due date 23:59:59 UTC 前生成 Forecast Set（id→probability 映射 JSON）上传 bucket；
  4. 官方计分后在排行榜公布 Brier——逐题 ground truth 不公开，本地无法回算。

本模块：
  fetch_open_questions()   读本地 data/fb_seed/ 最新一期 question set（resolved=False 的
                           未解决题）；本地缺失时尝试 GitHub raw（TODO URL 待复核）。
  submit_predictions()     生成 Forecast Set JSON 落盘 data/forecast_sets/，返回条数；
                           配置 FORECASTBENCH_GCS_BUCKET 且有 gcloud 时尝试上传
                           （TODO：邮件注册拿到 bucket 后配置）。
  api_token 参数为实施计划固定签名预留（官方通道无 API token 机制，当前忽略）。

本地记账：提交的题以 is_public=FALSE 复制进本地 questions 表，映射与来源标注
（forecastbench-official）记入 data/forecastbench_ledger.json（storage.py 无来源列且
本任务不改已有文件，故 ledger 文件承载）；揭晓后经 resolve 流程回填 outcome。
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from predictor.config import Settings
from predictor.data.benchmarks import BenchQuestion
from predictor.llm.client import LLMError

# ---- 路径/常量（TODO: 邮件注册后配置 bucket；raw URL 网络恢复后复核）----
SEED_DIR = Path("data/fb_seed")  # 官方 question_sets 本地落盘（36 期 + latest 指针）
FORECAST_SET_DIR = Path("data/forecast_sets")
GCS_BUCKET_ENV = "FORECASTBENCH_GCS_BUCKET"
# TODO: 复核 raw 路径格式（仓库已确认存在；raw.githubusercontent.com 被本机网络掐断）。
# 形如 datasets/question_sets/<YYYY-MM-DD>-llm.json 或 latest-llm.json
QUESTION_SET_RAW_URL = (
    "https://raw.githubusercontent.com/forecastingresearch/"
    "forecastbench-datasets/main/datasets/question_sets/<name>-llm.json"
)

# ---- 本地记账 ----
LEDGER_DEFAULT_PATH = Path("data/forecastbench_ledger.json")
LEDGER_SOURCE_TAG = "forecastbench-official"


def resolve_api_token() -> str:
    """读取 METACULUS_API_TOKEN（计划固定签名/旧版 Metaculus 通道预留；官方通道不用）。"""
    try:
        tok = getattr(Settings(), "metaculus_api_token", "") or ""
    except Exception:
        tok = ""
    if tok:
        return tok
    load_dotenv()
    return os.environ.get("METACULUS_API_TOKEN", "")


# ---- 拉未解决题 ----
def _latest_seed_file(seed_dir: str | Path) -> Path | None:
    """优先 latest-llm.json（官方最新指针），否则取日期最大的一期。"""
    d = Path(seed_dir)
    if not d.exists():
        return None
    latest = d / "latest-llm.json"
    if latest.exists():
        return latest
    dated = sorted(d.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-llm.json"))
    return dated[-1] if dated else None


def _parse_close_datetime(item: dict, due: Any) -> datetime:
    """closes_at 取 freeze_datetime（预测冻结时间，最保守）；占位值（如 2525 年）跳过；
    兜底 set 级 forecast_due_date，再兜底当前时间。"""
    for key in ("freeze_datetime", "market_info_close_datetime"):
        s = item.get(key)
        if not s:
            continue
        try:
            dt = datetime.fromisoformat(str(s))
        except ValueError:
            continue
        if dt.year > 2100:
            continue  # 占位值（2525-01-01 等）
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    if due:
        try:
            dt = datetime.fromisoformat(str(due))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _parse_question_set(raw: dict, *, limit: int) -> list[BenchQuestion]:
    """官方 question set 宽松解析；只保留未解决题（resolved=False）。

    规则：id 为数组的 combination question 丢弃；带 resolution 字段（已揭晓）丢弃；
    缺 question 文本丢弃；closes_at 用 freeze_datetime/due date。
    """
    due = raw.get("forecast_due_date")
    out: list[BenchQuestion] = []
    for item in raw.get("questions", []):
        qid = item.get("id")
        if isinstance(qid, list):
            continue  # combination question（旧题集）→ 不参与提交
        if item.get("resolution") not in (None, "", "none"):
            continue  # 已揭晓 → 跳过
        title = item.get("question")
        if not title:
            continue
        out.append(
            BenchQuestion(
                id=str(qid),
                title=title,
                closes_at=_parse_close_datetime(item, due),
                resolved=False,
                outcome=None,
                category=item.get("source") or "forecastbench",
            )
        )
        if len(out) >= limit:
            break
    return out


def fetch_open_questions(
    limit: int = 20, *, seed_dir: str | Path = SEED_DIR, timeout: float = 30.0, _transport=None
) -> list[BenchQuestion]:
    """拉取官方当前未解决题（resolved=False）。

    主路径：本地 data/fb_seed/ 最新一期 question set（离线可用）。
    本地缺失/为空 → 尝试 GitHub raw（QUESTION_SET_RAW_URL，TODO 复核）；测试用
    _transport mock。两者都失败抛 LLMError。
    """
    local = _latest_seed_file(seed_dir)
    if local is not None:
        try:
            raw = json.loads(local.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise LLMError(f"question set 解析失败 {local}: {e}") from e
        return _parse_question_set(raw, limit=limit)

    url = QUESTION_SET_RAW_URL.replace("<name>", "latest")
    try:
        with httpx.Client(transport=_transport, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise LLMError(f"官方 question set 拉取失败（本地 data/fb_seed/ 亦缺失）: {e}") from e
    return _parse_question_set(raw, limit=limit)


# ---- 提交预测（生成 Forecast Set JSON；上传需先邮件注册）----
def _infer_due_date() -> str:
    """从最新一期 question set 取 forecast_due_date；没有则用今天（占位）。"""
    try:
        raw = json.loads(_latest_seed_file(SEED_DIR).read_text(encoding="utf-8"))
        if raw.get("forecast_due_date"):
            return str(raw["forecast_due_date"])
    except Exception:
        pass
    return datetime.now(UTC).date().isoformat()


def _try_upload_gcs(path: Path) -> None:
    """配置了 FORECASTBENCH_GCS_BUCKET 且有 gcloud 时尝试上传（best-effort，失败仅告警）。

    TODO: 邮件注册拿到 bucket 后配置 .env；上传命令/路径按官方回信为准。
    """
    bucket = os.environ.get(GCS_BUCKET_ENV, "")
    gcloud = shutil.which("gcloud")
    if not bucket or not gcloud:
        return
    try:
        subprocess.run(
            [gcloud, "storage", "cp", str(path), f"gs://{bucket}/"],
            check=True,
            capture_output=True,
            timeout=300,
        )
        print(f"[fb_submit] 已上传 {path.name} → gs://{bucket}/")
    except Exception as e:
        print(f"[fb_submit] gcloud 上传失败（不影响本地落盘）: {e}", file=sys.stderr)


def submit_predictions(
    predictions: list[dict],
    *,
    api_token: str,
    forecast_due_date: str | None = None,
    out_dir: str | Path = FORECAST_SET_DIR,
) -> int:
    """生成 Forecast Set（id→probability 映射）落盘 data/forecast_sets/，返回条数。

    predictions: [{"question_id" 或 "id": <官方题 id>, "probability": <float>}, ...]
    api_token 为计划固定签名预留（官方通道无 token 机制，忽略）。
    """
    entries: list[dict] = []
    for p in predictions:
        try:
            qid = p.get("question_id", p.get("id"))
            prob = float(p["probability"])
        except (KeyError, TypeError, ValueError) as e:
            raise LLMError(f"预测条目字段缺失/非法: {p!r} ({e})") from e
        if qid in (None, ""):
            raise LLMError(f"预测条目缺 id: {p!r}")
        entries.append({"id": str(qid), "probability": max(0.0, min(1.0, prob))})
    if not entries:
        return 0

    due = forecast_due_date or _infer_due_date()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fname = f"{due}_forecast_set.json"
    payload = {"forecast_due_date": due, "forecast_set": fname, "forecasts": entries}
    path = out / fname
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _try_upload_gcs(path)
    return len(entries)


# ---- 本地记账（append-only JSON ledger）----
def load_ledger(path: str | Path = LEDGER_DEFAULT_PATH) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def record_submissions(entries: list[dict], *, path: str | Path = LEDGER_DEFAULT_PATH) -> int:
    """把提交记录追加进 ledger；精确重复（同题号+同概率）去重。

    条目示例：
      {"question_id": "123", "local_question_id": 5, "title": "...",
       "probability": 0.7, "closes_at": "...", "submitted_at": "...",
       "source": "forecastbench-official", "resolved": False, "outcome": None}
    返回实际写入条数。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = load_ledger(p)
    seen = {
        (str(e.get("question_id")), round(float(e["probability"]), 6))
        for e in existing
        if e.get("question_id") and e.get("probability") is not None
    }
    added = []
    for e in entries:
        key = (str(e.get("question_id")), round(float(e["probability"]), 6))
        if key in seen:
            continue
        seen.add(key)
        added.append(e)
    if added:
        p.write_text(json.dumps(existing + added, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(added)
