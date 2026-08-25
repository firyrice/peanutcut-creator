#!/usr/bin/env python3
"""
小红书创作者后台「笔记数据」自动抓取 → 落盘到视频工作目录的《数据/小红书_数据.md》。

原理：
    note-detail 页是 SPA，数字全部来自登录态下的签名 XHR 接口（x-s/x-t 签名，纯
    requests 无法自己算）。所以用 Playwright 驱动真实 Chromium，注入创作者后台的
    会话 cookie，让页面自己发这些签名请求，我们在 response 事件里把 JSON 截下来解析。

两个关键接口（都在 creator.xiaohongshu.com/api/galaxy/creator/datacenter/note/ 下）：
    - note/base?note_id=…            → 核心指标（观看/点赞/收藏/评论/分享/涨粉/完播/
                                       封面点击率…）+ note_info（标题/封面/发布时间）
                                       + hour（逐小时趋势）
    - note/audience/source?note_id=… → 流量来源占比（视频推荐/首页推荐/搜索/主页…）

用法：
    # 按视频目录名（= workspace 下的文件夹名，通常含日期前缀）落盘
    python3 xiaohongshu_metrics.py \
        --url "https://creator.xiaohongshu.com/statistics/note-detail?noteId=6a85ad1b0000000028002b38" \
        --video "2026-08-19_牧原股份"

    # 只抓取打印、不落盘（调试）
    python3 xiaohongshu_metrics.py --url "..." --dry-run

    # 显示浏览器窗口排查（默认无头）
    python3 xiaohongshu_metrics.py --url "..." --video "..." --show

cookie：
    默认读技能根目录下的 .xiaohongshu_cookie（已 gitignore）。cookie 过期会抓到空数据，
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
from datetime import datetime
from http.cookies import SimpleCookie

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import workspace_root  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = workspace_root()  # 本 skill 的 workspace/
COOKIE_FILE = os.path.join(SKILL_DIR, ".xiaohongshu_cookie")  # cookie 跟着本 skill 走

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 单条笔记核心指标：英文键 → (中文标签, 类型)  int=计数 pct=百分比(已是百分数) sec=秒
NOTE_METRIC_FIELDS = [
    ("view_count",       "观看量",     "int"),
    ("impl_count",       "曝光量",     "int"),
    ("like_count",       "点赞量",     "int"),
    ("collect_count",    "收藏量",     "int"),
    ("comment_count",    "评论量",     "int"),
    ("share_count",      "分享量",     "int"),
    ("danmaku_count",    "弹幕量",     "int"),
    ("quote_count",      "引用量",     "int"),
    ("rise_fans_count",  "涨粉量",     "int"),
    ("play60s_count",    "60秒播放数",  "int"),
    ("cover_click_rate", "封面点击率",  "pct"),
    ("finish5s_rate",    "5秒完播率",   "pct"),
    ("full_view_rate",   "完播率",     "pct"),
    ("exit_view2s_rate", "2秒退出率",   "pct"),
    ("interaction_rate", "互动率",     "pct"),
    ("view_rate_with_fans", "粉丝观看占比", "pct"),
    ("view_time_avg",    "人均观看时长", "sec"),
]

# 逐小时趋势分组：中文小节名 → (hour 里的 *_list 键, 中文列头, 取值字段)
# value=count（整数计数）; value=double（count_with_double，比率/均值）
NOTE_HOUR_GROUPS = [
    ("逐小时互动", [
        ("view_list",    "观看",  "count"),
        ("like_list",    "点赞",  "count"),
        ("collect_list", "收藏",  "count"),
        ("comment_list", "评论",  "count"),
        ("share_list",   "分享",  "count"),
        ("rise_fans_list", "涨粉", "count"),
    ]),
    ("逐小时质量", [
        ("finish5s_list", "5秒完播率%", "double"),
        ("finish_list",   "完播率%",    "double"),
        ("view_time_list", "人均时长s", "double"),
        ("play60s_list",  "60秒播放",   "count"),
    ]),
]

# 流量来源 source_type → 中文（对齐后台「观众来源」面板）
SOURCE_TYPE_LABELS = {
    1: "首页推荐",
    2: "搜索",
    3: "个人主页",
    4: "视频推荐",
    5: "关注页面",
    6: "附近",
    99: "其他来源",
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
    return [{"name": k, "value": m.value, "domain": ".xiaohongshu.com", "path": "/"}
            for k, m in ck.items()]


def extract_note_id(url):
    m = re.search(r"noteId=([0-9a-fA-F]+)", url)
    return m.group(1) if m else None


def fmt(val, typ):
    """按类型格式化指标值。pct 字段已是百分数（9.8 表示 9.8%），不再乘 100。"""
    if val is None:
        return "-"
    try:
        f = float(val)
    except (TypeError, ValueError):
        return str(val)
    if typ == "int":
        return f"{int(round(f)):,}"
    if typ == "pct":
        return f"{f:.1f}%"
    if typ == "sec":
        return f"{f:.1f}s"
    return str(val)


def _fmt_ts(ms):
    """毫秒时间戳 → 'MM-DD HH:MM'。"""
    if not ms:
        return "-"
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")


def scrape(url, headless=True, wait=13):
    """驱动浏览器抓取笔记的两个接口，返回 dict。"""
    note_id = extract_note_id(url)
    if not note_id:
        raise ValueError(f"无法从 URL 解析 noteId: {url}")

    raw = open(COOKIE_FILE, encoding="utf-8").read().strip()
    cookies = parse_cookies(raw)
    print(f"[xhs] note_id={note_id}，加载 {len(cookies)} 个 cookie", file=sys.stderr)

    captured = {"base": None, "source": None}

    def on_response(resp):
        u = resp.url
        try:
            if "datacenter/note/base" in u:
                body = resp.json()
                if body.get("data", {}).get("note_info"):
                    captured["base"] = body["data"]
            elif "datacenter/note/audience/source" in u and "/detail" not in u:
                body = resp.json()
                if body.get("data", {}).get("source"):
                    captured["source"] = body["data"]["source"]
        except Exception:
            pass

    exe = find_chromium()
    kw = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if exe:
        kw["executable_path"] = exe
        print(f"[xhs] 浏览器: {exe}", file=sys.stderr)

    with sync_playwright() as p:
        b = p.chromium.launch(**kw)
        ctx = b.new_context(user_agent=UA, locale="zh-CN",
                            viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        pg = ctx.new_page()
        pg.on("response", on_response)
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(wait * 1000)
            pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            pg.wait_for_timeout(4000)
        finally:
            b.close()

    if not captured["base"]:
        raise RuntimeError("未抓到 note/base 数据 —— cookie 可能已过期，请重新导出覆盖 .xiaohongshu_cookie")
    return {"note_id": note_id, "url": url, **captured}


def build_markdown(video_title, data):
    base = data["base"]
    info = base.get("note_info", {})
    desc = (info.get("desc") or "").strip().replace("\n", " ")
    pub = _fmt_ts(info.get("post_time"))
    upd = _fmt_ts(base.get("data_last_update_time"))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    out = []
    out.append(f"# 小红书数据 · {video_title}\n")
    out.append(f"\n> 最近更新：{stamp}　｜　抓取脚本：scripts/xiaohongshu_metrics.py（自动抓取，小红书创作者后台）\n")
    out.append(f"> noteId：{data['note_id']}　｜　发布时间：{pub}　｜　数据更新至：{upd}\n")
    out.append(f"> 笔记标题：{desc}\n\n")

    # 核心指标
    out.append("## 指标总览\n\n")
    out.append("| 指标 | 数值 |\n| --- | --- |\n")
    for key, label, typ in NOTE_METRIC_FIELDS:
        if key in base:
            out.append(f"| {label} | {fmt(base[key], typ)} |\n")
    out.append("\n")

    # 流量来源
    src = data.get("source")
    if src:
        out.append("## 流量来源\n\n")
        out.append("> 占比 = 该来源带来的观看占比；含各来源的观看/曝光/互动细分。\n\n")
        out.append("| 来源 | 观看占比 | 观看数 | 曝光数 | 互动数 | 人均时长 |\n")
        out.append("| --- | --- | --- | --- | --- | --- |\n")
        for s in sorted(src, key=lambda x: x.get("value_with_double", 0), reverse=True):
            label = s.get("title") or SOURCE_TYPE_LABELS.get(s.get("source_type"), str(s.get("source_type")))
            i = s.get("info", {})
            out.append(f"| {label} | {fmt(s.get('value_with_double'), 'pct')} | "
                       f"{fmt(i.get('view_count'), 'int')} | {fmt(i.get('imp_count'), 'int')} | "
                       f"{fmt(i.get('interaction_count'), 'int')} | {fmt(i.get('view_time_avg'), 'sec')} |\n")
        out.append("\n")

    # 逐小时趋势
    hour = base.get("hour")
    if hour:
        for section, cols in NOTE_HOUR_GROUPS:
            # 汇总该分组涉及的所有小时点
            all_ts = set()
            for key, _lbl, _vf in cols:
                for pt in hour.get(key) or []:
                    all_ts.add(pt.get("date"))
            if not all_ts:
                continue
            ts_list = sorted(t for t in all_ts if t)
            out.append(f"## {section}\n\n")
            header = "| 时间 | " + " | ".join(c[1] for c in cols) + " |"
            out.append(header + "\n")
            out.append("| --- | " + " | ".join(["---"] * len(cols)) + " |\n")
            # 每个指标建 {ts: value} 索引
            idx = {}
            for key, _lbl, vf in cols:
                idx[key] = {pt.get("date"): pt.get("count_with_double" if vf == "double" else "count")
                            for pt in (hour.get(key) or [])}
            for t in ts_list:
                row = [_fmt_ts(t)]
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

    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="小红书创作者后台笔记数据 → 视频目录《数据/小红书_数据.md》")
    ap.add_argument("--url", required=True, help="note-detail 页面 URL（含 noteId）")
    ap.add_argument("--video", help="视频目录名（workspace 下文件夹名，通常含日期前缀）")
    ap.add_argument("--show", action="store_true", help="显示浏览器窗口（调试）")
    ap.add_argument("--wait", type=int, default=13, help="页面加载后等待秒数（默认 13）")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不落盘")
    ap.add_argument("--save-raw", action="store_true", help="把原始 JSON 存到视频目录 数据/数据_raw/ 下")
    args = ap.parse_args()

    data = scrape(args.url, headless=not args.show, wait=args.wait)
    title = args.video or data["note_id"]
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
    out_md = os.path.join(data_dir, "小红书_数据.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写入：{out_md}")

    if args.save_raw:
        raw_dir = os.path.join(data_dir, "数据_raw")
        os.makedirs(raw_dir, exist_ok=True)
        raw_path = os.path.join(raw_dir, f"xiaohongshu_raw_{data['note_id']}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已归档原始 JSON：{raw_path}")


if __name__ == "__main__":
    main()
