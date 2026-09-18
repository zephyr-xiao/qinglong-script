# iKuuu 自动签到（Node.js）

青龙面板自动签到脚本，针对 ikuuu 机场（面板域名随发布页轮换，当前为 `ikuuu.top` / `ikuuu.pw`）。Node.js 实现，复用目录内 `sendNotify.js` 推送全通道。

## 脚本文件

| 文件 | 说明 |
|---|---|
| `ikuuu.js` | 签到主脚本 |
| `ikuuu.test.js` | 单元测试（发布页域名提取 / 响应分类 / 账号解析），`node --test ikuuu.test.js` |
| `sendNotify.js` | 青龙官方 Notify（推送通道），勿删 |

## 青龙任务命令

```text
task wuang-wu_Ikuuu/ikuuu.js
```

> 本脚本为 Node.js（`.js`），其余子目录脚本为 Python。命令路径需带子目录前缀。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `ACCOUNTS` | ✅ | 账号列表，支持三种格式（见下） |
| `HOST` | | 强制锁定签到域名（锁定时不轮换），留空则从发布页自动抓取 + 兜底 `ikuuu.top` / `ikuuu.pw` |
| `IKUUU_PUBLISH_URL` | | 发布页地址，默认 `https://ikuuu.win/`。发布页整体迁移域名时改这里，不必改代码 |
| `IKUUU_PROXY` | | HTTP 代理（ikuuu 被墙，建议配置），如 `http://172.17.0.1:7890`；留空时自动回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` / `GLOBAL_AGENT_*`）。有可用代理时发布页抓取**优先经代理直连**，最稳 |
| `IKUUU_NOTIFY_ONLY_FAIL` | | `1` = 仅失败时推送，留空/`0` = 全部推送 |
| `IKUUU_DEBUG` | | `1` = 打印详细调试信息（域名抓取/响应原文/重试间隔等） |

### ACCOUNTS 三种格式

```json
// 1) 多账号 JSON 数组（推荐）
[
  {"name":"主号","cookie":"uid=xxx; ip=yyy; expire_time=zzz"},
  {"name":"副号","cookie":"uid=aaa; ip=bbb"}
]
```

```json
// 2) 单账号 JSON 对象
{"name":"主号","cookie":"uid=xxx; ip=yyy"}
```

```text
// 3) 原始 cookie 字符串（自动当作"默认账号"）
uid=xxx; ip=yyy; expire_time=zzz
```

## 推送通道

复用同目录 `sendNotify.js`，在青龙「环境变量」里配置以下任意通道即可（与青龙官方 Notify 一致）：

- `DD_BOT_TOKEN` / `DD_BOT_SECRET`（钉钉）
- `BARK_PUSH`（Bark）
- `PUSH_KEY` / `TG_BOT_TOKEN` / `TG_USER_ID`（Server 酱 / TG）
- `QYWX_AM` / `QYWX_KEY`（企业微信）
- ……

具体支持通道见 `sendNotify.js` 顶部注释。

## 功能特性

- **动态域名**：自动从发布页 `https://ikuuu.win/`（2026-09 起由 `ikuuu.eu` 迁到这里，可用 `IKUUU_PUBLISH_URL` 覆盖）抓取当前可用主域名。抓取多通道：配了代理（`IKUUU_PROXY` 或青龙全局代理）则优先代理直连发布页，否则公共 CORS 代理逐个试（allorigins → cors.lol → whateverorigin）。
  - **发布页域名永不作为签到目标**：`ikuuu.win` / `ikuuu.eu` 是 nginx 静态发布页，`POST /user/checkin` 只会返回 405，进候选必定失败，脚本会显式过滤掉。
  - **域名混淆还原**：发布页用 `javascript-obfuscator` 把域名拆成多段字符串拼接（`'ikuuu'+'.top'`），脚本会先把相邻字符串字面量折叠再匹配，否则一条都抓不到。
  - **兜底列表**：`ikuuu.top` → `ikuuu.pw`（与发布页标注的「主要域名 / 备用域名 1」一致）。混淆只还原得出部分域名，抓取失败时靠它保证脚本仍可用；也可用 `HOST` 强制锁定。
- **多账号串行**：账号间随机 3–8 秒间隔，降低风控。
- **状态分档**（非一句话糊掉）：
  | 状态 | 含义 |
  |---|---|
  | ✅ `success` | 签到成功 |
  | ✅ `already` | 已签到（幂等视为成功）|
  | ❌ `cookie_dead` | Cookie 失效（302 跳登录页 / 401 / 登录页 HTML）|
  | ❌ `domain_block` | 域名墙、重定向到非业务页、405（该域名不是面板）|
  | ⚠️ `parse_err` | 响应解析不了 |
  | ❌ `network_err` | 网络异常（自动重试 2 次）|
