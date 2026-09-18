# whos.tv 签到

适配青龙面板的 whos.tv 签到脚本，支持 **Cookie** 和 **账号密码** 两种认证方式。

> ⚠️ **浏览器方案说明**：whos.tv 已启用 Cloudflare Managed Challenge 人机验证，
> 纯 HTTP 库（`requests` / `curl_cffi`）即使伪造 UA、Cookie、TLS 指纹也会被
> 403 挑战页拦截（已实测）。因此脚本改为 **Patchright（未检测版 Playwright）
> 驱动真实浏览器内核**——无 `Runtime.enable` 等 CDP 自动化痕迹，可通过托管挑战；
> 原版 Playwright 会被识别。自动通过挑战后，再在浏览器会话内调用接口。
> 这是目前唯一可行的方案。

站点：`https://whos.tv/`

签到页：`https://whos.tv/points-center/tasks`

## 功能特点

- 支持 **Cookie 认证**（直接复制 Cookie 字符串）；
- 支持 **账号密码认证**（自动登录获取 Cookie，无需手动维护）；
- 两种方式可同时使用；
- 支持多账号，使用 `&` 分隔；
- 支持代理，适合大陆网络访问受限场景；
- 自动通过 Cloudflare 人机验证挑战；
- 自动探测签到接口；
- 支持"已签到"幂等识别；
- 支持青龙 `notify.py` 推送；
- Debug 模式可输出探测路径、状态码和响应片段。

## 环境要求

| 依赖 | 说明 |
|---|---|
| Python 包 | `patchright`（青龙「依赖管理」-「Python」中安装） |
| 系统 | `chromium` + `xvfb`（青龙容器内执行下面的安装命令） |

> 脚本用 Patchright 的 `launch_persistent_context` 有头启动 chromium（启动超时上限
> `WHOSTV_BROWSER_WAIT` 秒，失败时自动输出浏览器 stderr 摘要定位原因）；容器必需参数
> （`--no-sandbox`、`--disable-dev-shm-usage` 等）已在 Patchright 默认参数表中，无需手动配置。
> 若端口已有上次异常退出遗留的浏览器实例，脚本会走 CDP 接管复用，省一次冷启动。

青龙容器内安装命令：

```bash
docker exec -it qinglong bash
apt-get update && apt-get install -y chromium xvfb
pip install patchright
```

> `chromium` 包会自动带齐浏览器运行所需的系统依赖库。

## 青龙任务命令

> ⚠️ **必须用 `xvfb-run` 运行**：headless 无头模式实测会被 Cloudflare 拦截，
> 青龙容器内没有显示器，需要用 xvfb 提供虚拟显示，浏览器以有头模式运行。

```text
xvfb-run -a task whos_tv_checkin/whos_tv_checkin.py
```

建议定时：

```text
15 8 * * *
```

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `WHOSTV_COOKIE` | 二选一 | - | 完整 Cookie 字符串，多账号用 `&` 分隔 |
| `WHOSTV_ACCOUNT` | 二选一 | - | 账号密码，格式 `用户名#密码`，多账号用 `&` 分隔 |
| `WHOSTV_PROXY` | 建议 | - | HTTP/SOCKS 代理，国内网络建议配置；留空时自动回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`） |
| `WHOSTV_BROWSER_PATH` | 否 | 自动探测 | 指定 chromium 可执行文件路径（默认自动查找） |
| `WHOSTV_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `WHOSTV_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅当存在失败时才推送（需 `WHOSTV_NOTIFY=true`，全部成功则静默） |
| `WHOSTV_TIMEOUT` | 否 | `30` | 接口请求超时时间，单位秒 |
| `WHOSTV_BROWSER_WAIT` | 否 | `180` | 启动 chromium 后等待就绪的最大秒数（受限容器冷启动可能较慢，同时作为 Patchright 启动超时） |
| `WHOSTV_BROWSER_PORT` | 否 | `9222` | 浏览器调试端口，被其他进程占用时换一个（profile 目录随端口生成） |
| `WHOSTV_DEBUG` | 否 | `false` | 输出探测细节（试过哪些路径、状态码、响应片段） |

> **网络级失败自动重试**：请求拿不到任何 HTTP 响应（超时 / 代理抖动断连）时自动重发
> 最多 2 次（间隔 3 秒）；服务器已有响应（含 403/500）属业务结果不重复请求。
> 首跳导航（打开站点）遇到连接被断/重置等网络错误同样自动重试 2 次（间隔 5 秒），
> 但导航自身超时不重试（链路极慢时重发无意义）。
> 网络失败的具体原因（超时 / Failed to fetch 等）无论是否开启 DEBUG 都会打印，
> 且已知签到接口网络失败时会直接短路返回，不再白跑十几个候选接口逐个超时。

> `WHOSTV_COOKIE` 和 `WHOSTV_ACCOUNT` 至少配置一个。两者都配时，Cookie 账号先执行，账号密码账号后执行。

代理示例：

```text
WHOSTV_PROXY=http://172.17.0.1:7890
```

或：

```text
WHOSTV_PROXY=socks5://172.17.0.1:7891
```

> 注意：脚本每次运行都会启动一次真实浏览器并等待 Cloudflare 挑战通过
> （实测约 5~90 秒，波动较大），单次任务总耗时约 1~3 分钟属正常现象。

## 认证方式

### 方式一：Cookie 认证

1. 浏览器登录 `https://whos.tv/`；
2. 打开开发者工具；
3. 进入 Network / 网络；
4. 刷新页面或打开任务页；
5. 找到请求头中的 `Cookie`；
6. 复制完整 Cookie 字符串到青龙环境变量 `WHOSTV_COOKIE`。

