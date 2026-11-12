#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电费余量监控：分组采集、分组推送、运行/推送窗口分离、
周期报告（含每日用电量）、充值反推、中断补告警。
仅用 Python 标准库。
"""

import argparse
import json
import logging
import math
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")
TS_FMT = "%Y-%m-%d %H:%M"
DAY_FMT = "%Y-%m-%d"
EPS = 1e-6


# ---------------- 基础工具 ----------------

def setup_logging(log_path: Path):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else BASE_DIR / p


def in_window(hour: int, win: dict) -> bool:
    return int(win["start_hour"]) <= hour < int(win["end_hour"])


# ---------------- HTTP ----------------

def post_form(url: str, data: dict, timeout: int = 15) -> str:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": "https://sdufedk.node.yktsv.cc/SJ/pre.shtml",
            "Origin": "https://sdufedk.node.yktsv.cc",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_kwh(text: str):
    m = re.search(r"剩余电量[：:]\s*([0-9]+(?:\.[0-9]+)?)", text)
    return float(m.group(1)) if m else None


# ---------------- 数据库 ----------------

def init_db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meter (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            group_name TEXT NOT NULL,
            name TEXT NOT NULL,
            lh TEXT NOT NULL,
            fj TEXT NOT NULL,
            kwh REAL,
            ok INTEGER NOT NULL,
            err TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meter_lookup "
        "ON meter(group_name, name, ts)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT NOT NULL,
            group_name TEXT NOT NULL,
            value TEXT NOT NULL,
            ts TEXT NOT NULL,
            PRIMARY KEY (key, group_name)
        )
    """)
    conn.commit()
    return conn


def get_state(conn, key, group_name):
    return conn.execute(
        "SELECT value, ts FROM state WHERE key=? AND group_name=?",
        (key, group_name),
    ).fetchone()


def set_state(conn, key, group_name, value, ts=None):
    ts = ts or datetime.now().strftime(TS_FMT)
    conn.execute(
        "INSERT OR REPLACE INTO state (key,group_name,value,ts) VALUES (?,?,?,?)",
        (key, group_name, value, ts),
    )
    conn.commit()


def save_record(conn, ts, group_name, name, lh, fj, kwh, ok, err):
    conn.execute(
        "INSERT INTO meter (ts,group_name,name,lh,fj,kwh,ok,err) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts, group_name, name, lh, fj, kwh, 1 if ok else 0, err),
    )
    conn.commit()


def consecutive_failures(conn, group_name, name, limit=20) -> int:
    rows = conn.execute(
        "SELECT ok FROM meter WHERE group_name=? AND name=? "
        "ORDER BY id DESC LIMIT ?",
        (group_name, name, limit),
    ).fetchall()
    n = 0
    for (ok,) in rows:
        if ok:
            break
        n += 1
    return n


def last_ok_ts(conn, group_name, name):
    row = conn.execute(
        "SELECT ts FROM meter WHERE group_name=? AND name=? AND ok=1 "
        "ORDER BY id DESC LIMIT 1",
        (group_name, name),
    ).fetchone()
    return row[0] if row else None


# ---------------- 飞书 ----------------

def send_feishu(webhook: str, text: str) -> bool:
    if not webhook:
        logging.info("该组未配置 Webhook，跳过推送")
        return False
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": text}}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        logging.info("飞书返回：%s", body)
        return True
    except Exception as e:
        logging.error("飞书推送失败：%s", e)
        return False


def fmt_reading(r):
    if not r["ok"]:
        return f"{r['name']}：采集失败"
    if r["kwh"] < r["threshold_kwh"]:
        return f"{r['name']}：余 {r['kwh']:.2f} 度 ⚠️ 余量偏低"
    return f"{r['name']}：余 {r['kwh']:.2f} 度"


# ---------------- 每日统计（含充值反推） ----------------

