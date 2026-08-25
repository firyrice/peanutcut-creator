#!/usr/bin/env python3
"""
B站创作者后台「稿件数据」自动抓取 → 落盘到视频工作目录的《数据/B站_数据.md》。

原理（区别于抖音）：
    B站创作者后台的数据接口是**普通 cookie 鉴权的 JSON 接口**，不带 a_bogus/msToken
    这类前端签名，所以直接用 requests 带 cookie 请求即可，无需 Playwright 驱动浏览器。

三个关键接口，⚠️ 注意两套口径别搞混（曾踩坑：核心指标误取了 archive 的昨日结算值）：
    - api.bilibili.com/x/web-interface/view?bvid=  → BV 换 aid + 标题/发布时间 +【实时】stat
        ★ 指标总览（播放/点赞/投币/收藏/分享/评论/弹幕）**只认这里的 view.stat**，
          它才和后台「稿件分析」核心数据汇总卡片一致（实时、随时在涨）。无需登录。
    - member.../x/web/data/archive?aid=            → 【昨日结算】口径（每日 12:00 更新前一日），
          数值比实时小一截。**stat.play 这类是过时值，绝不能当核心指标**；这里只取
          play(观看数/时长/人均时长/平均播放进度) + group(粉丝/游客) + area(地域 Top10)。
    - member.../x/web/data/base?aid=               → 观众画像：地域(粉丝/非粉)、性别、年龄、终端
          ⚠️ 这份画像是【账号级累计】口径，B站在每支稿件页都展示同一份，非本视频维度。

用法：
    python3 bilibili_metrics.py \
        --url "https://member.bilibili.com/platform/upload-manager/article/data/BV1168J6rEBb" \
        --video "2026-08-19_牧原股份" --save-raw

    # 只抓取打印、不落盘（调试）
    python3 bilibili_metrics.py --url "..." --dry-run

cookie：
    默认读技能根目录下的 .bilibili_cookie（已 gitignore）。cookie 过期会返回 code!=0，
    重新从浏览器复制 document.cookie 覆盖该文件即可（至少含 SESSDATA / bili_jct / DedeUserID）。

依赖：
    pip install requests
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import workspace_root  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = workspace_root()  # 本 skill 的 workspace/
COOKIE_FILE = os.path.join(SKILL_DIR, ".bilibili_cookie")  # cookie 跟着本 skill 走

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

MEMBER = "https://member.bilibili.com"

# 稿件核心互动指标：用公开 view 接口的实时 stat（与后台「稿件分析」核心数据汇总一致，
# 非 data/archive 的昨日结算口径）。view.stat 键 → 中文标签
STAT_FIELDS = [
    ("view",     "播放量"),
    ("like",     "点赞"),
    ("coin",     "投币"),
    ("favorite", "收藏"),
    ("share",    "分享"),
    ("reply",    "评论"),
    ("danmaku",  "弹幕"),
]

# 终端 key → 中文（viewer_base 里的 plat_*）
PLAT_LABELS = [
    ("plat_ios",       "iOS"),
    ("plat_android",   "Android"),
    ("plat_h5",        "H5/网页"),
    ("plat_pc",        "PC客户端"),
    ("plat_out",       "站外"),
    ("plat_other_app", "其他端"),
]

# 年龄段 key → 中文（官方口径：0-16 / 16-25 / 25-40 / 40+）
AGE_LABELS = [
    ("age_one",   "0-16岁"),
    ("age_two",   "16-25岁"),
    ("age_three", "25-40岁"),
    ("age_four",  "40岁以上"),
]


def load_cookie():
    if not os.path.exists(COOKIE_FILE):
        sys.stderr.write(f"未找到 cookie 文件：{COOKIE_FILE}\n请从浏览器复制 B站 document.cookie 存进去。\n")
        sys.exit(1)
    return open(COOKIE_FILE, encoding="utf-8").read().strip()


def make_session(cookie, referer):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": referer,
        "Cookie": cookie,
    })
    return s


def extract_bvid(url):
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    return m.group(1) if m else None


def get_json(sess, url, params=None):
    r = sess.get(url, params=params, timeout=20)
    ct = r.headers.get("Content-Type", "")
    if "json" not in ct.lower():
        raise RuntimeError(f"接口未返回 JSON（{url}）—— cookie 可能已过期或无权限，请重新导出覆盖 .bilibili_cookie")
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"接口返回 code={body.get('code')} msg={body.get('message')}（{url}）")
    return body.get("data")


def scrape(url):
    cookie = load_cookie()
    bvid = extract_bvid(url)
    if not bvid:
        raise ValueError(f"无法从 URL 解析 BV 号：{url}")
    print(f"[bili] bvid={bvid}", file=sys.stderr)

    # 1) 公开接口：BV → aid + 标题/发布时间/公开 stat（不需要登录，但带上 cookie 无妨）
    pub = requests.get("https://api.bilibili.com/x/web-interface/view",
                       params={"bvid": bvid}, headers={"User-Agent": UA}, timeout=20).json()
    if pub.get("code") != 0:
        raise RuntimeError(f"view 接口返回 code={pub.get('code')} msg={pub.get('message')}")
    view = pub["data"]
    aid = view["aid"]
    print(f"[bili] aid={aid}", file=sys.stderr)

    # 结构校验：指标总览只认 view.stat（实时）。若接口改版没返回 stat，宁可报错也别静默出空表。
    if not isinstance(view.get("stat"), dict) or "view" not in view["stat"]:
        raise RuntimeError("view 接口未返回预期的 stat.view 字段——接口可能已改版，指标总览无法生成，"
                           "请检查 https://api.bilibili.com/x/web-interface/view 返回结构")

    referer = f"{MEMBER}/platform/upload-manager/article/data/{bvid}"
    sess = make_session(cookie, referer)

    # 2) 稿件核心数据
    archive = get_json(sess, f"{MEMBER}/x/web/data/archive", {"aid": aid})
    # 3) 观众画像
    base = get_json(sess, f"{MEMBER}/x/web/data/base", {"aid": aid})

    return {
        "bvid": bvid, "aid": aid, "url": url,
        "view": view, "archive": archive, "base": base,
    }


def _sum_platform(d):
    """viewer_base 的 fan/not_fan 合并成总量 dict。"""
    out = {}
    for grp in ("fan", "not_fan"):
        for k, v in (d.get(grp) or {}).items():
            out[k] = out.get(k, 0) + (v or 0)
    return out


def _pct(part, whole):
    return f"{part / whole * 100:.1f}%" if whole else "-"


def build_markdown(video_title, data):
    view = data["view"]
    archive = data["archive"]
    base = data["base"]
    stat = view.get("stat") or {}          # 实时核心指标（与后台「稿件分析」核心数据汇总一致）
    play = archive.get("play") or {}       # 播放行为，data/archive 昨日结算口径
    group = archive.get("group") or {}     # 粉丝/游客构成，同上昨日结算口径

    # 口径自检：实时播放量应 ≥ 昨日结算观看数。若反了，说明两个接口的口径又串了
    # （曾踩坑把结算值当核心指标），醒目告警提醒人工核对，别让错数静默落盘。
    rt_view = stat.get("view")
    settled_view = play.get("view")
    if isinstance(rt_view, (int, float)) and isinstance(settled_view, (int, float)) and rt_view < settled_view:
        print(f"[bili] ⚠️ 口径异常：实时播放量({rt_view}) < 昨日结算观看数({settled_view})，"
              f"疑似接口口径变动，请人工核对 view.stat 与 data/archive 后再采信数据。", file=sys.stderr)

    pub_ts = view.get("pubdate")
    pub = datetime.fromtimestamp(int(pub_ts)).strftime("%Y-%m-%d %H:%M") if pub_ts else "未知"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = (view.get("title") or "").replace("\n", " ")

    out = []
    out.append(f"# 数据（B站）· {video_title}\n")
    out.append(f"\n> 最近更新：{stamp}　｜　抓取脚本：scripts/bilibili_metrics.py（自动抓取，B站创作者后台）\n")
    out.append(f"> BV：{data['bvid']}　｜　aid：{data['aid']}　｜　发布时间：{pub}\n")
    out.append(f"> 稿件标题：{title}\n")
    out.append("> 口径说明：指标总览为**实时**（与后台「稿件分析」核心数据汇总一致）；下方播放行为/来源构成/地域为**昨日结算**（B站每日 12:00 更新前一日，故略滞后）。\n\n")

    # 核心指标（实时，来自公开 view 接口）
    out.append("## 指标总览（实时）\n\n")
    out.append("| 指标 | 数值 |\n| --- | --- |\n")
    for key, label in STAT_FIELDS:
        if stat.get(key) is not None:
            out.append(f"| {label} | {int(stat[key]):,} |\n")
    out.append("\n")

    # 播放行为（昨日结算）
    if play:
        dur = play.get("duration") or 0
        avg = play.get("avg_duration") or 0
        rate = play.get("rate")  # 万分比：平均播放进度
        out.append("## 播放行为（昨日结算）\n\n")
        out.append("| 指标 | 数值 |\n| --- | --- |\n")
        if play.get("view") is not None:
            out.append(f"| 观看人数（去重） | {int(play['view']):,} |\n")
        if dur:
            out.append(f"| 视频时长 | {dur}s（{dur // 60}分{dur % 60}秒） |\n")
        if avg:
            out.append(f"| 人均播放时长 | {avg}s |\n")
        if rate is not None:
            out.append(f"| 平均播放进度 | {rate / 100:.1f}% |\n")
        out.append("\n")

    # 粉丝/游客播放构成
    if group:
        fans = group.get("fans") or 0
        guest = group.get("guest") or 0
        total = fans + guest
        out.append("## 播放来源构成（粉丝 vs 游客）\n\n")
        out.append("| 来源 | 播放数 | 占比 |\n| --- | --- | --- |\n")
        out.append(f"| 粉丝 | {fans:,} | {_pct(fans, total)} |\n")
        out.append(f"| 游客 | {guest:,} | {_pct(guest, total)} |\n\n")

    # 地域 Top10（archive.area 已是 Top10 降序）
    area = archive.get("area") or []
    if area:
        out.append("## 观看地域 Top 10\n\n")
        out.append("| 地区 | 观看数 |\n| --- | --- |\n")
        for a in area:
            out.append(f"| {a.get('location')} | {int(a.get('count', 0)):,} |\n")
        out.append("\n")

    # 观众画像（base.viewer_base，合并粉丝+非粉）
    vb = base.get("viewer_base") or {}
    if vb:
        merged = _sum_platform(vb)
        male = merged.get("male", 0)
        female = merged.get("female", 0)
        gtotal = male + female
        out.append("## 账号观众画像（B站仅提供账号级画像，非本稿件维度）\n\n")
        out.append("> 注：B站在每支稿件数据页展示的性别/年龄/终端画像其实是全账号累计口径，非本视频独有。地域 Top10（上表）才是本稿件维度。\n\n")
        out.append("### 性别\n\n")
        out.append("| 性别 | 人数 | 占比 |\n| --- | --- | --- |\n")
        out.append(f"| 男 | {male:,} | {_pct(male, gtotal)} |\n")
        out.append(f"| 女 | {female:,} | {_pct(female, gtotal)} |\n\n")

        atotal = sum(merged.get(k, 0) for k, _ in AGE_LABELS)
        out.append("### 年龄\n\n")
        out.append("| 年龄段 | 人数 | 占比 |\n| --- | --- | --- |\n")
        for k, label in AGE_LABELS:
            out.append(f"| {label} | {merged.get(k, 0):,} | {_pct(merged.get(k, 0), atotal)} |\n")
        out.append("\n")

        ptotal = sum(merged.get(k, 0) for k, _ in PLAT_LABELS)
        out.append("### 终端\n\n")
        out.append("| 终端 | 人数 | 占比 |\n| --- | --- | --- |\n")
        for k, label in PLAT_LABELS:
            v = merged.get(k, 0)
            if v:
                out.append(f"| {label} | {v:,} | {_pct(v, ptotal)} |\n")
        out.append("\n")

    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="B站创作者后台稿件数据 → 视频目录《数据/B站_数据.md》")
    ap.add_argument("--url", required=True, help="稿件数据页 URL（含 BV 号）")
    ap.add_argument("--video", help="视频目录名（workspace 下文件夹名，通常含日期前缀）")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不落盘")
    ap.add_argument("--save-raw", action="store_true", help="把原始 JSON 存到视频目录 数据/数据_raw/ 下")
    args = ap.parse_args()

    data = scrape(args.url)
    title = args.video or data["bvid"]
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
    out_md = os.path.join(data_dir, "B站_数据.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写入：{out_md}")

    if args.save_raw:
        raw_dir = os.path.join(data_dir, "数据_raw")
        os.makedirs(raw_dir, exist_ok=True)
        raw_path = os.path.join(raw_dir, f"bilibili_raw_{data['bvid']}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已归档原始 JSON：{raw_path}")


if __name__ == "__main__":
    main()
