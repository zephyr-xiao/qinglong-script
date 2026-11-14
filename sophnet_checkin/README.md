# SophNet 签到

适配青龙面板的 SophNet（www.sophnet.com，云算力平台）福利中心每日签到脚本，仅依赖 `requests` + 标准库。

站点：`https://www.sophnet.com/`

签到页面：`https://www.sophnet.com/organization/welfare`

签到流程：① `POST /api/sys/login/refresh` 用 refreshToken 换取短期 accessToken → ② `GET /api/sys/checkin/welfare` 预检今日是否已签到 → ③ `POST /api/sys/checkin/do` 执行签到 → ④ 再次 `GET /api/sys/checkin/welfare` 取连续签到 / 累计 / 余额统计。

## 为何用 refreshToken

SophNet 登录页有阿里云验证码（设备认证 + 滑块），纯 API 账号密码登录难以复现。但签到接口走标准 `Authorization: Bearer <accessToken>` 认证，accessToken 仅约 2 小时有效，无法直接用于每日定时任务；而 **refreshToken 约 14 天有效**，可静默换取新 accessToken。因此脚本主凭证使用 refreshToken，每次运行先刷新再签到，无需浏览器与验证码。

> refreshToken 过期后需重新登录 SophNet 网页，按下方「refreshToken 获取方式」重新复制并更新环境变量。

## 功能特点

- 纯 API 签到，无需浏览器 / Playwright / OpenCV，部署轻量；
- 用 refreshToken 自动兑换 accessToken，规避登录验证码；
- 签到前预检 `todayCheckedIn`，已签则直接幂等返回，省一次签到请求；
- 支持"今日已签到"幂等识别（重复签到视为成功）；
- 可选开启「交互任务自动领取」（`SOPHNET_INTERACT`）：按服务端阈值上报当日交互时长，自动完成三项 bot 交互任务，默认关闭；
- 上报可提现 Token、连续签到天数、累计签到天数统计；
- 支持多账号，`&` 或换行分隔；
- refreshToken 在日志与推送中脱敏（前 6 后 4）；
- 支持代理，适合网络受限场景；TLS 默认校验证书，仅 SSL 握手失败时自动降级并告警一次；`trust_env` 关闭，代理只认 `SOPHNET_PROXY` 与青龙全局代理，不受系统代理干扰；
- 支持青龙 `notify.py` 推送，标题按成败分档；
- Debug 模式输出接口状态码与响应片段，便于排错（响应中的 accessToken / refreshToken 等凭证字段自动脱敏）。

## 青龙任务命令

```text
task sophnet_checkin/sophnet_checkin.py
```

建议定时（脚本头 `cron:` 与此一致）：

```text
10 8 * * *
```

（每天 8:10 执行，避开整点拥堵）

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `SOPHNET_REFRESH_TOKEN` | ✅ | - | refreshToken，多账号用 `&` 或换行分隔 |
| `SOPHNET_PROXY` | 否 | - | HTTP/SOCKS 代理，网络受限时填写 |
| `SOPHNET_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `SOPHNET_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅当存在失败时才推送（需 `SOPHNET_NOTIFY=true`，全部成功则静默） |
| `SOPHNET_INTERACT` | 否 | `false` | 是否把「使用bot交互N分钟」三项任务做到发奖，见下方专门章节 |
| `SOPHNET_TIMEOUT` | 否 | `30` | HTTP 超时时间，单位秒 |
| `SOPHNET_DEBUG` | 否 | `false` | 输出各接口状态码与响应片段 |

代理示例：

```text
SOPHNET_PROXY=http://172.17.0.1:7890
```

或：

```text
SOPHNET_PROXY=socks5://172.17.0.1:7891
```

## 交互任务自动领取（`SOPHNET_INTERACT`）

福利中心右侧「每日签到与任务」共四项：

