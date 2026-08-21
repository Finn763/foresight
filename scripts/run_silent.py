"""scripts/run_silent.py — schtasks 任务的 pythonw 包装器（消灭定时任务弹窗）。

背景：Win11 默认终端委托设为 Windows Terminal 后，schtasks 用 cmd/python.exe
（控制台子系统）跑任务会弹可见 WT 窗口。改用 pythonw.exe 直跑则无控制台，
但 pythonw 下 sys.stdout/stderr 是 None，脚本 print 会炸。

用法（schtasks action，WorkingDirectory 设项目根）：
  pythonw.exe scripts\\run_silent.py scripts\\daily.py data\\daily.log [透传参数...]

行为：stdout/stderr 为 None 时重定向到指定日志（append, utf-8），再以 __main__
执行目标脚本（argv 透传）。日志句柄已存在时（交互调试）原样保留。
"""

import runpy
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: run_silent.py target.py [log] [args...]", file=sys.stderr)
        return 2
    target = Path(args[0]).resolve()
    if not target.exists():
        print(f"target missing: {target}", file=sys.stderr)
        return 2
    if len(args) >= 2:
        log_path = Path(args[1]).resolve()
    else:
        log_path = target.with_suffix(".out.log")
    passthrough = args[2:]

    # 无条件重定向：pythonw 无控制台启动时 stdout 不是 None，而是指向无效句柄的
    # 假 TextIOWrapper（实测探针），按 None 判断会漏——输出全部进黑洞。故一律
    # 打开日志文件替换句柄；交互调试时输出进日志而非终端（tail 即可看）。
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = _log
    sys.stderr = _log

    from datetime import datetime

    print(f"\n===== run_silent {datetime.now():%Y-%m-%d %H:%M:%S} → {target.name} =====")
    sys.argv = [str(target), *passthrough]
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as e:  # 目标脚本 sys.exit(code) 透传退出码
        code = e.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
