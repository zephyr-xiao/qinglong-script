# 尚香书苑签到 - 青龙面板脚本

自动完成尚香书苑论坛（Discuz! X3.5，主域名不定期更换，当前为 sxsy33.com）`k_misign` 插件的每日签到。

该站**登录强制图片验证码**，且 k_misign 签到还叠加了一道**算术验证**（"签到验证：9 + 15 = ?"），两道都能自动通过，无需人工介入。

## 特性

- 两种认证方式：**Cookie 直签** 或 **账号密码登录**
- **域名自动发现**：站点换主域名后自动切换，不用改代码（见下节）
- **默认直连**：本站国内可达，脚本不读全局 `HTTPS_PROXY`（走境外代理反而会被 Cloudflare 当数据中心 IP 发质询）；需要时用 `SXSY_PROXY` 显式指定
- 账密模式自动识别登录验证码：本地 **ddddocr** OCR（识别错自动换图重试，最多 3 次）
- 登录 Cookie JSON 缓存复用：缓存有效完全不走登录流程，失效自动重登
- 签到算术验证自动计算（支持 `+ - × ÷`）
- 已签到自动跳过（以签到页有无 `JD_sign` 按钮为准，幂等可重复执行）
- 签到后输出统计：连续天数 / 等级 / 积分奖励 / 累计天数
- 站点瞬时故障自动重试：Cloudflare 520~526（本站常见 522 回源超时）、网关 5xx 与网络异常都会退避重试，重试仍失败才报错
- Cloudflare 人机质询（403 `Just a moment...`）单独识别：按出口 IP 风控处理，不清 Cookie 缓存、不空耗登录重试，报错直接说明与账号密码无关
- 不误判登录态：论坛首页拿不到正常页面时保留 Cookie 缓存继续签到，不会因为一次 522 就清空缓存去重登
- 多账号支持（`&` 或换行分隔）
- 复用青龙 `notify.py` 推送，`SXSY_NOTIFY_ONLY_FAIL=true` 可配置全成功时静默

## 认证方式怎么选

| 方式 | 环境变量 | 优点 | 注意 |
|---|---|---|---|
| 账号密码 | `SXSY_ACCOUNTS` 格式 `用户名#密码` | 免维护，Cookie 失效自动重登 | 需安装 `ddddocr` 依赖 |
| Cookie 直签 | `SXSY_ACCOUNTS` 直接填整段 Cookie（不含 `#`） | 不需要 ddddocr | Cookie 过期需手动重新抓取 |

两种格式都填在 `SXSY_ACCOUNTS` 一个变量里，脚本按是否含 `#` 自动区分。

> 登录验证码识别连续 3 次失败会放弃并提示改用 Cookie 模式（避免反复触发站点风控）。
> 账号密码错误属于致命错误，**不会自动重试**。

## 接口流程（2026-09-29 实测）

### 登录（仅账密模式）

| 步骤 | 请求 | 说明 |
|---|---|---|
| 1 | `GET member.php?mod=logging&action=login` | 取 `formhash` 与验证码入口 `idhash`（当前固定为 `cS`） |
| 2 | `GET misc.php?mod=seccode&action=update&idhash=<x>&modid=member::logging&inajax=1` | 返回 JS，内含真实 `seccodehash` |
| 3 | `GET misc.php?mod=seccode&update=<随机>&idhash=<x>` | 验证码 PNG，送 ddddocr 识别 |
| 4 | `POST member.php?mod=logging&action=login&loginsubmit=yes` | body 含账密 + `seccodehash` / `seccodemodid` / `seccodeverify` 三件套；响应含 `succeedhandle` 即成功 |

### 签到（k_misign）