| 任务 | taskKey | 阈值 | 奖励 |
|---|---|---|---|
| 使用bot交互3分钟 | `interact_3min` | 180 秒 | 30,000 Token |
| 使用bot交互10分钟 | `interact_10min` | 600 秒 | 150,000 Token |
| 使用bot交互30分钟 | `interact_30min` | 1800 秒 | 600,000 Token |
| 充值¥100 | `recharge_100` | ¥100 | 7,000,000 Token |

设置 `SOPHNET_INTERACT=true` 后，脚本会在签到流程结束后调用 `POST /api/sys/checkin/interact/report` 上报当日交互时长，把三项交互任务做到发奖。不开启时行为与之前完全一致。

机制要点（均为实测得出）：

- 上报时长由客户端提供，服务端按上报值累加当日进度；
- 进度跨过阈值时**自动发奖**，不需要点「点击去完成」；脚本只在复查发现「已完成但未发放」时调用 `POST /api/sys/checkin/task/claim` 兜底；
- 服务端限制：单次上报 ≤1800 秒、两次上报间隔 ≥10 秒、每日累计上限 7200 秒；
- 三档阈值封顶就是 1800 秒，所以脚本**只报到最高阈值**（目标值取自服务端下发的 `threshold`，不硬编码），不追求 7200 秒日上限——多报的秒数不产生额外奖励；
- 三项全部已领取（或当日进度已达标）时直接跳过，不发任何上报请求，保证幂等。

收益参考：三项合计 780,000 Token/天。按账户实际提现比例（6,000,000 Token → ¥12）折算约 ¥1.56/天。

`recharge_100` 属 `RECHARGE` 类型，进度按实付金额（分）累计，必须真实充值，脚本不处理；且充值 ¥100 只换到约 ¥14 额度，不划算。

> 风险提示：该功能等价于把当天 bot 使用时长按固定值上报，属于伪造使用记录。若平台后续加风控，风险在账号侧，请自行评估是否开启。

## refreshToken 获取方式

1. 浏览器登录 `https://www.sophnet.com/`（需通过阿里云验证码完成登录）；
2. 登录成功后，在已登录的标签页按 `F12` 打开开发者工具；
3. 进入 **Console / 控制台**，粘贴并回车执行：

   ```js
   localStorage.getItem('sophnet-auth-tab-sync-v1')
   ```

4. 返回一段 JSON 字符串，其中 `p.refreshToken` 字段的值就是 refreshToken，形如一串以 `Utbx...` 开头的长字符；
5. 复制该 refreshToken 值，填入青龙环境变量 `SOPHNET_REFRESH_TOKEN`。

> 若控制台返回 `null`，说明当前标签未登录，请先在浏览器完成登录并停留在站内任意页面再执行。

> 也可在 **Application / 应用 → Local Storage → www.sophnet.com** 下找到 `sophnet-auth-tab-sync-v1` 键，其值 JSON 中的 `p.refreshToken` 即为所需。

多账号示例：

```text
SOPHNET_REFRESH_TOKEN=refreshToken1&refreshToken2&refreshToken3
```

或换行分隔：

```text
SOPHNET_REFRESH_TOKEN=refreshToken1
refreshToken2
refreshToken3
```

## 依赖安装

青龙环境一般已内置 `requests`。如缺失，在青龙「依赖管理」-「Python」中安装：

```text
requests
```

或在容器内执行：

```bash
pip install requests
```

## 本地调试

Linux / macOS：

```bash
export SOPHNET_REFRESH_TOKEN="your_refresh_token"
export SOPHNET_NOTIFY=false
export SOPHNET_DEBUG=true
python sophnet_checkin.py
```

Windows PowerShell：

```powershell
$env:SOPHNET_REFRESH_TOKEN="your_refresh_token"
$env:SOPHNET_NOTIFY="false"
$env:SOPHNET_DEBUG="true"
python .\sophnet_checkin.py
```

## 通知