def infer_charge(delta: float, price: float) -> float:
    """delta 为余额增加量（>0）。返回最小合法充值度数。"""
    candidates = []
    c_int = max(1, math.ceil(delta - EPS))
    candidates.append(float(c_int))
    m = max(1, math.ceil(delta * price - EPS))
    candidates.append(m / price)
    valid = [c for c in candidates if c >= delta - EPS]
    return min(valid) if valid else max(candidates)


def compute_daily(conn, gname, name, start_str, end_str, price_per_kwh=0.55):
    rows = conn.execute(
        "SELECT ts, kwh FROM meter WHERE group_name=? AND name=? AND ok=1 "
        "AND ts>=? AND ts<=? ORDER BY ts ASC",
        (gname, name, start_str, end_str),
    ).fetchall()
    by_day = {}
    for ts, kwh in rows:
        by_day[ts[:10]] = kwh
    days = sorted(by_day.keys())

    daily = []
    total_used = 0.0
    total_charged = 0.0

    for i in range(1, len(days)):
        prev_day, cur_day = days[i - 1], days[i]
        prev_kwh, cur_kwh = by_day[prev_day], by_day[cur_day]
        diff = prev_kwh - cur_kwh
        gap = (datetime.strptime(cur_day, DAY_FMT)
               - datetime.strptime(prev_day, DAY_FMT)).days

        if diff >= 0:
            used, charged = diff, 0.0
        else:
            delta = -diff
            charged = infer_charge(delta, price_per_kwh)
            used = max(0.0, charged - delta)

        total_used += used
        total_charged += charged
        daily.append({
            "day": cur_day,
            "used": round(used, 2),
            "charged": round(charged, 2),
            "gap": gap,
        })

    return daily, total_used, total_charged, days, by_day


def build_report(group, rep, conn, now, price_per_kwh=0.55):
    period_start = now - timedelta(days=rep["period_days"])
    start_str = period_start.strftime(TS_FMT)
    end_str = now.strftime(TS_FMT)
    lines = [
        f"【{rep['name']} {period_start:%Y-%m-%d} ~ {now:%Y-%m-%d}】{group['name']}",
        "",
    ]
    for t in group["targets"]:
        daily, used, charged, days, by_day = compute_daily(
            conn, group["name"], t["name"], start_str, end_str, price_per_kwh
        )
        if len(days) < 2:
            lines.append(f"{t['name']}：数据不足（{len(days)} 天有记录）")
            lines.append("")
            continue

        first_kwh = by_day[days[0]]
        last_kwh = by_day[days[-1]]
        span_days = (datetime.strptime(days[-1], DAY_FMT)
                     - datetime.strptime(days[0], DAY_FMT)).days
        avg = used / span_days if span_days > 0 else 0
        remain_txt = (f"预计可用 {last_kwh / avg:.1f} 天"
                      if avg > 0 else "日均消耗为 0，无法估算")

        lines.append(f"{t['name']}")
        lines.append(f"  期初 {first_kwh:.2f} → 期末 {last_kwh:.2f}")
        charge_note = f"，期间充值 {charged:.2f} 度" if charged > 0 else ""
        lines.append(
            f"  期间用电 {used:.2f} 度{charge_note}，"
            f"日均 {avg:.2f} 度，{remain_txt}"
        )
        if daily:
            lines.append("  每日用电：")
            for d in daily:
                tag = f"（充值 {d['charged']:.2f} 度）" if d["charged"] > 0 else ""
                gap_tag = f"（{d['gap']} 天合计）" if d["gap"] > 1 else ""
                lines.append(f"    {d['day'][5:]}  {d['used']:.2f} 度{tag}{gap_tag}")
        else:
            lines.append("  每日用电：数据不足")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------- 各类推送 ----------------