| 步骤 | 请求 | 说明 |
|---|---|---|
| 1 | `GET plugin.php?id=k_misign:sign` | 有 `id="JD_sign"` 按钮 → 今天未签；无按钮且有 `lxdays` 统计 → 已签（幂等跳过）；被跳转登录页 → 登录态失效 |
| 2 | `GET plugin.php?id=k_misign:sign&operation=qiandao&formhash=<fh>&format=text` | 返回算术验证题 XML（`var q="签到验证：A - B = ?"`） |
| 3 | `GET ...&mathverify_answer=<计算结果>` | 答对即完成签到，返回结果 XML |

签到最终状态以**回查签到页 `JD_sign` 是否消失**为准（接口返回文案因插件版本而异，不作判据）。回查页本身拿不到时，只有接口文案明确含"已签到/签到成功"才判成功，并标注"回查页暂不可用"。

## 域名自动发现

站点主域名会不定期更换（2026-10-30 就从 `sxsy45.com` 换到了 `sxsy33.com`）。换域名时的行为很坑：

```
sxsy45.com  --301-->  sxsy.pw（纯 JS 导航页，HTTP 200「小说论坛 - 最新地址」）
                       真实站点在 sxsy33.com
```

旧域名 301 到的是**导航页而不是新站登录页**，requests 默认跟随重定向，于是登录流程拿到一个 200 状态、标题正常、但没有 `formhash` 的页面，报出来就是"登录页无 formhash"——看着像改版或风控，实际只是域名换了。

脚本启动时会先确定本次可用的域名，再进入账号流程：

| 步骤 | 做法 |
|---|---|
| 1 | 逐个探测 `DOMAIN_CANDIDATES`（内置候选域名）的登录页，能拿到 `formhash` 即采用 |
| 2 | 探测中若 301 落到导航页，直接从页面 JS 的 `CONFIG.websiteUrl` / `latestDomain` 解析最新域名 |
| 3 | 候选全不可用时，主动访问导航页 `NAV_URL` 再解析一次，并对新域名重试探测 |
| 4 | 命中 Cloudflare 人机质询时**不再换域名**（同一出口换域名一样被拦），直接按风控报错 |

日志第二行会打印实际使用的域名，例如 `🌐 站点域名: https://sxsy33.com（HTTP 200）`。全部探测失败时按默认域名继续，让后续流程如实报错。

> 站点再次换域名时，通常无需改代码：旧域名会 301 到导航页，第 2/3 步就能拿到新域名。只有在站点同时换掉导航页地址时才需要更新 `NAV_URL`。

## 站点瞬时故障处理

站点套了 Cloudflare，回源超时会间歇性返回 **520~526 错误页（实测 522）**，此时任何页面都没有 `formhash`/`discuz_uid`，单次请求耗时约 20 秒。脚本对此的处理：

| 场景 | 处理 |
|---|---|
| 论坛首页返回 522 | 退避重试；仍失败则返回"无法确认"，**保留 Cookie 缓存**直接尝试签到，不清缓存、不重登 |
| 登录页返回 522 | 退避重试；仍失败才报错，消息附 `HTTP 状态码 + 页面标题` |
| 签到页/签到接口返回 522 | 退避重试（接口 2 次，页面 3 次，间隔 5s/15s） |
| 签到成功但回查页 522 | 接口文案含"已签到"判成功并标注未复核，否则报"签到结果未确认"（不误报成功） |

因此偶发 522 不再表现为"登录页无 formhash"或"缓存 Cookie 失效"，通知里的报错也能直接看出是哪一类故障。

### Cloudflare 人机质询（403 `Just a moment...`）

出口 IP 被 Cloudflare 判定可疑时，站点会改发**托管质询页**：HTTP 403、标题 `Just a moment...`、正文是 JS 挑战脚本。这类页面同样没有 `formhash`/`discuz_uid`，与 522 的区别是**不是站点故障，而是这条出口 IP 被风控**，换节点/等一段时间即可恢复，改代码或改密码都没用。

