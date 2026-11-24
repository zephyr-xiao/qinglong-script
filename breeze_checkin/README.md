# Breeze 积分签到

BreezeBlue（Breeze TTS 2）每天北京时间 08:00 重置一次免费积分（默认 400），领取入口只在网页控制台。本脚本用网页登录态 Cookie 调用网页接口自动领取，支持多账号、幂等跳过、失败隔离。

## 为什么只能用网页 Cookie

Breeze 的开发者 API 只有余额 / 用量 / 合成，**没有领取端点**；领取只存在于网页端：

```text
POST https://breezeblue.ai/api/credits/daily-claim
Cookie: breeze_blue_session=…
Origin: https://breezeblue.ai
Referer: https://breezeblue.ai/app/home

→ {"claimed": true, "credits_issued": 400, "balance": …, "daily_claim_claimed_today": true, …}
```

`GET /api/credits/summary` 用来查余额、今日是否已领、下次重置时间。原 [DSH 插件](https://github.com/mi-tuan/dsh-breeze-claim) 用 Playwright 登录并导出 Cookie；青龙里无法交互登录，所以凭证改为手动复制 `breeze_blue_session`。

## 功能特点

- 纯 API，无浏览器 / Playwright，只依赖 `requests`
- 多账号：`user=名字; cookie=值`，换行或 `&&` 分隔
- 幂等：先查 summary，今日已领直接跳过；重复运行不会重复领取
- 领取请求失败（网络异常 / 5xx / 429）时复查 summary，避免「其实已到账」误报失败
- 失败隔离：运行期单账号失败不影响其它账号；配置解析错误会整轮退出 2（不会带着半套配置继续跑）
- 401 / 403 / `AUTH_REQUIRED` 单独识别，明确提示重新复制 Cookie
- 默认**失败才推送**（`BREEZE_NOTIFY_ONLY_FAIL=true`），全成功静默
- 日志与推送不输出 Cookie 明文
- 可选代理 `BREEZE_PROXY`，兼容青龙全局代理
- `--check` 只读自检 / `--probe` 额外打印接口响应片段（已截断）

## 青龙任务命令

```bash
task breeze_checkin/breeze_checkin.py
```

建议 cron：`30 8 * * *`（北京时间 08:00 重置后半小时；脚本头 docstring 已写）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `BREEZE_ACCOUNTS` | ✅ | — | 账号配置，多账号用**换行**或 `&&` 分隔，账号内用 `;` 分隔字段 |
| `BREEZE_TIMEOUT` | 否 | `20` | 请求超时秒数，允许 5–120 |
| `BREEZE_NOTIFY` | 否 | `true` | 是否走青龙 `notify.py` 推送 |
| `BREEZE_NOTIFY_ONLY_FAIL` | 否 | `true` | 全部成功时静默；设 `false` 可每天收成功汇总 |
| `BREEZE_DEBUG` | 否 | `false` | 打印接口响应片段（截断到 800 字符） |
| `BREEZE_PROXY` | 否 | — | 代理地址，如 `http://172.17.0.1:7890`；填 `none` / `direct` / `off` 表示强制直连 |
| `BREEZE_CHECK` | 否 | `false` | 设为 `true` 等价 `--check`（青龙「运行」按钮不转发参数） |
| `BREEZE_PROBE` | 否 | `false` | 设为 `true` 等价 `--probe`，额外打印接口响应片段，用完请删除 |

## 凭证获取

1. 浏览器打开 <https://breezeblue.ai/app/home> 并登录
2. 按 `F12` → **Application（应用）** → **Cookies** → `https://breezeblue.ai`
3. 找到 `breeze_blue_session`，复制它的**值**（只复制值，不要复制整行）
4. 填进 `BREEZE_ACCOUNTS`

### 单账号

```text
user=主号; cookie=粘贴的值
```

### 多账号

换行分隔：

```text
user=主号; cookie=xxxxxxxx
user=小号; cookie=yyyyyyyy
```

或用 `&&` 分隔：

```text
user=主号; cookie=xxxxxxxx && user=小号; cookie=yyyyyyyy
```

### 完整 Cookie 头

如果除了 `breeze_blue_session` 还有别的 Cookie（如 `__cf_bm`），可以把整段 Cookie 头贴进来。**`cookie=` 必须是最后一个字段**：

```text
user=主号; cookie=breeze_blue_session=xxx; __cf_bm=yyy
```

`cookie=` 的值也支持 `Cookie:` 前缀、`session=` 别名和裸值，脚本会自动归一化。完整头里如果还有名为 `session` / `user` 的 Cookie 也没问题；只要 `cookie=` 字段本身是最后一个，后面不要再出现第二个 `cookie=`（会被当成下一个账号）。

> Cookie 是账号凭据：不要提交到仓库、不要发到群里。脚本不落盘，只在内存里使用。

## 代理

优先级：`BREEZE_PROXY` > 青龙全局 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` > 直连。

Breeze 站点国内可直连；如果青龙容器出口访问不稳定，在环境变量里加：

```text
BREEZE_PROXY=http://172.17.0.1:7890
```

如果环境里有全局代理但想让本脚本直连，填 `BREEZE_PROXY=none`（也接受 `direct` / `off`）。

脚本设置了 `trust_env=False`，代理只认上述变量，不受容器系统代理干扰。

## 本地调试

Linux / macOS：

```bash
cd breeze_checkin
export BREEZE_ACCOUNTS='user=主号; cookie=xxx'
export BREEZE_NOTIFY=false
python breeze_checkin.py --check   # 只查状态，不领取
python breeze_checkin.py           # 正式领取
```

Windows PowerShell：

```powershell
cd breeze_checkin
$env:BREEZE_ACCOUNTS = 'user=主号; cookie=xxx'
$env:BREEZE_NOTIFY = 'false'
python breeze_checkin.py --check
```

## 通知

复用青龙自带 `notify.py`（Server酱、PushPlus、企业微信、钉钉、飞书、Bark、Telegram 等）。

- 默认 `BREEZE_NOTIFY_ONLY_FAIL=true`：只有账号失败 / 登录态过期才推送
- 想每天看成功汇总：设 `BREEZE_NOTIFY_ONLY_FAIL=false`
- 推送失败重试 3 次，不影响退出码

## 常见问题

**`登录态失效（HTTP 401）：请重新登录…`**
`breeze_blue_session` 过期或被服务端轮换。重新登录网页，按「凭证获取」复制新值，更新 `BREEZE_ACCOUNTS`。

**`登录态失效（HTTP 403）`**
Cookie 无效，或请求被风控 / 代理改写。脚本已带 `Origin` 与 `Referer`；先确认 Cookie 正确，再检查代理是否拦截了 `breezeblue.ai`。

**`今天已经领过了`**
正常幂等结果。今天已经领过，脚本不会再提交领取。

**`当前不可领取（daily_claim_available=false）`**
账号当前不满足领取条件（未到重置时间、账号状态异常等）。脚本会带上余额与下次重置时间。

**`领取接口未返回 claimed=true`**
接口返回成功但没标记领取成功，通常意味着网页接口改版。加 `BREEZE_PROBE=true` 跑一次看接口响应片段。

**Cookie 会自己轮换吗？**
网页会话可能刷新 Cookie。脚本不落盘、也不回写环境变量；如果发现频繁 401，重新复制一次即可。

**时间不对 / 到点没领**
青龙定时按容器时区执行。确认容器时区是 `Asia/Shanghai`，否则把 cron 换算成容器时间。

**能自动刷新登录态吗？**
不能。原插件的刷新依赖持久化浏览器 profile，青龙里没有。Cookie 过期只能手动重抓，脚本会明确告警。

## 实测记录

2026-11-20（真实账号，当日已领状态）：

- `GET /api/credits/summary` → `balance=1800`、`daily_claim_claimed_today=true`、`daily_claim_amount=400`、`daily_claim_reward_tier=discord`、`daily_claim_next_reset_at=2026-11-21T00:00:00+00:00`（UTC，对应北京时间 08:00）
- 脚本走幂等路径：不提交 POST，输出「今天已经领过了」，退出码 0
- 直接 `POST /api/credits/daily-claim` → `HTTP 200`、`claimed=false`、`already_claimed=true`、`credits_issued=0`、`balance=1800`；脚本 `claim()` 正确识别为已领
- 未验证：全新账号的首次领取（`claimed=true` + `credits_issued=400`），该路径由单测覆盖

## 测试

```bash
cd breeze_checkin
python -m unittest discover -s tests -v
```

105 项单测，全部使用假响应，不发真实请求。

## 来源与致谢

改写自 [mi-tuan/dsh-breeze-claim](https://github.com/mi-tuan/dsh-breeze-claim)（MIT）：原项目是 DeepSeekRuntime 插件，本脚本保留其网页接口调用方式，去掉 Playwright 登录与设置卡片，改为青龙多账号定时任务。

## 已知限制

- 依赖网页接口，Breeze 前端改版可能导致失效
- Cookie 过期需手动更新
- 不做登录态自动刷新、不做积分消费
