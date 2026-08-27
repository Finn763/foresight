"""预测结果展示页 FastAPI 应用。两种模式：
- internal（默认）：全量 API + 静态前端
- public：仅 /api/public/* + 静态前端（内部端点不注册 → 404，对外泄漏面归零）

连接纪律（spec §3.1，实测必须）：每次请求新建 read_only 短连接、用完即关——
DuckDB 在 Windows 上排他文件访问，任何常驻连接都会阻塞每日 evolve 写轮。
"""

from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from predictor.config import Settings
from predictor.data.storage import Storage

STATIC_DIR = Path(__file__).parent / "static"

_BANNER_MARKER = '<main id="app"></main>'


def _alerts_dir(db_path: str) -> Path:
    """告警目录：data/alerts/（与 health_check 落盘处一致）。"""
    return Path(db_path).parent / "alerts"


def _latest_unack_alert(alerts_dir: Path) -> dict | None:
    """最新未确认告警（评审 §3.5 告警消费）：文件名时间序取最后一条，跳过 .ack.md。

    解析标题/检出时间/告警要点；error 类文件（无「## 告警」小节）取检出时间后的
    首段描述作要点。任何读取失败返回 None（横幅缺席，不影响页面本身）。
    """
    try:
        files = [p for p in sorted(alerts_dir.glob("alert-*.md")) if not p.stem.endswith(".ack")]
    except OSError:
        return None
    if not files:
        return None
    f = files[-1]
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), f.name)
    detected = next(
        (ln.split("：", 1)[1].strip() for ln in lines if ln.startswith("检出时间：")), ""
    )
    items = []
    in_alerts = False
    for ln in lines:
        if ln == "## 告警":
            in_alerts = True
            continue
        if in_alerts:
            if ln.startswith("## "):
                break
            if ln.startswith("- "):
                items.append(ln[2:].strip())
    if not items:  # error 类文件：取检出时间后的首段描述
        started = False
        for ln in lines:
            if ln.startswith("检出时间："):
                started = True
                continue
            if started and ln.strip() and not ln.startswith("#"):
                items.append(ln.strip())
                break
    return {"file": f.name, "title": title, "detected": detected, "items": items}


def _render_alert_banner(alert: dict) -> str:
    """告警横幅 HTML（深色主题 inline 样式 + form POST 确认）。

    CSP（index.html script-src 'self'）禁 inline JS——确认按钮用原生 form POST
    /api/ops/alerts/ack（303 回首页），无需任何脚本；style-src 允许 inline style。
    """
    items = "".join(f"<li>{escape(i)}</li>" for i in alert["items"]) or (
        f"<li>{escape(alert['title'])}</li>"
    )
    return (
        f'<div id="alert-banner" role="alert" aria-label="健康告警" style="'
        f"background:#121722;border:1px solid #ff6b6b;border-radius:8px;"
        f"color:#e8ecf4;margin:10px 14px 0;padding:10px 14px;font-size:13px;"
        f'display:flex;gap:12px;align-items:flex-start">'
        f'<div style="flex:1;min-width:0">'
        f'<div style="color:#ff6b6b;font-weight:600;margin-bottom:4px">'
        f"⚠ {escape(alert['title'])}"
        f'<span style="color:#96a0b5;font-weight:400;margin-left:8px">'
        f"{escape(alert['detected'])}</span></div>"
        f'<ul style="margin:0;padding-left:18px">{items}</ul></div>'
        f'<form method="post" action="/api/ops/alerts/ack" style="margin:0">'
        f'<button type="submit" title="确认已处理" style="background:#ff6b6b;color:#0a0d14;'
        f'border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">'
        f"确认</button></form></div>"
    )


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
        html_path = STATIC_DIR / "index.html"
        # 告警横幅只注入 internal 模式（public 战绩榜不暴露内部运维信息）
        if app.state.mode != "internal":
            return FileResponse(html_path)
        try:
            html = html_path.read_text(encoding="utf-8")
        except OSError:
            return FileResponse(html_path)
        alert = _latest_unack_alert(_alerts_dir(app.state.db_path))
        if alert is None:
            return HTMLResponse(html)
        html = html.replace(_BANNER_MARKER, _render_alert_banner(alert) + _BANNER_MARKER, 1)
        return HTMLResponse(html)

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
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return JSONResponse(status_code=404, content={"detail": "log file not found"})
        return {"name": name, "lines": text.splitlines()[-100:]}

    @app.get("/api/ops/alerts")
    def ops_alerts():
        """最新未确认告警（评审 §3.5）：首页横幅数据源，无告警时 latest=null。"""
        return {"latest": _latest_unack_alert(_alerts_dir(app.state.db_path))}

    @app.post("/api/ops/alerts/ack")
    def ops_alerts_ack(origin: str | None = Header(default=None),
                       host: str | None = Header(default=None)):
        """确认告警：最新未确认告警文件改名 .ack.md（退出横幅），303 回首页。

        CSRF 防护（评审新问题#3）：浏览器跨源表单会带 Origin——存在且主机与
        本请求 Host 不一致时拒绝；无 Origin 的本地调用（curl/同源浏览器）放行。
        """
        if origin:
            oh = urlsplit(origin).hostname
            rh = (host or "").split(":")[0]
            if oh and rh and oh != rh:
                return JSONResponse(status_code=403, content={"detail": "cross-origin ack rejected"})
        info = _latest_unack_alert(_alerts_dir(app.state.db_path))
        if info is not None:
            f = _alerts_dir(app.state.db_path) / info["file"]
            acked = f.with_name(f.name[: -len(".md")] + ".ack.md")
            try:
                f.replace(acked)
            except OSError:
                pass
        return RedirectResponse("/", status_code=303)


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
