#!/usr/bin/env python3
"""
小红书创作者后台「账号数据中心」自动抓取 → 落盘到 workspace/账号数据/小红书.md。

原理：
    数据中心（statistics/account/v2）是 SPA，账号级数字来自登录态签名接口
    （x-s/x-t 签名，纯 requests 无法自己算）。本脚本用 Playwright 驱动真实
    Chromium，注入创作者后台会话 cookie，打开数据中心页，在页面 response 事件里
    截三个接口的 JSON，解析成 Markdown。

四个关键接口：
    - api/galaxy/v2/creator/datacenter/account/base
        → 账号概览指标，data 下含 seven（近7天）/ thirty（近30天）两段，每段带
          观看/曝光/点赞/收藏/评论/分享/涨粉/净增粉/流失粉/主页访问/完播率/封面点
          击率/人均时长… 以及每项的逐日 *_list 趋势
        ⚠️ 注意：base 只给「区间涨粉增量」，没有「当前总粉丝量」
    - api/galaxy/creator/home/personal_info
        → 账号现状快照：fans_count（当前总粉丝量）/ follow_count（关注数）/
          faved_count（获赞与收藏）/ grow_info.max_fans_count（历史峰值粉丝）/
          grow_info.level（创作者等级）。这个接口只在创作者首页 /new/home 触发，
          所以 scrape 会先访问首页再访问数据中心
    - api/galaxy/v2/creator/datacenter/audience/source/account
        → 流量来源占比（视频推荐/首页推荐/搜索/主页/其他），也分 seven/thirty
    - api/galaxy/user/info
        → 账号昵称 / 小红书号（用于表头）

用法：
    # 抓账号数据并落盘（近7天 + 近30天都写）
    python3 xiaohongshu_account.py

    # 只预览不落盘 / 显示浏览器窗口
    python3 xiaohongshu_account.py --dry-run
    python3 xiaohongshu_account.py --show

cookie：
    默认读技能根目录下的 .xiaohongshu_cookie（已 gitignore）。cookie 过期会抓到空数据，
    报错提示后重新从浏览器复制 document.cookie 覆盖该文件即可。

依赖：
    pip install playwright   （Chromium 内核复用 ms-playwright 缓存）
"""
import argparse
import json
import os
import sys
from datetime import datetime

from playwright.sync_api import sync_playwright

# 复用 xiaohongshu_metrics.py 里的 Chromium 定位 / cookie 解析（同目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xiaohongshu_metrics import find_chromium, parse_cookies, COOKIE_FILE, fmt  # noqa: E402
from _paths import workspace_root  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT_DIR = os.path.join(workspace_root(), "账号数据")  # 回指创作 skill 的 workspace/
OUT_MD = os.path.join(ACCOUNT_DIR, "小红书.md")

URL = "https://creator.xiaohongshu.com/statistics/account/v2"
HOME_URL = "https://creator.xiaohongshu.com/new/home"  # personal_info（总粉丝量）只在首页触发

# 账号概览指标：英文键 → (中文标签, 类型)  int=计数 pct=百分比(已是百分数) sec=秒
ACCOUNT_FIELDS = [
    ("view_count",         "观看量",       "int"),
    ("impl_count",         "曝光量",       "int"),
    ("home_view_count",    "主页访问",     "int"),
    ("like_count",         "点赞量",       "int"),
    ("collect_count",      "收藏量",       "int"),
    ("comment_count",      "评论量",       "int"),
    ("share_count",        "分享量",       "int"),
    ("rise_fans_count",    "新增粉丝",     "int"),
    ("loss_fans_count",    "流失粉丝",     "int"),
    ("net_rise_fans_count", "净增粉丝",     "int"),
    ("publish_note_num",   "发布笔记数",   "int"),
    ("publish_video_note_num", "发布视频数", "int"),
    ("cover_click_rate",   "封面点击率",   "pct"),
    ("video_full_view_rate", "完播率",     "pct"),
    ("home_conversion_rise_fans_rate", "主页访问转粉率", "pct"),
    ("avg_view_time",      "人均观看时长", "sec"),
]

