"""Foresight 项目内自检（盯梢）：schtasks 定时触发。

复用 ops 三件套（build_facts / probes / assess）红黄绿判定 + 补充 24h 事件规则。
入场控制：复用 evolve.lock 机制排队等锁（--lock-wait 上限 45 分钟，轮次结束即接手）；
等锁超时兜底告警落盘（监控不在被监控对象持锁时失明——实测 daily
轮持 DuckDB 独占锁 39 分钟，旧 6×10s 重试必败）；拿到锁后持锁执行全部 DB 读，
与轮次互斥杜绝对撞。
异常 → 写 data/alerts/alert-*.md + 弹 Windows toast + 退出码 1（schtasks 记失败）；
正常 → 静默退出 0（watchdog 语义：没事不打扰）。

告警消费（评审 §3.5）：同日同类告警合并去重（同一天同一类异常只保留/刷新一个
文件，不刷屏）+ 30 天自动清理；已被确认（.ack 后缀）的文件不参与合并——确认后
同类复发按新告警落盘，dashboard 横幅重现。
"""

import argparse
import re
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
_ALERT_RETENTION_DAYS = 30  # 告警文件保留窗口（评审 §3.5）
_ACK_SUFFIX = ".ack"  # 已确认告警的后缀：alert-*.ack.md 不参与合并、不进 dashboard 横幅


def _normalize_alert_line(line: str) -> str:
    """同类归一：数字序列折叠为 #——「LLM 揭晓失败 2 次」与「…5 次」视为同类。"""
    return re.sub(r"\d+", "#", line)


def _alert_signature(lines: list[str], *, is_error: bool) -> str:
    """告警签名：error 类取首行标题；普通告警取「## 告警」小节要点（归一+排序）。"""
    if is_error:
        heading = next((ln.strip() for ln in lines if ln.startswith("# ")), "")
        return f"err|{heading}"
    bullets = []
    in_alerts = False
    for ln in lines:
        if ln == "## 告警":
            in_alerts = True
            continue
        if in_alerts:
            if ln == "## 全量检查":
                break
            if ln.startswith("- "):
                bullets.append(_normalize_alert_line(ln))
    return "alert|" + "\n".join(sorted(bullets))


def _signature_of_file(f: Path) -> str | None:
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return _alert_signature(lines, is_error=f.name.endswith("-health-error.md"))


def find_same_day_duplicate(
    out_dir: Path, now: datetime, lines: list[str], *, is_error: bool
) -> Path | None:
    """同日同签名且未确认的既有告警文件 → 返回之；否则 None。

    已确认（.ack.md）的文件跳过：确认后同类复发按新告警落盘，横幅重现。
    """
    sig = _alert_signature(lines, is_error=is_error)
    for f in sorted(out_dir.glob(f"alert-{now:%Y%m%d}-*.md")):
        if f.name.endswith(f"{_ACK_SUFFIX}.md"):
            continue
        if f.name.endswith("-health-error.md") != is_error:
            continue
        if _signature_of_file(f) == sig:
            return f
    return None


def cleanup_stale_alerts(
    out_dir: Path, now: datetime, *, retention_days: int = _ALERT_RETENTION_DAYS
) -> list[Path]:
    """删除早于保留窗口的告警文件（文件名日期判定，解析失败按 mtime 兜底）。"""
    if not out_dir.is_dir():
        return []
    cutoff = now.date() - timedelta(days=retention_days)
    removed = []
    for f in out_dir.glob("alert-*.md"):
        try:
            day = datetime.strptime(f.name[6:14], "%Y%m%d").date()
        except ValueError:
            try:
                day = datetime.fromtimestamp(f.stat().st_mtime).date()
            except OSError:
                continue
        if day < cutoff:
            try:
                f.unlink()
                removed.append(f)
            except OSError:
                pass
    return removed


