# 老王FIP签到（浏览器版）

适配青龙面板的老王FIP论坛签到脚本，使用 Playwright 启动 Chromium 完成登录、滑块验证和签到。

站点默认地址：`https://laowangfip372.vip`

## 为什么使用浏览器版

老王FIP 的 tncode 滑块校验涉及前端 JavaScript 计算的 `sign` / `track` 字段。纯 `requests` 很难稳定复现，浏览器版让 Chromium 自己运行站点 JS，脚本只负责：

1. 打开页面并填写账号密码；
2. 截取验证码弹窗；
3. 用 OpenCV 识别滑块距离；
4. 模拟人类拖动轨迹；
5. 完成登录和签到；
6. 读取签到统计并通过青龙 `notify.py` 推送报告。

## 青龙任务命令

```text
task laowangfip_browser_checkin/laowangfip_browser_checkin.py
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
| `LWFIP_BASE_URL` | 否 | `https://laowangfip372.vip` | 站点换域名时覆盖 |
| `LWFIP_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `LWFIP_TIMEOUT` | 否 | `30000` | Playwright 超时时间，单位毫秒 |
| `LWFIP_DEBUG` | 否 | `false` | 开启后输出调试信息并保存 `_browser_debug_*.png` 截图 |
| `LWFIP_HEADFUL` | 否 | `false` | 本地调试时设为 `true` 显示浏览器窗口 |
| `LWFIP_CHROMIUM_PATH` | 否 | 自动搜索 | 系统 Chromium 路径，如 `/usr/bin/chromium` |
| `LWFIP_MAX_CAPTCHA_RETRY` | 否 | `5` | 单次滑块识别失败重试次数 |
| `LWFIP_NOTIFY_ONLY_FAIL` | 否 | `false` | `true` 时仅在有账号失败时才推送 |
| `LWFIP_MAX_RETRY` | 否 | `5` | 账号级任务重试次数（仅可重试错误触发，见下文重试策略） |
| `LWFIP_RETRY_INTERVAL` | 否 | `60` | 两次任务重试间隔，单位秒 |
| `LWFIP_COOKIE_CACHE` | 否 | `true` | 登录成功后会话 Cookie 落盘复用，直通签到跳过滑块 |

多账号示例：

```text
LWFIP_ACCOUNTS=user1#pass1&user2#pass2
```

## 依赖安装

### Python 版本

要求 **Python 3.10+**（代码使用了 `int | None` 联合类型语法）。青龙容器自带 Python 3.11，无需处理。

### Python 依赖

```bash
pip install playwright opencv-python-headless numpy
```

### 青龙容器内安装 Chromium

推荐使用系统 Chromium，避免 Playwright 下载浏览器失败：

```bash
docker exec -it qinglong bash
apt update
apt install -y chromium fonts-noto-cjk
pip install playwright opencv-python-headless numpy
```

如果脚本没有自动检测到 Chromium，可设置：

```text
LWFIP_CHROMIUM_PATH=/usr/bin/chromium
```

## 青龙配置示例

```text
LWFIP_ACCOUNTS=your_username#your_password
LWFIP_PROXY=http://172.17.0.1:7890
LWFIP_NOTIFY=true
LWFIP_CHROMIUM_PATH=/usr/bin/chromium
```

## 本地调试示例

Windows PowerShell：

```powershell
$env:LWFIP_ACCOUNTS="your_username#your_password"
$env:LWFIP_PROXY="http://127.0.0.1:7890"
$env:LWFIP_DEBUG="true"
$env:LWFIP_HEADFUL="true"
$env:LWFIP_NOTIFY="false"
python .\laowangfip_browser_checkin.py
```

Linux / macOS：

```bash
export LWFIP_ACCOUNTS="your_username#your_password"
export LWFIP_PROXY="http://127.0.0.1:7890"
export LWFIP_DEBUG=true
export LWFIP_HEADFUL=true
export LWFIP_NOTIFY=false
python laowangfip_browser_checkin.py
```

## 重试策略

账号级重试（`LWFIP_MAX_RETRY` 次，间隔 `LWFIP_RETRY_INTERVAL` 秒固定）：

| 错误类型 | 是否重试 | 示例 |
|---|---|---|
| 网络 / 超时 / 代理 | ✅ | 登录超时、proxy error、连接被拒、浏览器启动失败、导航被中断 / Chromium 错误页（`chrome-error://`） |
| 滑块验证码失败 | ✅ | 验证码识别失败、服务端判定未通过、验证码弹窗未出现 |
| 签到流程异常 | ✅ | 未到达验证页、提交后仍在验证页、签到结果未知、找不到签到按钮 |
| 业务错误 | ❌ | 密码错误、用户名不存在、账号禁用（重试无意义） |

