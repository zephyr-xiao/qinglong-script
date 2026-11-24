# 福利吧预注册签到

自动完成福利吧论坛（wnflb2023.com）`fuliba_prereg` 插件的每日签到。预注册账号连续签到 30 天可免费转正，漏签会中断连续天数。支持 Cookie 直签或账号密码登录，二选一，也可同时配置各自独立跑一遍。已签到会自动跳过，可安全重复执行；运行结束输出汇总报告（连续天数 / 进度 / 距转正天数）。

## 青龙任务命令

```text
task wnflb_checkin/wnflb_checkin.py
```

定时 `15 8 * * *`（脚本 docstring 已自带，`ql repo` 拉库会自动建任务）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `WNFLB_COOKIE` | 二选一 | 空 | 完整 Cookie 字符串，多账号换行分隔 |
| `WNFLB_ACCOUNTS` | 二选一 | 空 | 账号密码 `用户名#密码`，多账号换行分隔 |
| `WNFLB_HOST` | 否 | `www.wnflb2023.com` | 站点域名，有备用域名时填这里 |
| `WNFLB_PROXY` | 否 | 空 | HTTP 代理，如 `http://172.17.0.1:7890` |
| `WNFLB_TIMEOUT` | 否 | `25` | HTTP 超时秒数 |
| `WNFLB_NOTIFY` | 否 | `true` | 是否推送（复用青龙 `notify.py`） |
| `WNFLB_NOTIFY_ONLY_FAIL` | 否 | `false` | 全部成功时静默 |
| `WNFLB_DEBUG` | 否 | `false` | 调试模式，打印请求 URL / 状态码 / 跳转地址 / 登录响应 |

多账号必须用换行分隔（Cookie 值里可能含 `&`，用 `&` 分隔会被截断）。

代理优先级：`WNFLB_PROXY` > 青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` 等）> 直连。脚本已关闭 `trust_env`，青龙 `ProxyUrl` 对它无效，Windows 注册表系统代理也不会被静默采用。

## 依赖安装

青龙「依赖管理」-「Python」安装：

```text
requests
```

`ddddocr` 可选，仅登录遇到验证码时才惰性加载，不装也能正常跑（未安装时会提示改用 Cookie 模式）。本机调试 ddddocr 若报 `_ARRAY_API not found` / `numpy.core.multiarray failed to import`，是 numpy 2.x 与旧 onnxruntime 不兼容，装 `numpy<2` 或升级 onnxruntime；青龙容器一般是 numpy 1.x，无需处理。

## 凭证获取

- **Cookie（推荐）**：浏览器登录站点 → F12 打开 Network → 刷新页面 → 点任意 `plugin.php` 或 `forum.php` 请求 → 复制完整 `Cookie` 值。关键是 `S5r8_2132_auth`（登录态）与 `S5r8_2132_saltkey`，缺 saltkey 会判定 Cookie 失效；`X_CACHE_KEY` 等非 Discuz 键一并复制。
- **账号密码**：`WNFLB_ACCOUNTS=用户名#密码`。登录成功后 Cookie 缓存到脚本同目录 `wnflb_cookie.json`（已被 `.gitignore` 的 `*_cookie.json` 忽略，只存 Cookie 不存密码），之后优先复用，删文件可强制重登。账号设置了安全提问时无法用此模式，请改用 Cookie。

多账号写法（换行分隔，不能用 `&`）：

```text
WNFLB_COOKIE 变量，每行一个账号：
CookieA
CookieB

WNFLB_ACCOUNTS 变量，每行一个账号：
用户名A#密码A
用户名B#密码B
```

## 常见问题

- **提示「Cookie 已失效」** → 重新登录抓完整 Cookie 更新 `WNFLB_COOKIE`；账密模式会自动重登，无需手动处理。
- **账密模式提示「该 IP 剩余尝试 N 次」** → 站点登录失败按 IP 累计，连续 5 次要验证码，约 15 分钟后或一次成功登录后重置；脚本本身对认证失败不自动重试（只重试网络异常），也不要反复重跑试探，先核对密码。
- **日志出现「站点要求输入验证码」** → 正常降级路径，ddddocr 自动识别，识别错会换图重试最多 3 次；未装 ddddocr 按上节安装，连续 3 次失败建议改用 Cookie。
- **本机全部 ReadTimeout 但浏览器能打开** → 多半是 Windows 系统代理（Clash/V2Ray）作怪；脚本已关闭 `trust_env`，只认 `WNFLB_PROXY` 与环境变量代理。
- **日志「TLS 证书校验失败,已自动降级为不校验继续」** → CDN 边缘证书异常或本机时间不对，属可用性兜底；频繁出现请核查系统时间与证书链。
- **「formhash 已过期」/「请通过签到页上的按钮提交本操作」** → 脚本会自动重取页面重试一次；持续失败多为站点改版，用 `WNFLB_DEBUG=true` 看跳转地址。
- **签到成功但连续天数没变** → 当天已签过（幂等返回 `done=-2`），正常现象。
- **签到结果码** → `done=1` 成功、`done=-2` 今日已签到（按成功处理）、其他为失败；`ac=do` 必须用 POST，GET 会被站点拒绝。
- **登录判定** → 响应含「欢迎您回来」或拿到 `S5r8_2132_auth` Cookie 即成功；但「待验证码」状态服务端也会下发 auth Cookie，脚本把验证码判定放在最前面，避免误判登录成功。