多账号示例：

```text
WHOSTV_COOKIE=cookie_for_account_1&cookie_for_account_2
```

### 方式二：账号密码认证

直接配置账号和密码，脚本自动登录后签到，无需手动复制 Cookie。

格式：`用户名#密码`，多账号用 `&` 分隔：

```text
WHOSTV_ACCOUNT=user1@mail.com#password1&user2@mail.com#password2
```

> 用户名可以是注册邮箱或用户名（与网页登录框一致，支持"邮箱 / 用户名"输入）。
> 密码中如果包含 `#` 或 `&` 特殊字符，请确保青龙环境变量保存完整，不要换行截断。

### 混合使用

两种方式可同时配置，例如有 2 个 Cookie 账号 + 1 个账号密码：

```text
WHOSTV_COOKIE=cookie1&cookie2
WHOSTV_ACCOUNT=user3@mail.com#password3
```

## 本地调试

Linux / macOS：

```bash
export WHOSTV_ACCOUNT="user@mail.com#password"
export WHOSTV_PROXY="http://127.0.0.1:7890"
export WHOSTV_NOTIFY=false
export WHOSTV_DEBUG=true
python whos_tv_checkin.py
```

Windows PowerShell：

```powershell
$env:WHOSTV_ACCOUNT="user@mail.com#password"
$env:WHOSTV_PROXY="http://127.0.0.1:7890"
$env:WHOSTV_NOTIFY="false"
$env:WHOSTV_DEBUG="true"
python .\whos_tv_checkin.py
```

> 本地运行会弹出浏览器窗口，属正常现象（headless 会被 Cloudflare 拦截）。

## 通知

- 青龙环境中自动复用青龙 `notify.py`；
- 设置 `WHOSTV_NOTIFY=false` 可关闭推送；
- 标题按成败分档显示：`✅ 全部成功（N/N）` / `⚠️ 部分失败（M/N）` / `❌ 全部失败（0/N）`；
- 正文为 Markdown 分组格式，顶部带 ⏰ 执行时间、📊 账号统计，结果按「成功 / 失败」分组列出；
- 设置 `WHOSTV_NOTIFY_ONLY_FAIL=true` 可在本次全部成功时静默不推送（仅在有失败时才通知）；
- 推送失败会自动重试最多 3 次；
- 本地运行找不到 `notify.py` 时会跳过推送。

## 常见问题

### 报错"浏览器启动或挑战失败: Timeout 180000ms exceeded"？

这是 chromium 启动超时（Patchright 等待 `WHOSTV_BROWSER_WAIT` 秒后放弃，报错自带
浏览器 stderr 摘要）。请按顺序排查：

1. **确认容器已装 chromium 和 xvfb**（见上文"环境要求"），任务命令带 `xvfb-run -a` 前缀；
2. **启动超时**：结合报错中的 stderr 摘要，以及脚本输出的**磁盘 / 内存 / 浏览器进程数 /
   代理连通性**判断。常见原因：
   - 磁盘满或内存不足（chromium 写 profile / 临时文件卡住）→ 清理容器空间；
   - 容器 CPU 受限导致冷启动极慢 → 把 `WHOSTV_BROWSER_WAIT` 调大到 300；
   - 代理不可达（脚本会输出"❌ 代理 ... 不可达"）→ 改用容器内可达的代理地址，如 `http://172.17.0.1:7890`。
3. 开启 `WHOSTV_DEBUG=true` 重跑，失败时脚本会输出**完整环境诊断**（chromium 路径 /
   xvfb / DISPLAY / /dev/shm / 磁盘 / 内存 / 进程数 / 代理连通性），据此定位；
4. 也可进容器手动验证 chromium 能否启动：

