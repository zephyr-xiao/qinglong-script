# 雨云自动签到

基于 **Playwright + Xvfb 有头 Chromium** 的雨云每日签到脚本，支持多账号、点选验证码识别，并可按账号开关服务器自动续费。需要 Playwright 自带的 Chromium 与系统 `Xvfb`。

> **为什么是有头浏览器**：雨云签到提交强制走 Cap（cap.rainyun.cn）人机验证，服务端校验 instrumentation 采集的浏览器行为数据。无头浏览器会被判为自动化；Xvfb 提供虚拟显示让 Chromium 以有头模式运行，才能通过验证。

## 青龙任务命令

```text
task Rainyun/main.py
```

定时建议 `0 9 * * *`（每天 9 点）。用青龙「订阅管理」拉取本仓库时任务会自动创建，无需手动添加。

**退出码语义**：任一账号签到失败时脚本以 `exit 1` 结束，青龙面板会标红；全部成功才返回 0。未解析到任何账号（配置写错）同样按失败处理。失败原因会写进汇总报告与通知（如「未找到每日签到卡片」「签到弹窗内未找到奖励」「人机验证被拦」），便于区分是页面改版还是验证失败。

## 签到流程说明

雨云签到页已改为「卡片 + 弹窗选奖励」形态，脚本流程：

1. 打开赚取积分页，定位「每日签到」卡片头；卡片显示「已完成」则直接判定今日已签（幂等，可重复运行）
2. 点击卡片头打开签到弹窗，读取弹窗标题；显示「今日已签到」同样直接成功
3. 在弹窗里按奖励名点击对应按钮，默认选 **500 积分**（`main.py` 的 `SIGN_REWARD_DEFAULT`）
4. 提交时雨云强制走 Cap（cap.js）人机验证（HashWX PoW + 浏览器行为采集，由页面自身在后台求解）。脚本先静默 25 秒把 CPU 让给采集，再开始轮询，**单次等待上限 90 秒**；未成功则刷新页面重试一次，**单账号最坏约 3 分钟**
5. 判定成功依据：弹窗状态出现「已领取」→ 卡片变「已完成」→ 积分比签到前增加（兜底）

Cap 验证依赖真实浏览器环境。脚本用 **Xvfb + 有头 Chromium** 运行，并注入最小反检测补丁（`navigator.webdriver`、`languages`、`window.chrome`），提交时用带轨迹的拟人鼠标点击。

> **不要注入 `stealth.min.js`**（默认已关闭，`use_stealth_js: false`）。Cap 的 instrumentation 会遍历 21 个原生函数校验 `[native code]` 标记，stealth 大量改写原型链会触发该校验，导致 `instr_timeout` 采集超时。实测：开启 stealth 时稳定超时，关闭后一次通过。

**注意：雨云已在 Cap 上启用行为检测**（challenge 响应里含 `instrumentation` 挑战与 `behavior: true`，阈值 0.75），会采集指针移动与按键节奏。无头浏览器即使通过 PoW，仍可能因行为分不足被拒。若签到失败，日志会打出：

- `🎯 选择奖励: ... (disabled=?, class=?)` —— 按钮是否可点
- `🔍 诊断：捕获到 N 条签到提交记录` 及其 HTTP 状态与响应体
- `🔍 浏览器控制台最近 N 条日志` —— Cap 的具体报错

据此判断是「压根没提交」还是「提交被行为检测拒了」。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `RAINYUN_ACCOUNT` | 是 | - | 账号 JSON 数组，每个子数组为 `[账号, 密码, 续费开关, API Key]`，后两项可省略 |
| `RAINYUN_CONFIG` | 否 | `{}` | 高级配置 JSON 对象，可调键见下表 |

`RAINYUN_ACCOUNT` 子数组字段：

| 位置 | 必填 | 默认 | 说明 |
|---|---|---|---|
| 1 | 是 | - | 雨云登录邮箱/手机号 |
| 2 | 是 | - | 雨云登录密码 |
| 3 | 否 | `false` | 自动续费开关 `true` / `false` |
| 4 | 否 | 空 | 雨云 API Key（续费必需，不续费可留空） |

最小示例：

```text
# 单账号 + 续费
RAINYUN_ACCOUNT=[["user@example.com","password123","true","ryapi_xxxxxxxx"]]

# 多账号，部分续费
RAINYUN_ACCOUNT=[["user1@example.com","pwd1","true","key1"],["user2@example.com","pwd2","false"]]
```

`RAINYUN_CONFIG` 可调键与默认值：

