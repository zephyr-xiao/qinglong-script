# 芯幻签到

适配青龙面板的芯幻（xhcyv.com，二次元 ACG 社区）每日签到脚本，仅依赖 `requests` + 标准库（图形验证码兜底需要可选的 `ddddocr`）。

站点：`https://xhcyv.com/`

签到入口：站内「个人中心 → 今日签到」

## 签到流程（2026-10-21 真机实测）

| 步骤 | 请求 | 说明 |
|---|---|---|
| 登录 | `POST https://xhcyv.com/api/session/login` | body `{"account","password"}`；**token 只在 `Set-Cookie` 的 `x_bbs_token` 里**，响应 body 只返回 user 信息 |
| 查状态 | `GET https://api.xhcyv.com/api/v1/users/me/signin` | 返回 `signed` / `record` / `streak` |
| 签到 | `POST https://api.xhcyv.com/api/v1/users/me/signin` | 返回 `points` / `day_order` / `streak` / `bonus` / `member_bonus` |
| 用户信息 | `GET https://api.xhcyv.com/api/v1/users/me` | 用于展示当前积分与等级 |
| 验证码 | `GET https://api.xhcyv.com/api/v1/captcha` | 仅登录被风控时返回 `id` + base64 图片 |

- 鉴权：`Authorization: Bearer <x_bbs_token>`（Cookie 也能用，脚本统一走 Bearer，避免 Cookie 域限制）；
- 统一响应：`{"code","message","data"}`，`code=0` 成功；
- `x_bbs_token` 是 JWT，**有效期 3 天**（`Max-Age=259199`），过期后脚本自动重新登录；
- 重复签到返回 `code=11006 already signed in today`，脚本按幂等成功处理。

## 功能特点

- 纯 API 签到，无需浏览器 / Playwright / OpenCV，部署轻量；
- 账密登录自动换取 `x_bbs_token`，并缓存到本地文件复用（**3 天有效期内不再登录**，减少风控暴露）；
- 缓存 token 失效（`10002`）自动清理并重登，无需人工干预；
- 签到前预检 `signed`，已签到直接幂等返回，省一次签到请求；
- 上报本次积分、今日第几位、连签天数、连签奖励、会员加成、当前积分与等级；
- 支持直接填 `x_bbs_token`（跳过登录，适合账号开了 2FA 或不想存密码的场景）；
- 登录被要求图形验证码时（`10007`/`10008`）自动用 `ddddocr` 识别，识别失败给出明确提示；
- 支持多账号（`XHCYV_ACCOUNTS` 每行一个，`XHCYV_TOKEN` 可用 `&` 或换行分隔），账号间隔 1 秒避免同 IP 风控；
- 账号与 token 在日志/推送中脱敏；
- `--check` 自检模式：只验证配置与凭证，不提交签到；
- 支持青龙 `notify.py` 推送，标题按成败分档，失败重试 3 次。

## 青龙任务命令

```text
task xhcyv_checkin/xhcyv_checkin.py
```

建议定时（脚本头 `cron:` 与此一致）：

```text
5 8 * * *
```

（每天 8:05 执行；站点按北京时间划分自然日）

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `XHCYV_ACCOUNTS` | ✅ | - | 账号密码，格式 `账号#密码`（兼容 `账号:密码` / `账号&密码`），多账号用换行分隔；每行也可以直接填 `x_bbs_token` |
| `XHCYV_ACCOUNT` | 否 | - | 单账号便捷写法，需同时配置 `XHCYV_PASSWORD`（会排在 `XHCYV_ACCOUNTS` 之前执行） |
| `XHCYV_PASSWORD` | 否 | - | 配合 `XHCYV_ACCOUNT` 使用 |
| `XHCYV_TOKEN` | 否 | - | 直接提供 `x_bbs_token`，优先于账号密码；多账号用 `&` 或换行分隔 |
| `XHCYV_PROXY` | 否 | - | HTTP/SOCKS 代理，网络受限时填写（未填时回退读 `HTTPS_PROXY`/`HTTP_PROXY`） |
| `XHCYV_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `XHCYV_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅当存在失败时才推送（需 `XHCYV_NOTIFY=true`） |
| `XHCYV_TIMEOUT` | 否 | `30` | HTTP 超时时间，单位秒 |
| `XHCYV_DEBUG` | 否 | `false` | 输出各接口状态码与响应片段 |

单账号最简配置：

```text
XHCYV_ACCOUNTS=your_account#your_password
```

多账号：

```text
XHCYV_ACCOUNTS=user1#pass1
user2#pass2
```

或：

```text
XHCYV_ACCOUNT=user1
XHCYV_PASSWORD=pass1
```

## token 获取方式（可选）

1. 浏览器登录 `https://xhcyv.com/login`；
2. `F12` → **Application / 应用 → Cookies → https://xhcyv.com**；
3. 找到 `x_bbs_token`，复制其值（`eyJ` 开头的三段 JWT）；
4. 填入 `XHCYV_TOKEN`（或 `XHCYV_ACCOUNTS` 的某一整行）。

