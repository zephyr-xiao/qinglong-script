# iKuuu 自动签到（Node.js）

青龙面板自动签到脚本，针对 ikuuu 机场（面板域名随发布页轮换，当前为 `ikuuu.top` / `ikuuu.pw`）。Node.js 实现，复用目录内 `sendNotify.js` 推送全通道。

> 改写自 [wuang-wu/Ikuuu](https://github.com/wuang-wu/Ikuuu)，按青龙面板规范适配。
> **2026-10 起支持账号密码自动登录**：Cookie 失效不再需要手工重新抓取，脚本自动开浏览器过验证码续期。

## 脚本文件

| 文件 | 说明 |
|---|---|
| `ikuuu.js` | 签到主脚本（含浏览器自动登录） |
| `ikuuu.test.js` | 单元测试，`node --test ikuuu.test.js` |
| `sendNotify.js` | 青龙官方 Notify（推送通道），勿删 |
| `.token/` | 登录会话 Cookie 缓存（运行时生成，按邮箱隔离，**含敏感凭证勿提交勿外传**） |

## 青龙任务命令

```text
task wuang-wu_Ikuuu/ikuuu.js
```

> 本脚本为 Node.js（`.js`），其余子目录脚本为 Python。命令路径需带子目录前缀。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `ACCOUNTS` | 二选一 | 账密账号列表，格式 `邮箱#密码`，多账号用 `&` 或换行分隔（推荐，失效自动重登） |
| `IKUUU_COOKIE` | 二选一 | cookie 字符串直填（旧行为），多账号用换行分隔；失效需手工更新 |
| `HOST` | | 强制锁定签到域名（锁定时不轮换），留空则从发布页自动抓取 + 兜底 `ikuuu.top` / `ikuuu.pw` |
| `IKUUU_PUBLISH_URL` | | 发布页地址，默认 `https://ikuuu.win/`。发布页整体迁移域名时改这里，不必改代码 |
| `IKUUU_PROXY` | | HTTP 代理（ikuuu 被墙，建议配置），如 `http://172.17.0.1:7890`；留空时自动回退青龙全局代理。HTTP 签到与浏览器登录**都会走该代理** |
| `IKUUU_HEADFUL` | | `1` = 登录用有头浏览器（仅本地调试，容器内勿开） |
| `IKUUU_CHROMIUM_PATH` | | 系统 Chromium 路径，如 `/usr/bin/chromium`（青龙容器装系统包后推荐此项） |
| `IKUUU_NOTIFY_ONLY_FAIL` | | `1` = 仅失败时推送，留空/`0` = 全部推送 |
| `IKUUU_DEBUG` | | `1` = 详细调试信息 + 登录过程截屏到 `_ikuuu_debug/` |

### ACCOUNTS 格式（账密模式，推荐）

```text
# 单账号
you@example.com#password123

# 多账号：& 或换行分隔
you@example.com#password123&other@163.com#pw456
```

- 按首个 `#` 切分，密码里可以包含 `#`；密码请避免包含 `&` 和换行（会被当作账号分隔符）
- Cookie 失效时自动开浏览器重新登录续期，**无需手工抓 cookie**

### IKUUU_COOKIE 格式（cookie 直填模式）

```text
# 单账号
uid=xxx; email=yyy; key=zzz; ip=aaa; expire_in=1234567890

# 多账号：换行分隔
uid=xxx; email=yyy; key=zzz
uid=aaa; email=bbb; key=ccc
```

- 每行必须包含 `uid=`，否则解析报错（缺 uid 的 cookie 签到必然 302）
- 此模式 Cookie 失效后**只能手工更新**（推送会提醒）；无需 playwright 依赖

两个变量可同时配置（cookie 账号排在前面先签到）。

## 登录原理（为什么需要浏览器）

ikuuu 的登录是**分阶段流程**（`POST /auth/login`，`phase=password`），且**强制 Geetest V4 验证码**（adaptive 模式：正常风控下是"点击按钮直过"，不弹拼图）。纯 HTTP 无法复现，因此：

1. 平时签到走**纯 HTTP**（快、省资源），会话 Cookie 缓存在 `.token/` 下按邮箱隔离；
2. ikuuu 会话有效期 **24 小时**（cookie 里 `expire_in` 字段），缓存余量不足 30 分钟或签到被 302 弹回登录页时，自动开 Playwright 浏览器：填表 → 点验证码按钮 → 提交 → 抓取会话 Cookie → 回写缓存 → 重试签到（仅重试一次，防死循环）；
3. 缓存文件只存 Cookie 与到期时间，**不存密码**；密码只活在青龙环境变量里。

## 依赖安装（青龙容器）

Node 脚本依赖：`undici`（`package.json` 已含，需在容器内 `npm i`）+ `playwright`（仅自动登录用）：

```bash
docker exec -it qinglong bash
cd /ql/data/scripts/wuang-wu_Ikuuu
npm i
npx playwright install chromium        # 或用系统 chromium（下一条）
apt update && apt install -y chromium  # 可选：系统包方式
```

用系统 chromium 时设置环境变量 `IKUUU_CHROMIUM_PATH=/usr/bin/chromium`。浏览器未就绪时签到照常可跑（cookie 未失效期间无需浏览器），登录环节会报引导性错误。

## 推送通道

复用同目录 `sendNotify.js`，在青龙「环境变量」里配置以下任意通道即可（与青龙官方 Notify 一致）：

- `DD_BOT_TOKEN` / `DD_BOT_SECRET`（钉钉）
- `BARK_PUSH`（Bark）
- `PUSH_KEY` / `TG_BOT_TOKEN` / `TG_USER_ID`（Server 酱 / TG）
- `QYWX_AM` / `QYWX_KEY`（企业微信）
- ……

具体支持通道见 `sendNotify.js` 顶部注释。

## 功能特性

- **账号密码自动登录**：Geetest V4 adaptive 验证码自动点击，Cookie 失效自愈。
- **Cookie 缓存复用**：24h 会话内不重复登录；到期时间直接取服务端 `expire_in`。
- **动态域名**：自动从发布页 `https://ikuuu.win/`（2026-09 起由 `ikuuu.eu` 迁到这里，可用 `IKUUU_PUBLISH_URL` 覆盖）抓取当前可用主域名。抓取多通道：配了代理则优先代理直连发布页，否则公共 CORS 代理逐个试（allorigins → cors.lol → whateverorigin）。
  - **发布页域名永不作为签到目标**：`ikuuu.win` / `ikuuu.eu` 是 nginx 静态发布页，`POST /user/checkin` 只会返回 405，进候选必定失败，脚本会显式过滤掉。
  - **域名混淆还原**：发布页用 `javascript-obfuscator` 把域名拆成多段字符串拼接（`'ikuuu'+'.top'`），脚本会先把相邻字符串字面量折叠再匹配。
  - **兜底列表**：`ikuuu.top` → `ikuuu.pw`。抓取失败时靠它保证可用；也可用 `HOST` 强制锁定。
- **多账号串行**：账号间随机 3–8 秒间隔，降低风控。
- **状态分档**：
  | 状态 | 含义 |
  |---|---|
  | ✅ `success` | 签到成功 |
  | ✅ `already` | 已签到（幂等视为成功）|
  | ❌ `cookie_dead` | Cookie 失效（302 跳登录页 / 401）；账密模式自动重登后重试，cookie 模式推送提醒手工更新 |
  | ❌ `login_fail` | 自动登录失败（密码错 / 2FA / 邮箱验证码 / 风控弹拼图）|
  | ❌ `domain_block` | 域名墙、重定向到非业务页、405（该域名不是面板）|
  | ⚠️ `parse_err` | 响应解析不了 |
  | ❌ `network_err` | 网络异常（自动重试 2 次）|
- **标题分档推送**：`✅ 全部成功` / `⚠️ 部分失败` / `❌ 全部失败`；`IKUUU_NOTIFY_ONLY_FAIL=1` 时全部成功不推送。
- **账号脱敏**：日志与推送中邮箱/账号名自动打码（`29********@qq.com`）。

## 排错指引

| 日志现象 | 排查方向 |
|---|---|
| `未安装 playwright（自动登录需要）` | 按上面「依赖安装」在容器内 `npm i && npx playwright install chromium`，或配 `IKUUU_CHROMIUM_PATH` |
| `Geetest 验证码连续 2 轮未通过（风控弹了拼图）` | 风控临时升级。稍后重试；持续出现时先用本机浏览器登录一次该站，或检查代理出口 IP 是否被标记 |
| `密码阶段被拒: 邮箱或密码错误` | 核对 `ACCOUNTS` 里的 email/password；注意密码里含特殊字符时推荐用 JSON 格式 |
| `账号开启了两步验证(2FA)` / `要求邮箱验证码登录` / `反向邮件验证` | 站点要求额外交互，脚本无法代填，需人工处理（关闭 2FA 或调整账号安全设置） |
| `[域名加载] ⚠️ 发布页抓取失败（各通道均不可用）` | 发布页被墙或 DNS 污染。兜底 `ikuuu.top` / `ikuuu.pw` 仍可用；建议配 `IKUUU_PROXY` 或 `HOST` 锁定 |
| `❌ 域名异常：HTTP 405 该域名不是签到面板` | 当前域名是发布页。检查 `IKUUU_PUBLISH_URL` 是否被误设成面板域名 |
| `⚠️ 响应异常` | 接口返回结构变化，开 `IKUUU_DEBUG=1` 看原文 |

`IKUUU_DEBUG=1` 时登录过程截屏存到脚本目录 `_ikuuu_debug/`（该目录已被 .gitignore 忽略）。

## 本地调试

```bash
export ACCOUNTS='you@example.com#password123'   # 或 IKUUU_COOKIE='uid=xxx; ...'
export IKUUU_DEBUG=1
node ikuuu.js
```

Windows PowerShell：

```powershell
$env:ACCOUNTS='you@example.com#password123'   # 或 $env:IKUUU_COOKIE='uid=xxx; ...'
$env:IKUUU_DEBUG='1'
node ikuuu.js
```

调试登录弹窗过程可加 `$env:IKUUU_HEADFUL='1'`（有头浏览器，仅本机）。

## 单元测试

```bash
node --test ikuuu.test.js
```

覆盖：发布页域名提取（字符串拼接还原、发布页域名过滤）、签到响应分类、登录响应 phase 分类、账号解析（邮箱#密码 单/多账号与非法输入）、Cookie 缓存读写与余量判定、Cookie 串拼装过滤、邮箱脱敏。

## 接口备忘（2026-10 实测）

| 项目 | 值 |
|---|---|
| 发布页 | `https://ikuuu.win/`（nginx 静态站，标题「iKuuuVPN最新域名」，`POST /user/checkin` → 405） |
| 面板域名 | `ikuuu.top`（主要）、`ikuuu.pw`（备用 1） |
| 签到接口 | `POST https://{面板域名}/user/checkin`（带会话 Cookie） |
| 签到成功 | `{"msg":"你获得了 475 MB流量","ret":1}` |
| 重复签到 | `{"ret":0,"msg":"您似乎已经签到过了..."}` ← 注意是「已**经**签到」 |
| 未登录表现 | `302 → /auth/login`（分类为 `cookie_dead`） |
| 登录接口 | `POST /auth/login`，**分阶段**：请求带 `phase=password` + `host` + `email` + `passwd` + `remember_me` + `pageLoadedAt`（页面加载毫秒时间戳，防重放）；响应 `phase=authenticated`（成功）/ `password`（密码错）/ `totp` / `email_code` / `reverse_email_verify` |
| 登录验证码 | **Geetest V4**（captchaId `cc96d05ba8b60f9112f76e18526fcb73`，captcha_type=ai / adaptive 模式）。服务端强制校验 `captcha_result`，纯 HTTP 无验证码提交返回 `captcha_failed`。低风控时点 `.geetest_btn_click` 按钮即可直过（约 1 秒），无需拼图 |
| 会话有效期 | **24 小时**（`expire_in` = 登录时刻 + 86400 秒，`remember_me` 不延长） |
| Cookie 组成 | `uid` / `email`(URL编码) / `key`(40位hex) / `ip`(32位hex) / `expire_in` / `session_version`；`PHPSESSID` 可有可无，签到接口均可用 |
| 登录页形态 | HTML 为 base64 混淆（前端 `SlowerDecodeBase64` 解出真实 DOM），登录逻辑在内联第 8 个 `<script>` 里 |

## 退出码

- `0`：全部成功或部分失败（任务绿色）
- `1`：全部失败 / 主流程异常（任务红色，便于在青龙识别）
