"""进程锁工具：文件锁（data/evolve.lock）与锁状态判定。

从 scripts/evolve.py 下沉（P2 包边界修复）：daily/evolve 双轨共用同一把
data/evolve.lock，health 事实组装也需锁状态判定——下沉后 src 不再反向依赖
scripts/，wheel 可独立运行。锁文件格式：`pid|unix_ts`。
"""

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """Windows 进程存活检查：OpenProcess 成功 ≠ 存活——被终止进程的对象在父链
    （venv stub / uv）句柄释放前仍可打开（实测 exitcode=15 已死但 handle 可开），
    GetExitCodeProcess 的 STILL_ACTIVE(259) 才是权威判据。

    brief 原写法 `os.path.exists(pid)` 在 Windows 上是检查"名为 pid 的文件"恒 False
    （锁永不生效），落地改用 GetExitCodeProcess：259=存活；已终止（含原生崩溃
    0xc0000005 后被父链收割）→ 接管，不等 6h stale。
    """
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return k32.GetLastError() != 87  # 87=无此 pid（已死）；权限等 → 保守按存活
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    if not ok:
        return True  # 拿不到退出码 → 保守按存活（宁可拒绝也不双跑）
    return code.value == 259


def lock_state(lock_path: Path, *, stale_seconds: int = 6 * 3600) -> str:
    """锁文件状态判定，返回 "none"/"active"/"stale"。

    锁文件格式 `pid|unix_ts`；判定规则与 acquire_lock 的接管判定严格一致：
    - 文件不存在 → "none"
    - 内容损坏/无法解析 pid|ts → "stale"
    - 时间戳超龄（age >= stale_seconds）→ "stale"
    - 持锁进程已死 → "stale"
    - 其余（未超龄且进程存活）→ "active"
    """
    if not lock_path.exists():
        return "none"
    try:
        pid, ts = lock_path.read_text().strip().split("|")
        pid = int(pid)
        age = time.time() - float(ts)
    except Exception:
        return "stale"  # 锁内容损坏/时间戳不可解析 → 视为 stale，接管
    if age >= stale_seconds:
        return "stale"
    if not pid_alive(pid):
        return "stale"
    return "active"


@contextmanager
def acquire_lock(lock_path: Path, *, stale_seconds: int = 6 * 3600, caller: str = "evolve"):
    """文件锁（daily/evolve 双轨共用一把 data/evolve.lock）：

    - 存在且未过 stale 且持锁进程存活 → SystemExit 拒绝（调用方按"对称轨道已覆盖"处理）
    - 持锁进程已死（原生崩溃 0xc0000005 类）→ 立即接管，不等 6h
    - stale（>6h 或内容无法解析）→ 接管
    """
    if lock_state(lock_path, stale_seconds=stale_seconds) == "active":
        try:
            pid = lock_path.read_text().strip().split("|")[0]
        except Exception:
            pid = "?"  # 判定后瞬间被释放（unlink）/篡改：消息降级，拒绝语义不变
        raise SystemExit(f"{caller} 已在运行 (pid {pid})")
    lock_path.parent.mkdir(parents=True, exist_ok=True)  # data/ 不存在时（新检出）不崩
    lock_path.write_text(f"{os.getpid()}|{time.time()}")
    try:
        yield True
    finally:
        lock_path.unlink(missing_ok=True)


class LockWaitTimeout(Exception):
    """排队等锁超时：锁在超时上限内始终被存活进程持有，轮次疑似挂死或异常超长。"""


def _lock_holder(lock_path: Path) -> str | None:
    """读锁文件首段 pid（报错时指明持有者）；读不到/内容损坏返回 None。"""
    try:
        return lock_path.read_text().strip().split("|")[0]
    except Exception:
        return None


@contextmanager
def wait_acquire(
    lock_path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float = 20.0,
    stale_seconds: int = 6 * 3600,
    caller: str = "wait",
):
    """排队等锁：锁 active 时轮询等待，空闲后立即接管并持锁执行 body。

    监控类调用方（health_check）用：不因轮次持锁而退出失明，而是排队，
    轮次一结束就接手。语义与 evolve.main 的等锁判定一致——active 轮询不放弃；
    持锁进程已死或锁 stale（>6h 超龄/内容损坏）则跳过等待、立即接管。

    注意：
    ① 等待循环判定与随后 acquire_lock 接管之间是竞态窗口——本函数判定空闲后
       仍可能被轮次抢锁，此时 acquire_lock 抛 SystemExit，由调用方显式兜底
       （绝不静默），不能假设走出本函数就必然拿到锁；
    ② 拿到锁后持锁直至 body 结束（与 daily/evolve 轮次互斥，杜绝对撞），
       body 内的 DB 读与锁状态探测因此无撞库之虞。
    """
    deadline = time.time() + timeout_seconds
    while lock_state(lock_path, stale_seconds=stale_seconds) == "active":
        if time.time() >= deadline:
            holder = _lock_holder(lock_path)
            holder_desc = f"pid {holder}" if holder else "未知进程"
            raise LockWaitTimeout(
                f"{caller} 等锁超时（>{timeout_seconds:.0f}s）：锁仍由 {holder_desc} 持有，"
                "轮次疑似挂死或异常超长"
            )
        time.sleep(poll_seconds)
    with acquire_lock(lock_path, stale_seconds=stale_seconds, caller=caller):
        yield True