间隔保持固定（不做指数退避）：多账号最坏时长可控（如默认 5 次重试、4 个间隔 × 60 秒 = 4 分钟），避免任务跨 cron 周期。

## 会话 Cookie 复用

登录成功后脚本把会话 Cookie 保存到脚本目录下 `cookie_<哈希>.json`（文件名按账号哈希，不含明文）：

- **当天重试**：账号级重试直接带 Cookie 直通签到，不再重过滑块（滑块是最大失败源）；
- **后续 cron 任务**：Cookie 有效期内（Discuz 会话通常 15-30 天）直接签到，登录频率大幅降低、防风控；
- Cookie 失效自动重新登录并覆盖缓存，无需人工干预；
- 关闭方式：`LWFIP_COOKIE_CACHE=false`。

## 单元测试

```bash
pip install playwright opencv-python-headless numpy
python -m unittest discover -s tests -v
```

覆盖：账号解析、脱敏、签到统计解析、拖动轨迹、重试错误分类、Cookie 缓存读写、滑块识别核心算法（合成图回归测试，定位误差 ≤5px）。

## 执行报告

成功后会输出类似：

```markdown
# 老王FIP签到(浏览器版) - 执行报告

📊 总计 1 账号，✅ 1 / ❌ 0

## 详细结果
- ✅ **[********]** 今日已签到（btnvisted）
  - 签到统计：连续签到: * 天；签到等级: *；积分奖励: *；总天数: * 天
```

推送失败会自动重试 3 次，不影响签到结果。

## 常见问题

### 浏览器启动失败，提示缺少 Chromium？

在青龙容器内安装系统 Chromium：

```bash
apt update && apt install -y chromium
```

然后设置：

```text
LWFIP_CHROMIUM_PATH=/usr/bin/chromium
```

### 页面打不开或超时？

老王FIP 站点需要代理。脚本按 `LWFIP_PROXY` → 青龙全局代理（`HTTPS_PROXY` /
`HTTP_PROXY` / `ALL_PROXY`）的顺序取代理，两者都没有则直连（大概率打不开）。
确认脚本取到的代理在青龙容器内可访问，Docker 青龙常用宿主机地址：

```text
http://172.17.0.1:7890
```

代理软件建议开启**规则模式**：国内域名直连、境外域名走代理。这样青龙里配一个全局代理
就能同时兼顾各脚本，不必逐个脚本单独配置 `LWFIP_PROXY`。

### 报错「Navigation ... is interrupted by another navigation」或「ERR_CONNECTION_CLOSED」？

Chromium 访问站点时连接被关闭，页面跳到内置错误页（`chrome-error://chromewebdata/`）。
这属于可重试的网络/代理抖动，脚本会自动重试，无需人工干预；若频繁出现，请检查
代理（`LWFIP_PROXY` 或青龙全局代理）连通性是否稳定。

### 验证码失败？

脚本内置弹窗自愈：点击 `#tncode` 后 3 秒内弹窗未出现会自动重新点击（最多 3 轮），
避免单次点击无效导致整轮全败。若仍持续失败，先开启：

```text
LWFIP_DEBUG=true
LWFIP_HEADFUL=true
```

本地观察浏览器窗口和 `_browser_debug_*.png` 截图，确认代理、页面和验证码弹窗是否正常。
日志中若出现「弹窗 DOM 已存在但未显示」多为验证码图片在代理下加载失败；
「弹窗 DOM 完全不存在」多为 tncode JS 未加载成功，请检查代理连通性。

### 统计数据为空或只有“天”？

脚本会优先读取 `input.hidnum` 的 `value`。如果站点改版导致 DOM 结构变化，请开启 `LWFIP_DEBUG=true` 并检查签到页的 `ul.countqian.cl` 区域。
