# 维咔 VikACG 签到（vikacg_checkin）

维咔VikACG[V站]（https://www.vikacg.cc）每日钱包签到领积分，纯 API 免验证码，青龙面板 Python 脚本。站点是 Nuxt 3 SSR 应用，同源 API 为 `https://www.vikacg.cc/api/vikacg/v1/*`；登录下发 JWT 双 token（有效期 30 天），用 `Authorization: Bearer <token>` 鉴权，签到端点 `userMission` 幂等友好：已签到返回 HTTP 409「用户今天已签到」，按成功处理。

## 青龙任务命令

```text
task vikacg_checkin/vikacg_checkin.py
```

定时建议 `23 8 * * *`（与脚本头 cron 一致即可）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `VIKACG_ACCOUNTS` | 是 | — | 账号：`邮箱#密码`，多账号用 `&` 或换行分隔 |
| `VIKACG_PROXY` | 否 | 空（直连） | 代理，如 `http://172.17.0.1:7890`。默认直连并强制 IPv4，`none` / `direct` 也表示直连；直连被掐断时自动用环境里的 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` 作备用出口（这些全局变量不参与主路由选择）。SOCKS 代理需另装 `pip install requests[socks]` |
| `VIKACG_INSECURE` | 否 | `false` | 跳过 TLS 证书校验（仅自签代理等特殊场景），默认严格校验 |
| `VIKACG_NOTIFY` | 否 | `true` | 是否通过 `notify.py` 推送 |
| `VIKACG_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅失败时推送（全成功静默） |
| `VIKACG_TIMEOUT` | 否 | `30` | HTTP 超时秒数 |
| `VIKACG_DEBUG` | 否 | `false` | 输出 token 来源等调试细节 |
| `VIKACG_BASE_URL` | 否 | `https://www.vikacg.cc` | 站点地址（用备用域名时改） |

示例：`VIKACG_ACCOUNTS=user1@example.com#pass1&user2@example.com#pass2`

## 依赖安装

`requests`（青龙一般自带，缺少去「依赖管理 - Python」安装）。

## 凭证缓存与续期

登录态缓存在脚本同目录 `vikacg_token.json`（只存 token / refreshToken / 设备指纹，绝不存密码），按账号隔离、原子写入（先落 `.tmp` 再 `os.replace`）、权限收紧 0600（青龙 Linux 下生效）。降级链：缓存 token 有效（JWT exp 剩余 > 12h 且设备指纹完整）→ 直接签到；过期 → 先 `POST /refreshToken` 静默续期；续期失败才重新账密登录并覆盖缓存。签到时遇 401 同样走「续期 → 重登」且只重试一次，防死循环；缓存条目缺设备指纹（手工编辑 / 老版本缓存）视为无效，自动走重登链补齐。

## 常见问题

- **报「非法的客户端，请下载官方版本」** → 站点校验浏览器特征头（`Sec-Fetch-*`、`Sec-Ch-Ua*`、`X-Client-Name`、`Architecture`、`X-Device-Code`、`X-Client-Code` 等），脚本已内置完整头模拟，勿删改 `BASE_HEADERS`；站点改版需重新抓包对照。
- **报「Cloudflare 人机校验拦截」** → 请求走了代理且出口 IP 被风控，重试已自动做 2 次，仍失败就换节点。快速判断：用同一代理 `curl -I https://www.vikacg.cc/`，若首页也返回 403 + `Just a moment...`，就是出口 IP 的问题。
- **全部请求超时 / 连接失败** → 直连与备用代理都被掐断，或环境里没有可用代理。查日志首行路由信息：只有直连就配 `VIKACG_PROXY`；出现 `🔀 直连不可用，本次运行改用代理` 说明两条路当时都不通。
- **提示密码错误** → 检查 `VIKACG_ACCOUNTS` 的 `#` 分隔是否正确；密码本身含 `#` 时按首个 `#` 切分。
- **换密码后失效** → 删掉 `vikacg_token.json` 中该账号条目（或整个文件），下次运行自动重登。
- **重试范围** → 连接类异常（连接 / 响应超时、连接失败、代理失败、TLS 失败）重试 2 次（间隔 3s、6s），业务响应（4xx/5xx）不重试；Cloudflare 挑战页（403 + `Cf-Mitigated: challenge`）同样重试 2 次。非 JSON 响应会去 HTML 标签后截断进日志，不会把 HTML 原文带进推送。

## 来源

原创脚本，原 README 未给出上游仓库链接。
