"""Foresight 项目内自检（盯梢）：schtasks 每日 09:35/16:40 触发。

复用 ops 三件套（build_facts / probes / assess）红黄绿判定 + 补充 24h 事件规则。
入场控制：复用 evolve.lock 机制排队等锁（--lock-wait 上限 45 分钟，轮次结束即接手）；
等锁超时兜底告警落盘（监控不在被监控对象持锁时失明——2026-08-27 09:35 实测 daily
轮持 DuckDB 独占锁 39 分钟，旧 6×10s 重试必败）；拿到锁后持锁执行全部 DB 读，
与轮次互斥杜绝对撞。
异常 → 写 data/alerts/alert-*.md + 弹 Windows toast + 退出码 1（schtasks 记失败）；
正常 → 静默退出 0（watchdog 语义：没事不打扰）。
"""

import argparse
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ALERT_TYPES = ("llm_resolve_failed", "prediction_skipped", "resolution_failed")
_SKIP_WARN_THRESHOLD = 5  # 24h 内 prediction_skipped ≥ 5 → 告警（疑似 key/网络系统性故障）


def _toast(title: str, body: str) -> None:
    """Windows 原生 toast（WinRT，无需额外组件）。失败静默——不因通知失败误报。"""
    ps = (
        "$null = [Windows.UI.Notifications.ToastNotificationManager,"
        " Windows.UI.Notifications, ContentType = WindowsRuntime];"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f'$xml.LoadXml(\'<toast><visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        f"</binding></visual></toast>');"
        "$t = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        "[Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier('Foresight').Show($t)"
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps], timeout=40, capture_output=True
        )
    except Exception:
        pass


def _open_storage_with_retry(db_path: str, *, attempts: int = 6, delay: float = 10.0):
    """锁协议外进程撞库的兜底：daily/evolve 轮次已被 evolve.lock 挡在门外（main 持锁后
    才开库，与轮次互斥），此重试只剩异常场景——其他程序（手动 REPL/分析脚本）持有
    DuckDB 独占文件锁时重试等释放；仍失败上抛由 main 落告警文件。6×10s 参数不动。
    """
    from predictor.data.storage import Storage

    last = None
    for attempt in range(attempts):
        try:
            return Storage(db_path)
        except Exception as e:
            last = e
            if attempt < attempts - 1:
                time.sleep(delay)
    raise last


