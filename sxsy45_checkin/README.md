# 尚香书苑签到

自动完成尚香书苑论坛（Discuz! X3.5，主域名不定期更换，当前为 sxsy33.com）每日签到。站点已换用自研 `sx_signin` 插件（旧 `k_misign` 地址返回「插件不存在或已关闭」），签到需提交 4 位图形验证码，由本地 ddddocr 识别；支持 Cookie 直签或账号密码登录，域名自动发现。

登录有两种方式：账号密码（`用户名#密码`，Cookie 失效自动重登）或直接填整段 Cookie（不触发登录验证码）。两种方式都依赖 ddddocr，因为签到验证码必须识别。脚本启动时先探测可用域名，日志第二行会打印实际使用的域名（如 `站点域名: https://sxsy33.com（HTTP 200）`）；全部探测失败时按默认域名继续，让后续流程如实报错。

## 青龙任务命令

```text
task sxsy45_checkin/sxsy45_checkin.py
```

定时 `23 8 * * *`（脚本 docstring 已自带）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `SXSY_ACCOUNTS` | 是 | 空 | `用户名#密码`（多账号 `&` 或换行分隔），或整段 Cookie（不含 `#`） |
| `SXSY_NOTIFY` | 否 | `true` | 是否推送（复用青龙 `notify.py`） |
| `SXSY_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅失败时推送，全成功静默 |
| `SXSY_TIMEOUT` | 否 | `30` | 请求超时秒 |
| `SXSY_DEBUG` | 否 | `false` | 调试模式，打印 OCR 结果、页面状态等 |
| `SXSY_PROXY` | 否 | 空（直连） | **默认直连**且**不读** `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`；填 URL 走代理，填 `direct` / `none` / `off` / `0` 也是直连 |

Cookie 与账密都填在 `SXSY_ACCOUNTS` 一个变量里，脚本按是否含 `#` 自动区分。青龙「配置文件」里的 `ProxyUrl` 只供面板自身使用，脚本读不到。

多账号写法（`&` 或换行分隔）：

```text
用户名A#密码A
用户名B#密码B
```

签到流程：GET `plugin.php?id=sx_signin:index` 解析内嵌 `window.SIGNIN`（`signed=1` 即今天已签）→ 取 4 位图形验证码 OCR → POST `plugin.php?id=sx_signin:ajax`，body 为 `op=signin&formhash=<fh>&seccode=<code>`，返回 JSON `code=0` 成功（带 `credit/streak/total`）→ 再回查签到页确认。

## 依赖安装

青龙「依赖管理」-「Python」：`requests`（必装）、`ddddocr`（必装，登录验证码与签到图形验证码都要识别）。

## 凭证获取

- **账号密码**：`SXSY_ACCOUNTS=用户名#密码`。登录 Cookie 缓存到 `sxsy45_cookie.json`（含敏感凭证，已 gitignore，勿分享），缓存有效时完全不走登录流程。
- **Cookie**：把整段 Cookie 填进 `SXSY_ACCOUNTS`（不含 `#`），过期需手动重新抓取。

## 常见问题

- **频繁 403 `Just a moment...`** → 出口 IP 被 Cloudflare 风控，不是站点故障、与账号密码无关。判据是 `cf-mitigated: challenge` 响应头、标题含 `Just a moment` / `Attention Required`、或 503 页里的 `challenge-platform` 脚本，不看状态码；此时脚本不换域名（同一出口换域名一样被拦），首页被质询时保留 Cookie 缓存继续试签到，签到页被质询退避重试一次，登录页被质询则立即停止、不空耗登录重试。根治办法是别用境外代理：脚本默认直连，不读全局 `HTTPS_PROXY`。
- **间歇 520~526（本站常见 522）** → Cloudflare 回源超时，单请求约 20 秒、页面无 `formhash` / `discuz_uid`。脚本对签到接口重试 2 次、页面 3 次（间隔 5s / 15s）；首页 522 时保留 Cookie 缓存直接尝试签到，不清缓存、不重登。
- **WAF 直拦 403 / 路径 404** → 任何非瞬时 4xx 都按「不可用页面」处理，不重试、直接报状态码 + 标题，不会被误读成未登录或页面改版。
- **提示「登录页无 formhash」** → 先看日志第二行的实际域名。旧域名 301 到的是 `sxsy.pw` 导航页（HTTP 200、标题正常），不是新站登录页；脚本会自动探测 `DOMAIN_CANDIDATES` 并从导航页解析最新域名，通常无需改代码，只有导航页地址也变了才需更新 `NAV_URL`。
- **登录成功却被判失败** → 不能只看 `succeedlocation`（登录失败页也含该元素），也不要用 `member.php?mod=logging` 子串判登录态（已登录页的退出链接就含它）；脚本以非 0 的 `discuz_uid` 判已登录。
- **验证码连续 3 次识别失败** → 脚本放弃并提示改用 Cookie 模式，避免反复触发风控；账号密码错误与站点限流文案「密码错误次数过多，请 15 分钟后重新登录」按致命错误处理，不自动重试。
- **签到结果判定** → 以回查签到页 `window.SIGNIN.signed == 1` 为最终判据；回查页不可用时只有接口 `code=0` 才判成功并标注未复核。`code=2` 表示今天已签、`code=11` 表示验证码错误。
- **确认出口是否被拦** → `curl -s -o /dev/null -w '%{http_code}\n' https://sxsy33.com/forum.php`：直连返回 302 或 200 就是通的，返回 403 且带 `Just a moment` 说明该出口被拦。
