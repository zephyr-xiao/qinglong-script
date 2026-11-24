# gpt.qt.cool 自动签到

基于 Playwright + OpenCV 的 gpt.qt.cool 每日签到续期脚本，自动填 API Key 登录并识别滑动验证码。登录态会缓存复用，站点出现 API 401 时自动清缓存重登；需要 Playwright 自带的 chromium（需单独安装浏览器内核）。

## 青龙任务命令

```text
task gptqtcool_checkin/gptqtcool_checkin.py
```

定时建议 `35 8 * * *`（每天 8:35）。`ql repo` 拉库时会按脚本 docstring 里的 `cron:` 自动建任务。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `GPTQTCOOL_KEY` | 是 | - | 签到网站 API Key |
| `GPTQTCOOL_FORCE_RUN` | 否 | `false` | 忽略「今日已签到」强制执行 |
| `GPTQTCOOL_LOG_LEVEL` | 否 | `info` | `error` / `warn` / `info` / `debug` |
| `GPTQTCOOL_SCREENSHOT_DIR` | 否 | `artifacts` | 截图与验证码调试图输出目录 |
| `GPTQTCOOL_TIMEOUT` | 否 | `30000` | 页面加载超时（毫秒） |
| `GPTQTCOOL_RETRY_ROUNDS` | 否 | `3` | 整轮重试次数（重载页面重来） |
| `GPTQTCOOL_MAX_RUNTIME_MIN` | 否 | `12` | 单次运行总时长上限（分钟） |
| `GPTQTCOOL_NOTIFY` | 否 | `true` | 是否推送通知 |
| `GPTQTCOOL_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅在失败时推送 |
| `GPTQTCOOL_SERVERPUSHKEY` | 否 | 复用 `SERVERPUSHKEY` | Server酱 Turbo KEY |
| `HEADLESS` | 否 | `true` | 无头模式；本地调试设 `false` 可显示窗口 |

单次流程：打开签到页 → 判登录态（必要时登录）→ 判今日已签到 → 点签到并处理滑块 → 以 `/portal/checkin` 返回 `code=0` 视为成功 → 推送通知。

## 依赖安装

```bash
pip install playwright opencv-python numpy
playwright install chromium
playwright install-deps chromium   # 补 libnss3 等系统库
```

浏览器缓存在 `~/.cache/ms-playwright`，容器重启不丢；也可在青龙「依赖管理 → Python」中安装这些依赖（同 `requirements.txt`）。把整个 `gptqtcool_checkin/` 目录放到 `/ql/data/scripts/` 即可，入口文件名不变；`core/` 子包是为了避免顶层 `notify.py` 遮蔽青龙自带同名模块、导致通知兜底失效。

## 凭证获取

- `GPTQTCOOL_KEY`：登录 gpt.qt.cool，在续期页的 `#renewKey` 输入框里使用的 API Key。
- 首次登录成功后，登录态（cookie/localStorage）会存到脚本同目录 `gptqtcool_state.json`；下次命中即跳过「填 Key 登录 + 滑块」，命中后仍用 API 401 计数校验，失效自动清凭证重新登录。怀疑缓存异常可手动删除该文件后重跑。
- 自检（不签到）：`python3 gptqtcool_checkin.py --check`，检查 KEY 是否设置、chromium 能否启动、签到页是否可达、登录态与 401 计数。KEY / Chromium / 页面可达是硬门槛，登录态与 401 计数仅告警（缓存过期属正常，正式运行会自愈）。
- 本地冒烟（KEY 只作环境变量传入，不写入文件）：

  ```bash
  export GPTQTCOOL_KEY="sk-user-xxxx"
  python smoke.py              # 先自检再真跑一次签到
  python smoke.py --check-only # 只自检
  ```

## 常见问题

- **浏览器启动失败** → 确认执行过 `playwright install chromium` 与 `playwright install-deps chromium`；常见缺失库：`libnss3 libatk1.0 libatk-bridge2.0 libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2`。
- **验证码识别失败** → 看 `artifacts/captcha_fail_*.png` 与 `captcha_detect_*.png`（红=首选候选、橙=备选），开 `GPTQTCOOL_LOG_LEVEL=debug` 看检测过程。`请完成人机验证` 是签到失败后的结果文案，不代表验证码还在。
- **缺口定位不准** → 首选判据是缺口处 1px 纯白描边的 52×52 方框；站点 `/auth/captcha` 直接返回缺口行 `y`，二维搜索降为一维。滑动距离必须等于缺口 x（手柄是 `left:2px`，按「背景图左 + 缺口 x − 手柄左」算会稳定少 2px）。
- **页面显示已登录但签到全 401** → 站点前端会用 localStorage 残影渲染「假已登录」UI，纯看页面会误判；脚本监听全部响应，加载期或签到周期内出现新 401 即清 cookie/web storage 后重新登录并重试。
- **首次滑动失败后找不到滑块** → 站点常会隐藏滑块，脚本连续 2 次重定位失败即放弃当前验证码，交外层重新点签到/重载页面拿新题；多候选 + `GPTQTCOOL_RETRY_ROUNDS` 整轮重试就是为这种情况兜底。
- **登录失败** → 核对 `GPTQTCOOL_KEY` 是否过期；站点改版时检查 `#renewKey` 选择器；可用 `GPTQTCOOL_FORCE_RUN=true` 排除「今日已签到」干扰。
- **每次都重复点签到（幂等失效）** → 站点语言切换是 toggle，且点击后会把页面状态重置成「未签到」；脚本必须在切换后重载页面再判状态，日志缺「今日已签到,跳过执行」时先检查这一步。
- **通知未推送** → 通知复用青龙 `notify.py`（Bark/TG/企业微信等），脚本有独立开关；Server酱另有脚本内自实现推送（GET + UA + 超时 + 3 次重试），默认复用青龙 `SERVERPUSHKEY`。
- 运行结束会输出汇总报告，成功通知区分「一次通过 / 重试后通过」，失败通知带失败阶段与截图文件名。
