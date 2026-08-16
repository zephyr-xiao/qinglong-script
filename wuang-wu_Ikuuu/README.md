# iKuuu 自动签到（Node.js）

青龙面板自动签到脚本，针对 ikuuu 机场（`ikuuu.win` 等同系列域名）。Node.js 实现，复用目录内 `sendNotify.js` 推送全通道。

## 脚本文件

| 文件 | 说明 |
|---|---|
| `ikuuu.js` | 签到主脚本 |
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
| `HOST` | | 强制锁定签到域名（锁定时不轮换），留空则自动从发布页抓取候选列表 + 兜底 `ikuuu.win` |
| `IKUUU_PROXY` | | HTTP 代理（ikuuu 被墙必备），如 `http://172.17.0.1:7890` |
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

- **动态域名**：自动从发布页 `ikuuu.eu` 抓取当前可用主域名（正则收紧，排除发布页自身 `ikuuu.eu`），失败兜底 `ikuuu.win`；可用 `HOST` 强制锁定。
- **多账号串行**：账号间随机 3–8 秒间隔，降低风控。
- **状态分档**（非一句话糊掉）：
  | 状态 | 含义 |
  |---|---|
  | ✅ `success` | 签到成功 |
  | ✅ `already` | 已签到（幂等视为成功）|
  | ❌ `cookie_dead` | Cookie 失效（响应为登录页）|
  | ❌ `domain_block` | 域名墙/重定向到非业务页 |
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
| `[域名加载] ❌ 获取动态域名异常` + DEBUG 显示网络超时 | allorigins 公共代理被限流/挂掉，临时设 `HOST=ikuuu.win` 锁定域名 |
| 账号结果 `❌ Cookie失效` | Cookie 过期，浏览器重新登录后复制新 cookie 到 `ACCOUNTS` |
| 账号结果 `❌ 域名异常`（HTML 但无 login 字样） | 当前域名被墙或重定向，尝试 `HOST=其它ikuuu域名` 或换节点 |
| 账号结果 `⚠️ 响应异常` | 接口返回结构变化，看 DEBUG 原文判断是否需要更新判定规则 |

加 `IKUUU_DEBUG=1` 后日志会带 `❌ (Cookie失效，或当前获取的域名已被墙导致重定向)` 这类老文案对照，故新版拆分状态后再按上表定位。

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

## 退出码

- `0`：全部成功或部分失败（任务绿色）
- `1`：全部失败 / 主流程异常（任务红色，便于在青龙识别）