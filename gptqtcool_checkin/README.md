# gpt.qt.cool 自动签到(青龙面板版)

基于 **Playwright + OpenCV** 的自动签到脚本，定时访问 [gpt.qt.cool](https://gpt.qt.cool/checkin) 完成签到续期，自动识别并处理滑动验证码。

## 功能特点

- 自动登录(填充 API Key，多重登录态判定)
- **登录态持久化复用**：登录成功后将 cookie/localStorage 存为 `gptqtcool_state.json`，下次运行命中则跳过"填 Key 登录 + 滑块验证"直接签到；命中后再用 API 401 计数校验登录态真伪，失效自动回退完整登录
- **登录态 401 自愈**：该站前端会用 localStorage 残影渲染"假已登录"UI(服务端 token 已过期，API 全部 401)，纯看页面会误判；脚本监听全部响应，页面加载期或签到周期内出现新 401 即判定登录态失效，清 cookie/web storage 后重新登录并重试
- 滑动验证码自动识别(高亮方块检测 + OpenCV 模板匹配 + 边缘检测四层递进，含拼图块区域漂移拒绝与多枪连发)
- **多候选缺口定位**：四层检测各自产出候选位置并按可靠度排序，首选失败后改试其它候选（该站点首次滑动失败常隐藏滑块，"首选就对"比"多试几次"更关键）
- **整轮重试**：验证码未通过/结果不明时，重载页面重新签到，最多 `GPTQTCOOL_RETRY_ROUNDS` 轮，并有总时长封顶
- 拟人化滑动轨迹(加减速 + 回退微调 + Y 轴抖动)
- 反自动化检测(navigator.webdriver 隐藏)
- 青龙通知接入：Server酱 自实现推送(GET+UA+超时+3次重试)+ 青龙 `notify.py` 兜底，支持仅在失败时推送；成功通知区分"一次通过/重试后通过"，失败通知带上失败阶段与失败截图文件名
- **`--check` 自检模式**：不执行签到，只验证环境与登录态
- 支持强制执行、日志级别、整轮重试等环境变量配置

## 依赖安装

### 1. 安装 Python 依赖

在青龙面板「依赖管理 → Python」中添加以下依赖，或进容器执行：

```bash
pip install playwright opencv-python numpy
```

### 2. 安装 Playwright 浏览器(必须)

Playwright 需要单独下载 Chromium 浏览器内核，进青龙容器执行一次：

```bash
docker exec -it qinglong bash
playwright install chromium
playwright install-deps chromium   # 安装系统依赖(如缺 libnss3 等)
```

> 安装完成后浏览器缓存在 `~/.cache/ms-playwright`，容器重启不会丢失。

## 环境变量

在青龙面板「环境变量」中添加：

| 变量名 | 必填 | 说明 | 默认值 |
|---|---|---|---|
| `GPTQTCOOL_KEY` | ✅ | 签到网站 API Key | — |
| `GPTQTCOOL_FORCE_RUN` | ❌ | 强制执行(忽略今日已签到) | `false` |
| `GPTQTCOOL_LOG_LEVEL` | ❌ | 日志级别(error/warn/info/debug) | `info` |
| `GPTQTCOOL_SCREENSHOT_DIR` | ❌ | 截图输出目录 | `artifacts` |
| `GPTQTCOOL_TIMEOUT` | ❌ | 页面加载超时(毫秒) | `30000` |
| `GPTQTCOOL_RETRY_ROUNDS` | ❌ | 整轮重试次数(重载页面重来) | `3` |
| `GPTQTCOOL_MAX_RUNTIME_MIN` | ❌ | 单次运行总时长上限(分钟) | `12` |
| `GPTQTCOOL_NOTIFY` | ❌ | 是否推送通知 | `true` |
| `GPTQTCOOL_NOTIFY_ONLY_FAIL` | ❌ | 仅在失败时推送 | `false` |
| `GPTQTCOOL_SERVERPUSHKEY` | ❌ | Server酱 Turbo KEY(默认复用青龙 `SERVERPUSHKEY`) | 复用 `SERVERPUSHKEY` |
| `HEADLESS` | ❌ | 浏览器无头模式(本地调试可设 `false` 显示窗口) | `true` |

## 青龙任务配置

在青龙面板「定时任务」中新建：

| 项 | 值 |
|---|---|
| 名称 | `gpt.qt.cool 签到` |
| 命令 | `task gptqtcool_checkin/gptqtcool_checkin.py`(或 `python3 /ql/data/scripts/gptqtcool_checkin/gptqtcool_checkin.py`) |
| 定时规则 | `35 8 * * *`(每天北京时间早 8 点 35 分) |

> cron 为 5 段式：分 时 日 月 周。`35 8 * * *` 即每天 8:35 执行（与脚本 docstring 内 `cron:` 保持一致，青龙 `ql repo` 拉库时按 docstring 建任务）。

## 项目结构

```
gptqtcool_checkin/
├── gptqtcool_checkin.py   # 入口：docstring(青龙任务元信息) + 依赖检查 + main + --check
├── core/                  # 核心逻辑（子包）
│   ├── config.py          #   配置 / 日志 / 环境变量
│   ├── notify.py          #   通知推送(Server酱 / 青龙 notify.py 兜底)
│   ├── detect.py          #   页面检测 + 缺口定位纯算法(可单测)
│   ├── slider.py          #   滑块轨迹 + 验证码处理
│   ├── auth.py            #   登录 / 会话持久化
│   └── flow.py            #   主流程 / 结果判定 / 通知文案 / 自检
├── tests/                 # pytest 单测(纯函数)
├── smoke.py               # 本地冒烟脚本(非青龙环境跑一次真实流程)
├── requirements.txt       # Python 依赖
├── README.md              # 本文档
└── .gitignore
```

> **为什么放在 `core/` 子包**：脚本目录会进 `sys.path[0]`。若在顶层放一个 `notify.py`，`from notify import send` 会命中本包而非青龙自带的 `notify.py`，导致通知兜底失效。子包内的模块名只在 `core.` 命名空间下解析，不会遮蔽青龙顶层模块。
>
> **部署**：把整个 `gptqtcool_checkin/` 目录拷贝到 `/ql/data/scripts/` 即可，入口文件名不变（`task gptqtcool_checkin/gptqtcool_checkin.py` 照旧）。

## 工作流程

```
启动 Chromium → 注入反检测脚本 → 挂 401 响应监听 → 访问签到页
  → 切中文 → **重载页面**(拿回被语言切换点击重置的签到状态) → 判登录态
  → (已登录:复用持久化登录态,再等 3s 校验加载期无 401)
  → (未登录或有 401:清凭证 → 填 KEY 登录,3 次重试,成功后落盘登录态)
  → 检查今日已签到(默认命中即幂等跳过;GPTQTCOOL_FORCE_RUN=true 可跳过该检查)
  → 整轮重试循环(最多 GPTQTCOOL_RETRY_ROUNDS 轮,总时长封顶 MAX_RUNTIME_MIN 分钟):
      点签到按钮(三级选择器回退)
      → 处理滑块验证码(内层 2 轮)
        → 缺口候选:高亮方块(带限) → 高亮方块(全图) → 模板匹配 → 边缘检测
        → 按可靠度排序,首选候选 + 小偏移扫描;失败改试其它候选
      → 验证码通过则直接刷新页面判定(页面内按钮不即时更新,省去 18s 空转)
      → 判结果(按钮文本 / 日历标记);未确认再刷新兜底;仍失败则重载页面进入下一轮
  → 推送通知(Server酱 自实现 / notify.py 兜底)
```

## 自检与本地冒烟

### 自检(不签到)

```bash
python3 gptqtcool_checkin.py --check
```

只验证：`GPTQTCOOL_KEY` 是否设置、Chromium 能否启动、签到页是否可达、登录态与 401 计数。
KEY / Chromium / 页面可达 为硬门槛（❌ 表示需排查）；登录态与 401 计数仅告警（⚠️，缓存过期时属正常，正式运行会自愈）。

### 本地冒烟

```bash
export GPTQTCOOL_KEY="sk-user-xxxx"   # 只作环境变量，不写入任何文件
python smoke.py                       # 先自检，再真跑一次签到
python smoke.py --check-only          # 只自检
HEADLESS=false python smoke.py        # 显示浏览器窗口
```

> 冒烟会真实访问站点；若今日已签到会幂等跳过（除非 `GPTQTCOOL_FORCE_RUN=true`）。

### 单元测试

```bash
pip install pytest            # 开发依赖，不在 requirements.txt 内(青龙无需安装)
python -m pytest tests -q
```

覆盖纯函数：环境变量解析、拟人化轨迹、缺口检测与多候选排序（含"带限漏检→全图补位"回归）。

## 故障排除

### 浏览器启动失败

- 确认已执行 `playwright install chromium`
- 确认系统依赖已装：`playwright install-deps chromium`
- 常见缺失库：`libnss3 libatk1.0 libatk-bridge2.0 libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2`

### 验证码处理失败

- 查看 `artifacts/captcha_fail_*.png` 失败截图与 `captcha_detect_*.png` 检测图（红=首选候选，橙=备选候选）
- 网站可能更新了验证码机制，需更新选择器或识别逻辑
- 调高日志级别(`GPTQTCOOL_LOG_LEVEL=debug`)查看详细检测过程
- 缺口定位相关（2026-08 修复 + 2026-10 增强）：
  - 该站点缺口为**白色半透明方块标注**（拼图块图案与背景图无内容对应），默认走高亮方块检测；低分时回退模板匹配/边缘检测
  - **2026-10 增强**：分析 2026-08-14 的三次真实失败发现——带限高亮漏检时旧代码回退到边缘检测，锁在路面结构上偏 52~75px，而**高亮全图扫描能稳定命中真缺口**。现改为四层检测各自产出候选并按可靠度排序（高亮带限 → 高亮全图 → 模板 → 边缘），首选失败后改试备选；真实样本回放三次失败全部命中（Δ=0）
  - 边缘检测的宽度参数应等于**拼图块宽度**（如 52px），传滑块宽度会导致定位偏差数十像素
  - 模板匹配只用 `TM_CCOEFF_NORMED`（`CCORR` 在海水纹理上置信度虚高 0.9+，失去区分度），采信阈值 0.85，命中拼图块自身区域的结果会被拒绝
  - 每次滑动后验证码 DOM 可能刷新，脚本会自动重新定位滑块，避免旧 locator 阻塞 30 秒超时
  - 若首次滑动失败导致站点隐藏滑块（切到"人机验证"），检测到连续 2 次重定位失败会立即放弃当前验证码并刷新重试

### 登录失败

- 确认 `GPTQTCOOL_KEY` 正确且未过期
- 网站可能改版，检查 `#renewKey` 输入框选择器是否仍有效
- 手动触发 `GPTQTCOOL_FORCE_RUN=true` 排除「今日已签到」干扰
- 登录态缓存失效(含"页面显示已登录但 API 全 401"的假登录态)会自动清除并重新登录；若怀疑缓存异常，可手动删除脚本同目录 `gptqtcool_state.json` 后重跑

### 幂等跳过（今日已签到）相关

- 该站语言切换按钮是 **toggle**（点一次在中文/英文之间来回跳），且**点击后会把页面签到状态重置成"未签到"文案、不会自行恢复**（2026-10-11 实测）。若在切换后立刻做幂等检查，会误判成"未签到"而重复点签到。
- 因此脚本在 `switch_to_chinese_if_needed` 之后**重载一次页面**再判登录态/幂等，拿回真实状态。若看到日志里"今日已签到,跳过执行"缺失、每次运行都点签到，先确认这一步重载是否还在。
- 注意：页面内的签到按钮在**签到成功后不会即时更新**，只有重载才显示"✓ 今日已签到"。因此脚本在**验证码通过后直接刷新页面判定**（不再空转轮询 18s），其余情况才走"轮询 → 刷新兜底"。

### 通知未推送

- 确认 `GPTQTCOOL_NOTIFY` 未被设为 `false`
- 若 `GPTQTCOOL_NOTIFY_ONLY_FAIL=true`，签到成功时不推送属正常行为
- **Server酱 为脚本自实现推送**(GET+浏览器 UA+超时+3 次重试)，直接复用青龙 `SERVERPUSHKEY`(或独立变量 `GPTQTCOOL_SERVERPUSHKEY`)。脚本会屏蔽青龙 `notify.py` 的旧 serverJ 渠道，避免重复推送与旧实现(无超时/无重试的异步子线程)网络抖动即丢通知的问题
- 青龙面板「通知设置」中配置 Bark/Telegram/企业微信等渠道，走 `notify.py` 兜底
- 非青龙环境找不到 `notify.py` 会降级为日志输出(显示 `[通知内容]`)

## 与原 Node.js 版本的差异

本脚本由原 `checkin.js`(Node.js + Playwright + Jimp)迁移为 Python 版，并修复了以下问题：

| 问题 | 修复方式 |
|---|---|
| `jimp` 未声明依赖 | 改用 OpenCV/numpy，不再依赖 jimp |
| `addInitScript` 在 goto 之后执行 | 改为 context 级，在 goto 之前注入 |
| Python OpenCV 子进程调用 + 未安装 | 原生 `import cv2`，无子进程 |
| `checkin.log` 不存在 | 用 logging 输出 stdout，青龙自动捕获 |
| `GPTQTCOOL_FORCE_RUN`/`GPTQTCOOL_LOG_LEVEL` 未接入 | 完整接入环境变量 |
| 登录重试无等待 | 每轮统一 `sleep(1.5)` |
| 验证码选择器过宽(误判) | 删除 `div[style*="position: absolute"][style*="left"]` |
| 日历 `text="${today}"` 误匹配 | 限定 `.ci-cal-day` 容器内查找 |
| 页面 console 日志噪音 | 只打印 warning/error 级别 |
| `ddddocr` 死代码 | 移除(滑块验证码无需 OCR) |

## 注意事项

1. 本脚本仅用于学习研究，请确保使用符合相关网站服务条款。
2. 网站结构变化可能导致脚本失效，需检查更新选择器和逻辑。
3. 滑动验证码识别非 100% 成功，脚本含多候选 + 整轮重试机制提高成功率。
