#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取本仓库的 GitHub Traffic 流量数据并存档到 stats/。

为什么需要它：GitHub 只在接口与网页保留**最近 14 天**的流量数据，过期即失，
想留下长期趋势只能定期抓取。本脚本把 14 天内的每日浏览量/克隆量按日期合并进
stats/traffic_daily.csv（同一天覆盖更新，因此漏跑几次也能补回来），把来源站点、
热门页面与 star/fork 快照写入 stats/latest.json，并为每天另存一份
stats/raw/YYYY-MM-DD.json——来源站点和热门页面也只保留 14 天，靠每日快照留档。

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
from datetime import datetime, timezone
from pathlib import Path

STATS_DIR = Path(__file__).resolve().parent
REPO_ROOT = STATS_DIR.parent
CSV_PATH = STATS_DIR / "traffic_daily.csv"
LATEST_PATH = STATS_DIR / "latest.json"
CSV_FIELDS = ["date", "views", "unique_visitors", "clones", "unique_cloners",
              "stars", "forks", "watchers"]
RAW_DIR = STATS_DIR / "raw"
# 固定提交的数据产物；raw/ 下当天的快照文件名是动态的，由 main() 单独追加
DATA_FILES = ["stats/traffic_daily.csv", "stats/latest.json", "stats/traffic.svg"]

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


def stamp_repo_counts(rows: dict[str, dict], repo: dict, day: str) -> str:
    """把当前 star/fork/watcher 数记到当天（或最近一天）的流量行上。

    GitHub 的 traffic 接口通常包含当天，但不保证；当天还没出现时落到最近一天，
    避免为了记这三个数凭空造一行 0 流量的日期。
    """
    target = day if day in rows else (max(rows) if rows else "")
    if not target:
        return ""
    rows[target]["stars"] = repo.get("stargazers_count", 0)
    rows[target]["forks"] = repo.get("forks_count", 0)
    rows[target]["watchers"] = repo.get("subscribers_count", 0)
    return target


