# 福利吧每日签到（fx_checkin）- 青龙面板脚本

自动完成福利吧论坛(wnflb2023.com)「每日签到」插件(`fx_checkin`)的签到。

脚本对应该插件列表页：<https://www.wnflb2023.com/plugin.php?id=fx_checkin:list&type=0>

## 特性

- 两种认证方式：**账号密码** 或 **Cookie 直签**
- 账号密码模式带 Cookie 缓存：首次登录后缓存凭证，之后优先复用，缓存失效才重新登录
- 登录遇到图片验证码时自动用 **ddddocr** 本地识别（识别错会重新取图重试）
- 已签到自动跳过，可安全重复执行（幂等）
- 签到后输出今日排名 / 连续天数 / 累计天数
- 成功判定采用**正向证据制**：签到后重新拉取列表页，确认页面确实变为「已签到」才算成功，不单凭关键词冒报
- 多账号支持（换行分隔）
- Cookie 失效自动检测，推送告警提醒重新抓取
- 网络抖动自动重试；`formhash` 过期自动重取重试一次
- **业务失败自动重试**：签到接口返回异常文案或回查仍未签到，按可配间隔重新拉页面再签，默认重试 2 次
- 登录提交**不做网络重试**（该站失败次数按 IP 计数，超时/断连时服务端可能已计入失败）
- 运行锁防止 cron 重叠执行
- 复用青龙 `notify.py` 推送
- 默认校验 TLS 证书（证书异常自动降级不校验并提示一次）
- 代理只认 `WNFX_PROXY` 与青龙全局代理 env，**Windows 注册表系统代理不会被静默采用**
- 只对网络类异常自动重试；认证失败绝不重试

## 认证方式怎么选

| 方式 | 环境变量 | 格式 | 优点 | 注意 |
|---|---|---|---|---|
| 账号密码 | `WNFX_ACCOUNTS` | `用户名#密码` | 不会因 Cookie 过期而失效 | 该站登录失败按 IP 计数，连续失败 5 次要验证码 |
| Cookie 直签 | `WNFX_COOKIE` | 浏览器完整 Cookie | 简单直接，不触发登录 | Cookie 过期需手动更新 |

两种方式可以同时配置，会各自独立跑一遍。

> ⚠️ **该站登录失败次数是按 IP 统计的**（实测：换新会话次数仍会递减，5 次后要求验证码）。
> 所以账号密码模式下脚本**认证失败绝不自动重试**（只有网络错误才重试），且缓存命中时
> 完全不走登录流程。请务必确认密码正确后再配置。

## 接口流程（2026-10-19 实测）

### 签到

| 步骤 | 请求 | 说明 |
|---|---|---|
| 1 | `GET plugin.php?id=fx_checkin:list&type=0` | 取 `formhash`，并读取当前签到状态 |
| 2 | `GET plugin.php?id=fx_checkin:checkin&formhash=<hash>&<hash>&inajax=1` | 签到入口（页面 JS 里 `showWindow` 打开的就是这个地址） |
| 3 | `GET plugin.php?id=fx_checkin:list&type=0` | 签到后回查页面，确认已变为「已签到」 |

页面状态判据（实测）：

| 状态 | 页面特征 |
|---|---|
| 游客 / Cookie 失效 | `discuz_uid = '0'` |
| 今日已签到 | JS 变量 `fx_chk_menu=true`；顶栏图片 `alt="已签到"`；出现「您今日第 N 个签到,已连续签到 X 天,累计签到 Y 天!」 |
| 今日未签到 | `fx_chk_menu=false`，顶栏图片 `alt="签到"`，无「您今日第 N 个签到」 |

签到响应文案（实测）：

| 文案 | 含义 | 脚本处理 |
|---|---|---|
| `您今日已签到，无需重复签到！` | 今日已签 | 🔁 幂等成功 |
| 含「签到成功」 | 签到成功 | ✅ 成功（仍会回查页面确认） |
| 含「尚未登录」 | 登录态失效 | ❌ Cookie 失效 |
| 含「表单验证串不符」 | `formhash` 过期 | 重取页面重试一次 |

> 注意：站点全局模板里也有「签到成功!」字样（游客页会输出 `{todayrank}` 占位符），
> 因此脚本**只解析签到响应的提示块**，并用签到后的页面状态做最终确认，不会误判。

### 登录（仅账号密码模式）

正常情况一步到位：

| 步骤 | 请求 | 说明 |
|---|---|---|
| 1 | `GET member.php?mod=logging&action=login&...&inajax=1` | 取 `formhash` + `loginhash` |
| 2 | `POST member.php?mod=logging&action=login&loginsubmit=yes&...&loginhash=<x>&inajax=1` | body `formhash` / `referer` / `username` / `password` / `questionid=0` / `answer=` / `cookietime=2592000` |

登录成功判定：响应含「欢迎您回来」或会话拿到 `S5r8_2132_auth` Cookie。

**同一 IP 连续失败 5 次后**，第 2 步返回「请输入验证码后继续登录」并给出 `auth` 令牌，进入两步验证：

| 步骤 | 请求 | 说明 |
|---|---|---|
| 3 | `GET member.php?mod=logging&action=login&auth=<令牌>&...` | 取该页 `formhash`、`auth`、`loginhash`、`idhash` |
| 4 | `GET misc.php?mod=seccode&update=<时间戳>&idhash=<idhash>` | 取验证码 PNG |
| 5 | `POST` 同第 2 步，额外带 `auth` / `seccodehash=<idhash>` / `seccodemodid=member::logging` / `seccodeverify=<OCR 结果>` | 识别错会重新取图，最多 3 次 |

