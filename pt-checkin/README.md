# PT 站点自动签到（Node.js）

NovaHD（`pt.novahd.top`）/ HDArea（`hdarea.club`）/ BTSchool（`pt.btschool.club`）/ CrabPT（`crabpt.vip`）四站每日签到。Node.js 实现，复用目录内 `sendNotify.js` 推送全通道。未配置的站点自动跳过，互不影响。

## 脚本文件

| 文件 | 说明 |
|---|---|
| `pt_checkin.js` | 签到主脚本 |
| `sendNotify.js` | 青龙官方 Notify（推送通道），勿删 |
| `ddddocr_ocr.py` | ddddocr 降级识别辅助（颜色过滤 + 连通域去噪 + 双模型投票，实测 80%） |
| `novahd_cookie.json` / `btschool_cookie.json` / `crabpt_cookie.json` | 自动登录成功后的 Cookie 缓存（运行时生成，已 gitignore） |
| `pt_checkin.lock` | 运行锁（防并发 / 重复消耗登录配额，运行时生成，已 gitignore） |
| `tests/pt_checkin_test.js` | 单元测试（`npm test`，真实页面片段做 fixture） |

## 青龙任务命令

```text
task pt-checkin/pt_checkin.js
```

依赖安装（首次部署或更新后执行一次）：

```bash
cd /ql/scripts/pt-checkin && npm install --production
```

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `PT_SITE_NOVAHD_CK` | 站点二选一 | NovaHD Cookie 种子；自动登录成功后缓存到 `novahd_cookie.json` 并优先使用 |
| `PT_NOVAHD_ACCOUNTS` | 站点二选一 | NovaHD 账密，格式 `用户名#密码`，Cookie 失效时自动登录兜底（旧写法 `PT_NOVAHD_USERNAME` + `PT_NOVAHD_PASSWORD` 仍兼容） |
| `PT_SITE_HDAREA_CK` | 站点二选一 | HDArea Cookie；**该站不支持自动登录**，失效需重新导出 |
| `PT_SITE_BTSCHOOL_CK` | 站点二选一 | BTSchool Cookie 种子；自动登录成功后缓存到 `btschool_cookie.json` 并优先使用 |
| `PT_BTSCHOOL_ACCOUNTS` | 站点二选一 | BTSchool 账密，格式 `用户名#密码`，Cookie 失效时自动登录兜底（旧写法 `PT_BTSCHOOL_USERNAME` + `PT_BTSCHOOL_PASSWORD` 仍兼容） |
| `PT_SITE_CRABPT_CK` | 站点二选一 | CrabPT Cookie 种子；自动登录成功后缓存到 `crabpt_cookie.json` 并优先使用 |
| `PT_CRABPT_ACCOUNTS` | 站点二选一 | CrabPT 账密，格式 `用户名#密码`，Cookie 失效时自动登录兜底（旧写法 `PT_CRABPT_USERNAME` + `PT_CRABPT_PASSWORD` 仍兼容） |
| `PT_OCR_API_URL` | | OpenAI 兼容视觉接口地址（如 `https://xx/v1/chat/completions`），多站共用 |
| `PT_OCR_API_KEY` | | 对应 API Key |
| `PT_OCR_MODEL` | | 视觉模型名，默认 `gpt-4o-mini` |
| `PT_PYTHON` | | ddddocr 降级用的 Python 解释器，默认 `python` |
| `PT_PROXY` | | HTTP 代理，如 `http://172.17.0.1:7890`；留空时自动回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` / `GLOBAL_AGENT_*`） |
| `PT_NOTIFY_ONLY_FAIL` | | `1` = 仅失败时推送 |
| `PT_DEBUG` | | `1` = 打印详细调试信息（含页面片段，注意勿泄露） |
| `PT_TIMEOUT` | | HTTP 超时秒数，默认 20（OCR 识别固定 60s） |
| `PT_RUN_COOLDOWN` | | 运行冷却窗口分钟数，默认 10；距上次运行结束不足该时长则跳过（防青龙自动重试/并发叠加消耗登录配额），`0` 关闭 |

每个站点「Cookie」与「账密」二选一即可：只配 Cookie 则 Cookie 失效时推送提醒；配了账密 + OCR 则自动登录兜底。

## 各站签到机制

| 站点 | 签到端点 | 认证兜底 |
|---|---|---|
| NovaHD | `GET/POST attendance.php`（未签页为验证码表单 `imagehash`+`imagestring`，无 action 字段；已签页显示统计文案） | challenge-response 挑战认证 + 验证码 OCR（登录/签到共用） |
| HDArea | 首页判状态 + `POST sign_in.php` | 无（仅 Cookie） |
| BTSchool | `GET index.php?action=addbonus`（非标准 NexusPHP，无 formhash） | 标准 NexusPHP 表单 + 验证码 OCR |
| CrabPT | 先回首页判「签到已得」标记 → 未签再 `GET attendance.php` 直接触发签到（无表单无验证码，奖励为蟹币值） | 标准 NexusPHP 表单 + 验证码 OCR |

> CrabPT 注意：`attendance.php` 是 **GET 即签到**，已签日重复 GET 幂等（显示当日结果不重复计数）；脚本仍先查首页避免多余请求。

### NovaHD 登录算法

该站为魔改 challenge-response 登录（密码不明文传输）：

```
POST /api/challenge {"username"} → {secret, challenge}
response = HMAC-SHA256(key=challenge, msg=SHA256(secret + SHA256(password)))   // hex 小写
POST /takelogin.php  (secret + response + username + password + two_step_code + imagestring + imagehash)
```

### 验证码识别两级链路

```
视觉模型（PT_OCR_API_URL + KEY，识别率 ~100%）
  ↓ 未配置 / 超时 / 报错时自动降级