```bash
# 容器内手动试启动 chromium 并 curl 调试端口：
xvfb-run -a timeout 30 chromium --no-sandbox --disable-dev-shm-usage \
  --headless=new --remote-debugging-port=9223 about:blank & sleep 8; \
  curl -s http://127.0.0.1:9223/json/version | head -3; \
  kill %1 2>/dev/null
```

### 报错"登录失败: 响应非 JSON (HTTP 403)"？

这是 Cloudflare 人机验证拦截了请求。请确认：

- 青龙容器已安装 `chromium` 和 `xvfb`（见上文"环境要求"）；
- 任务命令使用 `xvfb-run -a` 前缀（headless 无头模式会被拦截）；
- `WHOSTV_PROXY` 配置正确，代理出口 IP 信誉正常。

### 报错"响应非 JSON (HTTP 0)"或"登录网络异常"？

HTTP 0 表示请求根本没拿到服务器响应（不是站点拒绝），脚本会打印具体原因并自动重试：

- **`__TIMEOUT__|30s 内未收到响应`**：代理链路太慢或不通，检查 `WHOSTV_PROXY` 是否可达；
- **`__NETWORK__|TypeError: Failed to fetch`**：连接被拒 / DNS 解析失败 / 代理瞬断，
  重试仍失败说明网络持续异常，检查容器到代理的连通性；
- 若日志出现"网络异常无法完成签到……请检查代理 WHOSTV_PROXY 与容器网络"，
  说明已知接口和候选探测都拿不到响应，优先恢复代理再跑。

### 报错"ERR_CONNECTION_CLOSED / ERR_CONNECTION_RESET"（启动阶段）？

浏览器首跳打开站点时代理链路瞬时抖动，脚本会自动重试最多 2 次（间隔 5 秒），
一般无需干预；若重试耗尽仍失败，按上一条的思路检查代理连通性后重跑即可。
注意"Timeout xxx ms exceeded"形式的导航超时不属于此列（不重试），按启动超时排查。

### 本地 Windows 调试提示

- Chrome 自动探测：脚本会依次查找 PATH 及 `C:\Program Files\Google\Chrome\Application\chrome.exe`
  等 Windows 常见安装路径，一般无需手动设置；特殊安装位置可用
  `WHOSTV_BROWSER_PATH` 指定；
- 无需 xvfb（有真实显示），直接 `python whos_tv_checkin.py` 即可，浏览器会弹窗；
- profile 目录用系统临时目录（`%TEMP%\whostv_profile\<端口>`），多端口天然隔离；
  若启动报错提示已有实例占用，换一个 `WHOSTV_BROWSER_PORT` 即可。

### 报错"Cloudflare 挑战未通过"？

脚本会等待挑战最长 120 秒并重试 1 次。若仍失败：

- 检查代理是否可用、出口 IP 是否被 Cloudflare 判定为高风险（共享/机房 IP 更容易触发）；
- 换一个代理节点后重试；
- 可开启 `WHOSTV_DEBUG=true` 观察浏览器启动与挑战状态。

### 请求超时或连接失败？

whos.tv 在大陆网络通常需要代理。脚本按 `WHOSTV_PROXY` → 青龙全局代理（`HTTPS_PROXY` /
`HTTP_PROXY` / `ALL_PROXY`）的顺序取代理，两者都没有则直连。请确认脚本取到的代理
在青龙容器内可访问。

Docker 青龙常见代理值：

```text
http://172.17.0.1:7890
```

### 提示 Cookie 失效？

- **Cookie 模式**：重新登录 whos.tv，复制新的完整 Cookie 字符串并更新 `WHOSTV_COOKIE`。
- **账号模式**：Cookie 由脚本自动登录获取，一般不会出现此问题。如果遇到，可能是账号密码错误或被封禁，检查日志中的具体错误信息。

### 账号密码登录失败？

常见原因：

- **密码错误**：日志会明确提示"登录失败: 密码错误"
- **账号不存在**：检查用户名是否拼写正确
- **网络不通**：确保代理（`WHOSTV_PROXY` 或青龙全局代理）配置正确且容器内可达
- **账号被封禁**：日志会提示"账号已被封禁"

### 不知道脚本试了哪些接口？

开启：

```text
WHOSTV_DEBUG=true
```

脚本会输出探测路径、状态码和响应片段，便于排查站点接口变化。

### 多账号怎么配置？

Cookie 模式用 `&` 分隔：

```text
WHOSTV_COOKIE=cookie1&cookie2&cookie3
```

账号密码模式用 `&` 分隔，每个账号内部用 `#` 分隔用户名和密码：

```text
WHOSTV_ACCOUNT=user1@mail.com#pass1&user2@mail.com#pass2
```

> 多账号共用同一浏览器实例，切换账号时会清空业务 Cookie 但保留
> `cf_clearance`（与浏览器指纹绑定，清掉会重新触发挑战）。
