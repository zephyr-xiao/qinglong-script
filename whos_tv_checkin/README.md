# whos.tv 签到

青龙面板的 whos.tv 每日签到脚本，支持 Cookie 与账号密码两种认证。站点启用了 Cloudflare Managed Challenge：纯 HTTP 库伪造 UA/Cookie/TLS 指纹仍会被 403，headless 也被拦，因此脚本用 Patchright（未检测版 Playwright）驱动有头 chromium 通过挑战，再在浏览器会话内调用接口（接口自动探测，支持「已签到」幂等识别）。

## 青龙任务命令

```text
xvfb-run -a task whos_tv_checkin/whos_tv_checkin.py
```

定时建议 `15 8 * * *`。必须带 `xvfb-run -a`：容器没有显示器，headless 实测被 Cloudflare 拦截，只有 xvfb 虚拟显示 + 有头模式能过挑战。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `WHOSTV_COOKIE` | 二选一 | - | 完整 Cookie 字符串，多账号用 `&` 或换行分隔；串里的 `cf_clearance` / `__cf*` 会被自动忽略 |
| `WHOSTV_ACCOUNT` | 二选一 | - | 账号密码，格式 `用户名#密码`，多账号用 `&` 或换行分隔 |
| `WHOSTV_PROXY` | 建议 | - | HTTP/SOCKS 代理，支持 `http://`、`socks5://`、`socks5h://` 与 `http://user:pass@host:port`；留空回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`） |
| `WHOSTV_BROWSER_PATH` | 否 | 自动探测 | 指定 chromium 可执行文件路径 |
| `WHOSTV_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `WHOSTV_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅当存在失败时才推送（需 `WHOSTV_NOTIFY=true`，全部成功则静默） |
| `WHOSTV_TIMEOUT` | 否 | `30` | 接口请求超时（秒，最小 `1`，更小值回退默认） |
| `WHOSTV_BROWSER_WAIT` | 否 | `180` | 等待 chromium 就绪的最大秒数（同时是 Patchright 启动超时，最小 `10`） |
| `WHOSTV_BROWSER_PORT` | 否 | `9222` | 浏览器调试端口；端口决定 profile 目录与运行锁，被占用就换一个 |
| `WHOSTV_CHALLENGE_ROUNDS` | 否 | `4` | Cloudflare 挑战总轮数（最小 `1`）；轮数越多低信誉出口 IP 抽中放行概率越高、最坏耗时越长 |
| `WHOSTV_DEBUG` | 否 | `false` | 输出探测路径、状态码与响应片段（响应体中的令牌自动打码） |

`WHOSTV_COOKIE` 与 `WHOSTV_ACCOUNT` 至少配一个；两者都配时 Cookie 账号先执行、账号密码账号后执行，可混用：

```text
WHOSTV_COOKIE=cookie1&cookie2
WHOSTV_ACCOUNT=user3@example.com#password3
```

网络级失败（超时 / 代理断连，拿不到任何 HTTP 响应）会自动重发最多 2 次、间隔 3 秒；首跳导航被断/重置也会重试 2 次（间隔 5 秒），但导航自身超时不重试。

多账号共用同一浏览器实例；切换账号时会清空业务 Cookie 但保留 `cf_clearance`（与浏览器指纹绑定，清掉会重新触发挑战）。

## 依赖安装

```bash
docker exec -it qinglong bash
apt-get update && apt-get install -y chromium xvfb
pip install patchright
```

`chromium` 包会带齐浏览器运行库；`--no-sandbox`、`--disable-dev-shm-usage` 等容器必需参数已在 Patchright 默认参数表中，无需手动配置。同一 `WHOSTV_BROWSER_PORT` 只允许一个实例运行（profile 目录下有运行锁）；上次异常退出留下的残留锁会按「持有进程是否存活」自动接管，无需手工清理。端口已有遗留浏览器实例时脚本会走 CDP 接管复用，省一次冷启动。

本地调试（有真实显示器，无需 xvfb，会弹出浏览器窗口）：

```bash
export WHOSTV_ACCOUNT="user@example.com#password"
export WHOSTV_PROXY="http://127.0.0.1:7890"
export WHOSTV_NOTIFY=false
python whos_tv_checkin.py
```

Windows 下脚本会依次查找 PATH 与 `C:\Program Files\Google\Chrome\Application\chrome.exe` 等常见路径，特殊安装位置用 `WHOSTV_BROWSER_PATH` 指定；profile 目录用系统临时目录，多端口天然隔离。

## 凭证获取

- Cookie：浏览器登录 `https://whos.tv/` → F12 → Network → 刷新页面或打开任务页 → 复制请求头里的完整 `Cookie` 到 `WHOSTV_COOKIE`。`cf_clearance` / `__cf*` 由脚本浏览器自己维护，直接整段复制即可，不必手工剔除；回填别处复制的旧值反而会覆盖新鲜值、重新触发挑战。
- 账号密码：填 `用户名#密码`（用户名可填注册邮箱或用户名），脚本自动登录拿 Cookie；密码含 `#` 或 `&` 时确认青龙变量保存完整、没被换行截断。
- 退出码：`0` = 全部账号成功；`1` = 任一账号失败（含部分失败），便于外部监控。青龙本身不按退出码判定任务成败。