| 场景 | 处理 |
|---|---|
| 论坛首页被质询 | 判为"无法确认"，**保留 Cookie 缓存**直接尝试签到（只有登录页被拦时，签到未必受影响） |
| 签到页被质询 | 退避后重试一次；仍被拦则报"签到页请求失败：Cloudflare 人机质询（HTTP 403）" |
| 登录页/验证码接口/登录提交被质询 | **立即停止**，不再消耗登录重试次数、不连续请求（避免加重风控），消息提示"出口 IP 被 Cloudflare 风控，与账号密码无关" |

判定依据：`cf-mitigated: challenge` 响应头、标题关键字（`Just a moment` / `Attention Required` 等）、或 503 页面里的 `challenge-platform` 脚本。

另外，`get_retry` 把任何 4xx 都当作"不是可用页面"（4xx 不属瞬时故障，不重试，直接报状态码 + 标题），所以 WAF 直接拦的 403、路径 404 同样不会被误读成"未登录"或"页面改版"。

**根治办法是别用境外代理访问本站**：脚本因此**默认直连**，不读全局 `HTTPS_PROXY`（配置见下节）。国内家庭宽带出口是正常用户 IP，Cloudflare 不会质询；而机场节点多是 AWS/云厂商机房 IP，正是质询的高发对象。青龙容器里可以用下面两条命令对比出口：

```bash
# 直连（脚本默认走这条）
curl -s -o /dev/null -w 'direct: %{http_code}\n' 'https://sxsy33.com/forum.php'
# 经代理（仅当显式设了 SXSY_PROXY 才会用）
curl -x http://<代理地址>:7890 -s -o /dev/null -w 'proxy:  %{http_code}\n' 'https://sxsy33.com/forum.php'
```

直连 302（未登录跳登录页）或 200 就是通的；返回 403 且带 `Just a moment` 说明该出口被拦。

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `SXSY_ACCOUNTS` | 账号列表：`用户名#密码`（多账号 `&` 或换行分隔），或整段 Cookie 字符串 | 必填 |
| `SXSY_NOTIFY` | 是否推送 | `true` |
| `SXSY_NOTIFY_ONLY_FAIL` | 仅失败时推送（全成功静默） | `false` |
| `SXSY_TIMEOUT` | 请求超时（秒） | `30` |
| `SXSY_DEBUG` | 调试模式（打印 OCR 结果、页面状态等） | `false` |
| `SXSY_PROXY` | 网络出口。**默认直连**，且**不读** `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` 全局变量；填 URL 走代理，填 `direct`/`none`/`off`/`0` 也是直连 | 空（直连） |

> 为什么默认直连：本站是国内 Discuz 论坛，直连可达；经境外代理节点访问会被 Cloudflare 判为数据中心 IP 并下发 403 人机质询（2026-10-28 实测），是"登录页无 formhash / 缓存 Cookie 失效"的真实原因。青龙里即便配了全局代理，本脚本也不会用，日志首行会写明 `🌐 网络: 直连 （默认直连，已忽略 HTTPS_PROXY）`。
>
> 注意：青龙「配置文件」里的 `ProxyUrl` 只供面板自身使用，脚本读不到；确需代理请设 `SXSY_PROXY`。

## 依赖安装

青龙面板 →「依赖管理」→「Python」：

- `requests`（必装）
- `ddddocr`（账密模式必装；只用 Cookie 模式可不装）

## 本地测试

```bash
cd sxsy45_checkin
python -m unittest discover -s tests -v   # 85 个离线单测，不发真实请求

# 真实干跑
SXSY_ACCOUNTS="用户名#密码" SXSY_NOTIFY=false python sxsy45_checkin.py
```

## 文件说明

| 文件 | 说明 |
|---|---|
| `sxsy45_checkin.py` | 主脚本（单文件，青龙上传这一个即可） |
| `sxsy45_cookie.json` | 登录 Cookie 缓存（运行时自动生成，含敏感凭证，已被 `.gitignore` 忽略，**不要分享**） |
| `tests/` | 离线单元测试（真机抓包样本断言） |
