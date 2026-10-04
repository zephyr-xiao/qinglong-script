#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取本仓库的 GitHub Traffic 流量数据并存档到 stats/。

为什么需要它：GitHub 只在接口与网页保留**最近 14 天**的流量数据，过期即失，
想留下长期趋势只能定期抓取。本脚本把 14 天内的每日浏览量/克隆量按日期合并进
stats/traffic_daily.csv（同一天覆盖更新，因此漏跑几次也能补回来），并把来源
站点、热门页面与 star/fork 快照写入 stats/latest.json，最后提交并推送。

依赖：gh CLI（已登录、具备该仓库的 repo 权限）。
抓取失败时直接退出，不修改任何文件。

用法：
  python stats/fetch_traffic.py            # 抓取 + 存档 + 提交推送
  python stats/fetch_traffic.py --no-push  # 只抓取存档，不提交
  python stats/fetch_traffic.py --dry-run  # 只打印结果，不写文件

网络：gh 读取 HTTPS_PROXY / HTTP_PROXY 环境变量；直连 GitHub 不通时先设代理。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STATS_DIR = Path(__file__).resolve().parent
REPO_ROOT = STATS_DIR.parent
CSV_PATH = STATS_DIR / "traffic_daily.csv"
LATEST_PATH = STATS_DIR / "latest.json"
CSV_FIELDS = ["date", "views", "unique_visitors", "clones", "unique_cloners"]

_slug_cache: str | None = None


def run(cmd: list[str]) -> str:
    """执行命令并返回 stdout；失败抛 RuntimeError。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout


def repo_slug() -> str:
    """从 origin 远程地址解析 owner/repo，避免把仓库名写死在脚本里。"""
    global _slug_cache
    if _slug_cache is None:
        url = run(["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"]).strip()
        _slug_cache = url.rsplit("github.com", 1)[-1].lstrip(":/").removesuffix(".git")
    return _slug_cache


def gh_json(endpoint: str):
    base = f"repos/{repo_slug()}"
    return json.loads(run(["gh", "api", f"{base}/{endpoint}" if endpoint else base]))


def load_existing() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows[row["date"]] = row
    return rows


def merge_daily(rows: dict[str, dict], views: list[dict], clones: list[dict]) -> list[str]:
    """把接口返回的每日数据按日期并入 rows，返回本次新增的日期列表。"""
    added: list[str] = []

    def slot(day: str) -> dict:
        if day not in rows:
            added.append(day)
            rows[day] = {"date": day, "views": 0, "unique_visitors": 0,
                         "clones": 0, "unique_cloners": 0}
        return rows[day]

    for item in views:
        row = slot(item["timestamp"][:10])
        row["views"] = item["count"]
        row["unique_visitors"] = item["uniques"]
    for item in clones:
        row = slot(item["timestamp"][:10])
        row["clones"] = item["count"]
        row["unique_cloners"] = item["uniques"]
    return added


def render_csv(rows: dict[str, dict]) -> str:
    """渲染成 CSV 文本（保持与旧版一致的 CRLF 行尾，避免无谓的行尾 diff）。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for day in sorted(rows):
        writer.writerow({k: rows[day].get(k, 0) for k in CSV_FIELDS})
    return buf.getvalue()


