#!/usr/bin/env python3
"""
抖音创作者后台「作品数据」自动抓取 → 落盘到视频工作目录的《数据/抖音_数据.md》。

原理：
    work-detail 页是 SPA，数字全部来自登录态下的签名 XHR 接口（a_bogus/msToken 签名）。
    纯 requests 无法自己算签名，所以用 Playwright 驱动真实 Chromium，注入创作者后台的
    会话 cookie，让页面自己发这些签名请求，我们在 response 事件里把 JSON 截下来解析。

三个关键接口：
    - web/api/creator/item/mget            → 核心指标（播放/点赞/评论/完播/涨粉…）
    - janus/.../item_analysis/metrics_trend → 逐小时趋势（播放量、涨粉数按小时）
    - janus/.../diagnose/item_compare      → 与本账号近 10 条视频的对标（change_ratio）

用法：
    # 按视频目录名（= workspace 下的文件夹名，通常含日期前缀）落盘
    python3 douyin_metrics.py \
        --url "https://creator.douyin.com/creator-micro/work-management/work-detail/7674277612683939072" \
        --video "2026-08-16_贵州茅台十年首降，是失速还是在挤水分"

    # 只抓取打印、不落盘（调试）
    python3 douyin_metrics.py --url "..." --dry-run

    # 显示浏览器窗口排查（默认无头）
    python3 douyin_metrics.py --url "..." --video "..." --show

cookie：
    默认读技能根目录下的 .douyin_cookie（已 gitignore）。cookie 过期会抓到空数据，
    重新从浏览器复制 document.cookie 覆盖该文件即可。

依赖：
    pip install playwright   （Chromium 内核复用 ms-playwright 缓存）
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime
from http.cookies import SimpleCookie

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import workspace_root  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = workspace_root()  # 本 skill 的 workspace/
COOKIE_FILE = os.path.join(SKILL_DIR, ".douyin_cookie")  # cookie 跟着本 skill 走

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 指标英文键 → (中文标签, 类型)  type: int=整数计数, pct=百分比小数, sec=秒
METRIC_FIELDS = [
    ("view_count",           "播放量",       "int"),
    ("like_count",           "点赞量",       "int"),
    ("comment_count",        "评论量",       "int"),
    ("share_count",          "分享量",       "int"),
    ("favorite_count",       "收藏量",       "int"),
    ("danmaku_count",        "弹幕量",       "int"),
    ("subscribe_count",      "新增粉丝数",    "int"),
    ("unsubscribe_count",    "取关数",       "int"),
    ("homepage_visit_count", "主页访问量",    "int"),
    ("dislike_count",        "不感兴趣数",    "int"),
    ("download_count",       "下载数",       "int"),
    ("cover_show",           "封面曝光数",    "int"),
    ("cover_click_rate",     "封面点击率",    "pct"),
    ("like_rate",            "点赞率",       "pct"),
    ("comment_rate",         "评论率",       "pct"),
    ("share_rate",           "分享率",       "pct"),
    ("favorite_rate",        "收藏率",       "pct"),
    ("subscribe_rate",       "涨粉率",       "pct"),
    ("completion_rate",      "完播率",       "pct"),
    ("completion_rate_5s",   "5s完播率",     "pct"),
    ("bounce_rate_2s",       "2s跳出率",     "pct"),
    ("avg_view_proportion",  "平均播放占比",  "pct"),
    ("avg_view_second",      "平均播放时长",  "sec"),
    ("fan_view_proportion",  "粉丝播放占比",  "pct"),
]

# 净涨粉 = 新增 - 取关，单独算
# item_compare 的 change_ratio 用英文键，复用 METRIC_FIELDS 的中文标签，另补几个只在对标里出现的
COMPARE_LABELS = {key: label for key, label, _ in METRIC_FIELDS}
COMPARE_LABELS.update({"dislike_rate": "不感兴趣率"})

# 流量来源 play_source 的 key → 中文（对齐后台「流量来源」面板）
PLAY_SOURCE_LABELS = {
    "homepage_hot": "推荐页",
    "search": "搜索",
    "other": "其他",
    "follow": "关注页",
    "homepage": "个人主页",
    "message": "消息",
    "familiar": "关注的人/好友",
    "compilation": "合集",
    "fresh": "最新/同城",
}


def find_chromium():
    env = os.environ.get("PW_CHROMIUM")
    if env and os.path.exists(env):
        return env
    pats = [
        os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/Google Chrome for Testing"),
        os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"),
        os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-linux/chrome"),
    ]
    for p in pats:
        hits = sorted(glob.glob(p), reverse=True)
        if hits:
            return hits[0]
    return None


def parse_cookies(raw):
    ck = SimpleCookie()
    ck.load(raw)
    return [{"name": k, "value": m.value, "domain": ".douyin.com", "path": "/"}
            for k, m in ck.items()]


def extract_item_id(url):
    m = re.search(r"work-detail/(\d+)", url)
    return m.group(1) if m else None


def fmt(val, typ):
    """按类型格式化指标值。"""
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


def scrape(url, headless=True, wait=9):
    """驱动浏览器抓取三个接口的 JSON，返回 dict。"""
    item_id = extract_item_id(url)
    if not item_id:
        raise ValueError(f"无法从 URL 解析 item_id: {url}")

    raw = open(COOKIE_FILE, encoding="utf-8").read().strip()
    cookies = parse_cookies(raw)
    print(f"[douyin] item_id={item_id}，加载 {len(cookies)} 个 cookie", file=sys.stderr)

    captured = {"mget": None, "trend": [], "compare": None,
                "play_source": None, "progress": None, "comments": []}

    def on_response(resp):
        u = resp.url
        try:
            if "creator/item/mget" in u:
                body = resp.json()
                items = body.get("items") or []
                # 取字段最全的那次（带 metrics 的）
                if items and items[0].get("metrics"):
                    if not captured["mget"] or len(json.dumps(body)) > len(json.dumps(captured["mget"])):
                        captured["mget"] = items[0]
            elif "item_analysis/metrics_trend" in u:
                body = resp.json()
                if body.get("trend_map"):
                    captured["trend"].append(body["trend_map"])
            elif "diagnose/item_compare" in u:
                captured["compare"] = resp.json()
            elif "item/play/source" in u:
                body = resp.json()
                if body.get("play_source"):
                    captured["play_source"] = body["play_source"]
            elif "progress/analysis" in u:
                captured["progress"] = resp.json()
            elif "comment/list/select" in u:
                body = resp.json()
                for c in (body.get("comments") or []):
                    captured["comments"].append(c)
        except Exception:
            pass

    exe = find_chromium()
    kw = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if exe:
        kw["executable_path"] = exe
        print(f"[douyin] 浏览器: {exe}", file=sys.stderr)

    with sync_playwright() as p:
        b = p.chromium.launch(**kw)
        ctx = b.new_context(user_agent=UA, locale="zh-CN",
                            viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        pg = ctx.new_page()
        pg.on("response", on_response)
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=40000)
            pg.wait_for_timeout(wait * 1000)
            pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            pg.wait_for_timeout(3000)

            def click_tab(name):
                try:
                    pg.get_by_text(name, exact=True).first.click(timeout=8000)
                    print(f"[douyin] 切到 tab：{name}", file=sys.stderr)
                    pg.wait_for_timeout(5000)
                except Exception as e:
                    print(f"[douyin] 点击 {name} 失败（可忽略）：{e}", file=sys.stderr)

            # 流量分析 tab：触发 play/source（流量来源）+ progress/analysis（进度分析）
            click_tab("流量分析")
            pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            pg.wait_for_timeout(2500)

            # 评论管理 tab：触发 comment/list/select，滚动翻页把全部评论加载完
            click_tab("评论管理")
            for _ in range(8):
                pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                pg.wait_for_timeout(2000)
        finally:
            b.close()

    if not captured["mget"]:
        raise RuntimeError("未抓到 item/mget 数据 —— cookie 可能已过期，请重新导出覆盖 .douyin_cookie")
    return {"item_id": item_id, "url": url, **captured}


def merge_trend(trend_list):
    """把多次 metrics_trend 响应合并成 {指标: {小时: 值}}，只取分组 '0'（整体）。"""
    merged = {}
    for tm in trend_list:
        for metric, groups in tm.items():
            series = groups.get("0") or []
            if not series:
                continue
            merged.setdefault(metric, {})
            for pt in series:
                merged[metric][pt["date_time"]] = float(pt["value"])
    return merged


def build_markdown(video_title, data):
    m = data["mget"]["metrics"]
    desc = (data["mget"].get("description") or "").strip().replace("\n", " ")
    create_ts = data["mget"].get("create_time")
    pub = ""
    if create_ts:
        pub = datetime.fromtimestamp(int(create_ts)).strftime("%Y-%m-%d %H:%M")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    net_fans = None
    try:
        net_fans = int(round(float(m.get("subscribe_count", 0)))) - int(round(float(m.get("unsubscribe_count", 0))))
    except (TypeError, ValueError):
        pass

    out = []
    out.append(f"# 数据 · {video_title}\n")
    out.append(f"\n> 最近更新：{stamp}　｜　抓取脚本：scripts/douyin_metrics.py（自动抓取，抖音创作者后台）\n")
    out.append(f"> item_id：{data['item_id']}　｜　发布时间：{pub or '未知'}\n")
    out.append(f"> 作品文案：{desc}\n\n")

    # 核心指标
    out.append("## 指标总览\n\n")
    out.append("| 指标 | 数值 |\n| --- | --- |\n")
    if net_fans is not None:
        out.append(f"| **净涨粉数** | {net_fans:,} |\n")
    for key, label, typ in METRIC_FIELDS:
        if key in m:
            out.append(f"| {label} | {fmt(m[key], typ)} |\n")
    out.append("\n")

    # 对标近 10 条
    cmp = data.get("compare")
    if cmp and cmp.get("metrics"):
        ch = cmp.get("compare_hour")
        out.append(f"## 与本账号近期作品对标（发布约 {ch} 小时口径）\n\n")
        out.append("> change_ratio = 相对近 10 条视频中位数的变化，正=更好。\n\n")
        out.append("| 指标 | 相对变化 |\n| --- | --- |\n")
        for item in cmp["metrics"]:
            name = item.get("metric_name") or item.get("metric") or item.get("name") or ""
            cr = item.get("change_ratio")
            if cr is None:
                continue
            label = COMPARE_LABELS.get(name, name)
            arrow = "↑" if cr > 0 else ("↓" if cr < 0 else "→")
            out.append(f"| {label} | {arrow} {cr * 100:+.1f}% |\n")
        out.append("\n")

    # 逐小时趋势
    trend = merge_trend(data.get("trend") or [])
    if trend:
        out.append("## 逐小时趋势\n\n")
        for metric in ("view_count", "subscribe_count"):
            if metric not in trend:
                continue
            label = "播放量" if metric == "view_count" else "新增粉丝数"
            pts = sorted(trend[metric].items())
            total = sum(v for _, v in pts)
            peak_t, peak_v = max(pts, key=lambda kv: kv[1])
            out.append(f"### {label}（累计 {int(round(total)):,}，峰值 {peak_t} = {int(round(peak_v)):,}）\n\n")
            out.append("| 时间 | 值 |\n| --- | --- |\n")
            for t, v in pts:
                out.append(f"| {t} | {int(round(v)):,} |\n")
            out.append("\n")

    # 流量来源（流量分析 tab）
    ps = data.get("play_source")
    if ps:
        out.append("## 流量来源（流量分析 tab）\n\n")
        out.append("> 来源占比 = 该来源带来的播放占比；对比7日 = 相对近 7 日的变化。\n\n")
        out.append("| 来源 | 来源占比 | 对比7日 |\n| --- | --- | --- |\n")
        for item in sorted(ps, key=lambda x: x.get("value", 0), reverse=True):
            label = PLAY_SOURCE_LABELS.get(item.get("key"), item.get("key"))
            val = item.get("value", 0) * 100
            diff = item.get("history_difference", 0) * 100
            arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "→")
            out.append(f"| {label} | {val:.1f}% | {arrow} {diff:+.1f}% |\n")
        out.append("\n")

    # 进度分析（流量分析 tab · 观看趋势）
    prog = data.get("progress")
    if prog and (prog.get("jump_forward") or prog.get("jump_backward")):
        out.append("## 进度分析（流量分析 tab · 观看趋势）\n\n")
        out.append("> key = 视频进度（秒）。快进率高=该段被划走，回看率高=该段被反复看。定位中段流失点用。\n\n")
        fwd = {p["key"]: p["value"] for p in (prog.get("jump_forward") or [])}
        bwd = {p["key"]: p["value"] for p in (prog.get("jump_backward") or [])}
        keys = sorted(set(fwd) | set(bwd), key=lambda k: float(k))
        out.append("| 进度(秒) | 快进率 | 回看率 |\n| --- | --- | --- |\n")
        for k in keys:
            f_ = f"{fwd.get(k, 0) * 100:.2f}%" if k in fwd else "-"
            b_ = f"{bwd.get(k, 0) * 100:.2f}%" if k in bwd else "-"
            out.append(f"| {k} | {f_} | {b_} |\n")
        if fwd:
            pk, pv = max(fwd.items(), key=lambda kv: kv[1])
            out.append(f"\n快进率峰值：{pk}s = {pv * 100:.2f}%（最大流失段）\n")
        out.append("\n")

    # 评论 top 20（按点赞排序，评论管理 tab）
    comments = data.get("comments") or []
    if comments:
        # cid 去重后按点赞降序
        seen, uniq = set(), []
        for c in comments:
            cid = c.get("cid")
            if cid in seen:
                continue
            seen.add(cid)
            uniq.append(c)
        uniq.sort(key=lambda c: c.get("digg_count", 0), reverse=True)
        top = uniq[:20]
        out.append(f"## 高赞评论 Top {len(top)}（评论管理 tab，共抓到 {len(uniq)} 条）\n\n")
        out.append("| # | 点赞 | 回复数 | 用户 | 评论 |\n| --- | --- | --- | --- | --- |\n")
        for i, c in enumerate(top, 1):
            txt = (c.get("text") or "").replace("\n", " ").replace("|", "｜").strip()
            nick = ((c.get("user") or {}).get("nickname") or "").replace("|", "｜")
            out.append(f"| {i} | {c.get('digg_count', 0)} | {c.get('reply_comment_total', 0)} | {nick} | {txt} |\n")
        out.append("\n")

    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="抖音创作者后台作品数据 → 视频目录《数据/抖音_数据.md》")
    ap.add_argument("--url", required=True, help="work-detail 页面 URL")
    ap.add_argument("--video", help="视频目录名（workspace 下文件夹名，通常含日期前缀）")
    ap.add_argument("--show", action="store_true", help="显示浏览器窗口（调试）")
    ap.add_argument("--wait", type=int, default=9, help="页面加载后等待秒数（默认 9）")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不落盘")
    ap.add_argument("--save-raw", action="store_true", help="把原始 JSON 存到视频目录 数据/数据_raw/ 下")
    args = ap.parse_args()

    data = scrape(args.url, headless=not args.show, wait=args.wait)
    title = args.video or data["item_id"]
    md = build_markdown(title, data)

    if args.dry_run:
        sys.stdout.write(md)
        return

    if not args.video:
        sys.stderr.write("未指定 --video，无法定位落盘目录。用 --dry-run 预览，或补 --video。\n")
        sys.exit(1)

    out_dir = os.path.join(WORKSPACE, args.video)
    if not os.path.isdir(out_dir):
        sys.stderr.write(f"目录不存在：{out_dir}\n（请确认 --video 与 workspace 下文件夹名完全一致）\n")
        sys.exit(1)

    data_dir = os.path.join(out_dir, "数据")
    os.makedirs(data_dir, exist_ok=True)
    out_md = os.path.join(data_dir, "抖音_数据.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写入：{out_md}")

    if args.save_raw:
        raw_dir = os.path.join(data_dir, "数据_raw")
        os.makedirs(raw_dir, exist_ok=True)
        raw_path = os.path.join(raw_dir, f"douyin_raw_{data['item_id']}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已归档原始 JSON：{raw_path}")


if __name__ == "__main__":
    main()
