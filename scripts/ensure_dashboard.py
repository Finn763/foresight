"""scripts/ensure_dashboard.py — 预测结果展示页(127.0.0.1:8765)的每 30 分钟幂等自检/拉起。

CC 评审 §3.4：8765 服务长期手工启动（8/25 17:36 起的控制台 python 进程），无 ensure
机制，机器重启即丢，与 signsrv 待遇不对称。本脚本仿照 ensure_signsrv.py 接入 ensure
体系（schtasks Foresight-Dashboard 每 30 分钟触发）。逻辑：
  1. 探测 127.0.0.1:8765 端口监听 + GET /api/health HTTP 200 → 已活则静默 exit 0（幂等）；
  2. 未监听 → DETACHED 拉起 base 解释器（pyvenv.cfg 的 home）+ sys.path 注入 venv
     site-packages/项目路径，执行 scripts/web_server.py；stdout/stderr 追加
     data/web_server.log。不能直接用 .venv/Scripts/python.exe：uv 的 venv 垫片在
     SYSTEM/session-0 任务里 CreateProcess 后以 0xC0000142（DLL init failed）退出；
  3. 端口被占但 /api/health 非 200 → 视为异常占用，exit 1 让 LastTaskResult 可见
     （与 ensure_signsrv 的「占用但 pong 不对即失败」同构）。
本脚本只用标准库，任意 Python 均可运行（schtasks 动作经 run_silent.py 用 base GUI
pythonw 执行，见 docs/hermes-fix-report-dashboard-guard-2026-08-27.md）。
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8765
PY = ROOT / ".venv" / "Scripts" / "python.exe"
SRV_SCRIPT = ROOT / "scripts" / "web_server.py"
_LOG = ROOT / "data" / "web_server.log"  # 服务 stdout/stderr（新增文件）
_ENSURE_LOG = ROOT / "data" / "dashboard-ensure.log"  # 独立自检日志（不入服务日志）


def _log_line(msg: str) -> None:
    """schtasks 用 pythonw 跑本脚本时 sys.stdout 是 None，print 会炸；统一写独立日志。"""
    from datetime import datetime

    try:
        with _ENSURE_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[ensure {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def listening(port: int = PORT) -> bool:
    """端口是否已监听（connect_ex 成功即有人监听）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _http_ok() -> bool:
    """深度校验：GET /api/health 返回 HTTP 200 才是真 dashboard。

    /api/health 在缺库/被锁时返回 200+degraded（server.py 刻意设计，503 只在异常
    路径），因此 200 即视为降级存活；非 200/超时 = 不是我们的服务。
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def probe() -> str:
    """探测结果三态：alive（活）/ occupied（被占用但不是 dashboard）/ down（未监听）。"""
    if not listening():
        return "down"
    return "alive" if _http_ok() else "occupied"


def _base_python() -> Path:
    """从 .venv/pyvenv.cfg 取 base 解释器路径（uv 的 venv 垫片在 SYSTEM 下 0xC0000142）。"""
    cfg = ROOT / ".venv" / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home":
                home = Path(value.strip())
                exe = home / ("python.exe" if os.name == "nt" else "python3")
                if exe.exists():
                    return exe
    except OSError:
        pass
    return PY


def _launch_argv(argv: list[str]) -> list[str]:
    """base 解释器 + sys.path 注入（等价 run_silent.py 的做法），再执行 web_server。"""
    sp = ROOT / ".venv" / "Lib" / "site-packages"
    if not sp.is_dir():
        sp = ROOT / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0, {str(sp)!r});"
        f"sys.path.insert(0, {str(ROOT / 'src')!r});"
        f"sys.path.insert(0, {str(ROOT)!r});"
        f"sys.argv = [{str(SRV_SCRIPT)!r}, *{argv!r}];"
        "import runpy;"
        f"runpy.run_path({str(SRV_SCRIPT)!r}, run_name='__main__')"
    )
    return [str(_base_python()), "-E", "-X", "utf8", "-c", bootstrap]


def launch(argv: list[str] | None = None) -> subprocess.Popen:
    """DETACHED 无窗拉起 web_server.py（base 解释器 + 注入 venv site-packages）。

    argv 透传（默认空 → web_server 默认 internal 模式 127.0.0.1:8765）；测试可用
    --port 换端口做隔离演练。返回 Popen 句柄供调用方管理（main 忽略之）。
    """
    breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB：跳出宿主 Job Object
    flags = (
        0x00000008  # DETACHED_PROCESS：不继承父控制台
        | 0x00000200  # CREATE_NEW_PROCESS_GROUP
        | 0x08000000  # CREATE_NO_WINDOW：不弹窗
        | breakaway
    )
    env = dict(os.environ)
    # 项目铁律：Hermes 等宿主注入的 PYTHONPATH 会搅炸 venv（pydantic_core 加载失败），
    # 必须移除；-E 同时忽略所有 PYTHON* 环境变量（双保险）。
    env.pop("PYTHONPATH", None)
    # 服务 banner/日志含中文，stdout 重定向到文件后走 locale 编码(cp936)会
    # UnicodeEncodeError 崩溃；-X utf8 为主，环境变量为兜底。
    env["PYTHONIOENCODING"] = "utf-8"
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = _launch_argv(argv or [])
    kwargs = dict(
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        close_fds=True,
        env=env,
    )
    with _LOG.open("ab") as log:
        try:
            return subprocess.Popen(cmd, stdout=log, stderr=log, creationflags=flags, **kwargs)
        except OSError as e:
            # 受限 Job 拒绝 breakaway（实测 WinError 5）时降级：留在宿主 Job 内，
            # 至少保证本轮拉起成功；任务宿主回收时子树可能被杀，由下一轮 ensure 续上。
            _log_line(f"launch without breakaway ({type(e).__name__}: {e})")
            return subprocess.Popen(
                cmd, stdout=log, stderr=log, creationflags=flags & ~breakaway, **kwargs
            )


def _wait_up(timeout: float = 30.0) -> bool:
    """拉起后轮询端口 + HTTP 200，确认服务真的起来了（防启动崩溃假成功）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if listening() and _http_ok():
            return True
        time.sleep(1)
    return False


def main(
    probe_fn=probe,
    launch_fn=launch,
    wait_fn=_wait_up,
    venv: Path | None = PY,
) -> int:
    state = probe_fn()
    if state == "alive":
        _log_line(f"alive on {PORT} (/api/health 200), skip")
        return 0
    if state == "occupied":
        _log_line(f"port {PORT} occupied but /api/health not 200 — NOT dashboard, failing")
        return 1
    if venv is None or not venv.exists():
        _log_line(f"venv python missing: {venv}")
        return 1
    launch_fn()
    if not wait_fn():
        _log_line(f"failed to come up within 30s, see {_LOG}")
        return 1
    _log_line(f"started (port {PORT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