> `x_bbs_token` 是 `HttpOnly` Cookie，控制台 `document.cookie` 读不到，必须从 DevTools 的 Cookies 面板复制。

## 自检模式

部署后先跑一次自检，确认配置无误（**不会提交签到**）：

```text
task xhcyv_checkin/xhcyv_checkin.py --check
```

输出示例：

```text
🔍 自检模式：仅验证配置与凭证，不会提交签到

🔄 you****nt 开始自检 ...
   ✅ 登录成功（当前 20 积分，Lv.1）（有效期至 2026-10-24 02:25）
   ✅ 今日已签到 +20 积分，今日第 2354 位，连签 1 天

✅ 自检通过（1/1）
```

判定口径：出现 `✅ 自检通过` 且退出码为 0 才算通过；只看有没有报错不算。

## 单元测试

脚本自带 53 项离线单元测试（标准库 `unittest`，不联网、不需要账号）：

```bash
cd xhcyv_checkin
python -m unittest discover -s tests -v
```

覆盖账号解析、脱敏、JWT 有效期、缓存读写与损坏降级、接口响应分类（业务码 / 5xx / 非 JSON / 网络重试）、登录分支（Set-Cookie 取 token、验证码重试、2FA）与单账号编排（缓存失效重登、幂等、token 模式）。

## 依赖安装

青龙环境一般已内置 `requests`。如缺失，在青龙「依赖管理」-「Python」中安装：

```text
requests
```

只有登录被要求图形验证码时才需要 `ddddocr`（可选）：

```text
ddddocr
```

## 本地调试

Linux / macOS：

```bash
export XHCYV_ACCOUNTS="your_account#your_password"
export XHCYV_NOTIFY=false
python xhcyv_checkin.py --check
python xhcyv_checkin.py
```

Windows PowerShell：

```powershell
$env:XHCYV_ACCOUNTS="your_account#your_password"
$env:XHCYV_NOTIFY="false"
python .\xhcyv_checkin.py --check
```

## 通知

- 青龙环境中自动复用青龙 `notify.py`；
- 设置 `XHCYV_NOTIFY=false` 可关闭推送；
- 标题按成败分档：`✅ 全部成功（N/N）` / `⚠️ 部分失败（M/N）` / `❌ 全部失败（0/N）`；
- 正文为 Markdown 分组格式，顶部带 ⏰ 执行时间、📊 账号统计，结果按「成功 / 失败」分组；
- `XHCYV_NOTIFY_ONLY_FAIL=true` 时全部成功不推送；
- 本地找不到 `notify.py` 会跳过推送，不影响签到。

## 常见问题

### 提示"账号或密码错误"？

站点业务码 `11003`。账号与邮箱都可以填（即登录页「账号 / 邮箱」输入框的内容）。分隔符支持 `#`、`:`、`&`，推荐用 `#`；脚本按行内**最早出现**的分隔符切分，所以密码里含 `:` 或 `&` 一般无需额外处理。若密码以分隔符开头导致误切，改用 `XHCYV_ACCOUNT` / `XHCYV_PASSWORD` 分开配置。

### 提示"该账号已开启两步验证（2FA）"？

站点登录成功后会要求输入验证器 App 的 6 位验证码，脚本无法自动完成。两种选择：

1. 在站点「设置 → 安全」关闭两步验证；
2. 改用 `XHCYV_TOKEN` 模式（从浏览器复制 `x_bbs_token`，有效期 3 天，到期后重新复制）。

### 提示"登录需要图形验证码，但未安装 ddddocr"？

连续登录失败后站点会要求图形验证码（业务码 `10007`）。在青龙「依赖管理」-「Python」安装 `ddddocr` 后重试，脚本会自动取码识别；识别一次仍失败会提示改用 `XHCYV_TOKEN`。

### 提示"凭证已失效"？

- 账密模式：脚本会自动清缓存并重新登录，通常不会看到这条；
- `XHCYV_TOKEN` 模式：token 只有 3 天有效期，重新从浏览器复制 `x_bbs_token` 更新环境变量即可。

### 签到返回"今日已签到"算成功吗？

算。脚本对 `code=11006` 做了幂等识别，并且在签到前先查 `signed`，已签直接返回成功，不会重复提交。

### 请求超时或连接失败？

站点国内可直连。若青龙容器网络受限，在 `XHCYV_PROXY` 填写容器内可访问的代理，例如：

```text
XHCYV_PROXY=http://172.17.0.1:7890
```

脚本刻意设置了 `trust_env=False`（不读系统代理），只认 `XHCYV_PROXY` 与 `HTTPS_PROXY`/`HTTP_PROXY` 环境变量——避免 Windows 下 requests 自动读取注册表系统代理导致"本机能跑、容器超时"。

### 缓存文件在哪？

`xhcyv_checkin/xhcyv_token.json`，包含各账号的 `x_bbs_token`，权限收紧到 `0600`、原子写入。该文件名已被仓库 `.gitignore` 的 `*_token.json` 覆盖，**不要提交**。
