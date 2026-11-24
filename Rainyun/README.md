# 雨云自动签到

基于 Selenium + ddddocr 的雨云每日签到脚本，支持多账号、点选验证码识别，并可按账号开关服务器自动续费。需要 Chrome/Chromium ≥ 109 与配套 chromedriver，目录内还需 `stealth.min.js`。

## 青龙任务命令

```text
task Rainyun/main.py
```

定时建议 `0 9 * * *`（每天 9 点）。用青龙「订阅管理」拉取本仓库时任务会自动创建，无需手动添加。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `RAINYUN_ACCOUNT` | 是 | - | 账号 JSON 数组，每个子数组为 `[账号, 密码, 续费开关, API Key]`，后两项可省略 |
| `RAINYUN_CONFIG` | 否 | `{}` | 高级配置 JSON 对象，可调键见下表 |
| `CHROMEDRIVER_PATH` | 否 | 空（自动探测） | chromedriver 路径；也可用 `RAINYUN_CONFIG.chrome_driver_path` 指定 |

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
| `cookie_cache` | `true` | 会话 Cookie 落盘复用（`cookies/rainyun_{hash}.json`），命中则跳过登录与验证码；注入前先访问首页落到目标域，否则 `add_cookie` 报 invalid cookie domain |
| `stealth_js_path` | `./stealth.min.js` | 反检测脚本路径（相对/绝对） |
| `chrome_driver_path` | 空 | chromedriver 路径；留空探测顺序：`CHROMEDRIVER_PATH` → PATH → 青龙固定路径 → Selenium Manager |

## 依赖安装

青龙「依赖管理 → Python」安装（或容器内 pip）：

```bash
pip3 install selenium opencv-python-headless ddddocr requests
```

系统包（安装 Chrome/Chromium 与 ChromeDriver）：

```bash
apt update && apt install -y chromium-driver
```

Chrome/Chromium 必须 ≥ 109，过低会报 `DevToolsActivePort file doesn't exist`。先确认版本：

```bash
for b in chromedriver chromium chromium-browser google-chrome google-chrome-stable; do
  command -v "$b" >/dev/null 2>&1 && { echo "--- $b ---"; "$b" --version | head -1; }
done
```

版本过老或未安装时，可从 Chrome for Testing 下载同版本 chrome + chromedriver 到 `/ql/data/scripts/Rainyun/chrome-for-testing/`（青龙数据卷，容器重建不丢），再软链到 `/usr/local/bin/google-chrome` 与 `/usr/local/bin/chromedriver`。最新 Stable 报 `GLIBC_2.x not found` 时改用固定老版本号（如 `128.0.6613.84`）。

`stealth.min.js` 必须放在 `main.py` 同目录（或用 `stealth_js_path` 指定），可从 [puppeteer-extra 仓库](https://raw.githubusercontent.com/berstend/puppeteer-extra/master/packages/puppeteer-extra-plugin-stealth/evasions/stealth.min.js) 下载。

## 凭证获取

- 账号密码：即雨云登录凭据，填进 `RAINYUN_ACCOUNT`。
- API Key（续费必需）：登录 [雨云后台](https://app.rainyun.com/) → 用户中心 → API 密钥 → 创建并复制。
- 通知：复用青龙 `notify.py`（推送失败自动重试 3 次），在青龙「通知设置」配一次即可；运行结束会输出汇总报告（各账号积分变化与续费结果）。

## 常见问题

- **验证码识别率低** → 先看 `captcha_debug/<时间戳>/annotated.jpg` 与 `meta.json`，区分「检测框没找到图标」还是「匹配分数偏低」；可降低 `similarity_threshold`（如 0.3）或设 `captcha_retry_limit: -1`。
- **积分充足但续费失败** → 检查续费后余额是否 ≥ `min_points_reserve`、API Key 是否有效、服务器是否已到期。签到每天约 500 积分，7 天续费约需 2258 积分，注意留足余额。
- **`session not created: DevToolsActivePort file doesn't exist`** → Chrome < 109、chromedriver 与 Chrome 主版本不一致、缺系统库（`ldd $(which chromium) | grep "not found"`）、僵尸进程（`pkill -9 -f chromedriver`），或 `/tmp` 满/挂载 noexec。
- **通知没收到** → 检查青龙 `config.sh` 中变量名（区分大小写）与 Key 是否有效，改完重启容器。
- 多账号串行执行：每个账号前有 0–5 分钟随机延时、账号间隔 3–6 秒，属正常防风控行为。

## 来源

改编自 [SerendipityR-2022/Rainyun-Qiandao](https://github.com/SerendipityR-2022/Rainyun-Qiandao)（经 fatekey、Jielumoon 二次修改）。
