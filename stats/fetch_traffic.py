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
import json
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


def write_csv(rows: dict[str, dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for day in sorted(rows):
            writer.writerow({k: rows[day].get(k, 0) for k in CSV_FIELDS})


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

    write_csv(rows)
    LATEST_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.no_push:
        print("已写入 stats/（--no-push，未提交）")
        return 0
    pushed = commit_and_push(datetime.now().strftime("%Y-%m-%d"))
    print("已提交并推送" if pushed else "未提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
