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
- 上报可提现 Token、连续签到天数、累计签到天数统计；
- 支持多账号，`&` 或换行分隔；
- refreshToken 在日志与推送中脱敏（前 6 后 4）；
- 支持代理，适合网络受限场景；
- 支持青龙 `notify.py` 推送，标题按成败分档；
- Debug 模式输出接口状态码与响应片段，便于排错。

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

脚本会输出刷新 token、签到状态、签到执行三个接口的状态码与响应片段，便于排查接口变化或认证问题。

### 多账号怎么配置？

用 `&` 或换行分隔多个 refreshToken：

```text
SOPHNET_REFRESH_TOKEN=token1&token2&token3
```

脚本会逐个签到并汇总，账号之间间隔 1 秒避免风控。

### 签到返回"今日已签到"算成功吗？

算。脚本已对 `status=1` 与"今日已签到 / 已签到 / already"等幂等提示做识别，重复签到视为成功，不会报失败。同时签到前会先查 `todayCheckedIn`，已签直接返回，减少无效请求。

### 为什么刷新 token 用的是 accessToken 还是 refreshToken？

脚本只用 refreshToken 调用 `POST /api/sys/login/refresh` 换取新的 accessToken（accessToken 仅约 2 小时有效，无法用于每日定时任务）。签到本身用换得的 accessToken 鉴权。你只需配置 refreshToken 一个凭证即可。
