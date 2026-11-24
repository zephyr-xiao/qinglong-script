# Breeze 积分签到

BreezeBlue（Breeze TTS 2）每天北京时间 08:00 重置免费积分（默认 400），领取入口只在网页端。本脚本用网页登录态 Cookie 调用领取接口，纯 API、无浏览器 / Playwright，只依赖 `requests`，支持多账号与幂等跳过。

领取只存在于网页接口 `POST https://breezeblue.ai/api/credits/daily-claim`（开发者 API 只有余额 / 用量 / 合成，没有领取端点）；`GET /api/credits/summary` 用于查余额、今日是否已领、下次重置时间。青龙里无法交互登录，凭证只能手动复制 `breeze_blue_session`。

## 青龙任务命令

```bash
task breeze_checkin/breeze_checkin.py
```

建议 cron `30 8 * * *`（北京时间 08:00 重置后半小时，与脚本头 docstring 一致）。`--check` 只读自检不领取，`--probe` 额外打印接口响应片段（已截断）；青龙「运行」按钮不转发参数，可用 `BREEZE_CHECK=true` / `BREEZE_PROBE=true` 等价开启。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `BREEZE_ACCOUNTS` | 是 | — | 账号配置，多账号用**换行**或 `&&` 分隔，账号内用 `;` 分隔字段 |
| `BREEZE_TIMEOUT` | 否 | `20` | 请求超时秒数，允许 5–120 |
| `BREEZE_NOTIFY` | 否 | `true` | 是否走青龙 `notify.py` 推送 |
| `BREEZE_NOTIFY_ONLY_FAIL` | 否 | `true` | 全部成功时静默；设 `false` 可每天收成功汇总 |
| `BREEZE_DEBUG` | 否 | `false` | 打印接口响应片段（截断到 800 字符） |
| `BREEZE_PROXY` | 否 | — | 代理地址，如 `http://172.17.0.1:7890`；填 `none` / `direct` / `off` 表示强制直连。优先级高于青龙全局 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` |
| `BREEZE_CHECK` | 否 | `false` | 设为 `true` 等价 `--check`（青龙「运行」按钮不转发参数） |
| `BREEZE_PROBE` | 否 | `false` | 设为 `true` 等价 `--probe`，额外打印接口响应片段，用完请删除 |

## 凭证获取

1. 浏览器打开 <https://breezeblue.ai/app/home> 并登录；
2. 按 `F12` → Application → Cookies → `https://breezeblue.ai`；
3. 找到 `breeze_blue_session`，复制它的**值**（只复制值，不要复制整行）；
4. 填进 `BREEZE_ACCOUNTS`。

单账号格式 `user=主号; cookie=粘贴的值`；多账号用换行或 `&&` 分隔。`cookie=` 必须是最后一个字段，其值支持 `Cookie:` 前缀、`session=` 别名和裸值；完整 Cookie 头可直接贴入：

```text
user=主号; cookie=breeze_blue_session=xxx; __cf_bm=yyy
```

## 常见问题

**`登录态失效（HTTP 401）`** → `breeze_blue_session` 过期或被服务端轮换，重新登录网页按「凭证获取」复制新值。

**`登录态失效（HTTP 403）`** → Cookie 无效，或请求被风控 / 代理改写。脚本已带 `Origin` 与 `Referer`，先确认 Cookie 正确，再检查代理是否拦截 `breezeblue.ai`。

**`今天已经领过了`** → 正常幂等结果，脚本不会再提交领取。

**`当前不可领取（daily_claim_available=false）`** → 未到重置时间或账号状态异常，脚本会带上余额与下次重置时间。

**`领取接口未返回 claimed=true`** → 接口返回成功但未标记领取成功，通常意味着网页接口改版，加 `BREEZE_PROBE=true` 跑一次看响应片段。

**到点没领 / 时间不对** → 青龙定时按容器时区执行，确认容器时区是 `Asia/Shanghai`，否则把 cron 换算成容器时间。

**能自动刷新登录态吗** → 不能。原插件的刷新依赖持久化浏览器 profile，青龙里没有；Cookie 可能自己轮换，脚本不落盘也不回写，频繁 401 就重新复制一次。

## 来源

改编自 [mi-tuan/dsh-breeze-claim](https://github.com/mi-tuan/dsh-breeze-claim)（MIT）：原项目是 DeepSeekRuntime 插件，本脚本保留网页接口调用方式，去掉 Playwright 登录，改为青龙多账号定时任务。
