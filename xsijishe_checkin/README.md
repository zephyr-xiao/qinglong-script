# 司机社签到 - 青龙面板脚本

## 功能
自动完成 [司机社](https://dlsjs.net/)（xsijishe.net / dlsjs.net / sjs66.net / xsijishe.com 等多镜像）的每日签到。

支持两种认证方式：
- **Cookie 模式**：直接填入浏览器抓取的 Cookie，轻量无依赖
- **邮箱密码模式**：填入邮箱&密码，自动登录并缓存 Cookie；仅在站点触发验证码时才需要 ddddocr
- TLS 默认校验证书，仅 SSL 握手失败时自动降级并告警一次；`trust_env` 关闭，代理只认 `SIJISHE_PROXY` > 青龙全局代理 > 直连，不受系统代理干扰

> 💡 镜像域名会不定期更换/失效。脚本内置**实测可用**的域名列表并在失败时自动切换；
> 列表全部失效时才用 Playwright 渲染发布页兜底找新域名（可选依赖）。

## 快速开始

### 1. 青龙面板安装依赖

在青龙「依赖管理」-「Python」中安装（必需）：
```
requests
beautifulsoup4
```

按需安装（可选）：
- `ddddocr` —— 仅当站点触发登录验证码时（账号被连续登录失败推进验证码流程）才需要
- `playwright`（另需安装 chromium）—— 仅当内置域名全部失效、需要渲染发布页兜底时才需要

### 2. 青龙面板配置环境变量

| 变量名 | 必填 | 默认 | 说明 |
|--------|------|------|------|
| `SIJISHE_ACCOUNTS` | ✅ | — | 账号列表，见下方格式说明 |
| `SIJISHE_HOST` | ❌ | 空 | 手动锁定签到域名（优先于内置列表），如 `dlsjs.net` |
| `SIJISHE_NOTIFY` | ❌ | true | 是否推送通知 |
| `SIJISHE_NOTIFY_ONLY_FAIL` | ❌ | false | 仅失败时推送 |
| `SIJISHE_PROXY` | ❌ | 空 | 代理地址，如 `http://172.17.0.1:7890`；留空时自动回退青龙全局代理 |
| `SIJISHE_DEBUG` | ❌ | false | 调试模式，输出接口响应细节 |

代理取值优先级：`SIJISHE_PROXY` > 青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`，大小写均可）> 直连。
站点在国内直连会拿到被污染的 DNS，实际部署时**建议配置代理**；启动日志会打印脱敏后的代理与来源。

### 3. 账号格式

**Cookie 模式**（推荐）：
```
SIJISHE_ACCOUNTS=你的完整Cookie字符串
```

**邮箱密码模式**：
```
SIJISHE_ACCOUNTS=your@email.com&yourpassword
```

**多账号**（换行分隔，支持混合）：
```
SIJISHE_ACCOUNTS=user1@mail.com&pass1
user2@mail.com&pass2
完整Cookie字符串_账号3
```

> 判定规则：含 `;` 或 `=` 的整行按 Cookie 解析，否则按「邮箱&密码」解析——
> 因此 Cookie 值里出现 `@` 也不会被误判成凭据账号。

### 4. 获取 Cookie 的方法
1. 用 Chrome/Edge 打开站点并登录
2. 按 F12 打开开发者工具
3. 切换到 Application（应用程序）→ Cookies
4. 复制所有 Cookie，格式如 `key1=value1; key2=value2; ...`

## 域名发现策略

优先级从高到低：

1. `SIJISHE_HOST` 环境变量（手动锁定，此时不做任何自动发现）
2. 内置实测可用域名列表（4 个：xsijishe.net / dlsjs.net / sjs66.net / xsijishe.com）
3. Playwright 渲染发布页 `47447.net` 动态获取（仅当上面全部失败时启用，每次运行只做一次）

本次运行中**第一个签到成功的域名会被提升为后续账号的首选**，避免每个账号都从头重试一遍。

已失效域名（不要加回候选）：

| 域名 | 现象（2026-11-06 实测） |
|------|------------------------|
| `sjs96.com` / `sjs66.com` | 301 跳转到发布页 `47447.net`，站点已不在这两个域名上 |
| `dlsjs.com` | TLS 握手直接 EOF |
| `xsijishe.ink` / `sjslt.cc` | 已变成 JS 跳转页，不再提供 Discuz 页面 |
| `sjs47.com` | DNS 解析与 SSL 握手均失败 |

## 网络容错与失败判定

站点的 403 是**cookie 门槛**而不是封禁：首个请求返回 403 并下发 cookie，同一会话再请求即 200
（2026-11-06 实测 `[403, 200, 200]`）。因此脚本对 403 / 429 / 5xx（含 Cloudflare 的 520~524）
在**同一域名内**退避重试，最多 3 次；重试仍失败才切下一个域名。

失败判定分两类，避免把站点故障算到账号头上：

| 类型 | 例子 | 处理 |
|------|------|------|
| 主机级故障 | TLS EOF、连接失败、403/429/5xx、登录响应不是 Discuz 应答（挑战页 / System Error） | 换域名重试，**不累加登录失败**，也不会删除 Cookie 缓存 |
| 真实登录失败 | 拿到 Discuz 的 CDATA 应答但没有「欢迎您回来」 | 计入熔断，同一账号连续 2 次即中止（保护账号不被推进验证码流程） |

## 验证码说明

2026-10-10 实测：登录页不返回 `seccodehash`（页面内 `seccode`/`verify` 关键字出现 0 次），正常登录无需验证码。

但 Discuz 的验证码是按**登录失败次数**触发的，因此脚本保留 ddddocr 识别链路作为防御路径
（最多重试 3 次）；同时**同一账号连续 2 次登录失败即中止重试**，避免把账号推进验证码流程。

## 定时建议
```
cron: 10 9,20 * * *
```
一天两次（9:00 和 20:00），避免因临时故障导致签到遗漏。

## 单元测试

测试全部离线（mock，不发真实请求），使用标准库 `unittest`，无需额外依赖：

```bash
# 在仓库根目录
python -m unittest discover -s xsijishe_checkin/tests -t xsijishe_checkin/tests -v

# 或进入本目录后
python -m unittest discover -s tests -t tests -v
```

## 已知限制

- **签到成功文案已按真机响应钉死**（2026-11-05 抓取）：`<h3>签到成功</h3>` +
  `<p class="con">获得随机奖励 123车票 和 。</p>`，第二项为空时不会再输出悬空的「和」。
  脚本仍会把签到接口的响应原文完整打进日志，站点改文案时可据此核对。
- **容器内必须能正常解析域名**：青龙容器的 DNS 若被污染，直连会拿到错误 IP（表现为 TLS EOF 或莫名 403），
  请确认 `SIJISHE_PROXY` 或青龙全局代理可用。
- **Cookie 缓存**：`xsijishe_cookie.json` 为明文，已写入时收紧到 0600 权限（非 POSIX 环境自动跳过），
  并靠仓库根 `.gitignore` 的 `*_cookie.json` 兜底，请勿提交。

## 依赖
- `requests`、`beautifulsoup4`（青龙 Python 环境已预装）
- `ddddocr`（可选，OCR 验证码识别，仅验证码防御路径需要）
- `playwright`（可选，域名兜底发现需要，另需 chromium）

## 作者
zephyr_xiao
