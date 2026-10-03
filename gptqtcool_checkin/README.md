# gpt.qt.cool 自动签到(青龙面板版)

基于 **Playwright + OpenCV** 的自动签到脚本,定时访问 [gpt.qt.cool](https://gpt.qt.cool/checkin) 完成签到续期,自动识别并处理滑动验证码。

## 功能特点

- 自动登录(填充 API Key,多重登录态判定)
- **登录态持久化复用**:登录成功后将 cookie/localStorage 存为 `gptqtcool_state.json`,下次运行命中则跳过"填 Key 登录 + 滑块验证"直接签到;命中后再用 API 401 计数校验登录态真伪,失效自动回退完整登录
- **登录态 401 自愈**:该站前端会用 localStorage 残影渲染"假已登录"UI(服务端 token 已过期,API 全部 401),纯看页面会误判;脚本监听全部响应,页面加载期或签到周期内出现新 401 即判定登录态失效,清 cookie/web storage 后重新登录并重试签到一次
- 滑动验证码自动识别(高亮方块检测 + OpenCV 模板匹配 + 边缘检测四层递进,含拼图块区域漂移拒绝与多枪连发)
- 多枪连发滑动策略(按检测方法分档偏移扫描)
- 拟人化滑动轨迹(加减速 + 回退微调 + Y 轴抖动)
- 反自动化检测(navigator.webdriver 隐藏)
- 青龙通知接入:Server酱 自实现推送(GET+UA+超时+3次重试)+ 青龙 `notify.py` 兜底,支持仅在失败时推送
- 支持强制执行、日志级别等环境变量配置

## 依赖安装

### 1. 安装 Python 依赖

在青龙面板「依赖管理 → Python」中添加以下依赖,或进容器执行:

```bash
pip install playwright opencv-python numpy
```

### 2. 安装 Playwright 浏览器(必须)

Playwright 需要单独下载 Chromium 浏览器内核,进青龙容器执行一次:

```bash
docker exec -it qinglong bash
playwright install chromium
playwright install-deps chromium   # 安装系统依赖(如缺 libnss3 等)
```

> 安装完成后浏览器缓存在 `~/.cache/ms-playwright`,容器重启不会丢失。

## 环境变量

在青龙面板「环境变量」中添加:

| 变量名 | 必填 | 说明 | 默认值 |
|---|---|---|---|
| `GPTQTCOOL_KEY` | ✅ | 签到网站 API Key | — |
| `GPTQTCOOL_FORCE_RUN` | ❌ | 强制执行(忽略今日已签到) | `false` |
| `GPTQTCOOL_LOG_LEVEL` | ❌ | 日志级别(error/warn/info/debug) | `info` |
| `GPTQTCOOL_SCREENSHOT_DIR` | ❌ | 截图输出目录 | `artifacts` |
| `GPTQTCOOL_TIMEOUT` | ❌ | 页面加载超时(毫秒) | `30000` |
| `GPTQTCOOL_NOTIFY` | ❌ | 是否调用青龙 `notify.py` 推送 | `true` |
| `GPTQTCOOL_NOTIFY_ONLY_FAIL` | ❌ | 仅在失败时推送 | `false` |
| `GPTQTCOOL_SERVERPUSHKEY` | ❌ | Server酱 Turbo KEY(默认复用青龙 `SERVERPUSHKEY`) | 复用 `SERVERPUSHKEY` |
| `HEADLESS` | ❌ | 浏览器无头模式(本地调试可设 `false` 显示窗口) | `true` |

## 青龙任务配置

在青龙面板「定时任务」中新建:

| 项 | 值 |
|---|---|
| 名称 | `gpt.qt.cool 签到` |
| 命令 | `task gptqtcool_checkin/gptqtcool_checkin.py`(或 `python3 /ql/data/scripts/gptqtcool_checkin/gptqtcool_checkin.py`) |
| 定时规则 | `35 8 * * *`(每天北京时间早 8 点 35 分) |

> cron 为 5 段式:分 时 日 月 周。`35 8 * * *` 即每天 8:35 执行（与脚本 docstring 内 `cron:` 保持一致，青龙 `ql repo` 拉库时按 docstring 建任务）。

## 项目结构

```
gptqtcool_checkin/
├── gptqtcool_checkin.py          # 主脚本
├── requirements.txt    # Python 依赖
├── README.md           # 本文档
└── .gitignore
```

## 工作流程

```
启动 Chromium → 注入反检测脚本 → 挂 401 响应监听 → 访问签到页
  → 切中文 → 判登录态 → (已登录:复用持久化登录态,再等 3s 校验加载期无 401)
  → (未登录或有 401:清凭证 → 填 KEY 登录,3 次重试,成功后落盘登录态)
  → 检查今日已签到(GPTQTCOOL_FORCE_RUN 可跳过)
  → 点签到按钮(三级选择器回退) → 处理滑块验证码(4 轮)
    → 缺口检测:高亮方块(积分图亮度异常,该站点默认形态) → 模板匹配(CCOEFF,拼图内容型) → 边缘检测
  → 判结果(按钮文本 / 日历标记)
  → (结果不明确且本轮出现新 401:自愈重登后重试签到一次)
  → 推送通知(Server酱 自实现 / notify.py 兜底)
```

## 故障排除

### 浏览器启动失败

- 确认已执行 `playwright install chromium`
- 确认系统依赖已装:`playwright install-deps chromium`
- 常见缺失库:`libnss3 libatk1.0 libatk-bridge2.0 libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2`

### 验证码处理失败

- 查看 `artifacts/captcha_fail_*.png` 失败截图定位问题
- 网站可能更新了验证码机制,需更新选择器或识别逻辑
- 调高日志级别(`GPTQTCOOL_LOG_LEVEL=debug`)查看详细检测过程
- 缺口定位偏移时重点检查(2026-08 修复):
  - 该站点缺口为**白色半透明方块标注**(拼图块图案与背景图无内容对应),默认走高亮方块检测(积分图滑窗亮度异常);低分时判定为拼图内容型验证码回退模板匹配
  - 边缘检测的宽度参数应等于**拼图块宽度**(如 52px),传滑块宽度会导致定位偏差数十像素
  - 模板匹配只用 TM_CCOEFF_NORMED(CCORR 在海水纹理上置信度虚高 0.9+,失去区分度),采信阈值 0.85,命中拼图块自身区域的结果会被拒绝并回退边缘检测
  - 每次滑动后验证码 DOM 可能刷新,脚本会自动重新定位滑块,避免旧 locator 阻塞 30 秒超时(多枪连发因此可真正执行)
  - 若首次滑动失败导致站点隐藏滑块(切到"人机验证"),脚本检测到连续 2 次重定位失败会立即放弃当前验证码并刷新重试,避免多枪连发空转约 1.5 分钟(2026-08-14 优化)

### 登录失败

- 确认 `GPTQTCOOL_KEY` 正确且未过期
- 网站可能改版,检查 `#renewKey` 输入框选择器是否仍有效
- 手动触发 `GPTQTCOOL_FORCE_RUN=true` 排除「今日已签到」干扰
- 登录态缓存失效(含"页面显示已登录但 API 全 401"的假登录态)会自动清除并重新登录;若怀疑缓存异常,可手动删除脚本同目录 `gptqtcool_state.json` 后重跑
- 2026-09-26 修复:曾出现"假登录"失败——页面 UI 显示已登录(前端靠 localStorage 残影渲染),但服务端 token 已过期,`/portal/check` 等鉴权接口全部 401,签到 POST 被拒后轮询超时误报失败;现通过 401 响应监听 + 清凭证自愈重登覆盖该场景

### 通知未推送

- 确认 `GPTQTCOOL_NOTIFY` 未被设为 `false`
- 若 `GPTQTCOOL_NOTIFY_ONLY_FAIL=true`,签到成功时不推送属正常行为
- **Server酱 为脚本自实现推送**(GET+浏览器 UA+超时+3 次重试),直接复用青龙 `SERVERPUSHKEY`(或独立变量 `GPTQTCOOL_SERVERPUSHKEY`)。脚本会屏蔽青龙 `notify.py` 的旧 serverJ 渠道,避免重复推送与旧实现(无超时/无重试的异步子线程)网络抖动即丢通知的问题
- 青龙面板「通知设置」中配置 Bark/Telegram/企业微信等渠道,走 `notify.py` 兜底
- 非青龙环境找不到 `notify.py` 会降级为日志输出(显示 `[通知内容]`)

## 与原 Node.js 版本的差异

本脚本由原 `checkin.js`(Node.js + Playwright + Jimp)迁移为 Python 版,并修复了以下问题:

| 问题 | 修复方式 |
|---|---|
| `jimp` 未声明依赖 | 改用 OpenCV/numpy,不再依赖 jimp |
| `addInitScript` 在 goto 之后执行 | 改为 context 级,在 goto 之前注入 |
| Python OpenCV 子进程调用 + 未安装 | 原生 `import cv2`,无子进程 |
| `checkin.log` 不存在 | 用 logging 输出 stdout,青龙自动捕获 |
| `GPTQTCOOL_FORCE_RUN`/`GPTQTCOOL_LOG_LEVEL` 未接入 | 完整接入环境变量 |
| 登录重试无等待 | 每轮统一 `sleep(1.5)` |
| 验证码选择器过宽(误判) | 删除 `div[style*="position: absolute"][style*="left"]` |
| 日历 `text="${today}"` 误匹配 | 限定 `.ci-cal-day` 容器内查找 |
| 页面 console 日志噪音 | 只打印 warning/error 级别 |
| `ddddocr` 死代码 | 移除(滑块验证码无需 OCR) |

## 注意事项

1. 本脚本仅用于学习研究,请确保使用符合相关网站服务条款。
2. 网站结构变化可能导致脚本失效,需检查更新选择器和逻辑。
3. 滑动验证码识别非 100% 成功,脚本含多轮重试机制提高成功率。