本地 ddddocr（ddddocr_ocr.py，需 Python + ddddocr + opencv-python + numpy）
  管线：HSV 颜色过滤留黑字符 → 连通域去散点 → 3x 放大 → 标准模型
        + beta 模型原图识别投票：双模型一致直接用；分歧取预处理结果（80% > beta 63%），
        预处理结果长度≠6（漏字符）才取 beta
  实测：30 张两站样本 80.0%（基线原图仅 56.7%）
```

> ddddocr 依赖：`pip install ddddocr opencv-python numpy`（NAS 容器司机社脚本环境已具备）。
> 识别失败会消耗 1 次站点登录尝试——受「限次保护」约束自动止损。

## 登录限次保护（防封 IP）

三个站的登录都有**连续失败封 IP** 限制（NovaHD 10 次、BTSchool 20 次、CrabPT 10 次），脚本做了三层保护：

1. 每轮登录前解析登录页「你还有 [N] 次尝试机会」，剩余次数低于安全阈值（NovaHD 5 / BTSchool 10 / CrabPT 5）立即放弃并推送警告
2. 单次运行登录轮数上限：NovaHD 2 轮 / BTSchool 5 轮 / CrabPT 3 轮
3. 验证码识别失败自动换新验证码重试（每次失败消耗 1 次站点配额，受上两条约束）
4. **解析不到「剩余尝试次数」时保守处理**：本轮只尝试 1 次，失败即停，不盲目重试

> 另有**运行锁**兜底（见下节），即使青龙开启失败自动重试或手动并发触发，也不会叠加消耗配额。

## 运行锁（防并发 / 重复消耗配额）

脚本在目录内维护 `pt_checkin.lock`（运行期自动生成，已 gitignore）：

- **并发保护**：检测到上一次运行仍在进行（进程未结束）→ 本次直接跳过（退出码 0，非失败）
- **冷却窗口**：距上次运行结束不足 `PT_RUN_COOLDOWN` 分钟（默认 10）→ 跳过，防止青龙"失败自动重试"反复拉起
- **死锁自愈**：进程崩溃遗留的锁（30 分钟未标记结束）自动失效，不会永久卡住
- 确需立即重跑：删除 `pt_checkin.lock`，或设 `PT_RUN_COOLDOWN=0`

## Cookie 生命周期

```
环境变量种子 Cookie（首次使用）
    ↓ Cookie 失效
自动登录 → 新 Cookie 写入 <站点>_cookie.json（含时间戳与「来源指纹」）
    ↓ 之后每次运行
