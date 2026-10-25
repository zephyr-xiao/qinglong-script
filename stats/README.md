# 流量统计存档

GitHub 只在仓库的 **Insights → Traffic** 里保留最近 **14 天**的流量数据，过期就没了。
这个目录用来把数据定期抓下来长期留存。

## 文件

| 文件 | 说明 |
|---|---|
| `traffic_daily.csv` | 按日期合并的每日数据：浏览量、独立访客、克隆量、独立克隆来源；star / fork / watcher 记在抓取时的最新一行（流量接口通常滞后一天），旧行留空表示当时没有记录 |
| `latest.json` | 最近一次抓取时的完整快照：14 天汇总、来源站点、热门页面、star / fork |
| `raw/YYYY-MM-DD.json` | 每次数据变化时一份的来源与热门页面快照（按 UTC 日期命名）。GitHub 对这两项也只保留 14 天，靠它才能回看历史 |
| `traffic.svg` | 自动生成的趋势图：上方是每日浏览 + 7 日均线 + 独立访客，下方是每日克隆量柱状图（根 README 直接引用它） |
| `fetch_traffic.py` | 抓取脚本，见下 |

`traffic_daily.csv` 以日期为主键，**同一天重复抓取会覆盖更新**，所以偶尔漏跑几次也能在下次抓取时补回来（只要不超过 14 天）。

## 云端运行（GitHub Actions，推荐）

`.github/workflows/traffic-stats.yml` 每天 01:00 UTC（北京时间 09:00）在 GitHub 的服务器上自动跑，**不依赖本地电脑开关机**；也可以在仓库的 **Actions** 页面手动点运行。

### 先配置一个令牌（必做，只需一次）

GitHub 的流量接口**不接受** Actions 自带的 `GITHUB_TOKEN`（会返回 `403 Resource not accessible by integration`），必须单独配一个带权限的令牌：

1. 打开 <https://github.com/settings/personal-access-tokens/new>（Fine-grained token）
2. **Repository access** 选 `Only select repositories`，只勾 `qinglong-script`
3. **Permissions → Repository permissions**，找到 **Administration**，设为 **Read-only**（其余保持默认）
4. 生成后复制令牌（`github_pat_...`）
5. 打开仓库 **Settings → Secrets and variables → Actions → New repository secret**，名称填 `TRAFFIC_TOKEN`，值粘贴刚复制的令牌

配好后到 Actions 页面手动跑一次即可验证。用的是「Administration 只读」且只授权这一个仓库，泄漏面最小。

### 失败告警

令牌最长一年到期，过期后 workflow 会每天静默失败，而 14 天窗口一过数据就补不回来了。因此 workflow 加了两步：

- **失败时**自动在仓库开一个标题为「流量快照任务失败」的 issue，已存在就追加评论，正文附运行记录链接和常见原因；需要 `issues: write` 权限。
- **恢复后**（下一次运行成功）自动关闭该 issue。

收到 issue 时优先检查 `TRAFFIC_TOKEN` 是否过期，其次看 GitHub API 是否限流；处理完到运行记录页点 Re-run 即可补齐。

## 本地运行（备用）

```bash
python stats/fetch_traffic.py            # 抓取 + 存档 + 提交推送
python stats/fetch_traffic.py --no-push  # 只抓取存档，不提交
python stats/fetch_traffic.py --dry-run  # 只打印，不写文件
```

依赖 `gh` CLI 且已登录（需要该仓库的 repo 权限）。直连 GitHub 不通时先设代理：

```bash
HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 python stats/fetch_traffic.py
```

## 定时任务

只有云端这一个：GitHub Actions 每天自动跑（见上），不依赖本地电脑。本地不再挂定时任务，需要时手动执行上面的命令即可——数据按日期合并，重复跑不会冲突。

GitHub 的流量数据常滞后一两天，所以脚本会在**数据真的变了**的时候才提交（只改时间戳不提交），避免仓库里堆满空提交。SVG 也会参与比较，改了绘图逻辑后下次运行会重新生成。

`raw/` 下按 UTC 日期保存当天快照，同一天重复运行会覆盖。数据无变化时不会产生新的 raw 文件——相同内容没必要每天存一份。

## 说明

- 「克隆量」比「浏览量」更能反映真实使用：青龙订阅拉取脚本会计入克隆。
- 数据来自 GitHub 官方接口，只有仓库所有者能读；存档到本目录后即为公开。
- 图表里的 7 日均线按「最近 7 天（不足 7 天按实际天数）」计算，用来压掉日间波动，看趋势别看单日尖峰。