def maybe_send_reports(cfg, conn, now, dry_run, notify_ok):
    report_dir = resolve(cfg.get("report_dir", "reports"))
    price = float(cfg.get("charge_price_per_kwh", 0.55))
    for rep in cfg.get("reports", []):
        if not rep.get("enabled", True):
            continue
        for group in cfg["groups"]:
            st = get_state(conn, f"report:{rep['name']}", group["name"])
            due = True
            if st:
                last_dt = datetime.strptime(st[1], TS_FMT)
                due = (now - last_dt).total_seconds() >= rep["period_days"] * 86400
            if not due:
                continue
            if not notify_ok:
                logging.info("报告[%s/%s]到期但不在推送窗口，延后",
                             rep["name"], group["name"])
                continue
            text = build_report(group, rep, conn, now, price)
            logging.info("生成报告：\n%s", text)
            report_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{rep['name']}_{group['name']}_{now:%Y%m%d}.txt"
            (report_dir / fname).write_text(text, encoding="utf-8")
            if not dry_run:
                send_feishu(group.get("webhook", ""), text)
            set_state(conn, f"report:{rep['name']}", group["name"], "sent",
                      now.strftime(TS_FMT))


def check_stale(cfg, conn, now, dry_run):
    """中断补告警：不受推送窗口限制。"""
    stale_hours = float(cfg["notify"].get("stale_hours", 0))
    if stale_hours <= 0:
        return
    for group in cfg["groups"]:
        gname = group["name"]
        stale_items = []
        for t in group["targets"]:
            last_ok = last_ok_ts(conn, gname, t["name"])
            if last_ok is None:
                continue
            last_ok_dt = datetime.strptime(last_ok, TS_FMT)
            gap_h = (now - last_ok_dt).total_seconds() / 3600
            if gap_h <= stale_hours:
                continue
            st = get_state(conn, f"stale:{t['name']}", gname)
            if st and st[1] >= last_ok:
                continue
            stale_items.append((t["name"], gap_h, last_ok))
        if not stale_items:
            continue
        lines = [f"【监控中断告警】{gname}"]
        for name, gap_h, last_ok in stale_items:
            lines.append(
                f"{name}：已 {gap_h:.1f} 小时未成功采集（最后成功 {last_ok}）"
            )
        text = "\n".join(lines)
        logging.warning("中断告警：\n%s", text)
        if not dry_run:
            send_feishu(group.get("webhook", ""), text)
        for name, _, _ in stale_items:
            set_state(conn, f"stale:{name}", gname, "alerted",
                      now.strftime(TS_FMT))


def maybe_digest(cfg, conn, now, group, results, dry_run, notify_ok):
    notify = cfg["notify"]
    if not notify.get("digest_enabled", False):
        return
    interval_days = int(notify.get("digest_interval_days", 1))
    push_hour = int(notify.get("digest_push_hour", 20))
    if now.hour < push_hour:
        return
    st = get_state(conn, "__digest__", group["name"])
    if st:
        last_dt = datetime.strptime(st[1], TS_FMT)
        if (now - last_dt).total_seconds() < interval_days * 86400:
            return
    text = f"【简报 {now:%Y-%m-%d %H:%M}】{group['name']}\n" + "\n".join(
        fmt_reading(r) for r in results
    )
    logging.info("简报[%s]：\n%s", group["name"], text)
    if dry_run:
        logging.info("dry-run，不推简报[%s]", group["name"])
        return
    if not notify_ok:
        logging.info("简报[%s]不在推送窗口，跳过", group["name"])
        return
    send_feishu(group.get("webhook", ""), text)
    set_state(conn, "__digest__", group["name"], "sent", now.strftime(TS_FMT))


def maybe_threshold_alert(cfg, conn, now, group, results, dry_run, notify_ok):
    if not cfg["notify"].get("threshold_alert_enabled", False):
        return
    for r in results:
        if not r["ok"]:
            continue
        key = f"threshold:{r['name']}"
        st = get_state(conn, key, group["name"])
        prev_low = st is not None and st[0] == "low"
        now_low = r["kwh"] < r["threshold_kwh"]
        if now_low and not prev_low:
            text = (f"【告警 {now:%Y-%m-%d %H:%M}】{group['name']}\n"
                    f"{r['name']}：余 {r['kwh']:.2f} 度 ⚠️ 余量偏低")
            logging.warning("阈值告警：\n%s", text)
            if not dry_run and notify_ok:
                send_feishu(group.get("webhook", ""), text)
            set_state(conn, key, group["name"], "low", now.strftime(TS_FMT))
        elif not now_low and prev_low:
            set_state(conn, key, group["name"], "ok", now.strftime(TS_FMT))


