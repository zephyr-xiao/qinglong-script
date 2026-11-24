# 纸鸢下载签到（mybt-checkin-ql）

纸鸢下载（[mybt.kiteyuan.info](https://mybt.kiteyuan.info)）自动签到：Casdoor 账号密码自动登录换会话 token，完成每日签到与访问任务，通过青龙 `notify.py` 推送。需要账号密码，或手动提供 JWT。

## 青龙任务命令

```text
task mybt-checkin-ql/mybt_checkin.py
```

定时建议 `5 8 * * *`（每天 08:05，与脚本头部 `cron:` 保持一致）；上传 `mybt_checkin.py` 并在「环境变量」建好 `MYBT_ACCOUNT` 后即可运行。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MYBT_ACCOUNT` | 与 `MYBT_TOKEN` 二选一 | — | 账号：`用户名#密码`；多账号用 `&` 或换行分隔，**密码不能含 `&` 或换行**（会被截断），`#` 可以含 |
| `MYBT_TOKEN` | 兜底 | — | 手动 JWT；无账号密码时必填，自动登录失败时回退 |
| `MYBT_COOKIE` | 否 | — | 附加 Cookie，如 `cf_clearance=...` |
| `MYBT_TURNSTILE_TOKEN` | 否 | — | 登录触发人机验证时手动提供的验证码 token |
| `MYBT_DO_VISIT` | 否 | `true` | 是否执行访问任务 |
| `MYBT_NOTIFY` | 否 | `true` | 是否推送 |
| `MYBT_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅失败时推送（全成功静默） |
| `MYBT_EXIT_STRICT` | 否 | `false` | 只要有账号未成功（含「未确认」）就 `exit 1`；默认仅全失败才 `exit 1` |
| `MYBT_TIMEOUT` | 否 | `30` | HTTP 超时秒数 |
| `MYBT_DEBUG` | 否 | `false` | 输出调试细节（签名串等） |
| `MYBT_PROXY` | 否 | 空 | HTTP / SOCKS 代理，如 `http://172.17.0.1:7890` |
| `MYBT_BASE_URL` / `MYBT_AUTH_URL` | 否 | 站点默认地址 | 站点地址 / 认证地址，一般不用改 |
| `MYBT_SECRET` | 否 | 空 | 签名密钥，默认由 `/auth/me` 的 `sign_secret` 自动回填；仅站点收紧为全接口验签时才需显式提供 |

## 依赖安装

「依赖管理」→「Python」安装 `requests`（青龙镜像一般自带）。本地运行同理，直接 `python mybt_checkin.py`。

## 常见问题

- **报「站点登录触发了人机验证（Cloudflare Turnstile）」** → 登录页启用了 Turnstile，脚本不做自动过码；用 `MYBT_TURNSTILE_TOKEN` 传一次性 token，或配 `MYBT_TOKEN` 走 JWT 兜底。
- **报告里出现「未确认」** → 站点返回 200 但业务码非零或字段不认识，按保守口径计为未成功，避免静默误报；若真实成功被误判，提供响应样例调整成功标志字段。
- **401 / 403 会不会被「已签到」文案吞掉** → 不会：分层判定（HTTP 码 → 业务码 → 文案），401/403 与业务层「未登录」无条件触发续期 / 重登。
- **频繁重登或触发风控** → 登录态缓存在 `mybt_token.json`（只存 token / refresh_token，原子写入），过期或 401/403 才续期 / 重登；多账号之间自动加 3–5 秒间隔。文件可随时删除，下次运行自动重建。
- **网络抖动** → 网络异常自动重试 3 次；密码错误 / 验证码拦截不重试。
- **站点改版导致失效** → 本版同时兼容新链路（`login_code` + `POST /api/auth/session`）与旧链路（callback 直出 token），接口或签名算法再变时需对照前端 bundle 更新。
- 通知复用青龙 `notify.py`，渠道由面板统一管理。

## 来源

改编自 [elongou-checkin/mybt-signin](https://github.com/elongou-checkin/mybt-signin)。
