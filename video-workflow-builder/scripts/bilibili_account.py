#!/usr/bin/env python3
"""
B站创作者后台「账号数据中心」自动抓取 → 落盘到 workspace/账号数据/B站.md。

原理（区别于抖音）：
    B站创作者中心的数据接口是**普通 cookie 鉴权的 JSON 接口**，无 a_bogus/msToken 前端签名，
    直接用 requests 带 cookie 请求即可，无需 Playwright。

两个关键接口：
    - member.../x/web/index/stat            → 账号累计总量 + 昨日新增（播放/粉丝/点赞/投币/收藏/分享/评论/弹幕/充电）
    - member.../x/web/data/pandect?type=N   → 近 30 天逐日趋势，一个 type 一个指标：
        type=1 播放  2 弹幕  3 评论  4 分享  5 投币  6 收藏  7 充电  8 点赞
      （已逐个比对 index/stat 昨日增量确认；粉丝日增量后台无逐日接口，只给累计+昨日增量）

用法：
    python3 bilibili_account.py                 # 抓最近 30 天并落盘
    python3 bilibili_account.py --dry-run       # 只预览不落盘
    python3 bilibili_account.py --save-raw      # 同时归档原始 JSON

cookie：
    默认读技能根目录下的 .bilibili_cookie（已 gitignore）。过期会返回 code!=0，
    重新从浏览器复制 document.cookie 覆盖该文件即可。

依赖：
    pip install requests
"""
import argparse
import json
import os
import sys
from datetime import datetime

import requests

# 复用 metrics 脚本里的 cookie/UA/常量（同目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bilibili_metrics import load_cookie, make_session, get_json, UA, MEMBER  # noqa: E402
from _paths import workspace_root  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT_DIR = os.path.join(workspace_root(), "账号数据")  # 回指创作 skill 的 workspace/
OUT_MD = os.path.join(ACCOUNT_DIR, "B站.md")

# index/stat 累计总量 + 昨日新增 → (累计key, 昨日增量key, 中文标签)
STAT_FIELDS = [
    ("total_fans",  "incr_fans",  "粉丝"),
    ("total_click", "incr_click", "播放"),
    ("total_like",  "inc_like",   "点赞"),
    ("total_coin",  "inc_coin",   "投币"),
    ("total_fav",   "inc_fav",    "收藏"),
    ("total_share", "inc_share",  "分享"),
    ("total_reply", "incr_reply", "评论"),
    ("total_dm",    "incr_dm",    "弹幕"),
    ("total_elec",  "inc_elec",   "充电"),
]

# pandect type → 逐日趋势列（顺序即表格列顺序）
PANDECT_TYPES = [
    (1, "播放"),
    (8, "点赞"),
    (5, "投币"),
    (6, "收藏"),
    (4, "分享"),
    (3, "评论"),
    (2, "弹幕"),
    (7, "充电"),
]


def scrape(days=30):
    cookie = load_cookie()
    referer = f"{MEMBER}/platform/data-up/video/"
    sess = make_session(cookie, referer)
    print(f"[bili-account] 抓账号累计 + 近 {days} 天逐日趋势", file=sys.stderr)

    stat = get_json(sess, f"{MEMBER}/x/web/index/stat")

    trends = {}
    for t, _label in PANDECT_TYPES:
        series = get_json(sess, f"{MEMBER}/x/web/data/pandect", {"type": t})
        # [{date_key(秒级时间戳), total_inc}] → {date: value}
        trends[t] = {int(p["date_key"]): p.get("total_inc") for p in (series or [])}

    return {"stat": stat, "trends": trends, "days": days}


def build_markdown(data):
    stat = data["stat"]
    trends = data["trends"]
    days = data["days"]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    out = []
    out.append("# 账号数据（B站）\n")
    out.append(f"\n> 最近更新：{stamp}　｜　抓取脚本：scripts/bilibili_account.py（自动抓取，B站创作者中心）\n")
    out.append(f"> 数据口径：累计总量 + 昨日新增 + 近 {days} 天逐日趋势　｜　来源接口：member/x/web/index/stat + data/pandect\n")
    out.append("> 说明：B站后台每日 12:00 更新前一日数据，逐日趋势最后一天通常是前一日。\n\n")

    # 累计 + 昨日新增
    out.append("## 核心指标总览（累计 / 昨日新增）\n\n")
    out.append("| 指标 | 累计 | 昨日新增 |\n| --- | --- | --- |\n")
    for tk, ik, label in STAT_FIELDS:
        total = stat.get(tk)
        inc = stat.get(ik)
        total_s = f"{int(total):,}" if total is not None else "-"
        inc_s = f"{int(inc):+,}" if inc is not None else "-"
        out.append(f"| {label} | {total_s} | {inc_s} |\n")
    out.append("\n")

    # 逐日趋势（各 type 的 date_key 一致，取播放的日期轴为准）
    all_dates = set()
    for t, _ in PANDECT_TYPES:
        all_dates.update(trends.get(t, {}).keys())
    dates = sorted(all_dates)[-days:]

    out.append(f"## 逐日趋势（近 {days} 天）\n\n")
    header = "| 日期 | " + " | ".join(label for _, label in PANDECT_TYPES) + " |"
    out.append(header + "\n")
    out.append("| --- | " + " | ".join(["---"] * len(PANDECT_TYPES)) + " |\n")
    for d in dates:
        day = datetime.fromtimestamp(d).strftime("%Y-%m-%d")
        row = [day]
        for t, _ in PANDECT_TYPES:
            v = trends.get(t, {}).get(d)
            row.append(f"{int(v):,}" if v is not None else "-")
        out.append("| " + " | ".join(row) + " |\n")
    out.append("\n> 逐日均为当日增量；粉丝逐日增量后台未开放，粉丝仅见上方累计+昨日新增。\n")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="B站创作者中心账号数据 → workspace/账号数据/B站.md")
    ap.add_argument("--days", type=int, default=30, help="逐日趋势天数（默认 30，后台上限约 30）")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不落盘")
    ap.add_argument("--save-raw", action="store_true", help="把原始 JSON 存到账号数据 raw/ 下")
    args = ap.parse_args()

    data = scrape(args.days)
    md = build_markdown(data)

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
        raw_path = os.path.join(raw_dir, f"bilibili_account_raw_{stamp}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已归档原始 JSON：{raw_path}")


if __name__ == "__main__":
    main()