# 逐日趋势分组：中文小节名 → (base 里的 *_list 键, 中文列头, 取值字段)
TREND_GROUPS = [
    ("每日互动", [
        ("view_list",           "观看",   "count"),
        ("impl_count_list",     "曝光",   "count"),
        ("home_view_list",      "主页访问", "count"),
        ("like_list",           "点赞",   "count"),
        ("collect_list",        "收藏",   "count"),
        ("comment_list",        "评论",   "count"),
        ("share_list",          "分享",   "count"),
    ]),
    ("每日粉丝", [
        ("rise_fans_list",      "新增粉",  "count"),
        ("loss_fans_count_list", "流失粉", "count"),
        ("net_rise_fans_count_list", "净增粉", "count"),
        ("publish_note_num_list", "发布数", "count"),
    ]),
    ("每日质量", [
        ("cover_click_rate_list",  "封面点击率%", "double"),
        ("video_full_view_rate_list", "完播率%",  "double"),
        ("avg_view_time_list",     "人均时长s",   "double"),
    ]),
]

# 流量来源 source_type → 中文
SOURCE_TYPE_LABELS = {
    1: "首页推荐", 2: "搜索", 3: "个人主页", 4: "视频推荐",
    5: "关注页面", 6: "附近", 99: "其他来源",
}

PERIODS = [("seven", "近7天"), ("thirty", "近30天")]

# 账号现状快照：personal_info 里的字段 → (中文标签, 取值路径)  取值路径用点分层
PERSONAL_FIELDS = [
    ("当前总粉丝量", "fans_count"),
    ("获赞与收藏",   "faved_count"),
    ("关注数",       "follow_count"),
    ("历史峰值粉丝", "grow_info.max_fans_count"),
    ("创作者等级",   "grow_info.level"),
]


def _fmt_date(ms):
    """毫秒时间戳 → 'YYYY-MM-DD'。"""
    if not ms:
        return "-"
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")


