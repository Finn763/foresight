"""ops.lock 引擎级单测：lock_state 五态判定与 acquire_lock 排他/接管。

从 scripts/evolve.py 下沉后的引擎单测（P2 包边界）：不依赖 scripts/，
写法参考 tests/test_evolve.py 的既有锁用例。
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from predictor.ops.lock import acquire_lock, lock_state


def test_lock_state_none_when_file_missing(tmp_path):
    assert lock_state(tmp_path / "evolve.lock") == "none"


def test_lock_state_active_when_own_pid_alive(tmp_path):
    lock_path = tmp_path / "evolve.lock"
    lock_path.write_text(f"{os.getpid()}|{time.time()}")  # 自身进程存活、新鲜时间戳
    assert lock_state(lock_path) == "active"


def test_lock_state_stale_when_pid_dead(tmp_path):
    lock_path = tmp_path / "evolve.lock"
    lock_path.write_text(f"99999999|{time.time()}")  # 不存在的 pid、新鲜时间戳
    assert lock_state(lock_path) == "stale"


def test_lock_state_stale_when_age_exceeded(tmp_path):
    lock_path = tmp_path / "evolve.lock"
    # 7h 前的时间戳 → 超 6h stale；pid 用自身存活进程，证明超龄判定先于 pid 判定
    lock_path.write_text(f"{os.getpid()}|{time.time() - 7 * 3600}")
    assert lock_state(lock_path) == "stale"


def test_lock_state_stale_when_content_garbage(tmp_path):
    lock_path = tmp_path / "evolve.lock"
    lock_path.write_text("garbage")
    assert lock_state(lock_path) == "stale"


def test_acquire_lock_exclusive(tmp_path):
    lock_path = tmp_path / "evolve.lock"
    with acquire_lock(lock_path) as a:
        assert a
        # 与 tests/test_evolve.py 同款：contextmanager 函数体不执行，必须嵌套 with 才触发拒绝
        with pytest.raises(SystemExit):
            with acquire_lock(lock_path):
                pass


def test_acquire_lock_takeover_when_pid_dead(tmp_path):
    """原生崩溃（0xc0000005）接管：锁文件新（未过 stale）但持有进程已死 →
    pid 存活检查接管，不等 6h——备援轨道才能及时补上预测。"""
    lock_path = tmp_path / "evolve.lock"
    lock_path.write_text(f"99999999|{time.time()}")
    with acquire_lock(lock_path) as a:
        assert a