- **标题分档推送**：`✅ 全部成功` / `⚠️ 部分失败` / `❌ 全部失败`。
- **失败才推送**：`IKUUU_NOTIFY_ONLY_FAIL=1` 时，全部成功不推送。
- **重试**：网络异常自动重试 2 次，间隔 2–4 秒随机。
- **DEBUG**：`IKUUU_DEBUG=1` 打印代理 URL、HTML 长度、响应原文前 120 字、重试间隔，便于区分代理失败 vs 路径 404 vs Cookie 失效。
- **账号脱敏**：日志与推送中账号名自动打码（`主***副`）。

## 排错指引

| 日志现象 | 排查方向 |
|---|---|
| `[域名加载] ⚠️ 发布页抓取失败（各通道均不可用）` | 发布页被墙或本地 DNS 被污染（`ikuuu.win` 常被解析到 `93.46.8.90` 这类黑洞 IP）。此时兜底 `ikuuu.top` / `ikuuu.pw` 仍可用；建议在青龙配全局代理或 `IKUUU_PROXY`（宿主机代理）让发布页走代理直连 |
| `[域名加载] ⚠️ 发布页解析成功但未匹配到域名` | 发布页换了新的域名混淆方式（当前是「字符串拼接」已被脚本还原）。用 `IKUUU_DEBUG=1` 看抓到的 HTML 长度与实际内容，必要时补 `HOST` 顶住，或更新 `extractHosts` |
| 账号结果 `❌ Cookie失效：Cookie 已失效（302 跳转登录页）` | Cookie 过期。iKuuu 的 cookie 内含 `expire_in`（Unix 秒级时间戳），例如 `1789545221` = 2026-09-16 15:53（北京时间），过期后一定被拒。浏览器重新登录后复制完整 cookie 到 `ACCOUNTS`（注意：iKuuu 登录页是混淆 SPA，旧版脚本会把此情形误判成"域名被墙"，新版已靠 302 Location 正确区分） |
| 账号结果 `❌ 域名异常：HTTP 405 该域名不是签到面板` | 当前域名是发布页（nginx 静态站，不接受 POST）。新代码已把发布页域名排除出候选，若仍出现，检查 `IKUUU_PUBLISH_URL` 是否被误设成面板域名 |
| 账号结果 `❌ 域名异常`（302 到非登录地址 / 403 / 404） | 当前域名被墙、被风控或路径变化，尝试 `HOST=其它ikuuu域名` 或换节点 |
| 账号结果 `⚠️ 响应异常` | 接口返回结构变化，看 DEBUG 原文判断是否需要更新判定规则 |

加 `IKUUU_DEBUG=1` 可看发布页各抓取通道成败、签到请求的 HTTP 状态与 Location 头，便于按上表定位。

## 本地调试

```bash
export ACCOUNTS='[{"name":"主号","cookie":"uid=xxx; ip=yyy"}]'
export IKUUU_DEBUG=1
node ikuuu.js
```

Windows PowerShell：

```powershell
$env:ACCOUNTS='[{"name":"主号","cookie":"uid=xxx"}]'
$env:IKUUU_DEBUG='1'
node ikuuu.js
```

## 单元测试

无需额外依赖，用 Node 内置 test runner：

```bash
node --test ikuuu.test.js
```

覆盖发布页域名提取（含字符串拼接还原、发布页域名过滤）、响应分类分档、账号解析三种格式、账号脱敏。

## 接口备忘（2026-09 实测）

| 项目 | 值 |
|---|---|
| 发布页 | `https://ikuuu.win/`（nginx 静态站，标题「iKuuuVPN最新域名」，`POST /user/checkin` → 405） |
| 发布页列出的面板 | `ikuuu.top`（主要）、`ikuuu.pw`（备用 1） |
| 发布页抓取 | 实测公共 CORS 通道 `cors.lol` 可抓到（`allorigins` 常超时），能还原出 `ikuuu.top` |
| 签到接口 | `POST https://{面板域名}/user/checkin` |
| 签到成功 | `{"msg":"你获得了 581 MB流量","ret":1}` |
| 重复签到 | `{"ret":0,"msg":"您似乎已经签到过了..."}` ← 注意是「已**经**签到」，正则必须认这个写法 |
| 未登录表现 | `302 → /auth/login`（即分类为 `cookie_dead`） |
| Cookie 有效期 | cookie 内 `expire_in` 字段为 Unix 秒级时间戳，过期后必然被拒 |

## 退出码

- `0`：全部成功或部分失败（任务绿色）
- `1`：全部失败 / 主流程异常（任务红色，便于在青龙识别）