缓存优先于环境变量；种子 Cookie 或账号变化时指纹不匹配 → 自动废弃缓存、改用新配置
```

> 缓存写入改为**登录后回读校验通过才落盘**：登录判定误命中时不会用坏 Cookie 覆盖原本好用的缓存。
> 换号或重新导出 Cookie 后无需再手动删除缓存文件（来源指纹会自动失效旧缓存）。
> 账号配置支持多账号写法，但**本脚本只使用第一个账号**，检测到多个时会打印告警。

## 推送通道

复用同目录 `sendNotify.js`，在青龙「环境变量」里配置任意通道即可（`DD_BOT_TOKEN`、`BARK_PUSH`、`PUSH_KEY`、`QYWX_AM`、`TG_BOT_TOKEN` 等），详见 `sendNotify.js` 顶部注释。

## 功能特性

- **状态分档**：
  | 状态 | 含义 |
  |---|---|
  | ✅ `success` | 签到成功（含奖励/连续天数明细） |
  | ✅ `already` | 今日已签到（幂等视为成功） |
  | ⏭️ `skipped` | 未配置该站，跳过（不计入成败） |
  | ❌ `cookie_dead` | Cookie 失效（含自动登录失败） |
  | 🟡 `parse_err` | 结果未知（签到已发出但无法确认；黄档，**不计失败、不染红**） |
  | ❌ `network_err` | 网络异常（自动重试 2 次） |
- **成功判定为「正向证据制」**：必须有明确成功/已签文案才算成功；302、含 uid Cookie、首页无入口等间接证据只作辅助，不足以确认时降为 `parse_err`（宁黄不假绿）。「签到入口消失」这类间接但可靠的信号算已签，但报 `already`，不冒领 `success`。
- **标题分档推送**：`✅ 全部成功` / `⚠️ 部分失败` / `🟡 有结果未知` / `❌ 全部失败` / `⏭️ 未配置任何站点`。
- **站点间随机间隔** 3–8 秒，降低风控。
- **DEBUG**：`PT_DEBUG=1` 打印每步 HTTP 状态、重定向终点、OCR 识别原文、登录失败详情。

## 排错指引

| 日志现象 | 排查方向 |
|---|---|
| `Cookie 已失效且未配置账密` | 浏览器重新登录导出 Cookie 填入对应 `PT_SITE_*_CK` |
| `剩余尝试机会仅 N 次（低于安全阈值），放弃登录` | 站点侧失败计数已偏高（此前手动登录失败过多），等待站点计数重置（通常 24h）或换 IP |
| `登录页未解析到 imagehash` | 站点登录页结构变化，需更新解析正则 |
| `OCR 接口 HTTP xxx` | 检查 `PT_OCR_API_URL`/`PT_OCR_API_KEY`/`PT_OCR_MODEL` 是否正确、模型是否支持图片输入 |
| `自动登录后仍判定未登录` | 账号可能被禁用或需要两步验证（`two_step_code`，当前脚本未支持） |
| `签到请求已发出但未能确认结果` | 开 `PT_DEBUG=1` 看页面原文，多为站点改版导致解析规则失效 |
| `距上次运行结束仅 N 分钟…跳过本次` | 冷却窗口命中（默认 10 分钟）。确需立即重跑：删除 `pt_checkin.lock`，或设 `PT_RUN_COOLDOWN=0` |
| `检测到上一次运行仍在进行…跳过本次` | 上一实例未正常结束；确认无残留进程后删除 `pt_checkin.lock`（崩溃死锁 30 分钟后自动失效） |
| `未解析到「剩余尝试次数」，保守起见本轮仅尝试 1 次` | 站点登录页改版导致限次文案解析失败；本轮只试一次，频繁出现需更新 `parseLoginPage` 正则 |
| `首页无签到入口且未识别到正常首页标志` | BTSchool 首页疑似加载异常/维护页，脚本不冒报已签；先确认站点状态 |
| `检测到 N 个账号…仅使用第一个` | 账号变量里配了多个账号，本脚本不支持多账号，只保留第一个 |

## 本地调试

```bash
export PT_SITE_BTSCHOOL_CK='xxx'
export PT_DEBUG=1
node pt_checkin.js
```

Windows PowerShell：

```powershell
$env:PT_SITE_BTSCHOOL_CK='xxx'
$env:PT_DEBUG='1'
node pt_checkin.js
```

## 退出码

- `0`：有任一站点成功（含部分失败 / 结果未知），或运行锁命中跳过
- `1`：**全站皆挂**（无任何站点成功且存在 `cookie_dead` / `network_err`）、未配置任何站点、主流程异常，
  或「存在 `cookie_dead` / `network_err` 但推送失败」（避免告警静默丢失）
- 说明：`parse_err`（结果未知）为黄档，**不计失败、不染红**（宁黄不假绿）；`already`（已签到）计入成功。
