#!/usr/bin/env python3
"""
把手动导出的发布数据 xlsx 归纳成 workspace 下的 Markdown 汇总表，并保留原始 xlsx。

设计目标：让"发布后追踪数据"成为一个可重复、幂等的动作，而不是每次手贴表格。
字段以后可能会变，脚本按 sheet 名智能识别并写入对应小节，认不出的 sheet 也会
原样转成表格附在末尾，不丢数据。

用法:
    # 稿件级：把一支视频的数据（可多份 xlsx）写进 workspace/<标题>/数据.md
    python3 ingest_metrics.py --video "五粮液利润翻倍，散户全在骂修改液" \
        ~/Desktop/稿件数据示例（五粮液）-1.xlsx ~/Desktop/稿件数据示例（五粮液）-2.xlsx

    # 账号级：写进 workspace/账号数据/账号数据.md
    python3 ingest_metrics.py --account ~/Desktop/账号数据示例.xlsx

    # 只预览不写文件
    python3 ingest_metrics.py --account foo.xlsx --dry-run

原始 xlsx 会被复制进对应目录的 raw/ 下归档（--no-archive 可关闭）。

依赖:
    pip install openpyxl
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

try:
    import openpyxl
except ImportError:
    sys.stderr.write("缺少依赖：pip install openpyxl\n")
    sys.exit(1)

# workspace/ 在本 skill 内（本脚本无 cookie 依赖）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import workspace_root  # noqa: E402
WORKSPACE = workspace_root()

# sheet 名关键字 → 归一化的小节标题。认不出的 sheet 走兜底逻辑。
SHEET_ALIASES = [
    (("指标数据", "指标"), "指标总览"),
    (("每小时", "趋势"), "逐小时播放趋势"),
    (("进度分析", "进度"), "进度分析"),
]


def _classify_sheet(title):
    """按 sheet 名关键字归类到标准小节名；认不出返回原名。"""
    for keys, label in SHEET_ALIASES:
        if any(k in title for k in keys):
            return label
    return title


def _read_rows(ws):
    """读出非空行，尾部空列裁掉。返回 list[list]。"""
    rows = []
    for row in ws.iter_rows(values_only=True):
        r = list(row)
        while r and r[-1] is None:
            r.pop()
        if any(c is not None for c in r):
            rows.append([("" if c is None else c) for c in r])
    return rows


def _rows_to_md_table(rows):
    """第一行当表头，其余当数据，渲染成 Markdown 表格。"""
    if not rows:
        return "_（无数据）_\n"
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    out = ["| " + " | ".join(str(c) for c in header) + " |"]
    out.append("| " + " | ".join(["---"] * ncol) + " |")
    for r in body:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _kv_rows_to_md(rows):
    """两行（表头+一行值）的指标型 sheet，转成竖排 指标|数值 表，更好读。"""
    if len(rows) == 2 and len(rows[0]) == len(rows[1]):
        out = ["| 指标 | 数值 |", "| --- | --- |"]
        for k, v in zip(rows[0], rows[1]):
            out.append(f"| {k} | {v} |")
        return "\n".join(out) + "\n"
    return _rows_to_md_table(rows)


def build_markdown(title, xlsx_paths, level):
    """把若干 xlsx 的所有 sheet 归纳成一份 Markdown 文本。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if level == "account":
        head = "# 账号数据（抖音）\n"
        intro = "账号级每日数据汇总，来源为抖音创作者后台手动导出。\n"
    else:
        head = f"# 数据 · {title}\n"
        intro = "本稿件发布后数据汇总，来源为抖音创作者后台手动导出。数据与本目录的《视频基础信息.md》为同一支视频。\n"

    parts = [head, f"\n> 最近更新：{stamp}　｜　导入脚本：scripts/ingest_metrics.py\n\n{intro}\n"]

    seen_labels = {}
    for path in xlsx_paths:
        wb = openpyxl.load_workbook(path, data_only=True)
        for ws in wb.worksheets:
            rows = _read_rows(ws)
            if not rows:
                continue
            label = _classify_sheet(ws.title)
            # 同名小节去重：第二次出现加来源文件名后缀
            if label in seen_labels:
                label = f"{label}（{os.path.basename(path)}·{ws.title}）"
            seen_labels[label] = True
            parts.append(f"## {label}\n\n")
            # 指标型（就两行）走竖排 KV 表，趋势/进度型走普通表
            if len(rows) == 2 and "趋势" not in label and "进度" not in label:
                parts.append(_kv_rows_to_md(rows))
            else:
                parts.append(_rows_to_md_table(rows))
            parts.append("\n")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser(description="发布数据 xlsx → workspace Markdown 汇总表")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--video", metavar="标题", help="稿件级：视频文件夹名（= 最终标题）")
    g.add_argument("--account", action="store_true", help="账号级：写进 workspace/账号数据.md")
    ap.add_argument("xlsx", nargs="+", help="一个或多个源 xlsx 文件路径")
    ap.add_argument("--no-archive", action="store_true", help="不把原始 xlsx 复制进 raw/")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = ap.parse_args()

    for p in args.xlsx:
        if not os.path.isfile(p):
            sys.stderr.write(f"找不到文件：{p}\n")
            sys.exit(1)

    if args.account:
        level = "account"
        title = "账号数据"
        out_dir = os.path.join(WORKSPACE, "账号数据")
        out_md = os.path.join(out_dir, "抖音.md")
        raw_dir = os.path.join(out_dir, "raw")           # 账号级仍归档到 账号数据/raw/
    else:
        level = "video"
        title = args.video
        out_dir = os.path.join(WORKSPACE, args.video)
        data_dir = os.path.join(out_dir, "数据")
        os.makedirs(data_dir, exist_ok=True)
        out_md = os.path.join(data_dir, "抖音_数据.md")
        raw_dir = os.path.join(data_dir, "数据_raw")      # 稿件级归档到 数据/数据_raw/

    md = build_markdown(title, args.xlsx, level)

    if args.dry_run:
        sys.stdout.write(md)
        return

    os.makedirs(out_dir, exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写入：{out_md}")

    if not args.no_archive:
        os.makedirs(raw_dir, exist_ok=True)
        for p in args.xlsx:
            dst = os.path.join(raw_dir, os.path.basename(p))
            if os.path.abspath(p) != os.path.abspath(dst):
                shutil.copy2(p, dst)
                print(f"已归档原件：{dst}")


if __name__ == "__main__":
    main()