def write_csv(rows: dict[str, dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        fh.write(render_csv(rows))


# ---------------------------- SVG 图表 ----------------------------

CHART_DAYS = 30  # 图表最多展示最近多少天
BLUE = "#0A84FF"
ORANGE = "#FF9500"
GRID = "#E5E5EA"
INK = "#1C1C1E"
MUTED = "#8E8E93"


def nice_max(value: int) -> int:
    """把 Y 轴上限向上取到 1/2/5×10^n，让刻度好看。"""
    if value <= 0:
        return 1
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for mult in (1, 2, 5, 10):
        if value <= mult * base:
            return mult * base
    return 10 * base


def smooth_path(points: list[tuple[float, float]]) -> str:
    """把折线点转成平滑曲线（Catmull-Rom 转三次贝塞尔）。

    流量是逐日小数值，平滑曲线比折线折角更耐看，也更容易看出趋势。
    """
    if len(points) < 2:
        return ""
    path = f"M {points[0][0]:.1f},{points[0][1]:.1f}"
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        path += (f" C {c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} "
                 f"{p2[0]:.1f},{p2[1]:.1f}")
    return path


def render_svg(rows: dict[str, dict], days: int = CHART_DAYS) -> str:
    """把每日数据画成一张自包含的 SVG（浏览=实线，独立访客=虚线）。

    只依赖标准库，不引入绘图包；配色为白底卡片 + 系统蓝，深浅色模式都能看。
    """
    series = [rows[day] for day in sorted(rows)][-days:]
    if not series:
        return "<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'></svg>"

    width, height = 780, 250
    pad_l, pad_r, pad_t, pad_b = 48, 18, 60, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    base_y = pad_t + plot_h

    views = [int(r["views"]) for r in series]
    visitors = [int(r["unique_visitors"]) for r in series]
    top = nice_max(max(max(views), max(visitors)))

    slot = plot_w / len(series)

    parts: list[str] = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' role='img' aria-label='仓库流量趋势'>",
        # 颜色以「显式属性」为准（GitHub 清洗 SVG 时可能剥掉 <style>），
        # <style> 只做深色模式覆盖：被剥掉只是退回浅色，不会画错。
        "<style>@media (prefers-color-scheme:dark){"
        ".card{fill:#161618;stroke:#303034}.ink{fill:#F2F2F7}"
        ".muted{fill:#98989F}.grid{stroke:#303034}}</style>",
        f"<rect class='card' x='0.5' y='0.5' width='{width - 1}' height='{height - 1}' rx='12' "
        f"fill='#FFFFFF' stroke='{GRID}'/>",
        f"<text class='ink' x='{pad_l}' y='28' font-size='15' font-weight='600' "
        f"font-family='-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif' "
        f"fill='{INK}'>仓库流量</text>",
    ]

    total_views = sum(views)
    # 每日「独立访客」是按天去重的，直接相加会跨天重复计数，因此称「人次」
    total_visits = sum(int(r["unique_visitors"]) for r in series)
    total_clones = sum(int(r["clones"]) for r in series)
    span = f"{series[0]['date'][5:]} ~ {series[-1]['date'][5:]}"
    parts.append(
        f"<text class='muted' x='{pad_l}' y='47' font-size='11' "
        f"font-family='-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif' "
        f"fill='{MUTED}'>{span} · 浏览 {total_views} 次 / 访客 {total_visits} 人次 / "
        f"克隆 {total_clones} 次</text>"
    )

    # 图例（右上）：圆点 + 文字
    legend_y = 25
    lx = width - pad_r - 168
    parts.append(f"<circle cx='{lx + 5}' cy='{legend_y}' r='4' fill='{BLUE}'/>")
    parts.append(f"<text class='muted' x='{lx + 14}' y='{legend_y + 4}' font-size='11' "
                 f"font-family='-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif' "
                 f"fill='{MUTED}'>浏览</text>")
    parts.append(f"<circle cx='{lx + 62}' cy='{legend_y}' r='4' fill='{ORANGE}'/>")
    parts.append(f"<text class='muted' x='{lx + 71}' y='{legend_y + 4}' font-size='11' "
                 f"font-family='-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif' "
                 f"fill='{MUTED}'>独立访客</text>")

    # 横向网格线 + Y 轴刻度
    for i in range(5):
        ratio = i / 4
        y = base_y - plot_h * ratio
        parts.append(f"<line class='grid' x1='{pad_l}' y1='{y:.1f}' x2='{pad_l + plot_w}' y2='{y:.1f}' "
                     f"stroke='{GRID}' stroke-width='1'/>")
        parts.append(f"<text class='muted' x='{pad_l - 8}' y='{y + 3.5:.1f}' font-size='10' "
                     f"text-anchor='end' font-family='-apple-system,Segoe UI,PingFang SC,"
                     f"Microsoft YaHei,sans-serif' fill='{MUTED}'>{round(top * ratio)}</text>")

    # 浏览：实线平滑曲线（主指标）
    view_pts = [(pad_l + slot * (i + 0.5), base_y - plot_h * v / top) for i, v in enumerate(views)]
    parts.append(f"<path d='{smooth_path(view_pts)}' fill='none' stroke='{BLUE}' "
                 f"stroke-width='2.2' stroke-linecap='round'/>")

    # 独立访客：虚线 + 圆点（次要指标，弱化处理）
    visit_pts = [(pad_l + slot * (i + 0.5), base_y - plot_h * v / top)
                 for i, v in enumerate(visitors)]
    parts.append(f"<path d='{smooth_path(visit_pts)}' fill='none' stroke='{ORANGE}' "
                 f"stroke-width='1.8' stroke-linecap='round' stroke-dasharray='5 3'/>")
    for x, y in visit_pts:
        parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.6' fill='{ORANGE}'/>")

    # X 轴只标首尾日期：中间日期挤在一起反而难读，趋势看形状就够了
    marks = [(0, "start")] if len(series) == 1 else [(0, "start"), (len(series) - 1, "end")]
    for idx, anchor in marks:
        x = pad_l + slot * (idx + 0.5)
        parts.append(f"<text class='muted' x='{x:.1f}' y='{height - 12}' font-size='10' "
                     f"text-anchor='{anchor}' font-family='-apple-system,Segoe UI,PingFang SC,"
                     f"Microsoft YaHei,sans-serif' fill='{MUTED}'>{series[idx]['date']}</text>")

    parts.append("</svg>")
    return "".join(parts)


def commit_and_push(summary: str) -> bool:
    """只提交 stats/ 目录；暂存区里若有其它改动则跳过，避免误提交。"""
    run(["git", "-C", str(REPO_ROOT), "add", "stats"])
    staged = run(["git", "-C", str(REPO_ROOT), "diff", "--cached", "--name-only"]).split()
    others = [p for p in staged if not p.startswith("stats/")]
    if others:
        print(f"⚠ 暂存区里还有 stats/ 以外的改动，跳过提交：{others}")
        return False
    if not staged:
        print("数据无变化，跳过提交")
        return False
    run(["git", "-C", str(REPO_ROOT), "commit", "-q", "-m", f"stats: 流量快照 {summary}"])
    run(["git", "-C", str(REPO_ROOT), "push", "origin", "HEAD"])
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取并归档本仓库的 GitHub 流量数据")
    parser.add_argument("--no-push", action="store_true", help="只存档，不提交推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = parser.parse_args()

    prev_csv = CSV_PATH.read_text(encoding="utf-8") if CSV_PATH.exists() else ""
    prev_latest = (json.loads(LATEST_PATH.read_text(encoding="utf-8"))
                   if LATEST_PATH.exists() else {})

    try:
        views = gh_json("traffic/views")
        clones = gh_json("traffic/clones")
        referrers = gh_json("traffic/popular/referrers")
        paths = gh_json("traffic/popular/paths")
        repo = gh_json("")
    except RuntimeError as exc:
        print(f"抓取失败，未做任何修改：{exc}", file=sys.stderr)
        return 1

    rows = load_existing()
    added = merge_daily(rows, views.get("views", []), clones.get("clones", []))

    latest = {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "repo": repo_slug(),
        "views_14d": {"count": views.get("count", 0), "uniques": views.get("uniques", 0)},
        "clones_14d": {"count": clones.get("count", 0), "uniques": clones.get("uniques", 0)},
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "watchers": repo.get("subscribers_count", 0),
        "referrers": [{"referrer": r["referrer"], "views": r["count"], "uniques": r["uniques"]}
                      for r in referrers],
        "popular_paths": [{"path": p["path"], "views": p["count"], "uniques": p["uniques"]}
                          for p in paths],
    }

    print(f"仓库 {repo_slug()}：14 天浏览 {views.get('count')} 次 / {views.get('uniques')} 人；"
          f"克隆 {clones.get('count')} 次 / {clones.get('uniques')} 个来源")
    print(f"CSV 新增 {len(added)} 天，累计 {len(rows)} 天：{', '.join(sorted(added)) or '（无）'}")

    if args.dry_run:
        print("--dry-run：未写文件")
        return 0

    # GitHub 的流量数据常滞后一两天，日更时难免碰上「抓到了但没新数据」。
    # 若只比时间戳会产出大量空提交，所以剔除 fetched_at 后再判断有没有真变化。
    svg_path = STATS_DIR / "traffic.svg"
    csv_text, svg_text = render_csv(rows), render_svg(rows)
    changed = (csv_text != prev_csv
               or {k: v for k, v in latest.items() if k != "fetched_at"}
               != {k: v for k, v in prev_latest.items() if k != "fetched_at"})
    if not changed and svg_path.exists():
        print("数据无变化，跳过写入与提交")
        return 0

    write_csv(rows)
    LATEST_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    svg_path.write_text(svg_text, encoding="utf-8")

    if args.no_push:
        print("已写入 stats/（--no-push，未提交）")
        return 0
    pushed = commit_and_push(datetime.now().strftime("%Y-%m-%d"))
    print("已提交并推送" if pushed else "未提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