| 键 | 默认 | 说明 |
|---|---|---|
| `timeout` | `20` | 页面加载超时（秒） |
| `max_delay` | `5` | 账号间最大随机延时（分钟） |
| `captcha_retry_limit` | `10` | 验证码重试次数，`-1` 为无限重试 |
| `captcha_total_timeout` | `1800` | 无限重试模式的全局时间上限（秒），防止死循环 |
| `similarity_threshold` | `0.4` | 验证码相似度阈值（0–1） |
| `captcha_upscale_fallback` | `true` | 候选框不足 3 个时把背景图放大 1.5× 重检测并合并去重 |
| `captcha_sift_localize` | `true` | 用 SIFT 在整张背景上定位 ddddocr 漏检的图标，补入候选框 |
| `captcha_template_fallback` | `true` | 多尺度灰度模板匹配补充候选框（与 SIFT 定位并行） |
| `captcha_debug` | `true` | 验证码失败时把背景图/碎片图/标注图/相似度矩阵存到 `captcha_debug/` |
| `captcha_debug_keep` | `10` | 调试图最多保留最近 N 次 |
| `download_max_retries` | `3` | 图片下载重试次数 |
| `download_retry_delay` | `2` | 下载重试间隔（秒） |
| `download_timeout` | `10` | 下载超时（秒） |
| `renew_days` | `7` | 每次续费天数 |
| `renew_threshold_days` | `3` | 剩余天数 ≤ 此值时触发续费 |
| `min_points_reserve` | `5000` | 续费后余额需 ≥ 此值 |
| `points_to_cny_rate` | `2000` | 积分兑换比率（2000 分 = 1 元） |
| `cookie_cache` | `true` | 会话 Cookie 落盘复用（`cookies/rainyun_{hash}.json`），命中则跳过登录 |
| `chrome_binary_path` | 空 | 浏览器路径；留空自动探测 Playwright 自带 Chromium → 系统 Chrome |
| `use_xvfb` | `true` | 用 Xvfb 虚拟显示跑有头浏览器。**必须保持 true**，无头会被 Cap 判为自动化 |
| `limit_cpu_cores` | `true` | 伪装 `hardwareConcurrency=2`，减少 Cap 的并行 PoW worker，给 instrumentation 采集留 CPU |
| `use_stealth_js` | `false` | **建议保持 false**。开启会注入 `stealth.min.js`，但其原型链改写会触发 Cap 的原生函数校验，导致 `instr_timeout` |
| `stealth_js_path` | `./stealth.min.js` | 仅在 `use_stealth_js: true` 时生效 |

## 依赖安装

青龙「依赖管理 → Python」安装（或容器内 pip）：

```bash
pip3 install playwright opencv-python-headless ddddocr requests
```

安装 Playwright 自带的 Chromium（装到青龙数据卷，容器重建不丢）：

```bash
export PLAYWRIGHT_BROWSERS_PATH=/ql/data/.cache/ms-playwright
playwright install chromium
```

系统包（虚拟显示器，有头浏览器必需）：

```bash
apt update && apt install -y xvfb
```

> `xvfb` 是系统包，重建容器会丢。建议写进青龙「依赖管理 → Linux」或 `extra.sh` 持久化。

确认就绪：

```bash
python3 -c "import playwright; print('playwright ok')"
which Xvfb && ls /ql/data/.cache/ms-playwright/
```

## 凭证获取

- 账号密码：即雨云登录凭据，填进 `RAINYUN_ACCOUNT`。
- API Key（续费必需）：登录 [雨云后台](https://app.rainyun.com/) → 用户中心 → API 密钥 → 创建并复制。
- 通知：复用青龙 `notify.py`（推送失败自动重试 3 次），在青龙「通知设置」配一次即可；运行结束会输出汇总报告（各账号积分变化与续费结果）。

## 常见问题

- **验证码识别率低** → 先看 `captcha_debug/<时间戳>/annotated.jpg` 与 `meta.json`，区分「检测框没找到图标」还是「匹配分数偏低」；可降低 `similarity_threshold`（如 0.3）或设 `captcha_retry_limit: -1`。注意这套点选验证码只用于**登录**，签到用的是 Cap PoW。
- **签到失败且日志出现 `instr_timeout` / `Instrumentation timed out`** → Cap 的浏览器行为采集有前端硬编码 20 秒超时。常见原因：① 开了 `use_stealth_js`（原型链改写触发原生校验）② 容器 CPU 太紧张。脚本已内置 25 秒静默期 + 自动重试一次。
- **签到报「未找到每日签到卡片」或「未找到奖励 500 积分」** → 雨云前端再次改版，页面选择器需要更新（`main.py` 顶部 `SIGN_*` 常量）。
- **积分充足但续费失败** → 检查续费后余额是否 ≥ `min_points_reserve`、API Key 是否有效、服务器是否已到期。签到每天约 500 积分，7 天续费约需 2258 积分，注意留足余额。
- **浏览器起不来 / `Missing X server`** → 容器缺 `Xvfb`（`apt install -y xvfb`），或 Playwright 的 Chromium 未装到持久目录（`PLAYWRIGHT_BROWSERS_PATH=/ql/data/.cache/ms-playwright playwright install chromium`）。
- **通知没收到** → 检查青龙 `config.sh` 中变量名（区分大小写）与 Key 是否有效，改完重启容器。
- 多账号串行执行：每个账号前有 0–5 分钟随机延时、账号间隔 3–6 秒，属正常防风控行为。

## 来源

改编自 [SerendipityR-2022/Rainyun-Qiandao](https://github.com/SerendipityR-2022/Rainyun-Qiandao)（经 fatekey、Jielumoon 二次修改）。
