"""DuckDB 存储层。所有表 append-only 语义：只插入，resolve 只回填 outcome/brier。
Storage 是唯一数据访问入口，未来换 PG 时实现同接口即可。"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb


@dataclass
class Question:
    id: int
    title: str
    opens_at: datetime
    closes_at: datetime
    outcome: bool | None
    resolved_at: datetime | None
    is_public: bool


class Storage:
    def __init__(self, db_path: str, *, read_only: bool = False):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(db_path, read_only=read_only)
        self._path = db_path

    def close(self) -> None:
        self._conn.close()

    @property
    def path(self) -> str:
        """DB 文件路径（evolve.py 输出落盘目录跟随 DB：测试库 → tmp，生产 → data/）。"""
        return self._path

    def create_schema(self) -> None:
        self._conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS seq_questions START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_predictions START 1;
            CREATE SEQUENCE IF NOT EXISTS seq_documents START 1;
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_questions'),
                title TEXT NOT NULL,
                opens_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closes_at TIMESTAMP NOT NULL,
                outcome_type TEXT NOT NULL DEFAULT 'binary',
                outcome BOOLEAN,
                resolved_at TIMESTAMP,
                resolution_source TEXT,
                is_public BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_predictions'),
                question_id INTEGER NOT NULL REFERENCES questions(id),
                probability DOUBLE NOT NULL CHECK (probability BETWEEN 0.01 AND 0.99),
                brier_score DOUBLE,
                evidence_ids JSON NOT NULL,
                model_runs JSON,
                arm TEXT NOT NULL DEFAULT 'baseline',
                arm_group INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS source_documents (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_documents'),
                question_id INTEGER REFERENCES questions(id),
                source TEXT NOT NULL,
                url TEXT,
                title TEXT,
                content TEXT,
                published_at TIMESTAMP,
                fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS model_stats (
                model_name TEXT PRIMARY KEY,
                predictions INTEGER NOT NULL DEFAULT 0,
                brier_ema DOUBLE,
                last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- ---- 自我进化闭环：列扩展（幂等 ALTER，旧库迁移也走这里）----
            -- 注意：DuckDB 不支持 ADD COLUMN 带 NOT NULL 约束（ParserException），
            -- 旧库 arm 列退化为 DEFAULT 'baseline'（ALTER 自动回填存量行）+ 迁移脚本兜底 UPDATE。
            ALTER TABLE questions ADD COLUMN IF NOT EXISTS resolution_class TEXT;
            ALTER TABLE questions ADD COLUMN IF NOT EXISTS resolution_spec JSON;
            ALTER TABLE predictions ADD COLUMN IF NOT EXISTS arm TEXT DEFAULT 'baseline';
            ALTER TABLE predictions ADD COLUMN IF NOT EXISTS arm_group INTEGER;
            -- ---- 自我进化闭环：5 张新表 ----
            CREATE TABLE IF NOT EXISTS curated_documents (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_documents'),
                document_id INTEGER REFERENCES source_documents(id),
                value_score DOUBLE NOT NULL,
                impact_dir TEXT,              -- support/against/neutral
                impact_strength INTEGER,
                curated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS attributions (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_predictions'),
                question_id INTEGER NOT NULL REFERENCES questions(id),
                outcome_match BOOLEAN,
                primary_cause TEXT,           -- info_gap/misread/reasoning/base_rate/randomness/calibration/question_defect
                cause_detail TEXT,
                key_missed_info TEXT,
                key_misjudged TEXT,
                counterfactual TEXT,
                confidence DOUBLE,
                self_check TEXT,              -- JSON: {unavailable_info_used, post_hoc_bias, base_rate_ignored}
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_predictions'),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                question_type TEXT,
                question_id INTEGER REFERENCES questions(id),
                precondition TEXT,
                prediction_summary TEXT,
                outcome BOOLEAN,
                attribution TEXT,
                confidence DOUBLE,
                scope TEXT,
                status TEXT NOT NULL DEFAULT 'draft',  -- draft/validating/validated/retired
                testable_criteria TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0,
                misses INTEGER NOT NULL DEFAULT 0,
                evidence_refs TEXT,           -- JSON list of curated_document ids
                retired_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS lever_registry (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_predictions'),
                lever_key TEXT NOT NULL UNIQUE,
                lever_type TEXT NOT NULL,     -- prior_offset / postprocess_offset / allocation
                status TEXT NOT NULL DEFAULT 'candidate',  -- candidate/validating/active/retired
                effect_size DOUBLE,
                n_validated INTEGER NOT NULL DEFAULT 0,
                threshold_n INTEGER NOT NULL DEFAULT 500,
                threshold_delta DOUBLE NOT NULL DEFAULT 0.015,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                activated_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS evolution_log (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_predictions'),
                ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT NOT NULL,     -- resolution_failed/freeze/monitor_alarm/lever_promoted/lever_retired
                detail TEXT
            );
        """)

    # ---- questions ----
    def add_question(
        self,
        title: str,
        closes_at: datetime,
        *,
        opens_at: datetime | None = None,
        outcome_type: str = "binary",
        is_public: bool = True,
        resolution_class: str | None = None,
        resolution_spec: dict | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO questions (title, opens_at, closes_at, outcome_type, is_public, "
            "resolution_class, resolution_spec) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            [
                title,
                opens_at or datetime.now(),
                closes_at,
                outcome_type,
                is_public,
                resolution_class,
                json.dumps(resolution_spec) if resolution_spec else None,
            ],
        )
        return cur.fetchone()[0]

    def get_question(self, question_id: int) -> Question:
        row = self._conn.execute(
            "SELECT id, title, opens_at, closes_at, outcome, resolved_at, is_public "
            "FROM questions WHERE id = ?",
            [question_id],
        ).fetchone()
        if row is None:
            raise KeyError(question_id)
        return Question(*row)

    def list_open_questions(self, *, by: datetime | None = None) -> list[Question]:
        """已到期且未揭晓的题（当前无调用方，预留揭晓流程用）。"""
        rows = self._conn.execute(
            "SELECT id, title, opens_at, closes_at, outcome, resolved_at, is_public "
            "FROM questions WHERE outcome IS NULL AND closes_at <= ?",
            [by or datetime.now()],
        ).fetchall()
        return [Question(*r) for r in rows]

    def list_unresolved(self) -> list[Question]:
        rows = self._conn.execute(
            "SELECT id, title, opens_at, closes_at, outcome, resolved_at, is_public "
            "FROM questions WHERE outcome IS NULL ORDER BY closes_at"
        ).fetchall()
        return [Question(*r) for r in rows]

    def resolve_question(
        self, question_id: int, outcome: bool, resolution_source: str, *, force_score: bool = False
    ) -> None:
        self._conn.execute(
            "UPDATE questions SET outcome = ?, resolved_at = CURRENT_TIMESTAMP, "
            "resolution_source = ? WHERE id = ?",
            [outcome, resolution_source, question_id],
        )
        # 延迟归档（spec §5"延迟 >7 天 → 独立归档，不进技能桶"）：人工延迟揭晓的题
        # （resolved_at - closes_at > 7 天）不写 brier_score——题保留在库中但无战绩，
        # 天然不进技能桶（brier_by_horizon_bucket 只计 brier_score IS NOT NULL）。
        # force_score 仅供回测类调试脚本（compare_backtest 历史回填题，is_public=False
        # 本就不进公开战绩）豁免；生产揭晓路径（auto_resolve / resolve CLI）不传。
        if not force_score:
            row = self._conn.execute(
                "SELECT (CURRENT_TIMESTAMP - closes_at) > INTERVAL 7 DAY "
                "FROM questions WHERE id = ?",
                [question_id],
            ).fetchone()
            if row and row[0]:
                return
        # 回填 Brier——只给最后一条生产臂预测计分（预测可更新：同一题多行预测时
        # 旧行作废，否则 Brier 会重复计入战绩）。生产臂 = baseline（classic 回测）
        # + websearch（生产轮，daily/evolve/pm 题统一入口）。臂 experiment 不计入
        # 对外战绩：predict_round 先写臂 A 后写臂 B，一旦 P1 注册候选杠杆，最后一条
        # 恒是实验臂，不隔离会把实验臂当战绩污染 scoreboard，且配对 ΔBrier 地基缺失
        try:
            self._conn.execute(
                "UPDATE predictions SET brier_score = (probability - ?) * (probability - ?) "
                "WHERE id = (SELECT id FROM predictions WHERE question_id = ? "
                "             AND brier_score IS NULL AND arm IN ('baseline', 'websearch') "
                "             ORDER BY created_at DESC, id DESC LIMIT 1)",
                [int(outcome), int(outcome), question_id],
            )
            # 在线权重（EMA α=0.1）：model_stats 不再闲置，按 model_runs 里的模型名逐行 upsert
            rows = self._conn.execute(
                "SELECT model_runs, brier_score FROM predictions "
                "WHERE question_id = ? AND brier_score IS NOT NULL",
                [question_id],
            ).fetchall()
            for mr_json, brier in rows:
                for mname in json.loads(mr_json or "{}"):
                    self._conn.execute(
                        "INSERT INTO model_stats (model_name, predictions, brier_ema, last_updated) "
                        "VALUES (?, 1, ?, CURRENT_TIMESTAMP) "
                        "ON CONFLICT (model_name) DO UPDATE SET "
                        "predictions = model_stats.predictions + 1, "
                        "brier_ema = (model_stats.brier_ema * 9 + excluded.brier_ema) / 10, "
                        "last_updated = now()",
                        [mname, brier],
                    )
        except Exception as e:
            # 计分段异常（如 model_runs JSON 损坏）→ 不向上抛：outcome 已落库（题已揭晓），
            # 抛异常会让揭晓轮崩溃且重跑时该题被跳过、brier 永久缺失；记日志暴露残缺，
            # 人工可后补（resolutions 流程兜底）。DB 级错误（磁盘满等）同样只降级本题。
            try:
                self._conn.execute(
                    "INSERT INTO evolution_log (event_type, detail) VALUES (?, ?)",
                    [
                        "resolution_brier_failed",
                        json.dumps(
                            {"qid": question_id, "detail": f"brier/model_stats write failed: {e}"},
                            ensure_ascii=False,
                        ),
                    ],
                )
            except Exception:
                pass

    # ---- predictions ----
    def add_prediction(
        self,
        question_id: int,
        probability: float,
        *,
        evidence_ids: list[int],
        model_runs: dict,
        arm: str = "baseline",
        arm_group: int | None = None,
    ) -> int:
        if not evidence_ids:
            raise ValueError("evidence_ids 不能为空：预测必须可溯源")
        probability = max(0.01, min(0.99, probability))
        cur = self._conn.execute(
            "INSERT INTO predictions (question_id, probability, evidence_ids, model_runs, "
            "arm, arm_group) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            [
                question_id,
                probability,
                json.dumps(evidence_ids),
                json.dumps(model_runs),
                arm,
                arm_group,
            ],
        )
        return cur.fetchone()[0]

    # ---- documents（append-only）----
    def add_document(
        self,
        question_id: int,
        source: str,
        url: str,
        title: str,
        content: str,
        *,
        published_at: datetime | None,
        fetched_at: datetime | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO source_documents (question_id, source, url, title, content, "
            "published_at, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            [question_id, source, url, title, content, published_at, fetched_at or datetime.now()],
        )
        return cur.fetchone()[0]

    # ---- 计分口径（技能桶只用 latest）----
    def _brier_latest_id(self, question_id: int):
        row = self._conn.execute(
            "SELECT id FROM predictions WHERE question_id = ? AND brier_score IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            [question_id],
        ).fetchone()
        return row[0] if row else None

    def brier_latest(self, question_id: int) -> float | None:
        # 技能桶口径：resolve 只给最后一条存了 brier_score（现有语义），直接读存储值
        row = self._conn.execute(
            "SELECT brier_score FROM predictions WHERE question_id = ? AND brier_score IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            [question_id],
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def _question_outcome(self, question_id: int) -> bool | None:
        row = self._conn.execute(
            "SELECT outcome FROM questions WHERE id = ?", [question_id]
        ).fetchone()
        return row[0] if row else None

    def brier_first(self, question_id: int) -> float | None:
        # 首条预测的 Brier：用概率现算（首条没有存储的 brier_score——resolve 只给最后一条计分）
        outcome = self._question_outcome(question_id)
        if outcome is None:
            return None
        row = self._conn.execute(
            "SELECT probability FROM predictions WHERE question_id = ? "
            "ORDER BY created_at ASC, id ASC LIMIT 1",
            [question_id],
        ).fetchone()
        if not row:
            return None
        return (row[0] - int(outcome)) ** 2

    def brier_avg(self, question_id: int) -> float | None:
        # 全部预测的平均 Brier（口径报告用，不进技能桶）
        outcome = self._question_outcome(question_id)
        if outcome is None:
            return None
        rows = self._conn.execute(
            "SELECT probability FROM predictions WHERE question_id = ?", [question_id]
        ).fetchall()
        if not rows:
            return None
        return sum((p - int(outcome)) ** 2 for (p,) in rows) / len(rows)

    def brier_question_both(self, question_id: int) -> dict:
        return {"first": self.brier_first(question_id), "latest": self.brier_latest(question_id)}

    # ---- 揭晓规格与进化日志（Task 4/7 消费）----
    def set_resolution(
        self, question_id: int, resolution_class: str, resolution_spec: dict | None
    ) -> None:
        self._conn.execute(
            "UPDATE questions SET resolution_class = ?, resolution_spec = ? WHERE id = ?",
            [
                resolution_class,
                json.dumps(resolution_spec) if resolution_spec else None,
                question_id,
            ],
        )

    def question_resolution(self, question_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT resolution_spec FROM questions WHERE id = ?", [question_id]
        ).fetchone()
        if not row or not row[0]:
            return None
        return json.loads(row[0])

    def log_evolution(self, event_type: str, detail: str) -> None:
        self._conn.execute(
            "INSERT INTO evolution_log (event_type, detail) VALUES (?, ?)", [event_type, detail]
        )

    def count_predictions(self, question_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE question_id = ?", [question_id]
        ).fetchone()
        return int(row[0]) if row else 0

    def list_events(
        self, *, types: list[str] | None = None, limit: int = 200, before_id: int | None = None
    ) -> list[dict]:
        """事件流（id 倒序游标分页）：types 过滤、before_id 取更早事件。"""
        where, params = [], []
        if types:
            where.append("event_type IN (" + ",".join("?" for _ in types) + ")")
            params.extend(types)
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        sql = (
            "SELECT id, ts, event_type, detail FROM evolution_log"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)
        return self._rows_to_dicts(
            self._conn.execute(sql, params).fetchall(), ["id", "ts", "event_type", "detail"]
        )

    def ops_backlog(self, now: datetime) -> dict:
        """积压事实：A/B 类到期且超出 closes+grace 仍未降级（揭晓轮失能信号；
        2026-08-20 起 B 类纳入——B 类 LLM 揭晓失败同样依赖 16:30 轮宽限降级，
        只盯 A 会漏掉 #9 这类挂起）；未揭晓且无预测的死题 id 列表。
        grace_days 非数值回退 3 天（与 evolve 同语义）。"""
        past_grace_a = 0
        for q in self.list_open_questions(by=now):
            try:
                spec = self.question_resolution(q.id)
            except Exception:
                continue
            if spec is None or spec.get("class") not in ("A", "B"):
                continue
            try:
                grace = int(spec.get("grace_days", 3))
            except (TypeError, ValueError):
                grace = 3
            if now > q.closes_at + timedelta(days=grace):
                past_grace_a += 1
        dead = [q.id for q in self.list_unresolved() if self.count_predictions(q.id) == 0]
        return {"past_grace_a": past_grace_a, "dead_ids": dead}

    def ops_storm(self, now: datetime) -> int:
        """未来 48h 内将触发 7 天更新重预测的未到期题数（与 7 天规则同口径的日历天）。"""
        n = 0
        for q in self.list_unresolved():
            if q.closes_at <= now:
                continue
            last = self.last_prediction_at(q.id)
            if last is not None and 5 <= (now - last).days < 7:
                n += 1
        return n

    def brier_by_horizon_bucket(self) -> list[dict]:
        """按揭晓前天数分桶统计已揭晓公开题战绩。n<30 标 unreliable。"""
        rows = self._conn.execute("""
            WITH p AS (
                SELECT p.brier_score AS b,
                       date_diff('day', p.created_at, q.closes_at) AS horizon
                FROM predictions p JOIN questions q ON q.id = p.question_id
                WHERE p.brier_score IS NOT NULL AND q.is_public AND q.outcome IS NOT NULL
            )
            SELECT
                CASE
                    WHEN horizon <= 7 THEN '<=7'
                    WHEN horizon <= 30 THEN '7-30'
                    WHEN horizon <= 90 THEN '30-90'
                    ELSE '>=90'
                END AS bucket,
                COUNT(*) AS n,
                AVG(b) AS brier_mean
            FROM p GROUP BY 1 ORDER BY 1
        """).fetchall()
        out = []
        for bucket, n, brier_mean in rows:
            out.append(
                {
                    "bucket": bucket,
                    "n": int(n),
                    "brier_mean": float(brier_mean),
                    "unreliable": n < 30,
                }
            )
        return out

    def model_stats(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT model_name, predictions, brier_ema, last_updated "
            "FROM model_stats ORDER BY model_name"
        ).fetchall()
        return [
            {
                "model_name": m,
                "predictions": int(n),
                "brier_ema": float(e) if e is not None else None,
                "last_updated": u,
            }
            for m, n, e, u in rows
        ]

    def calibration_pairs(self) -> list[tuple[float, bool]]:
        """已揭晓题最后一条预测的 (probability, outcome) 对，供校准器 fit。
        口径=「系统最终输出」：每道题取最后一条预测（不限 arm——生产臂 websearch
        的预测行 brier_score 恒 NULL，按计分行口径会永远学不到生产分布），
        与揭晓 outcome 配对；未揭晓题（outcome IS NULL）不进。"""
        rows = self._conn.execute(
            "SELECT p.probability, q.outcome FROM predictions p "
            "JOIN questions q ON p.question_id = q.id "
            "WHERE q.outcome IS NOT NULL AND p.id = ("
            "  SELECT p2.id FROM predictions p2 WHERE p2.question_id = p.question_id "
            "  ORDER BY p2.created_at DESC, p2.id DESC LIMIT 1)"
        ).fetchall()
        return [(float(p), bool(o)) for p, o in rows]

    def source_market_ids(self, source: str) -> set[str]:
        """指定来源（resolution_spec.source）已入库的 market_id 集合，供拉题判重。"""
        rows = self._conn.execute(
            "SELECT resolution_spec FROM questions WHERE resolution_spec IS NOT NULL"
        ).fetchall()
        ids: set[str] = set()
        for (raw,) in rows:
            try:
                spec = json.loads(raw)
            except Exception:
                continue
            if isinstance(spec, dict) and spec.get("source") == source and spec.get("market_id"):
                ids.add(str(spec["market_id"]))
        return ids

    def source_question_ids(self, source: str) -> list[int]:
        """指定来源（resolution_spec.source）的全部题 id，供揭晓轮枚举。"""
        rows = self._conn.execute(
            "SELECT id, resolution_spec FROM questions WHERE resolution_spec IS NOT NULL"
        ).fetchall()
        out: list[int] = []
        for qid, raw in rows:
            try:
                spec = json.loads(raw)
            except Exception:
                continue
            if isinstance(spec, dict) and spec.get("source") == source:
                out.append(int(qid))
        return out

    def last_prediction_at(self, question_id: int) -> datetime | None:
        row = self._conn.execute(
            "SELECT MAX(created_at) FROM predictions WHERE question_id = ?", [question_id]
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    # ---- 只读展示查询（web 前端读库用；接口见 Task 1 brief Interface 节）----

    @staticmethod
    def _rows_to_dicts(rows, columns) -> list[dict]:
        return [dict(zip(columns, row)) for row in rows]

    def list_questions_all(
        self, *, resolution_class=None, status=None, arm=None, q=None, now=None
    ) -> list[dict]:
        now = now or datetime.now()
        where, params = [], []
        if resolution_class:
            where.append("q.resolution_class = ?")
            params.append(resolution_class)
        if arm:
            where.append("p.arm = ?")
            params.append(arm)
        if q:
            where.append("q.title ILIKE ?")
            params.append(f"%{q}%")
        if status == "open":
            where.append("q.outcome IS NULL AND q.closes_at > ?")
            params.append(now)
        elif status == "pending":
            where.append("q.outcome IS NULL AND q.closes_at <= ?")
            params.append(now)
        elif status == "resolved":
            where.append("q.outcome IS NOT NULL")
        sql = f"""
            SELECT q.id, q.title, q.opens_at, q.closes_at, q.outcome_type, q.outcome,
                   q.resolved_at, q.resolution_class,
                   p.probability, p.brier_score, p.arm
            FROM questions q
            LEFT JOIN (
                SELECT p.question_id, p.probability, p.brier_score, p.arm
                FROM predictions p
                WHERE p.id = (SELECT MAX(p2.id) FROM predictions p2
                              WHERE p2.question_id = p.question_id)
            ) p ON p.question_id = q.id
            {("WHERE " + " AND ".join(where)) if where else ""}
            ORDER BY q.closes_at
        """
        cols = [
            "id",
            "title",
            "opens_at",
            "closes_at",
            "outcome_type",
            "outcome",
            "resolved_at",
            "resolution_class",
            "probability",
            "brier_score",
            "arm",
        ]
        out = []
        for r in self._rows_to_dicts(self._conn.execute(sql, params).fetchall(), cols):
            if r["outcome"] is not None:
                r["status"] = "resolved"
            elif r["closes_at"] > now:
                r["status"] = "open"
            else:
                r["status"] = "pending"
            out.append(r)
        return out

    def get_question_detail(self, question_id: int, *, now: datetime | None = None) -> dict | None:
        row = self._conn.execute(
            """
            SELECT q.id, q.title, q.opens_at, q.closes_at, q.outcome_type, q.outcome,
                   q.resolved_at, q.resolution_source, q.resolution_class, q.resolution_spec,
                   p.prediction_id, p.probability, p.brier_score, p.evidence_ids,
                   p.model_runs, p.arm, p.arm_group, p.prediction_created_at
            FROM questions q
            LEFT JOIN (
                SELECT p.question_id, p.id AS prediction_id, p.probability, p.brier_score,
                       p.evidence_ids, p.model_runs, p.arm, p.arm_group,
                       p.created_at AS prediction_created_at
                FROM predictions p
                WHERE p.id = (SELECT MAX(p2.id) FROM predictions p2
                              WHERE p2.question_id = p.question_id)
            ) p ON p.question_id = q.id
            WHERE q.id = ?
        """,
            [question_id],
        ).fetchone()
        if row is None:
            return None
        cols = [
            "id",
            "title",
            "opens_at",
            "closes_at",
            "outcome_type",
            "outcome",
            "resolved_at",
            "resolution_source",
            "resolution_class",
            "resolution_spec",
            "prediction_id",
            "probability",
            "brier_score",
            "evidence_ids",
            "model_runs",
            "arm",
            "arm_group",
            "prediction_created_at",
        ]
        d = dict(zip(cols, row))
        # 与 list_questions_all 同款 status 派生（Task 8 浏览器手测发现：前端详情弹层
        # badgeStatus(d.status) 依赖该字段，缺失会渲染字面 "undefined"）
        now = now or datetime.now()
        if d["outcome"] is not None:
            d["status"] = "resolved"
        elif d["closes_at"] > now:
            d["status"] = "open"
        else:
            d["status"] = "pending"
        for k in ("resolution_spec", "evidence_ids", "model_runs"):
            if isinstance(d.get(k), str):
                d[k] = json.loads(d[k])
        return d

    def list_question_documents(self, question_id: int) -> list[dict]:
        cols = ["id", "source", "url", "title", "published_at", "fetched_at"]
        return self._rows_to_dicts(
            self._conn.execute(
                """
            SELECT id, source, url, title, published_at, fetched_at
            FROM source_documents WHERE question_id = ?
            ORDER BY published_at DESC NULLS LAST
        """,
                [question_id],
            ).fetchall(),
            cols,
        )

    def list_levers(self) -> list[dict]:
        cols = [
            "id",
            "lever_key",
            "lever_type",
            "status",
            "effect_size",
            "n_validated",
            "threshold_n",
            "threshold_delta",
            "created_at",
            "activated_at",
        ]
        return self._rows_to_dicts(
            self._conn.execute(
                "SELECT id, lever_key, lever_type, status, effect_size, n_validated, "
                "threshold_n, threshold_delta, created_at, activated_at "
                "FROM lever_registry ORDER BY id"
            ).fetchall(),
            cols,
        )

    def list_lessons(self) -> list[dict]:
        cols = [
            "id",
            "created_at",
            "question_type",
            "question_id",
            "precondition",
            "prediction_summary",
            "outcome",
            "attribution",
            "confidence",
            "scope",
            "status",
            "testable_criteria",
            "hits",
            "misses",
            "evidence_refs",
            "retired_at",
        ]
        return self._rows_to_dicts(
            self._conn.execute(
                "SELECT id, created_at, question_type, question_id, precondition, "
                "prediction_summary, outcome, attribution, confidence, scope, status, "
                "testable_criteria, hits, misses, evidence_refs, retired_at "
                "FROM lessons ORDER BY created_at DESC"
            ).fetchall(),
            cols,
        )

    def list_evolution_log(self) -> list[dict]:
        cols = ["id", "ts", "event_type", "detail"]
        return self._rows_to_dicts(
            self._conn.execute(
                "SELECT id, ts, event_type, detail FROM evolution_log ORDER BY ts DESC"
            ).fetchall(),
            cols,
        )

    def arm_stats(self) -> list[dict]:
        cols = ["arm", "n", "resolved", "brier_mean"]
        return self._rows_to_dicts(
            self._conn.execute("""
            SELECT p.arm, COUNT(*) AS n,
                   COUNT(DISTINCT CASE WHEN q.outcome IS NOT NULL THEN q.id END) AS resolved,
                   AVG(CASE WHEN q.outcome IS NOT NULL THEN p.brier_score END) AS brier_mean
            FROM predictions p JOIN questions q ON q.id = p.question_id
            GROUP BY p.arm ORDER BY p.arm
        """).fetchall(),
            cols,
        )

    def scoreboard_summary(self) -> dict:
        # 对外战绩口径（与 list_resolved_public / brier_by_horizon_bucket 一致）：
        # ① 取每题「最后一条生产臂行」（baseline/websearch）——resolve 只给最后一条
        # 生产臂计分（实验臂行 brier_score 恒 NULL，latest 行 = 实验臂时会把实验臂当战绩）；
        # ② AND q.is_public——回测题（compare_backtest 写入）不进对外榜与计数。
        row = self._conn.execute("""
            SELECT COUNT(DISTINCT q.id) AS resolved,
                   AVG(CASE WHEN q.outcome IS NOT NULL THEN p.brier_score END) AS brier_mean,
                   MIN(q.resolved_at) AS first_resolved_at,
                   MAX(q.resolved_at) AS last_resolved_at
            FROM questions q LEFT JOIN (
                SELECT p.question_id, p.brier_score
                FROM predictions p
                WHERE p.arm IN ('baseline', 'websearch')
                  AND p.id = (SELECT MAX(p2.id) FROM predictions p2
                              WHERE p2.question_id = p.question_id
                                AND p2.arm IN ('baseline', 'websearch'))
            ) p ON p.question_id = q.id
            WHERE q.outcome IS NOT NULL AND q.is_public
        """).fetchone()
        d = dict(zip(["resolved", "brier_mean", "first_resolved_at", "last_resolved_at"], row))
        d["buckets"] = self.brier_by_horizon_bucket()
        return d

    def public_summary(self) -> dict:
        return self.scoreboard_summary()

    def list_resolved_public(self) -> list[dict]:
        # 对外榜口径（与 scoreboard_summary 一致）：取每题「最后一条生产臂行」
        # （baseline/websearch）+ AND q.is_public（回测题不进对外榜）。字段白名单不变。
        cols = ["id", "title", "closes_at", "probability", "outcome", "brier_score", "resolved_at"]
        return self._rows_to_dicts(
            self._conn.execute("""
            SELECT q.id, q.title, q.closes_at, p.probability, q.outcome,
                   p.brier_score, q.resolved_at
            FROM questions q LEFT JOIN (
                SELECT p.question_id, p.probability, p.brier_score
                FROM predictions p
                WHERE p.arm IN ('baseline', 'websearch')
                  AND p.id = (SELECT MAX(p2.id) FROM predictions p2
                              WHERE p2.question_id = p.question_id
                                AND p2.arm IN ('baseline', 'websearch'))
            ) p ON p.question_id = q.id
            WHERE q.outcome IS NOT NULL AND q.is_public
            ORDER BY q.resolved_at DESC
        """).fetchall(),
            cols,
        )
