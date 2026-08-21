"""scripts/ensure_signsrv.py — 微博热搜等社交签名服务的每小时幂等自检/拉起。

schtasks 每小时触发（替代原 LogonTrigger：机器久不重启即不再触发，8-14 被 Ctrl+C
终止后停摆 6 天）。逻辑：
  1. 检查 127.0.0.1:8989 是否已监听（app 绑定 0.0.0.0）→ 已监听则静默 exit 0（幂等）；
  2. 未监听 → DETACHED 拉起 data/mediacrawler-pro-signsrv/app.py（服务自带 .venv，
     Python 3.12，与本项目 .venv 3.13 分离），stdout/stderr 追加 signsrv.log。
本脚本只用标准库，任意 Python 均可运行（schtasks 动作用项目 .venv python）。
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PORT = 8989
SRV = Path(__file__).resolve().parents[1] / "data" / "mediacrawler-pro-signsrv"
# 不用 venv 的 pythonw.exe launcher：uv venv launcher 内部再 spawn 真实 python.exe
# （控制台子系统），我们传的 NO_WINDOW 不继承 → 每次拉起弹一个控制台窗口
# （2026-08-21 19:02 实锤 conhost 25204）。改用 base pythonw.exe（真 GUI 子系统，
# 物理上无控制台）+ PYTHONPATH 注入 venv site-packages。
_BASE = None
for _line in (SRV / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
    if _line.startswith("home"):
        _BASE = Path(_line.split("=", 1)[1].strip())
PY = _BASE / "pythonw.exe" if _BASE else SRV / ".venv" / "Scripts" / "pythonw.exe"
_SP = SRV / ".venv" / "Lib" / "site-packages"
_LOG = SRV / "signsrv.log"
_ENSURE_LOG = SRV / "signsrv-ensure.log"  # 独立日志：app.py 的 logging 会独占锁 signsrv.log


def _log_line(msg: str) -> None:
    """schtasks 用 pythonw 跑本脚本时 sys.stdout 是 None，print 会炸；统一写独立日志。"""
    from datetime import datetime

    try:
        with _ENSURE_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[ensure {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def listening(port: int) -> bool:
    """端口是否已监听（connect_ex 成功即有人监听）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pong_ok() -> bool:
    """深度校验：8989 上应答 /signsrv/pong 且 isok=true 的才是真 signsrv。

    只查端口会把「无关程序占用 8989」误判为服务存活（假成功），故占用但
    pong 不对 → 视为异常，exit 1 让任务记失败（可从 LastTaskResult 察觉）。
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/signsrv/pong", timeout=3) as r:
            return b'"isok":true' in r.read().lower()
    except Exception:
        return False


def _wait_up(timeout: float = 30.0) -> bool:
    """拉起后轮询端口，确认服务真的起来了（防 app.py 启动崩溃假成功）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if listening(PORT):
            return True
        time.sleep(1)
    return False


def main() -> int:
    if listening(PORT):
        if _pong_ok():
            _log_line(f"alive on {PORT} (pong ok), skip")
            return 0
        _log_line(f"port {PORT} occupied but /signsrv/pong wrong — NOT signsrv, failing")
        return 1
    if not PY.exists():
        _log_line(f"signsrv venv missing: {PY}")
        return 1
    flags = (
        0x00000008  # DETACHED_PROCESS：不继承父控制台
        | 0x00000200  # CREATE_NEW_PROCESS_GROUP
        | 0x08000000  # CREATE_NO_WINDOW：不弹窗
        | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB：跳出宿主 Job Object（Hermes 终端
        # 等宿主把子进程放 Job 里，宿主重启/回收会话时整树被杀——2026-08-21 18:05-18:57
        # 服务静默死亡即此嫌疑；breakaway 让服务不受宿主生命周期牵连）
    )
    env = dict(os.environ)
    # app.py banner 含 ⚠ 等非 GBK 字符；stdout 重定向到文件后 Python 走 locale 编码
    # (cp936) 会在 print 时 UnicodeEncodeError 崩溃（实测 traceback 在 signsrv.log）。
    env["PYTHONIOENCODING"] = "utf-8"
    # base pythonw 直跑时注入 venv site-packages（绕开 launcher 的等价替换）
    _pp = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(_SP) + (os.pathsep + _pp if _pp else "")
    with _LOG.open("ab") as log:
        subprocess.Popen(
            [str(PY), "app.py"],
            cwd=str(SRV),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
            env=env,
        )
    if not _wait_up():
        _log_line(f"failed to come up within 30s, see {_LOG}")
        return 1
    _log_line(f"started (port {PORT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