# ---------------- 主流程 ----------------

def run_once(cfg, dry_run=False):
    now = datetime.now()
    if not in_window(now.hour, cfg["run_window"]):
        logging.info("当前 %02d 点不在运行窗口 [%s,%s)，跳过",
                     now.hour, cfg["run_window"]["start_hour"],
                     cfg["run_window"]["end_hour"])
        return

    ts = now.strftime(TS_FMT)
    url = cfg["base_url"].rstrip("/") + "/SJ/sydl.shtml"
    timeout = int(cfg.get("timeout_seconds", 15))
    conn = init_db(resolve(cfg.get("db_path", "meter.db")))
    notify_ok = in_window(now.hour, cfg["notify"]["window"])
    fail_after = int(cfg["notify"].get("fail_alert_after", 2))

    for group in cfg["groups"]:
        gname = group["name"]
        results = []
        for t in group["targets"]:
            kwh, err = None, None
            try:
                text = post_form(url, {"lh": t["lh"], "fj": t["fj"]},
                                 timeout=timeout)
                kwh = parse_kwh(text)
                if kwh is None:
                    err = f"响应无法解析: {text[:80]!r}"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            ok = kwh is not None
            save_record(conn, ts, gname, t["name"], t["lh"], t["fj"],
                        kwh, ok, err)
            streak = consecutive_failures(conn, gname, t["name"]) if not ok else 0
            logging.info("[%s] %s -> %s", gname, t["name"],
                         f"{kwh:.2f} 度" if ok else f"失败: {err}")
            results.append({
                "name": t["name"], "kwh": kwh, "ok": ok, "err": err,
                "threshold_kwh": float(t.get("threshold_kwh", 0)),
                "fail_streak": streak,
            })

        fail_msgs = [
            f"{r['name']}：连续失败 {r['fail_streak']} 次（{r['err']}）"
            for r in results
            if not r["ok"] and r["fail_streak"] >= fail_after
        ]
        if fail_msgs:
            text = (f"【采集失败告警 {now:%Y-%m-%d %H:%M}】{gname}\n"
                    + "\n".join(fail_msgs))
            logging.warning("失败告警：\n%s", text)
            if not dry_run and notify_ok:
                send_feishu(group.get("webhook", ""), text)

        maybe_threshold_alert(cfg, conn, now, group, results, dry_run, notify_ok)
        maybe_digest(cfg, conn, now, group, results, dry_run, notify_ok)

    check_stale(cfg, conn, now, dry_run)
    maybe_send_reports(cfg, conn, now, dry_run, notify_ok)
    conn.close()


def next_delay(cfg, now) -> float:
    interval = float(cfg.get("interval_hours", 2)) * 3600
    rw = cfg["run_window"]
    next_ts = now + timedelta(seconds=interval)
    if in_window(next_ts.hour, rw):
        return interval
    target = next_ts.replace(hour=int(rw["start_hour"]),
                             minute=0, second=0, microsecond=0)
    if target <= next_ts:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 60)


def main():
    parser = argparse.ArgumentParser(description="电费余量监控")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--loop", action="store_true", help="常驻循环模式")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推飞书")
    parser.add_argument("--test-feishu", action="store_true", help="逐组发测试消息")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(resolve(cfg.get("log_path", "meter_monitor.log")))

    if args.test_feishu:
        for g in cfg["groups"]:
            send_feishu(
                g.get("webhook", ""),
                f"【电费监控】测试消息 {g['name']} "
                + datetime.now().strftime(TS_FMT),
            )
        return

    if args.loop:
        while True:
            try:
                run_once(cfg, dry_run=args.dry_run)
            except Exception:
                logging.exception("本轮异常")
            delay = next_delay(cfg, datetime.now())
            logging.info("休眠 %.0f 秒后进入下一轮", delay)
            time.sleep(delay)
    else:
        run_once(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()