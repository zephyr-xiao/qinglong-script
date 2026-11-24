# 福利吧每日签到（fx_checkin）

福利吧论坛（wnflb2023.com）「每日签到」插件自动签到，支持账号密码或 Cookie 直签。前提：账号密码模式需安装 `ddddocr`（登录页默认带图片验证码），网站登录失败次数按 IP 计数。

## 青龙任务命令

```bash
task wnflb_fx_checkin/wnflb_fx_checkin.py
```

定时建议 `20 8 * * *`（与脚本头 `cron:` 一致，`ql repo` 拉库会自动建任务）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `WNFX_ACCOUNTS` | 二选一 | - | `用户名#密码`，多账号换行分隔；用第一个 `#` 分隔，密码里可再含 `#` |
| `WNFX_COOKIE` | 二选一 | - | 浏览器完整 Cookie，多账号换行分隔；允许只填一个键值对（如仅 `S5r8_2132_auth=xxx`） |
| `WNFX_HOST` | 否 | `www.wnflb2023.com` | 站点域名 |
| `WNFX_PROXY` | 否 | - | HTTP 代理，如 `http://proxy.example.com:7890`；留空回退青龙全局代理 |
| `WNFX_TIMEOUT` | 否 | `25` | HTTP 超时秒数 |
| `WNFX_SIGN_RETRY` | 否 | `2` | 业务失败重试次数，`0` 表示不重试 |
| `WNFX_SIGN_RETRY_DELAY` | 否 | `5` | 每次业务重试前等待秒数（带 ±20% 抖动） |
| `WNFX_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `WNFX_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅在有失败时推送 |
| `WNFX_DEBUG` | 否 | `false` | 输出接口响应细节，排错用 |

两种认证方式可同时配置，会各自独立跑一遍。

## 依赖安装

在「依赖管理 → Python」安装：

- `requests`（必需）
- `ddddocr`（账号密码模式必需；只用 `WNFX_COOKIE` 可以不装）

## 凭证获取

- **Cookie 直签**：浏览器登录论坛，F12 → Network 复制任意请求的完整 Cookie，填入 `WNFX_COOKIE`。
- **账号密码**：`WNFX_ACCOUNTS=example_user#example_password`。该站登录失败次数按 IP 统计（换新会话也会递减，5 次后强制验证码），脚本对认证失败绝不重试，配置前务必确认密码正确。

## 常见问题

- **站点要求输入登录验证码但未安装 ddddocr** → 安装 `ddddocr`，或改用 `WNFX_COOKIE`。
- **验证码连续 3 次未通过（OCR 识别率不足）** → 直接重跑一次（每轮重新取图），长期失败改用 `WNFX_COOKIE`。
- **验证码页结构解析失败（缺 formhash/idhash/loginhash）** → 登录页结构变了，开 `WNFX_DEBUG=true` 对照响应排查。
- **账号或密码错误（该 IP 剩余尝试 N 次）** → 核对密码后再配，不要反复试，失败次数按 IP 累计。
- **Cookie 已失效** → 重新登录抓完整 Cookie 更新 `WNFX_COOKIE`；脚本也能识别游客态（`discuz_uid = '0'`）并推送告警。
- **签到失败：系统繁忙** → 站点临时异常，脚本按 `WNFX_SIGN_RETRY` 重试，重试前先刷新页面，已签到则按成功处理。
- **检测到运行锁** → 上次任务未正常结束，锁超过 2 小时自动视为过期。
- **怎么判定签到成功** → 正向证据制：签到后重新拉取列表页，确认变为「已签到」（`fx_chk_menu=true`、顶栏 `alt="已签到"`）才算成功，不单凭关键词；「您今日已签到」按幂等成功。
