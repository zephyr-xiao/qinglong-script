# 老王FIP签到

适配青龙面板的老王论坛（laowang.vip）每日签到脚本，纯 `requests` 实现登录、滑块验证与签到。

候选域名（按序探测、主域名被拦自动降级）：

```text
https://laowang.vip,https://laowangfip372.vip
```

## 为什么可以纯 requests

老王FIP 使用 tncode 滑块验证码（登录与签到各一次）。站点的 `/captcha/tn_code.js` 是**未混淆的经典实现**：所谓 `sign` / `track` 只是「轨迹统计 → JSON → 与明文密钥逐字节 XOR → base64」，再用 FNV-1a 对 `trackStr + ts + offset + 密钥` 求签名，密钥硬编码在 JS 里。因此全部可在 Python 复现，无需浏览器：

1. 取 `/captcha/tncode.php` 的三联图（带缺口背景 / 拼图块 / 完整背景）；
2. 用 OpenCV 差异法（完整背景 vs 带缺口背景）解出缺口位置；站点用「双缺口」反爬，脚本按「真缺口与拼图块同行（y 对齐）」判别，并对每张图的多个候选逐个提交（实测同一张图可多次提交，全部候选失败才换图）；
3. 构造一条人类观感轨迹，按站点算法生成 payload，提交 `/captcha/check.php` 拿到 `<token>_ok`；
4. 把 `<token>_ok` 作为 `clicaptcha-submit-info` 填进登录/签到表单提交。

服务端行为风控（`error_track`）会拒绝点数过少/瞬移的轨迹，脚本用约 40 个点的余弦缓动轨迹规避。

## 青龙任务命令

```text
task laowangfip_checkin/laowangfip_checkin.py
```

建议定时：

