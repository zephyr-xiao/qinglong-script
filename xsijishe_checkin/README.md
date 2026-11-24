# 司机社签到

自动完成司机社（xsijishe.net / dlsjs.net / sjs66.net / xsijishe.com 等多镜像）每日签到。支持 Cookie 直填，也支持邮箱密码自动登录并缓存 Cookie；站点域名会不定期更换，脚本内置可用域名列表并在失败时自动切换。需要能访问站点（国内直连常被污染），建议配代理。

## 青龙任务命令

```text
task xsijishe_checkin/xsijishe_checkin.py
```

定时建议 `10 9,20 * * *`（每天 9:00 与 20:00 两次），避免临时故障导致漏签。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `SIJISHE_ACCOUNTS` | 是 | — | 账号列表，支持 Cookie / 邮箱密码 / 混合多账号（见下） |
| `SIJISHE_HOST` | 否 | 空 | 手动锁定签到域名（优先于内置列表），如 `dlsjs.net`；设置后不做任何自动发现 |
| `SIJISHE_NOTIFY` | 否 | `true` | 是否推送通知 |
| `SIJISHE_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅失败时推送 |
| `SIJISHE_PROXY` | 否 | 空 | 代理地址，如 `http://172.17.0.1:7890`；留空时自动回退青龙全局代理 |
| `SIJISHE_DEBUG` | 否 | `false` | 调试模式，输出接口响应细节 |

代理优先级：`SIJISHE_PROXY` > 青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`，大小写均可）> 直连。`trust_env` 关闭，不受系统代理干扰；启动日志会打印脱敏后的代理与来源。

账号格式（多账号换行分隔，支持混合）：

```text
SIJISHE_ACCOUNTS=你的完整Cookie字符串
SIJISHE_ACCOUNTS=your@email.com&yourpassword
```

判定规则：含 `;` 或 `=` 的整行按 Cookie 解析，否则按「邮箱&密码」解析，因此 Cookie 值里出现 `@` 也不会被误判成凭据账号。

## 依赖安装

青龙「依赖管理」→「Python」安装 `requests`、`beautifulsoup4`（必需）。`ddddocr` 仅在站点触发登录验证码时需要；`playwright`（另需 chromium）仅在内置域名全部失效、需渲染发布页兜底时需要。

## 凭证获取

浏览器登录站点 → F12 → Application（应用程序）→ Cookies，复制所有 Cookie，形如 `key1=value1; key2=value2; ...`。

## 域名发现与容错

发现优先级：`SIJISHE_HOST` > 内置 4 个可用域名 > Playwright 渲染发布页 `47447.net`（每次运行最多一次）。本次运行中第一个签到成功的域名会提升为后续账号的首选。

站点的 403 是 cookie 门槛而非封禁：首个请求返回 403 并下发 cookie，同一会话再请求即 200。脚本对 403 / 429 / 5xx（含 Cloudflare 520~524）在同一域名内退避重试，最多 3 次，仍失败才切下一个域名。

失败判定分两类：主机级故障（TLS EOF、连接失败、403/429/5xx、登录响应不是 Discuz 应答）换域名重试，不累加登录失败、不删 Cookie 缓存；真实登录失败（拿到 Discuz CDATA 应答但没有「欢迎您回来」）计入熔断，同一账号连续 2 次即中止，避免把账号推进验证码流程。

已失效域名不要加回候选：`sjs96.com` / `sjs66.com`（301 跳发布页）、`dlsjs.com`（TLS 握手 EOF）、`xsijishe.ink` / `sjslt.cc`（JS 跳转页）、`sjs47.com`（DNS 与 TLS 均失败）。

## 常见问题

- **容器内 TLS EOF 或莫名 403** → 多为容器 DNS 被污染，确认 `SIJISHE_PROXY` 或青龙全局代理可用。
- **签到成功文案对不上** → 成功判定按 `<h3>签到成功</h3>` + `获得随机奖励 N车票` 钉死，站点改文案时以日志里的接口响应原文对照。
- **Cookie 缓存安全** → `xsijishe_cookie.json` 为明文，已写入时收紧到 0600（非 POSIX 环境跳过），仓库根 `.gitignore` 的 `*_cookie.json` 兜底，勿提交。
- 通知复用青龙 `notify.py`，不用在脚本里单独配推送。

## 来源

原创脚本，原 README 未给出上游仓库链接。
