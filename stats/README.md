# 流量统计存档

GitHub 只在仓库的 **Insights → Traffic** 里保留最近 **14 天**的流量数据，过期就没了。
这个目录用来把数据定期抓下来长期留存。

## 文件

| 文件 | 说明 |
|---|---|
| `traffic_daily.csv` | 按日期合并的每日数据：浏览量、独立访客、克隆量、独立克隆来源 |
| `latest.json` | 最近一次抓取时的完整快照：14 天汇总、来源站点、热门页面、star / fork |
| `fetch_traffic.py` | 抓取脚本，见下 |

`traffic_daily.csv` 以日期为主键，**同一天重复抓取会覆盖更新**，所以偶尔漏跑几次也能在下次抓取时补回来（只要不超过 14 天）。

## 云端运行（GitHub Actions，推荐）

`.github/workflows/traffic-stats.yml` 每周一 01:00 UTC（北京时间 09:00）在 GitHub 的服务器上自动跑，**不依赖本地电脑开关机**；也可以在仓库的 **Actions** 页面手动点运行。

### 先配置一个令牌（必做，只需一次）

GitHub 的流量接口**不接受** Actions 自带的 `GITHUB_TOKEN`（会返回 `403 Resource not accessible by integration`），必须单独配一个带权限的令牌：

1. 打开 <https://github.com/settings/personal-access-tokens/new>（Fine-grained token）
2. **Repository access** 选 `Only select repositories`，只勾 `qinglong-script`
3. **Permissions → Repository permissions**，找到 **Administration**，设为 **Read-only**（其余保持默认）
4. 生成后复制令牌（`github_pat_...`）
5. 打开仓库 **Settings → Secrets and variables → Actions → New repository secret**，名称填 `TRAFFIC_TOKEN`，值粘贴刚复制的令牌

配好后到 Actions 页面手动跑一次即可验证。用的是「Administration 只读」且只授权这一个仓库，泄漏面最小。

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

## 本地定时任务（备用）

本地也挂了一个每周一次的定时任务：电脑开着时它会跑，没开机就自动跳过，不影响云端那次。两边数据按日期合并，重复跑不会冲突。

## 说明

- 「克隆量」比「浏览量」更能反映真实使用：青龙订阅拉取脚本会计入克隆。
- 数据来自 GitHub 官方接口，只有仓库所有者能读；存档到本目录后即为公开。
