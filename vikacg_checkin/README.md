# 维咔VikACG签到（vikacg_checkin）

维咔VikACG[V站]（https://www.vikacg.cc）每日钱包签到领积分，纯 API 免验证码，青龙面板 Python 脚本。

## 站点与接口说明

- 站点为 Nuxt 3 SSR 应用（Moonlight 主题），同源 API：`https://www.vikacg.cc/api/vikacg/v1/*`
- 登录即发 JWT 双 token（**有效期 30 天**），`Authorization: Bearer <token>` 鉴权
- 签到端点 `userMission` 幂等友好：未签返回 200 + 签到数据；已签返回 **HTTP 409「用户今天已签到」**（按成功处理）。
  幂等判定为**双门槛**（HTTP 409 且命中已签关键词），关键词不做全局匹配，避免未来成功文案含「已签」时误判；200+success 一律按成功解析，文案自带已签语义时如实输出「已签到」
- **服务端校验浏览器特征头**（`Sec-Fetch-*`、`Sec-Ch-Ua*`、`X-Client-Name: VikACG Moonlight`、`Architecture: AixPot`、`X-Device-Code`、`X-Client-Code` 等），缺失直接报「非法的客户端，请下载官方版本」——脚本已内置完整头模拟，勿删改 `BASE_HEADERS`
- 设备指纹（`X-Device-Code` / `X-Client-Code`，UUIDv4）与 token 绑定，随缓存持久化复用，避免每次运行换设备触发风控

## 部署

1. 青龙面板 →「脚本管理」→ 上传 `vikacg_checkin.py`
2. 「环境变量」→ 新建 `VIKACG_ACCOUNTS`（见下）
3. 「定时任务」→ 新建：命令 `task vikacg_checkin/vikacg_checkin.py`，定时规则 `23 8 * * *`（与脚本头 cron 一致即可）

依赖：`requests`（青龙一般自带，缺失去「依赖管理 - Python」安装）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `VIKACG_ACCOUNTS` | ✅ | — | 账号：`邮箱#密码`，多账号用 `&` 或换行分隔 |
| `VIKACG_PROXY` | — | 空（直连） | 可选代理，如 `http://172.17.0.1:7890`。**默认直连并强制 IPv4**；设为 `none`/`direct` 也表示直连。直连被掐断时会自动用环境里的 `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` 作备用出口（这些全局变量不参与主路由选择）。SOCKS 代理需额外安装 `pip install requests[socks]` |
| `VIKACG_INSECURE` | — | `false` | 跳过 TLS 证书校验（仅自签代理等特殊场景），默认严格校验 |
| `VIKACG_NOTIFY` | — | `true` | 是否通过 notify.py 推送 |
| `VIKACG_NOTIFY_ONLY_FAIL` | — | `false` | 仅失败时推送（全成功静默） |
| `VIKACG_TIMEOUT` | — | `30` | HTTP 超时秒数 |
| `VIKACG_DEBUG` | — | `false` | 输出 token 来源等调试细节 |
| `VIKACG_BASE_URL` | — | `https://www.vikacg.cc` | 站点地址（备用域名时改） |

示例：

```
VIKACG_ACCOUNTS=user1@example.com#pass1&user2@example.com#pass2
```

## 凭证缓存与降级链

登录态缓存在脚本同目录 `vikacg_token.json`（**只存 token / refreshToken / 设备指纹，绝不存密码**），按账号隔离，**原子写**（先落 `.tmp` 再 `os.replace`）+ 权限收紧 **0600**（青龙 Linux 下生效）：

1. 缓存 token 有效（JWT exp 剩余 > 12h，且设备指纹完整）→ 直接签到
2. 过期 → 先 `POST /refreshToken` 静默续期（不触碰登录流程，对风控友好）
3. 续期失败 → 才重新账密登录并覆盖缓存

签到时遇 401 同样走「续期 → 重登」降级链，且**只重试一次**，防死循环。
缓存条目缺设备指纹（手工编辑/老版本缓存）视为无效，自动走重登链补齐。

## 网络重试与报错分类

默认**直连**（强制 IPv4：站点 A 记录稳定可用，AAAA 记录在大陆常被 reset）。主路由由 `VIKACG_PROXY` 决定，`session.trust_env` 关闭，环境里的 `HTTPS_PROXY` 等不会暗中接管主路由。

大陆到 Cloudflare 的 reset 是**成片出现**的（可能连续十几秒全失败，过后又全通），所以加了双路由：主路由直连，遇到连接类失败自动用备用出口（环境里的 `HTTPS_PROXY` 等，或主路由是代理时退回直连）重发同一个请求，日志打一行 `🔀 直连不可用，本次运行改用代理 …`。这样单点抖动不会让整轮任务失败。

连接类网络异常（连接/响应超时、连接失败、代理失败、TLS 失败）自动重试 **2 次**（间隔 3s、6s），业务响应（4xx/5xx）不重试；重试耗尽后按异常类型分类报错。续期遇网络不可达时快速失败，不再白跑一轮登录重试。

Cloudflare 人机校验页（`403` + `Cf-Mitigated: challenge`）与网关错误页同样重试 **2 次**：挑战只看出口 IP 的风控评分，整站（含 API）都会被拦，换连接有机会通。重试耗尽后报「Cloudflare 人机校验拦截（代理出口 IP 被风控）」并提示换节点，不再只是一句「疑似拦截页」。
非 JSON 响应会去除 HTML 标签后截断进日志，不会把 HTML 原文带进推送。

## 常见问题

- **报「Cloudflare 人机校验拦截」**：说明请求走了代理，且该出口 IP 被 Cloudflare 风控（同一条代理的不同出口会时通时不通）。重试已自动做 2 次，仍失败就去 mihomo 换一个节点；快速判断：用同一代理 `curl -I https://www.vikacg.cc/`，若首页也返回 403 + `Just a moment...`，就是出口 IP 的问题。
- **报「非法的客户端，请下载官方版本」**：请求头被改或站点更新了客户端校验。脚本内置的浏览器模拟头是逆向当前前端 bundle 得出的，请勿精简；若站点改版需重新抓包对照。
- **全部请求超时/连接失败**：直连 + 备用代理都被掐断，或环境里没有任何可用代理。查日志首行的路由信息：只有直连就配 `VIKACG_PROXY`，已有 `🔀` 切换记录说明两条路当时都不通。
- **提示密码错误**：检查 `VIKACG_ACCOUNTS` 里 `#` 分隔是否正确；密码本身含 `#` 时按首个 `#` 切分。
- **换密码后**：删掉 `vikacg_token.json` 中该账号的条目（或整个文件）即可，下次运行自动重登。

## 单元测试

```bash
cd vikacg_checkin
python -m unittest discover -s tests -v
```