```text
30 8 * * *
```

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `LWFIP_ACCOUNTS` | ✅ | - | `用户名#密码`，多账号用 `&` 或换行分隔 |
| `LWFIP_PROXY` | 否 | - | HTTP 代理，站点被墙建议配置，如 `http://172.17.0.1:7890`；留空时自动回退青龙全局代理 |
| `LWFIP_PROXY_REQUIRED` | 否 | `false` | 设为 `true` 时缺代理直接报错退出（默认缺代理仅告警并直连尝试） |
| `LWFIP_BASE_URLS` | 否 | `https://laowang.vip,https://laowangfip372.vip` | 候选域名，逗号分隔，按顺序探测，首个可用即采用；主域名被拦时自动降级 |
| `LWFIP_BASE_URL` | 否 | - | 兼容旧配置；设置后**只用该域名、不做降级** |
| `LWFIP_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `LWFIP_NOTIFY_ONLY_FAIL` | 否 | `false` | `true` 时仅在有账号失败时才推送 |
| `LWFIP_TIMEOUT` | 否 | `30` | 请求超时，单位秒 |
| `LWFIP_DEBUG` | 否 | `false` | 开启后输出验证码求解等调试日志 |
| `LWFIP_MAX_CAPTCHA_RETRY` | 否 | `5` | 单次滑块识别失败重试次数 |
| `LWFIP_MAX_RETRY` | 否 | `5` | 账号级任务重试次数（仅可重试错误触发，见下文重试策略） |
| `LWFIP_RETRY_INTERVAL` | 否 | `60` | 两次任务重试间隔，单位秒 |
| `LWFIP_COOKIE_CACHE` | 否 | `true` | 登录成功后会话 Cookie 落盘复用，直通签到跳过滑块 |

多账号示例：

```text
LWFIP_ACCOUNTS=user1#pass1&user2#pass2
```

## 依赖安装

要求 **Python 3.8+**。青龙容器自带 Python 3.11，无需处理。在青龙面板「依赖管理」→「Python」中安装：

```text
requests
opencv-python-headless
numpy
```

## 青龙配置示例

```text
LWFIP_ACCOUNTS=your_username#your_password
LWFIP_PROXY=http://172.17.0.1:7890
LWFIP_NOTIFY=true
```

## 本地调试示例

Windows PowerShell：

```powershell
$env:LWFIP_ACCOUNTS="your_username#your_password"
$env:LWFIP_PROXY="http://127.0.0.1:7890"
$env:LWFIP_DEBUG="true"
$env:LWFIP_NOTIFY="false"
python .\laowangfip_checkin.py
```

Linux / macOS：

```bash
export LWFIP_ACCOUNTS="your_username#your_password"
export LWFIP_PROXY="http://127.0.0.1:7890"
export LWFIP_DEBUG=true
export LWFIP_NOTIFY=false
python laowangfip_checkin.py
```

## 域名降级（主域名被 Cloudflare 拦截时）

站点主域名 `laowang.vip` 挂在 Cloudflare 后面，**会对部分代理出口 IP 整站拦截**，返回 `403` 的 `Just a moment...` 质询页。该页里没有 `formhash`/`loginhash`，登录必然失败。备用域名 `laowangfip372.vip` 是同一套后端，但没被拦。

实测对照（2026-11-21，同一代理 `:7890`）：

| 域名 | 登录页 |
|---|---|
| `laowang.vip` | 403 × 6/6（CF 质询页） |
| `laowangfip372.vip` | 200 × 10/10，`formhash` 正常 |

脚本的两层处理：

1. **启动探测**：按 `LWFIP_BASE_URLS` 顺序请求登录页，第一个能解析出 `formhash` + `loginhash` 的域名被采用。全部不可用则推送告警并退出。
2. **运行中降级**：任务执行期间若又遇到 CF 拦截（例如代理节点轮换导致出口 IP 变了），自动切到下一个候选域名立即重试，不空等重试间隔。

判定 CF 拦截的依据是页面特征 `Just a moment...` 或 `window._cf_chl_opt`，不依赖状态码——实测同一出口下 `curl` 返回 403 而 `requests` 返回 200（TLS 指纹差异），只看状态码会误判。

> ⚠️ **换代理解决不了这个问题**。`is_agree` 之类的 Cookie 同样无效（实测 3/3 仍 403）——Cloudflare 质询与站点自身的年龄网关是两件独立的事。

## 年龄确认网关自动放行

站点在**根路径 `/`** 投放「性材料警告」页，需要点「同意 / I Agree」置 `is_agree=1` 才放行。该页没有业务内容，若被当作登录页解析就会得到「formhash 缺失」这类误导性错误。

脚本的处理：

- 每个会话预先置 `is_agree=1`（精确到当前域名，不会泄漏给别的域）；
- 所有业务页面请求走 `fetch_page()` 包装器，命中网关页时补置 Cookie 并重试一次；
- 重试后仍是网关页则抛 `AgeGateBlocked`，由上层换域名或明确报错。

实测（2026-11-21）：`laowang.vip/` 与 `laowangfip372.vip/` 裸请求都返回网关页，经 `fetch_page` 后网关页消失。目前 `forum.php` / `member.php` / `plugin.php` 并不拦，此处属防御性处理，防止站点日后扩大拦截范围。

## 重试策略

账号级重试（`LWFIP_MAX_RETRY` 次，间隔 `LWFIP_RETRY_INTERVAL` 秒固定）：

| 错误类型 | 是否重试 | 示例 |
|---|---|---|
| 网络 / 超时 / 代理 | ✅ | 连接超时、proxy error、SSLError、连接被拒 |
| 滑块验证码失败 | ✅ | 缺口识别失败、服务端判定未通过、验证码弹窗未出现 |
| 签到流程异常 | ✅ | 未到达验证页、提交后仍在验证页、签到结果未知、找不到签到按钮 |
| 域名被拦截 | ✅ | Cloudflare 质询、年龄网关未放行（**自动换域名立即重试，不等待间隔**） |
| 业务错误 | ❌ | 密码错误、用户名不存在、账号禁用（重试无意义且加剧风控） |

间隔保持固定（不做指数退避）：多账号最坏时长可控（如默认 5 次重试、4 个间隔 × 60 秒 = 4 分钟），避免任务跨 cron 周期。

## 会话 Cookie 复用

登录成功后脚本把会话 Cookie 保存到脚本目录下 `cookie_<哈希>.json`（文件名按「账号 + 域名」哈希，不含明文，已被仓库 `.gitignore` 覆盖）：

- **当天重试**：账号级重试直接带 Cookie 直通签到，不再重过滑块；
- **后续 cron 任务**：Cookie 有效期内直接签到，登录频率大幅降低、防风控；
- **域名隔离**：不同域名的 Cookie 分文件存放。Cookie 与域名绑定，共用一份会在切换域名时互相覆盖且加载后无效；
- Cookie 失效自动重新登录并覆盖缓存，无需人工干预；
- 关闭方式：`LWFIP_COOKIE_CACHE=false`。

## 单元测试

```bash
pip install requests opencv-python-headless numpy
python -m unittest discover -s tests -v
```

覆盖：账号解析、脱敏、代理脱敏、签到统计解析（含属性顺序容错）、重试错误分类、Cookie 缓存读写、**tncode payload 与站点真实 JS 输出的固定基准值逐字节对拍**（含 7 位短 sign，验证不补零）、**合成三联图滑块定位回归（误差 ≤5px）**、**双缺口判别回归（y 对齐候选排首位）**、**签到结果判定回归（静态标签「连续签到」不得误判为成功）**。

## 执行报告

成功后会输出类似：

```markdown
# ✅ 老王FIP签到 - 执行报告

⏰ 2026-10-10 22:01:46
📊 总计 1 账号，✅ 1 / ❌ 0

## 详细结果
- ✅ **[********]** 今日已签到（btnvisted）
  - 签到统计：连续签到: 2 天；签到等级: 1 级；积分奖励: 6 分；总天数: 2 天
```

推送失败会自动重试 3 次，不影响签到结果。

## 常见问题

### 页面打不开或超时？

老王FIP 站点需要代理。脚本按 `LWFIP_PROXY` → 青龙全局代理（`HTTPS_PROXY` /
`HTTP_PROXY` / `ALL_PROXY`）的顺序取代理，两者都没有则直连（大概率打不开）。
Docker 青龙常用宿主机地址：

```text
http://172.17.0.1:7890
```

代理软件建议开启**规则模式**：国内域名直连、境外域名走代理，这样青龙里配一个全局代理就能兼顾各脚本。

### 报错「验证码识别失败」？

脚本会重试 `LWFIP_MAX_CAPTCHA_RETRY` 次（每次重新取图求解）。若持续失败，开启：

```text
LWFIP_DEBUG=true
```

观察日志中的「缺口 bbox」与「服务端返回」信息。站点若改版（如改用点选验证码），需重新适配求解算法。

### 统计数据为空或只有「天」？

脚本读取签到页 `ul.countqian.cl` 区域各 `li` 的 `h4` 标签与 `input.hidnum` 的 value。若站点改版导致 DOM 结构变化，开启 `LWFIP_DEBUG=true` 检查签到页结构。