def scrape(headless=True, wait=13):
    """驱动浏览器抓三个接口，返回 {base, source, user}。"""
    raw = open(COOKIE_FILE, encoding="utf-8").read().strip()
    cookies = parse_cookies(raw)
    print(f"[xhs-account] 加载 {len(cookies)} 个 cookie", file=sys.stderr)

    captured = {"base": None, "source": None, "user": None, "personal": None}

    def on_response(resp):
        u = resp.url
        try:
            if "datacenter/account/base" in u:
                body = resp.json()
                if body.get("data", {}).get("seven"):
                    captured["base"] = body["data"]
            elif "datacenter/audience/source/account" in u:
                body = resp.json()
                if body.get("data", {}).get("seven"):
                    captured["source"] = body["data"]
            elif "creator/home/personal_info" in u:
                body = resp.json()
                if body.get("data", {}).get("fans_count") is not None:
                    captured["personal"] = body["data"]
            elif "galaxy/user/info" in u:
                body = resp.json()
                if body.get("data", {}).get("userName"):
                    captured["user"] = body["data"]
        except Exception:
            pass

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
        pg.on("response", on_response)
        try:
            # 先打首页拿 personal_info（当前总粉丝量），再进数据中心拿区间指标
            pg.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(6000)
            pg.goto(URL, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(wait * 1000)
            pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            pg.wait_for_timeout(4000)
        finally:
            b.close()

    if not captured["base"]:
        raise RuntimeError("未抓到 account/base 数据 —— cookie 可能已过期，请重新导出覆盖 .xiaohongshu_cookie")
    if not captured["personal"]:
        print("[xhs-account] ⚠️ 未抓到 personal_info（总粉丝量），仅落区间数据；可加大 --wait 重试", file=sys.stderr)
    return captured


def _build_period(out, base, source, period_key, period_label):
    seg = base.get(period_key)
    if not seg:
        return
    begin = _fmt_date(seg.get("begin_time"))
    end = _fmt_date(seg.get("end_time"))

    out.append(f"## {period_label}总览（{begin} ~ {end}）\n\n")
    out.append("| 指标 | 数值 | 环比 |\n| --- | --- | --- |\n")
    for key, label, typ in ACCOUNT_FIELDS:
        if key not in seg:
            continue
        rate = seg.get(f"{key}_rate")
        show = seg.get(f"{key}_rate_display")
        trend = ""
        if show and rate is not None:
            arrow = "↑" if rate > 0 else ("↓" if rate < 0 else "→")
            trend = f"{arrow} {rate:+d}%"
        out.append(f"| {label} | {fmt(seg.get(key), typ)} | {trend or '-'} |\n")
    out.append("\n")

    # 流量来源
    if source and source.get(period_key):
        out.append(f"### {period_label}流量来源\n\n")
        out.append("| 来源 | 占比 |\n| --- | --- |\n")
        for s in sorted(source[period_key], key=lambda x: x.get("value", 0), reverse=True):
            label = s.get("title") or SOURCE_TYPE_LABELS.get(s.get("source_type"), str(s.get("source_type")))
            out.append(f"| {label} | {s.get('value', 0)}% |\n")
        out.append("\n")


def _build_trends(out, base):
    """近7天逐日趋势（seven 段带 7 个点，thirty 段虽有 list 但更长，这里用 seven 做逐日表）。"""
    seg = base.get("seven")
    if not seg:
        return
    for section, cols in TREND_GROUPS:
        all_ts = set()
        for key, _lbl, _vf in cols:
            for pt in seg.get(key) or []:
                all_ts.add(pt.get("date"))
        if not all_ts:
            continue
        ts_list = sorted(t for t in all_ts if t)
        out.append(f"## {section}（近7天逐日）\n\n")
        out.append("| 日期 | " + " | ".join(c[1] for c in cols) + " |\n")
        out.append("| --- | " + " | ".join(["---"] * len(cols)) + " |\n")
        idx = {}
        for key, _lbl, vf in cols:
            idx[key] = {pt.get("date"): pt.get("count_with_double" if vf == "double" else "count")
                        for pt in (seg.get(key) or [])}
        for t in ts_list:
            row = [_fmt_date(t)]
            for key, _lbl, vf in cols:
                v = idx[key].get(t)
                if v is None:
                    row.append("-")
                elif vf == "double":
                    row.append(f"{float(v):.1f}")
                else:
                    row.append(f"{int(round(float(v))):,}")
            out.append("| " + " | ".join(row) + " |\n")
        out.append("\n")


def _dig(obj, path):
    """按点分路径 'grow_info.level' 从嵌套 dict 取值，缺失返回 None。"""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _build_personal(out, personal):
    """账号现状快照（当前总粉丝量等），来自 personal_info 接口。"""
    if not personal:
        return
    out.append("## 账号现状（快照）\n\n")
    out.append("| 指标 | 数值 |\n| --- | --- |\n")
    for label, path in PERSONAL_FIELDS:
        v = _dig(personal, path)
        if v is None:
            continue
        out.append(f"| {label} | {fmt(v, 'int') if isinstance(v, (int, float)) else v} |\n")
    out.append("\n> 说明：账号现状为抓取当刻的累计快照（总粉丝量为绝对值，非区间增量）。\n\n")


def build_markdown(data):
    base = data["base"]
    source = data.get("source")
    personal = data.get("personal")
    user = data.get("user") or {}
    name = user.get("userName", "") or (personal or {}).get("name", "")
    red_id = user.get("redId", "") or (personal or {}).get("red_num", "")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    out = []
    title_who = f"{name} · 小红书" if name else "小红书"
    out.append(f"# 账号数据（{title_who}）\n")
    out.append(f"\n> 最近更新：{stamp}　｜　抓取脚本：scripts/xiaohongshu_account.py（自动抓取，小红书创作者数据中心）\n")
    if red_id:
        out.append(f"> 小红书号：{red_id}\n")
    out.append("> 数据口径：账号现状快照 + 近7天 / 近30天区间聚合 + 近7天逐日趋势　｜　来源接口：creator/home/personal_info、datacenter/account/base\n\n")

    _build_personal(out, personal)

    for pk, pl in PERIODS:
        _build_period(out, base, source, pk, pl)

    _build_trends(out, base)

    out.append("> 说明：环比为该指标相对上一周期的变化（后台标注可展示时才显示）；逐日为当日增量/当日比率。\n")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="小红书创作者数据中心账号数据 → workspace/账号数据/小红书.md")
    ap.add_argument("--show", action="store_true", help="显示浏览器窗口（调试）")
    ap.add_argument("--wait", type=int, default=13, help="页面加载后等待秒数（默认 13）")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不落盘")
    ap.add_argument("--save-raw", action="store_true", help="把原始 JSON 存到账号数据 raw/ 下")
    args = ap.parse_args()

    data = scrape(headless=not args.show, wait=args.wait)
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
        raw_path = os.path.join(raw_dir, f"xiaohongshu_account_raw_{stamp}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已归档原始 JSON：{raw_path}")


if __name__ == "__main__":
    main()