- 青龙环境中自动复用青龙 `notify.py`；
- 设置 `SOPHNET_NOTIFY=false` 可关闭推送；
- 标题按成败分档显示：`✅ 全部成功（N/N）` / `⚠️ 部分失败（M/N）` / `❌ 全部失败（0/N）`；
- 正文为 Markdown 分组格式，顶部带 ⏰ 执行时间、📊 账号统计，结果按「成功 / 失败」分组列出；
- 设置 `SOPHNET_NOTIFY_ONLY_FAIL=true` 可在本次全部成功时静默不推送（仅在有失败时才通知）；
- 推送失败会自动重试最多 3 次；
- 本地运行找不到 `notify.py` 时会跳过推送。

## 常见问题

### 提示"刷新 token 失败 ... refreshToken 可能已失效"？

refreshToken 有效期约 14 天，过期后需重新登录 SophNet 网页，按「refreshToken 获取方式」重新复制 refreshToken 并更新 `SOPHNET_REFRESH_TOKEN`。

### 提示"未登录或 accessToken 已失效 (HTTP 401)"？

正常情况下脚本会先用 refreshToken 换取新 accessToken，不应出现此错误。若仍出现，多半是 refreshToken 已失效或被服务端撤销，请按上方步骤重新获取 refreshToken。

### 请求超时或连接失败？

SophNet 站点通常国内可直连。若你的青龙容器网络受限，请在 `SOPHNET_PROXY` 填写容器内可访问的代理地址。Docker 青龙常见代理值：

```text
http://172.17.0.1:7890
```

### 不知道脚本发了哪些请求？

开启：

```text
SOPHNET_DEBUG=true
```

脚本会输出刷新 token、签到状态、签到执行三个接口的状态码与响应片段，便于排查接口变化或认证问题。响应体里的 `token` / `refreshToken` / `accessToken` / `idToken` 会自动替换成前 6 后 4 的脱敏形式，可以放心贴日志求助。

### 多账号怎么配置？

用 `&` 或换行分隔多个 refreshToken：

```text
SOPHNET_REFRESH_TOKEN=token1&token2&token3
```

脚本会逐个签到并汇总，账号之间间隔 1 秒避免风控。

### 签到返回"今日已签到"算成功吗？

算。脚本已对 `status=1` 与"今日已签到 / 已签到 / already"等幂等提示做识别，重复签到视为成功，不会报失败。同时签到前会先查 `todayCheckedIn`，已签直接返回，减少无效请求。

### 交互任务为什么只上报 1800 秒，不报满 7200 秒？

三档任务的阈值分别是 180/600/1800 秒，最高档只要 1800 秒就发满全部奖励；服务端 `dailyLimit` 虽然是 7200 秒，但超出最高阈值不产生任何额外奖励，只会让上报数据更显眼。脚本按「服务端下发的最高 threshold」上报，未来档位变化会自动跟随。

### 开启 `SOPHNET_INTERACT` 后为什么没有上报？

常见三种情况：① 当天三项已全部领取（`rewarded=true`），脚本直接跳过；② 当天进度已达标（`currentProgress` 达到最高阈值），只需等发奖；③ 服务端限流，日志会打印「上报失败（本次已报 N 秒）」并给出原因，下次定时任务会重新补齐。开启 `SOPHNET_DEBUG=true` 可看到每次上报的响应原文。

### 充值任务能自动完成吗？

不能。`recharge_100` 是 `RECHARGE` 类型，服务端按真实支付金额（单位：分）累计，没有可绕过的客户端上报接口。

### 为什么刷新 token 用的是 accessToken 还是 refreshToken？

脚本只用 refreshToken 调用 `POST /api/sys/login/refresh` 换取新的 accessToken（accessToken 仅约 2 小时有效，无法用于每日定时任务）。签到本身用换得的 accessToken 鉴权。你只需配置 refreshToken 一个凭证即可。
