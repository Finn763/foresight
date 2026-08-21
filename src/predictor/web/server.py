"""预测结果展示页 FastAPI 应用。两种模式：
- internal（默认）：全量 API + 静态前端
- public：仅 /api/public/* + 静态前端（内部端点不注册 → 404，对外泄漏面归零）

连接纪律（spec §3.1，实测必须）：每次请求新建 read_only 短连接、用完即关——
DuckDB 在 Windows 上排他文件访问，任何常驻连接都会阻塞每日 evolve 写轮。
"""

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from predictor.config import Settings
from predictor.data.storage import Storage

STATIC_DIR = Path(__file__).parent / "static"


def create_app(mode: str = "internal") -> FastAPI:
    app = FastAPI(title="Foresight", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mode = mode
    app.state.db_path = Settings().db_path  # scripts/web_server.py 的 --db 覆盖此处

    def db_dep():
        st = Storage(str(app.state.db_path), read_only=True)
        try:
            yield st
        finally:
            st.close()

    @app.exception_handler(Exception)
    async def db_busy_handler(request: Request, exc: Exception):
        # 写窗口（evolve 09:05/16:30 持锁）或 DB 缺失 → 503，前端横幅+重试
        return JSONResponse(status_code=503, content={"detail": "database busy or unavailable"})

    @app.get("/api/health")
    def health():
        # 探活必须就地转换失败：DB 锁/缺失触发全局 Exception 处理器返回 503，真实
        # uvicorn 下 503 能正常送达探针；仅 TestClient 默认 raise_server_exceptions=True
        # 会把处理器重抛的异常原样抛回调用方。故缺库/被锁直接返回 degraded，保证
        # 测试与探针行为一致（探针拿到 200 + degraded 即视为降级存活）。
        try:
            st = Storage(str(app.state.db_path), read_only=True)
            st.close()
            return {"status": "ok"}
        except Exception:
            return {"status": "degraded"}

    if mode == "internal":
        _register_internal(app, db_dep)
    _register_public(app, db_dep)  # spec §3.2：internal 模式挂载全部端点（?mode=public 视图可用）

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _register_internal(app: FastAPI, db_dep) -> None:
    """内部模式端点（public 模式不注册，天然 404）。"""
    from datetime import datetime

    from fastapi import Query

    @app.api_route("/api/questions", methods=["GET", "HEAD"])
    def list_questions(
        resolution_class: str | None = Query(default=None, alias="class"),
        status: str | None = None,
        arm: str | None = None,
        q: str | None = None,
        st: Storage = Depends(db_dep),
    ):
        return {
            "items": st.list_questions_all(
                resolution_class=resolution_class, status=status, arm=arm, q=q, now=datetime.now()
            )
        }

    @app.get("/api/questions/{question_id}")
    def question_detail(question_id: int, st: Storage = Depends(db_dep)):
        detail = st.get_question_detail(question_id)
        if detail is None:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        detail["documents"] = st.list_question_documents(question_id)
        return detail

    @app.get("/api/scoreboard")
    def scoreboard(st: Storage = Depends(db_dep)):
        return st.scoreboard_summary()

    @app.get("/api/system")
    def system(st: Storage = Depends(db_dep)):
        return {
            "levers": st.list_levers(),
            "lessons": st.list_lessons(),
            "evolution_log": st.list_evolution_log(),
            "model_stats": st.model_stats(),
            "arm_stats": st.arm_stats(),
        }

    from fastapi import BackgroundTasks

    from predictor.ops.facts import build_facts
    from predictor.ops.health import assess
    from predictor.ops.probes import get_probes, refresh_probes

    @app.get("/api/ops/log")
    def ops_log(
        types: str | None = None,
        limit: int = 200,
        before_id: int | None = None,
        st: Storage = Depends(db_dep),
    ):
        type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
        return {
            "items": st.list_events(
                types=type_list, limit=max(1, min(limit, 500)), before_id=before_id
            )
        }

    @app.get("/api/ops/health")
    def ops_health(st: Storage = Depends(db_dep)):
        now = datetime.now()
        facts = build_facts(st, now)
        facts["probes"] = get_probes()
        return assess(facts, now)

    @app.post("/api/ops/health/refresh")
    def ops_health_refresh(background_tasks: BackgroundTasks):
        background_tasks.add_task(refresh_probes)
        return JSONResponse(status_code=202, content={"status": "refreshing"})

    @app.get("/api/ops/log-files")
    def ops_log_files(name: str):
        if name not in ("daily", "evolve"):
            return JSONResponse(status_code=400, content={"detail": "unknown log name"})
        log_path = Path(app.state.db_path).parent / f"{name}.log"
        try:
            text = log_path.read_text(encoding="gbk", errors="replace")
        except OSError:
            return JSONResponse(status_code=404, content={"detail": "log file not found"})
        return {"name": name, "lines": text.splitlines()[-100:]}


def _register_public(app: FastAPI, db_dep) -> None:
    """对外模式端点。仅注册白名单端点，内部端点不注册 → 天然 404。"""
    from fastapi import Depends

    from predictor.data.storage import Storage

    @app.get("/api/public/summary")
    def public_summary(st: Storage = Depends(db_dep)):
        return st.public_summary()

    @app.get("/api/public/resolved")
    def public_resolved(st: Storage = Depends(db_dep)):
        return {"items": st.list_resolved_public()}