def collect(st, now: datetime, *, lock_state: str | None = None) -> list[str]:
    """收集告警条目：health.assess 的 error 项 + 24h 失败事件规则。

    lock_state 透传给 build_facts：health_check 持锁入场后锁文件已被自己重写为
    自身 pid（wait_acquire 接管），现场判定会误报 active——传入 "none" 跳过读取。
    """
    from predictor.ops.facts import build_facts
    from predictor.ops.health import assess
    from predictor.ops.probes import get_probes, refresh_probes

    try:
        refresh_probes()
    except Exception:
        pass
    facts = build_facts(st, now, lock_state=lock_state)
    facts["probes"] = get_probes()
    report = assess(facts, now)

    alerts = []
    for c in report["checks"]:
        # 探针（probe_*）是真实网络探测，环境敏感，只在 web 面板人工触发时参考；
        # 自检只告确定性事实（轮次/事件/积压/锁）。key 失效由 401 事件规则兜底。
        if c["status"] == "error" and not c["key"].startswith("probe_"):
            alerts.append(c["summary"])

    # 补充 24h 事件规则（跨日：昨天 16:30 揭晓失败由今早 9:35 检出）
    try:
        evs = st.list_events(types=list(_ALERT_TYPES))
    except Exception:
        evs = []
    day_ago = now - timedelta(hours=24)
    recent = [e for e in evs if (e.get("ts") or now) >= day_ago]
    resolve_failed = [e for e in recent if e.get("event_type") == "llm_resolve_failed"]
    skipped = [e for e in recent if e.get("event_type") == "prediction_skipped"]
    if resolve_failed:
        alerts.append(f"LLM 揭晓失败 {len(resolve_failed)} 次（24h，api_error/护栏）")
    auth_fail = [
        e
        for e in skipped
        if any(k in (e.get("detail") or "").lower() for k in ("401", "auth", "invalid", "api_key"))
    ]
    if auth_fail:
        alerts.append("⚠️ 认证失败特征（401/auth）——LLM key 可能失效，需立即处理")
    elif len(skipped) >= _SKIP_WARN_THRESHOLD:
        alerts.append(f"预测跳过 {len(skipped)} 题（24h，疑似 key 失效/网络/管线故障）")
    return alerts, report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="DB 路径，默认 Settings().db_path")
    ap.add_argument("--now", default=None, help="测试注入 YYYY-MM-DDTHH:MM:SS")
    ap.add_argument("--no-notify", action="store_true", help="只写文件不弹 toast（测试）")
    ap.add_argument(
        "--lock-wait",
        type=int,
        default=2700,
        help="排队等 evolve.lock 的上限秒数，默认 2700（45 分钟）；超时落兜底告警",
    )
    ap.add_argument(
        "--lock-poll",
        type=float,
        default=20.0,
        help="等锁轮询间隔秒数，默认 20.0",
    )
    args = ap.parse_args(argv)

    from predictor.config import Settings

    db_path = args.db or Settings().db_path
    out_dir = Path(db_path).parent / "alerts"  # 不依赖 cwd（schtasks 工作目录不可控）
    lock_path = Path(db_path).parent / "evolve.lock"
    try:
        from predictor.ops.lock import LockWaitTimeout, wait_acquire

        # 排队等 evolve.lock（上限 --lock-wait 秒）：轮次持锁时轮询等待而非撞库重试，
        # 拿到锁后持锁执行全部 DB 读（与轮次互斥，杜绝对撞）；锁状态事实固定 "none"
        # （锁文件此刻已是自身 pid，现场判定会误报 active）。
        with wait_acquire(
            lock_path,
            timeout_seconds=args.lock_wait,
            poll_seconds=args.lock_poll,
            caller="health_check",
        ):
            st = _open_storage_with_retry(db_path)
            st.create_schema()
            now = datetime.fromisoformat(args.now) if args.now else datetime.now()
            alerts, report = collect(st, now, lock_state="none")
    except LockWaitTimeout as e:
        # 兜底告警：排队超时也必须有可见产物——监控不能在被监控对象持锁时失明
        # （2026-08-27 09:35 实测 daily 轮持锁 39 分钟，评审 §3.1）。
        out_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        f = out_dir / f"alert-{now:%Y%m%d-%H%M%S}-health-error.md"
        f.write_text(
            "\n".join(
                [
                    "# Foresight 健康自检异常（等待轮次锁超时）",
                    "",
                    f"检出时间：{now:%Y-%m-%d %H:%M:%S}",
                    "",
                    f"health_check 排队等待 evolve.lock 超过 {args.lock_wait} 秒上限"
                    + (f"（{args.lock_wait // 60} 分钟）" if args.lock_wait >= 60 else "")
                    + "。",
                    "",
                    f"异常：{e}",
                    "",
                    "判定建议：轮次可能挂死或异常超长，需人工确认 daily/evolve 进程；"
                    "轮次正常结束后下轮巡检自动恢复。",
                ]
            ),
            encoding="utf-8",
        )
        traceback.print_exc()
        return 1
    except SystemExit as e:
        # 竞态边缘：wait_acquire 判定空闲后、正式接管前被轮次抢锁 → acquire_lock 抛
        # SystemExit。同款兜底告警（绝不静默 exit 无痕迹）。
        out_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        f = out_dir / f"alert-{now:%Y%m%d-%H%M%S}-health-error.md"
        f.write_text(
            "\n".join(
                [
                    "# Foresight 健康自检异常（锁竞争：等待窗口被抢占）",
                    "",
                    f"检出时间：{now:%Y-%m-%d %H:%M:%S}",
                    "",
                    "health_check 等锁判定空闲后，接管瞬间被轮次抢先持锁。",
                    "",
                    f"异常：{e}",
                    "",
                    "判定建议：单次竞态，下轮巡检自动恢复；若反复出现需检查 daily/evolve "
                    "与巡检的调度重叠。",
                ]
            ),
            encoding="utf-8",
        )
        traceback.print_exc()
        return 1
    except Exception as e:
        # 撞锁等异常也必须落一个可见产物（data/alerts/），不能静默 exit 1 无痕迹——
        # schtasks 的 >> data\health.log 已捕获 traceback，这里补一份结构化告警。
        out_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        f = out_dir / f"alert-{now:%Y%m%d-%H%M%S}-health-error.md"
        f.write_text(
            "\n".join(
                [
                    "# Foresight 健康自检异常（未完成检测）",
                    "",
                    f"检出时间：{now:%Y-%m-%d %H:%M:%S}",
                    "",
                    "health_check 在打开数据库/收集事实阶段异常退出，可能是 daily/evolve",
                    "轮次持锁撞库或 DB 损坏。",
                    "",
                    f"异常：{type(e).__name__}: {e}",
                ]
            ),
            encoding="utf-8",
        )
        traceback.print_exc()
        return 1

    if alerts:
        out_dir.mkdir(parents=True, exist_ok=True)
        f = out_dir / f"alert-{now:%Y%m%d-%H%M%S}.md"
        lines = [
            "# Foresight 健康告警",
            "",
            f"检出时间：{now:%Y-%m-%d %H:%M:%S}",
            "",
            "## 告警",
            "",
        ]
        lines += [f"- {a}" for a in alerts]
        lines += ["", "## 全量检查", ""]
        for c in report["checks"]:
            lines.append(f"- [{c['status']}] {c['summary']}")
        f.write_text("\n".join(lines), encoding="utf-8")
        print(f"ALERT: {len(alerts)} 项异常 → {f}")
        for a in alerts:
            print(f"  - {a}")
        if not args.no_notify:
            _toast("Foresight 健康告警", "；".join(alerts[:3]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