def write_alert_file(
    out_dir: Path, now: datetime, lines: list[str], *, is_error: bool
) -> tuple[Path, bool]:
    """告警落盘统一入口：同日同类合并去重 + 30 天清理。返回 (文件, 是否合并)。

    合并 = 把本次（更新的）内容写回既有文件（保留原文件名），不再新建文件。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_alerts(out_dir, now)
    dup = find_same_day_duplicate(out_dir, now, lines, is_error=is_error)
    if dup is not None:
        dup.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return dup, True
    suffix = "-health-error" if is_error else ""
    base = f"alert-{now:%Y%m%d-%H%M%S}"
    name = f"{base}{suffix}.md"
    i = 2
    while (out_dir / name).exists():
        # 同一秒内不同类别告警落盘：-N 消歧，绝不互相覆盖
        name = f"{base}-{i}{suffix}.md"
        i += 1
    f = out_dir / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f, False


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


def _prediction_gap_assertion(db_path: str, now: datetime) -> list[str]:
    """业务断言：当日 predictions 零新增，但仍存在应预测题 → 落一条告警。

    应预测口径照抄 evolve.predict_round 候选逻辑：未揭晓（outcome IS NULL）且
    closes_at > now（未到期）且距上次预测已满 7×24h（或从未预测）。
    读走独立 read_only 连接（wait_acquire 持锁内），零写；读失败静默跳过，
    整体健康仍由既有 checks / 24h 事件规则覆盖。
    """
    from predictor.data.storage import Storage

    try:
        st = Storage(db_path, read_only=True)
        try:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            row = st._conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE created_at >= ? AND created_at < ?",
                [start, start + timedelta(days=1)],
            ).fetchone()
            if row and int(row[0]) > 0:
                return []
            due = 0
            for q in st.list_unresolved():
                if q.closes_at <= now:
                    continue
                last = st.last_prediction_at(q.id)
                if last is not None and (now - last) < timedelta(hours=168):
                    continue
                due += 1
            if due:
                return [
                    f"今日 predictions 新增 0 行，应预测 {due} 题"
                    "（未到期且距上次预测 ≥ 7 天）"
                ]
            return []
        finally:
            st.close()
    except Exception:
        return []


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
    # 每次巡检顺手清理超 30 天的告警（即使今天零告警也执行；目录不存在时是 no-op）
    cleanup_stale_alerts(out_dir, datetime.now())
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
            st.close()  # 释放 rw 连接，业务断言改走 read_only（同进程 rw+ro 并存会被 DuckDB 拒绝）
            alerts += _prediction_gap_assertion(db_path, now)
    except LockWaitTimeout as e:
        # 兜底告警：排队超时也必须有可见产物——监控不能在被监控对象持锁时失明
        # （实测 daily 轮持锁超过巡检等锁上限，评审 §3.1）。
        now = datetime.now()
        lines = [
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
        write_alert_file(out_dir, now, lines, is_error=True)
        traceback.print_exc()
        return 1
    except SystemExit as e:
        # 竞态边缘：wait_acquire 判定空闲后、正式接管前被轮次抢锁 → acquire_lock 抛
        # SystemExit。同款兜底告警（绝不静默 exit 无痕迹）。
        now = datetime.now()
        lines = [
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
        write_alert_file(out_dir, now, lines, is_error=True)
        traceback.print_exc()
        return 1
    except Exception as e:
        # 撞锁等异常也必须落一个可见产物（data/alerts/），不能静默 exit 1 无痕迹——
        # schtasks 的 >> data\health.log 已捕获 traceback，这里补一份结构化告警。
        now = datetime.now()
        lines = [
            "# Foresight 健康自检异常（未完成检测）",
            "",
            f"检出时间：{now:%Y-%m-%d %H:%M:%S}",
            "",
            "health_check 在打开数据库/收集事实阶段异常退出，可能是 daily/evolve",
            "轮次持锁撞库或 DB 损坏。",
            "",
            f"异常：{type(e).__name__}: {e}",
        ]
        write_alert_file(out_dir, now, lines, is_error=True)
        traceback.print_exc()
        return 1

    if alerts:
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
        f, merged = write_alert_file(out_dir, now, lines, is_error=False)
        if merged:
            print(f"ALERT: {len(alerts)} 项异常（同日同类已合并）→ {f}")
        else:
            print(f"ALERT: {len(alerts)} 项异常 → {f}")
        for a in alerts:
            print(f"  - {a}")
        if not args.no_notify:
            _toast("Foresight 健康告警", "；".join(alerts[:3]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
