# ZodGame 签到

适配青龙面板的 ZodGame 论坛（zodgame.xyz）每日脚本：完成 dsu_paulsign 每日签到（随机心情，领酱油奖励）与 jnbux 广告任务（逐个完成，每个 +2 点币，点币仅累积不自动兑换）。Cookie 认证，依赖 `requests`；装了 `curl_cffi` 时自动优先使用。

广告任务按接口判定：`do=click` → 等广告页真实倒计时 → `do=update` → **`do=check` 校验**，只有校验响应出现「检查成功」才算完成；签到 POST 带重试（接口幂等），已签到 / 已完成自动跳过。

## 青龙任务命令

```text
task zodgame_checkin/zodgame_checkin.py
```

建议定时 `30 9 * * *`（每天上午 9:30，与脚本头 `cron:` 一致）。首次部署或收到告警时先跑自检（不签到、不执行广告任务）：

```text
task zodgame_checkin/zodgame_checkin.py --check
```

退出码：`0` 全部账号成功、`2` 部分账号成功、`1` 全部账号失败（或未配置 `ZODGAME_COOKIE`）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `ZODGAME_COOKIE` | 是 | - | 完整 Cookie 字符串，多账号用换行分隔 |
| `ZODGAME_PROXY` | 否 | - | HTTP 代理，如 `http://192.168.1.100:7890`；留空回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`），仍为空则直连 |
| `ZODGAME_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `ZODGAME_NOTIFY_ONLY_FAIL` | 否 | `false` | 全部成功时静默（需 `ZODGAME_NOTIFY=true`） |
| `ZODGAME_DEBUG` | 否 | `false` | 输出接口响应细节，并把关键响应落盘用于排错 |
| `ZODGAME_DEBUG_DIR` | 否 | 脚本同目录 `_zodgame_debug/` | 调试产物目录 |
| `ZODGAME_AD_SECONDS` | 否 | - | 强制广告观看秒数；留空则从广告页真实倒计时解析（推荐留空） |

## 依赖安装

Python 依赖 `requests`。建议同时在青龙「依赖管理 → Python」安装 `curl_cffi`（装到 data 目录，容器重建后仍保留）：站点前置 Cloudflare managed challenge，判定依据是 TLS 指纹而非 IP，普通 `requests` 的 OpenSSL 指纹必被拦（403 `Just a moment`），`curl_cffi` 的 Chrome 指纹可直接通过。缺少时脚本仍可运行，但会被 CF 拦并如实告警。

## 凭证获取

1. 浏览器打开 <https://zodgame.xyz/> 并登录；
2. `F12` → Network，刷新页面，点任意 `forum.php` 请求；
3. 在请求标头中找到 `Cookie:`，复制完整值（含 `qhMq_2132_auth`、`cf_clearance` 等所有键值对），多账号换行分隔。

关键是 `qhMq_2132_auth`（登录态）。`cf_clearance` 可有可无——脚本用 `curl_cffi` 指纹过 CF，不依赖它，过期留着也无害。论坛 Cookie 长期有效，只有退出登录 / 改密码才需重抓。

## 常见问题

**提示「Cookie 已失效」** → 浏览器重新登录论坛，F12 抓取新 Cookie 更新 `ZODGAME_COOKIE`。

**提示「被 Cloudflare 拦截」** → 确认「依赖管理 → Python」装了 `curl_cffi`，启动日志会打印 `🧩 请求栈: curl_cffi/chrome`；没装时回退 `requests` 必被拦。已装仍被拦再换 `ZODGAME_PROXY` 出口节点。

**提示「疑似改版」** → 脚本只认站点明确文案（无任务链接时必须出现「暂时没有广告任务可执行」），其余按疑似改版告警而不静默当成功。设 `ZODGAME_DEBUG=true` 跑一次，用 `_zodgame_debug/` 里的响应定位。

**签到显示成功但没奖励** → 签到奖励随机（酱油 / 榨菜等），数量也随机，属正常现象。

**BUX 提示「校验未通过」** → 广告未看完（中途关闭则今日不再显示）或未加入 BUX 系统。脚本会在页面出现「开始参与任务」时自动加入；反复失败先跑 `--check` 确认「已加入 BUX 广告系统」。

**BUX 为什么有时一个任务都没有** → 站点对「今日无任务投放」与「今日任务已全部完成」共用同一句文案，无法区分，脚本按同一情况处理。

**BUX 任务只完成部分** → 单任务领奖失败不影响其他任务，次日会重新执行，汇总显示「完成 N/M 个任务」。
