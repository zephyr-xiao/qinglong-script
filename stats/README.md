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

## 手动运行

```bash
python stats/fetch_traffic.py            # 抓取 + 存档 + 提交推送
python stats/fetch_traffic.py --no-push  # 只抓取存档，不提交
python stats/fetch_traffic.py --dry-run  # 只打印，不写文件
```

依赖 `gh` CLI 且已登录（需要该仓库的 repo 权限）。直连 GitHub 不通时先设代理：

```bash
HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 python stats/fetch_traffic.py
```

## 自动运行

由工作区里的定时任务每周跑一次（GitHub 保留 14 天，周频足够，且漏跑一周也能补回）。

## 说明

- 「克隆量」比「浏览量」更能反映真实使用：青龙订阅拉取脚本会计入克隆。
- 数据来自 GitHub 官方接口，只有仓库所有者能读；存档到本目录后即为公开。
