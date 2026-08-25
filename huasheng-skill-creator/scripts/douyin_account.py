#!/usr/bin/env python3
"""
抖音创作者后台「账号数据中心」自动抓取 → 落盘到 workspace/账号数据/抖音.md。

原理：
    数据中心（data-center/operation）是 SPA，账号级数字来自登录态签名接口
    （a_bogus/msToken，纯 requests 无法自己算签名）。本脚本用 Playwright 驱动真实
    Chromium，注入创作者后台会话 cookie，打开数据中心页，在页面 JS 环境里直接
    fetch 两个 janus 接口（页面环境会自动带上签名），把返回的 JSON 解析成 Markdown。

两个关键接口：
    - janus/douyin/creator/data/overview/dashboard?recent_days=N
        → 账号概览指标（播放/点赞/评论/分享/净增粉/封面点击率/5s完播率/2s跳出率/
          平均播放时长/总粉丝量/投稿量/主页访问…），每项带逐日 trends
    - janus/douyin/creator/data/overview/dashboard/fans?recent_days=N
        → 粉丝维度补充（新增粉丝量、回访粉丝量），每项带逐日 trends

用法：
    # 抓最近 30 天（默认）并落盘
    python3 douyin_account.py

    # 自定义天数 / 只预览不落盘 / 显示浏览器窗口
    python3 douyin_account.py --days 14
    python3 douyin_account.py --dry-run
    python3 douyin_account.py --show

cookie：
    默认读技能根目录下的 .douyin_cookie（已 gitignore）。cookie 过期会抓到空数据，
    报错提示后重新从浏览器复制 document.cookie 覆盖该文件即可。

依赖：
    pip install playwright   （Chromium 内核复用 ms-playwright 缓存）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

# 复用 douyin_metrics.py 里的 Chromium 定位 / cookie 解析（同目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from douyin_metrics import find_chromium, parse_cookies, COOKIE_FILE  # noqa: E402
from _paths import workspace_root  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT_DIR = os.path.join(workspace_root(), "账号数据")  # 回指创作 skill 的 workspace/
OUT_MD = os.path.join(ACCOUNT_DIR, "抖音.md")

URL = "https://creator.douyin.com/creator-micro/data-center/operation"
API_BASE = "https://creator.douyin.com/janus/douyin/creator/data/overview/"

# 概览接口指标 → (中文标签, 类型)。type: int=计数, pct=百分比小数, sec=秒
DASHBOARD_FIELDS = [
    ("play_cnt",           "播放量",       "int"),
    ("digg_cnt",           "作品点赞",      "int"),
    ("share_count",        "作品分享",      "int"),
    ("comment_cnt",        "作品评论",      "int"),
    ("net_fans_cnt",       "净增粉丝",      "int"),
    ("cancel_fans_cnt",    "取关粉丝",      "int"),
    ("homepage_view_cnt",  "主页访问",      "int"),
    ("publish_cnt",        "投稿量",       "int"),
    ("cover_click_ratio",  "封面点击率",    "pct"),
    ("completion_rate_5s", "5秒完播率",     "pct"),
    ("bounce_rate_2s",     "2秒跳出率",     "pct"),
    ("avg_view_second",    "平均播放时长",  "sec"),
    ("total_fans_cnt",     "总粉丝量",      "int"),
]

# fans 接口独有补充指标
FANS_EXTRA_FIELDS = [
    ("new_fans_cnt",       "新增粉丝量",    "int"),
    ("home_view_fans_cnt", "回访粉丝量",    "int"),
]

# 每日趋势分组：中文小节名 → (指标键, 中文列头)
# total_fans_cnt 是累计值（非当日增量），单独说明；cover_click_ratio 等是当日比率。
TREND_GROUPS = [
    ("每日互动数据", [
        ("publish_cnt", "投稿量"),
        ("play_cnt", "播放量"),
        ("digg_cnt", "点赞"),
        ("comment_cnt", "评论"),
        ("share_count", "分享"),
        ("homepage_view_cnt", "主页访问"),
    ]),
    ("每日粉丝数据", [
        ("new_fans_cnt", "新增粉"),
        ("cancel_fans_cnt", "取关粉"),
        ("net_fans_cnt", "净增粉"),
        ("home_view_fans_cnt", "回访粉丝"),
        ("total_fans_cnt", "总粉丝量"),
    ]),
    ("每日质量数据", [
        ("cover_click_ratio", "封面点击率"),
        ("completion_rate_5s", "5秒完播率"),
        ("bounce_rate_2s", "2秒跳出率"),
        ("avg_view_second", "平均播放时长"),
    ]),
]


def fmt(val, typ):
    """按类型格式化指标值。"""
    if val is None:
        return "-"
    try:
        f = float(val)
    except (TypeError, ValueError):
        return str(val)
    if typ == "int":
        return f"{int(round(f)):,}"
    if typ == "pct":
        return f"{f * 100:.2f}%"
    if typ == "sec":
        return f"{f:.1f}s"
    return str(val)


def fetch_json(pg, path):
    """在页面 JS 环境里 fetch 一个 janus 接口，返回解析后的 dict。"""
    js = ("async (url) => {"
          "  const r = await fetch(url, {credentials:'include'});"
          "  return await r.json();"
          "}")
    return pg.evaluate(js, API_BASE + path)


def scrape(days=30, headless=True, wait=8):
    """驱动浏览器抓两个接口，返回 {dashboard: {...}, fans: {...}}。"""
    raw = open(COOKIE_FILE, encoding="utf-8").read().strip()
    cookies = parse_cookies(raw)
    print(f"[douyin-account] 加载 {len(cookies)} 个 cookie，抓最近 {days} 天", file=sys.stderr)

    exe = find_chromium()
    kw = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if exe:
        kw["executable_path"] = exe

    with sync_playwright() as p:
        b = p.chromium.launch(**kw)
        ctx = b.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
            locale="zh-CN", viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        pg = ctx.new_page()
        try:
            pg.goto(URL, wait_until="domcontentloaded", timeout=40000)
            # 等页面 JS 就绪（签名环境 ready），再在页面里主动 fetch
            pg.wait_for_timeout(wait * 1000)

            dash = fetch_json(pg, f"dashboard?recent_days={days}")
            fans = fetch_json(pg, f"dashboard/fans?recent_days={days}")
        finally:
            b.close()

    if not dash.get("metrics"):
        raise RuntimeError("未抓到 dashboard 数据 —— cookie 可能已过期，请重新导出覆盖 .douyin_cookie")
    return {"dashboard": dash, "fans": fans}


def _to_map(metrics):
    """[{english_metric_name, metric_name, metric_value, trends}] → {key: metric}。"""
    out = {}
    for m in metrics:
        out[m["english_metric_name"]] = m
    return out


def _merge_trends(dash, fans):
    """合并两个接口的逐日 trends，返回 {指标key: {date: value}}。"""
    merged = {}
    for src in (dash, fans):
        for m in src.get("metrics") or []:
            key = m["english_metric_name"]
            for t in m.get("trends") or []:
                if "date_time" not in t:
                    continue
                merged.setdefault(key, {})[t["date_time"]] = t.get("value")
    return merged


def _fmt_date(d):
    """20260811 → 2026-08-11。"""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def build_markdown(data, days):
    dash = data["dashboard"]
    fans = data["fans"]

    dm = _to_map(dash["metrics"])
    fm = _to_map(fans["metrics"])

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 概览指标聚合值
    total = {k: (dm[k]["metric_value"] if k in dm else None) for k, _, _ in DASHBOARD_FIELDS}
    for k, _, _ in FANS_EXTRA_FIELDS:
        total[k] = fm[k]["metric_value"] if k in fm else None

    out = []
    out.append("# 账号数据（抖音）\n")
    out.append(f"\n> 最近更新：{stamp}　｜　抓取脚本：scripts/douyin_account.py（自动抓取，抖音创作者数据中心）\n")
    out.append(f"> 数据口径：最近 {days} 天逐日趋势 + 区间聚合值　｜　来源接口：janus/creator/data/overview/dashboard\n\n")

    # 核心指标总览（最近 N 天聚合）
    out.append("## 核心指标总览（最近 {} 天聚合）\n\n".format(days))
    out.append("| 指标 | 数值 |\n| --- | --- |\n")
    for key, label, typ in DASHBOARD_FIELDS:
        out.append(f"| {label} | {fmt(total.get(key), typ)} |\n")
    for key, label, typ in FANS_EXTRA_FIELDS:
        out.append(f"| {label} | {fmt(total.get(key), typ)} |\n")
    out.append("\n")

    # 逐日趋势表
    trends = _merge_trends(dash, fans)
    # 汇总所有日期（按升序），最多 days 天
    all_dates = set()
    for series in trends.values():
        all_dates.update(series.keys())
    dates = sorted(all_dates)[-days:]

    for section, cols in TREND_GROUPS:
        out.append(f"## {section}\n\n")
        header = "| 日期 | " + " | ".join(c[1] for c in cols) + " |"
        out.append(header + "\n")
        out.append("| --- | " + " | ".join(["---"] * len(cols)) + " |\n")
        for d in dates:
            row = [_fmt_date(d)]
            for key, _label in cols:
                val = trends.get(key, {}).get(d)
                typ = dict((k, t) for k, _, t in DASHBOARD_FIELDS + FANS_EXTRA_FIELDS)[key]
                row.append(fmt(val, typ))
            out.append("| " + " | ".join(row) + " |\n")
        out.append("\n")

    out.append("> 说明：总粉丝量为当日累计值，其余为当日增量/当日比率。\n")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="抖音创作者数据中心账号数据 → workspace/账号数据/抖音.md")
    ap.add_argument("--days", type=int, default=30, help="抓最近多少天（默认 30）")
    ap.add_argument("--show", action="store_true", help="显示浏览器窗口（调试）")
    ap.add_argument("--wait", type=int, default=8, help="页面加载后等待秒数（默认 8）")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不落盘")
    ap.add_argument("--save-raw", action="store_true", help="把原始 JSON 存到账号数据 raw/ 下")
    args = ap.parse_args()

    data = scrape(args.days, headless=not args.show, wait=args.wait)
    md = build_markdown(data, args.days)

    if args.dry_run:
        sys.stdout.write(md)
        return

    os.makedirs(ACCOUNT_DIR, exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写入：{OUT_MD}")

    if args.save_raw:
        raw_dir = os.path.join(ACCOUNT_DIR, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_path = os.path.join(raw_dir, f"douyin_account_raw_{stamp}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已归档原始 JSON：{raw_path}")


if __name__ == "__main__":
    main()