def render_csv(rows: dict[str, dict]) -> str:
    """渲染成 CSV 文本。

    行尾统一用 LF：仓库 .gitattributes 声明了 `* text=auto eol=lf`，
    若这里写成 CRLF，工作区文件就会和 git 里的内容不一致，导致「每次都比对出差异」。
    旧行没有 star/fork 字段时留空，不用 0 冒充历史值。
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for day in sorted(rows):
        writer.writerow({k: rows[day].get(k, "") for k in CSV_FIELDS})
    return buf.getvalue()


def write_csv(rows: dict[str, dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        fh.write(render_csv(rows))


# ---------------------------- SVG 图表 ----------------------------

CHART_DAYS = 30  # 图表最多展示最近多少天
BLUE = "#0A84FF"
ORANGE = "#FF9500"
GREEN = "#34C759"
GRID = "#E5E5EA"
INK = "#1C1C1E"
MUTED = "#8E8E93"
FONT = "-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif"


def nice_axis(value: int, intervals: int = 4) -> tuple[int, int]:
    """给 Y 轴挑一个整刻度步长，返回 (上限, 步长)。

    例：33 → (40, 10)，刻度 0/10/20/30/40；比按比例取整出 12/38 这种数好看。
    """
    raw = max(1, value) / intervals
    exp = math.floor(math.log10(raw))
    base = 10 ** exp
    step = next(m * base for m in (1, 2, 5, 10) if m * base >= raw)
    step = max(1, int(step))
    return step * intervals, step


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


def moving_average(values: list[int], window: int = 7) -> list[float]:
    """尾部滑动平均：第 i 个点是最近 min(window, i+1) 天的均值。"""
    out: list[float] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= window:
            total -= values[i - window]
        out.append(total / min(window, i + 1))
    return out


def render_svg(rows: dict[str, dict], days: int = CHART_DAYS) -> str:
    """把每日数据画成一张自包含的 SVG。

    上方面板：每日浏览（细线）+ 7 日均线（粗线）+ 独立访客（虚线）；
    下方面板：每日克隆量柱状图。只依赖标准库，不引入绘图包。
    颜色以显式属性为准（GitHub 清洗 SVG 时可能剥掉 <style>），
    <style> 只做深色模式覆盖：被剥掉只是退回浅色，不会画错。
    """
    series = [rows[day] for day in sorted(rows)][-days:]
    if not series:
        return "<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'></svg>"

    width, height = 780, 360
    pad_l, pad_r = 48, 18
    plot_w = width - pad_l - pad_r
    top_top, top_bottom = 68, 228
    clone_top_y, clone_bottom = 262, 322
    top_h = top_bottom - top_top
    clone_h = clone_bottom - clone_top_y

    views = [int(r.get("views", 0) or 0) for r in series]
    visitors = [int(r.get("unique_visitors", 0) or 0) for r in series]
    clones = [int(r.get("clones", 0) or 0) for r in series]
    ma_views = moving_average(views)
    vis_max, vis_step = nice_axis(max(max(views), max(visitors), max(ma_views)))
    clone_max, clone_step = nice_axis(max(clones))
    slot = plot_w / len(series)
    bar_w = max(2.0, slot * 0.55)

    total_views = sum(views)
    # 每日「独立访客」是按天去重的，直接相加会跨天重复计数，因此称「人次」
    total_visits = sum(visitors)
    total_clones = sum(clones)
    last = series[-1]
    stars = last.get("stars", "")
    forks = last.get("forks", "")
    span = f"{series[0]['date'][5:]} ~ {last['date'][5:]}"
    subtitle = (f"{span} · 浏览 {total_views} 次 / 访客 {total_visits} 人次 / "
                f"克隆 {total_clones} 次")
    if stars != "" or forks != "":
        subtitle += f" · ★ {stars or 0} · Fork {forks or 0}"

    parts: list[str] = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' role='img' aria-label='仓库流量趋势'>",
        "<style>@media (prefers-color-scheme:dark){"
        ".card{fill:#161618;stroke:#303034}.ink{fill:#F2F2F7}"
        ".muted{fill:#98989F}.grid{stroke:#303034}}</style>",
        f"<rect class='card' x='0.5' y='0.5' width='{width - 1}' height='{height - 1}' rx='12' "
        f"fill='#FFFFFF' stroke='{GRID}'/>",
        f"<text class='ink' x='{pad_l}' y='28' font-size='15' font-weight='600' "
        f"font-family='{FONT}' fill='{INK}'>仓库流量</text>",
        f"<text class='muted' x='{pad_l}' y='47' font-size='11' "
        f"font-family='{FONT}' fill='{MUTED}'>{subtitle}</text>",
    ]

    # 图例（右上）：粗线=7 日均线，细线=每日浏览，虚线=独立访客
    legend_y = 25
    lx = width - pad_r - 206
    parts.append(f"<line x1='{lx}' y1='{legend_y}' x2='{lx + 14}' y2='{legend_y}' "
                 f"stroke='{BLUE}' stroke-width='2.4' stroke-linecap='round'/>")
    parts.append(f"<text class='muted' x='{lx + 19}' y='{legend_y + 4}' font-size='10' "
                 f"font-family='{FONT}' fill='{MUTED}'>7日均线</text>")
    parts.append(f"<line x1='{lx + 72}' y1='{legend_y}' x2='{lx + 86}' y2='{legend_y}' "
                 f"stroke='{BLUE}' stroke-width='1.2' opacity='0.35'/>")
    parts.append(f"<text class='muted' x='{lx + 91}' y='{legend_y + 4}' font-size='10' "
                 f"font-family='{FONT}' fill='{MUTED}'>每日</text>")
    parts.append(f"<line x1='{lx + 126}' y1='{legend_y}' x2='{lx + 140}' y2='{legend_y}' "
                 f"stroke='{ORANGE}' stroke-width='1.8' stroke-dasharray='5 3'/>")
    parts.append(f"<text class='muted' x='{lx + 145}' y='{legend_y + 4}' font-size='10' "
                 f"font-family='{FONT}' fill='{MUTED}'>独立访客</text>")

    # 上方面板：网格与 Y 轴刻度
    for i in range(5):
        ratio = i / 4
        y = top_bottom - top_h * ratio
        parts.append(f"<line class='grid' x1='{pad_l}' y1='{y:.1f}' x2='{pad_l + plot_w}' "
                     f"y2='{y:.1f}' stroke='{GRID}' stroke-width='1'/>")
        parts.append(f"<text class='muted' x='{pad_l - 8}' y='{y + 3.5:.1f}' font-size='10' "
                     f"text-anchor='end' font-family='{FONT}' fill='{MUTED}'>"
                     f"{vis_step * i}</text>")

    # 每日浏览：细线弱化，避免和均线抢视觉
    raw_pts = [(pad_l + slot * (i + 0.5), top_bottom - top_h * v / vis_max)
               for i, v in enumerate(views)]
    parts.append(f"<path d='{smooth_path(raw_pts)}' fill='none' stroke='{BLUE}' "
                 f"stroke-width='1.2' opacity='0.35' stroke-linecap='round'/>")

    # 7 日均线：粗线主指标
    ma_pts = [(pad_l + slot * (i + 0.5), top_bottom - top_h * v / vis_max)
              for i, v in enumerate(ma_views)]
    parts.append(f"<path d='{smooth_path(ma_pts)}' fill='none' stroke='{BLUE}' "
                 f"stroke-width='2.4' stroke-linecap='round'/>")

    # 独立访客：虚线 + 圆点
    visit_pts = [(pad_l + slot * (i + 0.5), top_bottom - top_h * v / vis_max)
                 for i, v in enumerate(visitors)]
    parts.append(f"<path d='{smooth_path(visit_pts)}' fill='none' stroke='{ORANGE}' "
                 f"stroke-width='1.6' stroke-linecap='round' stroke-dasharray='5 3'/>")
    for x, y in visit_pts:
        parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.4' fill='{ORANGE}'/>")

    # 下方面板：克隆量柱状图（克隆比浏览更接近真实使用）
    parts.append(f"<text class='muted' x='{pad_l}' y='{clone_top_y - 8}' font-size='11' "
                 f"font-family='{FONT}' fill='{MUTED}'>克隆（每日）</text>")
    for i in (0, 2, 4):
        y = clone_bottom - clone_h * i / 4
        parts.append(f"<line class='grid' x1='{pad_l}' y1='{y:.1f}' x2='{pad_l + plot_w}' "
                     f"y2='{y:.1f}' stroke='{GRID}' stroke-width='1'/>")
        parts.append(f"<text class='muted' x='{pad_l - 8}' y='{y + 3.5:.1f}' font-size='10' "
                     f"text-anchor='end' font-family='{FONT}' fill='{MUTED}'>"
                     f"{clone_step * i}</text>")
    for i, value in enumerate(clones):
        if value <= 0:
            continue
        bar_h = clone_h * value / clone_max
        x = pad_l + slot * (i + 0.5) - bar_w / 2
        y = clone_bottom - bar_h
        parts.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bar_w:.1f}' "
                     f"height='{bar_h:.1f}' rx='1.5' fill='{GREEN}' fill-opacity='0.75'/>")

    # X 轴只标首尾日期：中间日期挤在一起反而难读，趋势看形状就够了
    marks = [(0, "start")] if len(series) == 1 else [(0, "start"), (len(series) - 1, "end")]
    for idx, anchor in marks:
        x = pad_l + slot * (idx + 0.5)
        parts.append(f"<text class='muted' x='{x:.1f}' y='{height - 10}' font-size='10' "
                     f"text-anchor='{anchor}' font-family='{FONT}' fill='{MUTED}'>"
                     f"{series[idx]['date']}</text>")

    parts.append("</svg>")
    return "".join(parts)


def commit_and_push(summary: str, extra_files: list[str] | None = None) -> bool:
    """只提交数据产物，不碰脚本自身——否则脚本一旦有未完成的改动会被顺手卷进来。"""
    files = [*DATA_FILES, *(extra_files or [])]
    run(["git", "-C", str(REPO_ROOT), "add", *files])
    staged = run(["git", "-C", str(REPO_ROOT), "diff", "--cached", "--name-only"]).split()
    others = [p for p in staged if p not in files]
    if others:
        print(f"⚠ 暂存区里还有数据文件以外的改动，跳过提交：{others}")
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
    # 以 UTC 日期对齐 GitHub 流量接口的按天口径；快照文件名也用它
    snapshot_day = datetime.now(timezone.utc).date().isoformat()
    stamped = stamp_repo_counts(rows, repo, snapshot_day)

    latest = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    print(f"star/fork/watch 记到 {stamped or '（没有流量行，跳过）'}")

    if args.dry_run:
        print(f"--dry-run：未写文件（当天快照会写到 stats/raw/{snapshot_day}.json）")
        return 0

    # GitHub 的流量数据常滞后一两天，日更时难免碰上「抓到了但没新数据」。
    # 若只比时间戳会产出大量空提交，所以剔除 fetched_at 后再判断有没有真变化；
    # SVG 也要参与比较，否则改了绘图逻辑但数据没变时不会重新生成。
    svg_path = STATS_DIR / "traffic.svg"
    prev_svg = svg_path.read_text(encoding="utf-8") if svg_path.exists() else ""
    csv_text, svg_text = render_csv(rows), render_svg(rows)
    changed = (csv_text != prev_csv
               or {k: v for k, v in latest.items() if k != "fetched_at"}
               != {k: v for k, v in prev_latest.items() if k != "fetched_at"}
               or svg_text != prev_svg)
    if not changed:
        print("数据无变化，跳过写入与提交")
        return 0

    raw_path = RAW_DIR / f"{snapshot_day}.json"
    RAW_DIR.mkdir(exist_ok=True)
    write_csv(rows)
    LATEST_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    svg_path.write_text(svg_text, encoding="utf-8", newline="\n")
    raw_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    raw_rel = raw_path.relative_to(REPO_ROOT).as_posix()
    if args.no_push:
        print(f"已写入 stats/（含 {raw_rel}；--no-push，未提交）")
        return 0
    pushed = commit_and_push(snapshot_day, [raw_rel])
    print("已提交并推送" if pushed else "未提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
