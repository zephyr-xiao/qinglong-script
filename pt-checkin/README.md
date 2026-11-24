# PT 站点自动签到

NovaHD（pt.novahd.top）、HDArea（hdarea.club）、BTSchool（pt.btschool.club）、CrabPT（crabpt.vip）四站每日签到，Node.js 实现，复用同目录 `sendNotify.js` 推送全通道。未配置的站点自动跳过、互不影响；Cookie 失效时可用账密自动登录兜底（HDArea 不支持自动登录）。

## 青龙任务命令

```text
task pt-checkin/pt_checkin.js
```

退出码：`0` 有任一站点成功（含部分失败 / 结果未知），或运行锁命中跳过；`1` 为全站皆挂、未配置任何站点、主流程异常，或存在 `cookie_dead` / `network_err` 但推送失败。`parse_err`（结果未知）是黄档，不计失败。

## 环境变量

每个站点「Cookie」与「账密」二选一：只配 Cookie 则失效时推送提醒，配账密 + OCR 则自动登录兜底。

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `PT_SITE_NOVAHD_CK` | 站点二选一 | 空 | NovaHD Cookie 种子，自动登录成功后缓存并优先使用 |
| `PT_NOVAHD_ACCOUNTS` | 站点二选一 | 空 | NovaHD 账密 `用户名#密码`（旧写法 `PT_NOVAHD_USERNAME` + `PT_NOVAHD_PASSWORD` 仍兼容） |
| `PT_SITE_HDAREA_CK` | 站点二选一 | 空 | HDArea Cookie，该站不支持自动登录，失效需重新导出 |
| `PT_SITE_BTSCHOOL_CK` | 站点二选一 | 空 | BTSchool Cookie 种子，自动登录后缓存优先使用 |
| `PT_BTSCHOOL_ACCOUNTS` | 站点二选一 | 空 | BTSchool 账密 `用户名#密码`（旧写法 `PT_BTSCHOOL_USERNAME` + `PT_BTSCHOOL_PASSWORD` 兼容） |
| `PT_SITE_CRABPT_CK` | 站点二选一 | 空 | CrabPT Cookie 种子，自动登录后缓存优先使用 |
| `PT_CRABPT_ACCOUNTS` | 站点二选一 | 空 | CrabPT 账密 `用户名#密码`（旧写法 `PT_CRABPT_USERNAME` + `PT_CRABPT_PASSWORD` 兼容） |
| `PT_OCR_API_URL` | 否 | 空 | OpenAI 兼容视觉接口地址，如 `https://xx/v1/chat/completions`，多站共用 |
| `PT_OCR_API_KEY` | 否 | 空 | 对应 API Key |
| `PT_OCR_MODEL` | 否 | `gpt-4o-mini` | 视觉模型名 |
| `PT_PYTHON` | 否 | `python` | ddddocr 降级用的 Python 解释器 |
| `PT_PROXY` | 否 | 空 | HTTP 代理；留空时回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` / `GLOBAL_AGENT_*`） |
| `PT_NOTIFY_ONLY_FAIL` | 否 | 空 | `1` = 仅失败时推送 |
| `PT_DEBUG` | 否 | 空 | `1` = 打印详细调试信息（含页面片段，注意勿泄露） |
| `PT_TIMEOUT` | 否 | `20` | HTTP 超时秒数（OCR 识别固定 60s） |

账号变量支持多账号写法，但脚本只使用第一个，检测到多个会打印告警。

状态分档：`success` 签到成功 / `already` 今日已签（幂等成功）/ `skipped` 未配置该站 / `cookie_dead` Cookie 失效 / `parse_err` 结果未知（黄档）/ `network_err` 网络异常（自动重试 2 次）。

签到机制：NovaHD `attendance.php`（challenge-response 登录 + 验证码 OCR）；HDArea 首页判状态 + `POST sign_in.php`（仅 Cookie）；BTSchool `index.php?action=addbonus`（非标准 NexusPHP，无 formhash）；CrabPT 先回首页判「签到已得」标记，未签再 `GET attendance.php` 直接签到（GET 即签到，无表单无验证码，重复 GET 幂等）。

## 依赖安装

```bash
cd /ql/scripts/pt-checkin && npm install --production
```

验证码首选视觉模型（`PT_OCR_*`，识别率约 100%），未配置 / 超时 / 报错时降级本地 ddddocr，需 `pip install ddddocr opencv-python numpy` 并配好 `PT_PYTHON`。目录内 `sendNotify.js` 勿删；`*_cookie.json`、`pt_checkin.lock` 为运行时生成，已 gitignore。

## 凭证获取

浏览器登录各站后导出完整 Cookie，填入对应 `PT_SITE_*_CK`。NovaHD / BTSchool / CrabPT 配了账密会自动登录，成功后新 Cookie 缓存到 `<站点>_cookie.json` 并优先于环境变量；缓存只在登录后回读校验通过才落盘，登录误判不会用坏 Cookie 覆盖可用缓存。种子 Cookie 或账号变化时来源指纹不匹配，缓存自动失效，无需手动删除。

## 常见问题

- **`Cookie 已失效且未配置账密`** → 重新登录导出 Cookie 填入对应 `PT_SITE_*_CK`。
- **`剩余尝试机会仅 N 次（低于安全阈值），放弃登录`** → 站点按 IP 统计连续登录失败（NovaHD 10 次、BTSchool 20 次、CrabPT 10 次封禁，阈值 5 / 10 / 5）；等约 24 小时计数重置或换 IP。
- **`未解析到「剩余尝试次数」，保守起见本轮仅尝试 1 次`** → 登录页改版导致限次文案解析失败；频繁出现需更新 `parseLoginPage` 正则。
- **`登录页未解析到 imagehash`** → 站点登录页结构变化，需更新解析正则。
- **`OCR 接口 HTTP xxx`** → 检查 `PT_OCR_API_URL` / `PT_OCR_API_KEY` / `PT_OCR_MODEL` 是否正确、模型是否支持图片输入。
- **`自动登录后仍判定未登录`** → 账号可能被禁用或需要两步验证（`two_step_code` 当前未支持）。
- **`签到请求已发出但未能确认结果`** → 结果未知（黄档，不计失败），开 `PT_DEBUG=1` 看页面原文，多为站点改版导致解析规则失效。
- **`检测到上一次运行仍在进行…跳过本次`** → 确认无残留进程后删除 `pt_checkin.lock`（崩溃死锁 30 分钟后自动失效）。
- **`首页无签到入口且未识别到正常首页标志`** → BTSchool 首页疑似加载异常或维护页，先确认站点状态，脚本不会冒报已签。
- **`检测到 N 个账号…仅使用第一个`** → 账号变量里配了多个账号，本脚本只保留第一个。