> ⚠️ **坑（脚本已处理）**：进入「待验证码」状态时，服务端**也会下发 `S5r8_2132_auth` Cookie**。
> 如果只用「拿到 auth Cookie」判登录成功，会把「待验证码」误判成「登录成功」。
> 脚本把「需要验证码」的判定放在最前面。

## 快速开始

### 1. 青龙面板安装依赖

在青龙「依赖管理」-「Python」中安装：

- `requests`（必需）
- `ddddocr`（可选；仅在站点要求登录验证码时需要）

### 2. 配置环境变量

青龙面板 →「环境变量」新增：

| 变量名 | 值示例 | 必填 |
|---|---|---|
| `WNFX_ACCOUNTS` | `example_user#example_password` | 二选一 |
| `WNFX_COOKIE` | `S5r8_2132_saltkey=xxx; S5r8_2132_auth=yyy` | 二选一 |
| `WNFX_HOST` | `www.wnflb2023.com` | 可选，默认官网域名 |
| `WNFX_NOTIFY` | `true` | 可选，默认 true |
| `WNFX_NOTIFY_ONLY_FAIL` | `false` | 可选，默认 false |
| `WNFX_PROXY` | `http://proxy.example.com:7890` | 可选 |
| `WNFX_TIMEOUT` | `25` | 可选 |
| `WNFX_SIGN_RETRY` | `2` | 可选，业务失败重试次数，0 表示不重试 |
| `WNFX_SIGN_RETRY_DELAY` | `5` | 可选，每次重试前等待秒数 |
| `WNFX_DEBUG` | `false` | 可选，排错时开 |

> 多账号用**换行**分隔。账号密码用第一个 `#` 分隔，密码里可以再出现 `#`；
> Cookie 模式允许只填一个键值对（如仅 `S5r8_2132_auth=xxx`）。

### 3. 创建任务

```bash
task wnflb_fx_checkin/wnflb_fx_checkin.py
```

脚本头部的 `cron: 20 8 * * *` 会在 `ql repo` 拉库时自动建任务；手动添加任务时把定时规则填成 `20 8 * * *` 即可。

### 4. 本地验证

```bash
cd qinglong-script/wnflb_fx_checkin
WNFX_ACCOUNTS='example_user#example_password' WNFX_NOTIFY=false python wnflb_fx_checkin.py
```

预期输出（今日已签到时）：

```
🔁 今日已签到
📊 今日第 1008 名 │ 连续 1 天 │ 累计 1 天
```

### 5. 单元测试

```bash
cd qinglong-script/wnflb_fx_checkin
python -m unittest discover -s tests -v
```

当前共 42 项用例，覆盖页面状态判据、签到状态机（含幂等/未知文案/回查失败/业务重试）、登录响应分类、验证码流程、Cookie 缓存、运行锁与重试边界。

## 重试策略

| 场景 | 行为 |
|---|---|
| 网络超时 / 连接失败 / 连接中断 | 单次请求最多尝试 3 次，间隔 5s、10s |
| `formhash` 过期（表单验证串不符） | 重新拉页面取新 formhash，再试 1 次 |
| 业务失败（返回「系统繁忙」等未知文案，或回查页面仍未签到） | 按 `WNFX_SIGN_RETRY` 重试，默认 2 次，每次间隔 `WNFX_SIGN_RETRY_DELAY`（默认 5s，带 ±20% 抖动）；每次重试前先刷新页面，若发现已签到则按成功处理，不再重复提交 |
| 签到后回查页网络失败（结果无法确认） | 视为可重试业务失败；重试前刷新页面确认，已签到则直接按成功返回 |
| Cookie 失效 / 登录失败 | **不重试**（登录失败按 IP 计数，重试只会扩大损失） |

> 签到接口本身是幂等的：即使上一次实际已签到成功，重试也只会返回「您今日已签到，无需重复签到！」，不会重复签到。

## 文件说明

| 文件 | 说明 |
|---|---|
| `wnflb_fx_checkin.py` | 主脚本 |
| `tests/test_wnflb_fx_checkin.py` | 纯函数与签到状态机单元测试（不触网） |
| `wnflb_fx_cookie.json` | 运行期 Cookie 缓存（自动生成，已被 `.gitignore` 忽略，**不要提交**） |
| `wnflb_fx_checkin.lock` | 运行锁（运行期自动生成/删除，已被 `.gitignore` 忽略） |

## 排错

| 现象 | 原因与处理 |
|---|---|
| `站点要求输入登录验证码,但 未安装 ddddocr` | 安装 `ddddocr`，或改用 `WNFX_COOKIE` |
| `账号或密码错误(该 IP 剩余尝试 N 次)` | 核对密码；**不要反复试**，失败次数按 IP 累计 |
| `Cookie 已失效,请重新抓取` | 浏览器登录后 F12 复制完整 Cookie 更新 `WNFX_COOKIE` |
| `签到失败: 系统繁忙...` | 站点临时异常；脚本会重试网络错误，业务失败需看下一次定时任务 |
| `检测到运行锁` | 上一次任务未正常结束；锁超过 2 小时会自动视为过期 |