## 常见问题

- **`浏览器启动或挑战失败: Timeout 180000ms exceeded`** → 先确认容器装了 chromium + xvfb、命令带 `xvfb-run -a`；再结合报错里的 stderr 摘要与脚本输出的磁盘/内存/浏览器进程数/代理连通性判断。CPU 受限可把 `WHOSTV_BROWSER_WAIT` 调到 300；代理不可达会打印「代理 ... 不可达」。开 `WHOSTV_DEBUG=true` 重跑会输出完整环境诊断（chromium 路径 / xvfb / DISPLAY / /dev/shm / 磁盘 / 内存 / 进程数 / 代理连通性），据此定位。
- 也可进容器手动验证 chromium 能否启动：

  ```bash
  xvfb-run -a timeout 30 chromium --no-sandbox --disable-dev-shm-usage \
    --headless=new --remote-debugging-port=9223 about:blank & sleep 8
  curl -s http://127.0.0.1:9223/json/version | head -3
  kill %1 2>/dev/null
  ```

- **`登录失败: 响应非 JSON (HTTP 403)`** → 这不是普通状态码，是 Cloudflare 挑战页。脚本按页面特征识别后会自动补过一轮挑战并重试该账号；仍失败说明出口 IP 已不被 CF 接受，需要换代理节点。
- **`响应非 JSON (HTTP 0)` / `登录网络异常`** → 根本没拿到服务器响应：`__TIMEOUT__` 是代理链路太慢或不通，`__NETWORK__|Failed to fetch` 是拒连/DNS/代理瞬断；重试耗尽后检查容器到 `WHOSTV_PROXY` 的连通性。
- **`Cloudflare 挑战在 N 轮内均未通过`** → 默认 180 秒 × 4 轮，约 30 分钟后直接失败退出、不进入账号处理；换低风险出口 IP 或调大 `WHOSTV_CHALLENGE_ROUNDS`。
- **`出口 IP 被 Cloudflare 硬拦截（Access denied / Error 1020）`** → 与「挑战未通过」不同，CF 直接拒绝该 IP，重试无用，只能换节点；脚本识别到这类页面会立即失败退出、不再空耗轮数。
- **签到中途提示「被 Cloudflare 拦截（挑战已失效）」** → 多账号连续执行时 `cf_clearance` 可能中途过期，脚本自动补过一轮挑战并重试该账号；仍失败就换代理节点重跑。
- **Cookie 失效** → Cookie 模式重新登录抓完整 Cookie；账密模式的 Cookie 由脚本获取，一般不会遇到，若出现多为账密错误或封禁。
- **请求超时 / 连接失败** → whos.tv 在大陆网络通常需要代理，确认 `WHOSTV_PROXY` 或青龙全局代理在容器内可访问（Docker 青龙常用 `http://172.17.0.1:7890`）。
- 耗时预期：每次运行都会启动一次真实浏览器并等待挑战通过，单轮实测 5~150 秒波动很大，总耗时随出口 IP 信誉变化：

  | 情形 | 大致耗时 |
  |---|---|
  | 一轮就通过（常见） | 约 1~4 分钟 |
  | 每多抽一轮 | +3~4 分钟 |
  | 抽到最后一轮才通过 | 约 30 分钟 + 账号处理时间 |

  4 轮全部未通过时脚本约 30 分钟后直接失败退出，不会进入账号处理。挑战能否通过取决于出口 IP 信誉：同一环境实测放行率约 1/3 且随机，所以脚本用「多轮 + 轮间隔刷新」抽签而不是单轮死等，轮数可用 `WHOSTV_CHALLENGE_ROUNDS` 调。
