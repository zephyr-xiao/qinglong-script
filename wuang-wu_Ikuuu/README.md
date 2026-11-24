# iKuuu 自动签到（Node.js）

青龙面板运行的 ikuuu 机场签到脚本：平时走纯 HTTP 签到，会话 Cookie 缓存在 `.token/` 下（有效期 24 小时）；首次运行或缓存失效时用 Playwright 自动登录，Geetest V4 点选验证码交给外部解法器处理。面板域名从发布页自动解析并在主/备域名间轮换。

## 青龙任务命令

```text
task wuang-wu_Ikuuu/ikuuu.js
```

定时建议 `8 8 * * *`（随脚本头部 `@cron`）；想更抗挂可再加一条 `8 20 * * *` 补签（已签到会被识别为成功，重跑无副作用）。首次运行会完整登录，约 1~4 分钟；之后 24 小时内复用 `.token/` 缓存，几秒跑完。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `ACCOUNTS` | 与 `IKUUU_COOKIE` 二选一 | - | 账密列表，格式 `邮箱#密码`，多账号用 `&` 或换行分隔；按首个 `#` 切分，密码可含 `#` 但避免 `&` 和换行。Cookie 失效自动重登 |
| `IKUUU_COOKIE` | 与 `ACCOUNTS` 二选一 | - | cookie 字符串直填，多账号用换行分隔；每行必须含 `uid=`，失效只能手工更新（此模式不需要 playwright） |
| `IKUUU_SOLVER_CMD` | 站点下发点选时必填 | - | 点选验证码外部解法器，如 `node solver_vlm.js`；脚本自身不做图形识别 |
| `IKUUU_SOLVER_CMD_FALLBACK` | 否 | - | 备用解法器：主解法器不可用（key 失效 / 网关不通 / 超时）时改用 |
| `IKUUU_RETRY_TIMES` | 否 | `0` | 失败账号延迟重试次数，建议 `1~2`，兜限流/网关抖动这类几分钟自愈的故障 |
| `IKUUU_RETRY_DELAY` | 否 | `300` | 每次重试前等待秒数（最小 `10`） |
| `IKUUU_VLM_BASE_URL` | 用视觉解法器时必填 | - | OpenAI 兼容网关地址，如 `http://<网关>:3000/v1`；脚本内不内置任何网关 |
| `IKUUU_VLM_API_KEY` | 用视觉解法器时必填 | - | 网关 API Key（`sk-...`） |
| `IKUUU_VLM_MODEL` | 用视觉解法器时必填 | - | 视觉模型名，脚本内不内置；逗号可给多个，前一个不可用时自动换下一个 |
| `IKUUU_VLM_TIMEOUT` | 否 | `120` | 单次模型请求超时（秒） |
| `IKUUU_VLM_429_BACKOFF` | 否 | `20,60` | 429 退避秒数（逗号分隔，单位秒）；服务端给 `Retry-After` 时优先（夹在 5~90 秒），`0` 表示立即重试一次 |
| `IKUUU_VLM_VOTES` | 否 | `2` | 投票次数：同一张图多问几次，点序先按最近邻拉齐再取中位数，分歧过大则放弃本次重抽 |
| `IKUUU_VLM_ZOOM` | 否 | `3` | 合成图放大倍数（1~6），越大越清晰但更费 token |
| `IKUUU_VLM_MAX_TOKENS` | 否 | `4000` | 单次回复上限。不要随意调大：`max_tokens` 被网关当作输出预留计入每分钟额度，实测 8000 即 429；答案仅约 1.3k token，放大时夹在 60000 内 |
| `IKUUU_VLM_MODEL_ERROR_HINT` | 否 | `model_not_found,No available channel,invalid_request_error` | 网关报错含这些字样即判模型/渠道故障并换下一个模型；置空关闭该判定 |
| `IKUUU_VLM_DEBUG` | 否 | 关 | `1` = 把合成图与模型原始回复（含思考过程）写到 `_vlm_debug/` |
| `IKUUU_SOLVER_TIMEOUT` | 否 | 自动推导 | 解法器超时秒数；留空按 `票数 × (VLM_TIMEOUT + 4 + 429 退避预算) × 模型数 + 30` 推导并夹在 `[180, 600]`（默认单模型 2 票 = 450）。纯 HTTP 打码平台可显式调小 |
| `IKUUU_CAPTCHA_ROUNDS` | 否 | `6` | 点选验证码最多刷新重抽次数（每次刷新重新随机抽题） |
| `IKUUU_CAPTCHA_BUDGET` | 否 | `600` | 验证码阶段总时长预算（秒，最小 `60`），超预算不再重抽、如实报错 |
| `HOST` | 否 | 空 | 强制锁定签到域名（锁定时不轮换）；留空则从发布页抓取，兜底 `ikuuu.top` / `ikuuu.pw` |
| `IKUUU_PUBLISH_URL` | 否 | `https://ikuuu.win/` | 发布页地址，整体迁移域名时改这里 |
| `IKUUU_PROXY` | 否 | 空 | HTTP 代理（ikuuu 被墙，建议配置），如 `http://172.17.0.1:7890`；留空回退青龙全局代理，HTTP 签到与浏览器登录都走它 |
| `IKUUU_HEADFUL` | 否 | 关 | `1` = 登录用有头浏览器（仅本地调试，容器内勿开） |
| `IKUUU_CHROMIUM_PATH` | 否 | 自动探测 | Chromium 路径；留空依次探测 `<青龙数据根>/.cache/ms-playwright/chromium-*`（持久卷）与 `~/.cache/ms-playwright`，取 revision 最大 |
| `IKUUU_NOTIFY_ONLY_FAIL` | 否 | 关 | `1` = 仅失败时推送，留空/`0` = 全部推送 |
| `IKUUU_DEBUG` | 否 | 关 | `1` = 详细调试日志 + 登录过程截屏到 `_ikuuu_debug/` |

最小可用集示例（三个 `IKUUU_VLM_*` 仅视觉解法器需要）：

```text
ACCOUNTS=you@example.com#password123&other@example.com#pw456
IKUUU_SOLVER_CMD=node solver_vlm.js
IKUUU_VLM_BASE_URL=http://<网关>:3000/v1
IKUUU_VLM_API_KEY=sk-xxxxx
IKUUU_VLM_MODEL=<视觉模型>[,备用模型]
```

## 依赖安装

Node ≥ 18（青龙自带版本满足）。容器内执行：

```bash
docker exec -it qinglong bash
cd /ql/data/scripts/wuang-wu_Ikuuu
npm i
./node_modules/.bin/playwright install chromium
```

必须用项目内的 `./node_modules/.bin/playwright`：`npx playwright install chromium` 会临时下载最新版 Playwright，装出的浏览器 revision 与项目锁定版本对不上，脚本照样报 `Executable doesn't exist`。建议把浏览器放持久卷：

```bash
mkdir -p /ql/data/.cache/ms-playwright
mv /home/qinglong/.cache/ms-playwright/chromium-* /ql/data/.cache/ms-playwright/
```

青龙镜像不自带 Chromium 运行库和中文字体（验证码合成图需要中文字体），容器重建后仍需补装。可把 `apt-get update && apt-get install -y --no-install-recommends chromium fonts-wqy-zenhei` 写进 `data/config/extra.sh` 随容器启动执行。浏览器未就绪时纯 HTTP 签到照常可跑，只在需要登录时报引导性错误。

## 凭证获取

- 账密模式：直接填 `ACCOUNTS`，脚本自行登录并把 Cookie 写入 `.token/`（只存 Cookie 与到期时间，不存密码）。
- 视觉解法器（推荐）：把本目录 `solver_vlm.js` 挂为 `IKUUU_SOLVER_CMD`，配好三个 `IKUUU_VLM_*` 变量后自检（全部通过才算配好）：

  ```bash
  docker exec -it qinglong bash -lc 'cd /ql/data/scripts/wuang-wu_Ikuuu && node solver_vlm.js --check'
  ```

  自检会真的发一张测试图问模型；`docker exec` 的 shell 不带面板环境变量，需要临时加在命令前。只有需要重新登录时才会调用解法器，纯 HTTP 签到不消耗 token。
- Cookie 直填：在能过验证码的浏览器里登录后 F12 复制 Cookie（必须含 `uid=`），24 小时需手工更新一次。
- 自写解法器协议：stdin 收 `{"type","promptText","imagePath","tplPaths","imageSize","grid"}`，stdout 返回 `{"ok":true,"clicks":[[x,y],...]}`（失败为 `{"ok":false,"msg":"..."}`）；坐标用 `imageSize` 像素系，`tplPaths` 顺序即点击顺序。
- 推送复用目录内 `sendNotify.js`，通道变量（`DD_BOT_TOKEN` / `BARK_PUSH` / `PUSH_KEY` / `TG_BOT_TOKEN` / `QYWX_KEY` 等）与青龙官方 Notify 一致。

## 常见问题

- **`Executable doesn't exist ...chromium-XXXX`** → 用 `./node_modules/.bin/playwright install chromium` 重装（别用 npx），或设 `IKUUU_CHROMIUM_PATH` 指向任意 Chromium，或把浏览器放进 `/ql/data/.cache/ms-playwright`。
- **`未配置 IKUUU_VLM_BASE_URL / IKUUU_VLM_API_KEY / IKUUU_VLM_MODEL`** → 视觉解法器必填项缺失，补齐后先跑 `node solver_vlm.js --check`。
- **`解法器不可用（非题目本身问题），停止重抽`** → 配置/认证类故障（key 失效、模型名不存在、命令路径写错），换题无用故立即失败、退出码 1；限流 429 与超时属瞬时故障，会自动换抽取。
- **429 / `inference exceeds tpm/rpm limit`** → 先确认 `IKUUU_VLM_MAX_TOKENS` 没被调大（默认 4000）；仍反复出现说明渠道拥挤，加备用模型、把 `IKUUU_VLM_VOTES` 降到 1、配 `IKUUU_RETRY_TIMES` 或换网关。
- **`解法器超时` / `解法器输出无法解析`** → 日志已打出 `最终坐标` 说明算出来但太慢，调大 `IKUUU_SOLVER_TIMEOUT` 或调小 `IKUUU_VLM_TIMEOUT` / `IKUUU_VLM_VOTES`；超时会终止整棵进程树。
- **`HTTP 405 该域名不是签到面板` / 发布页抓取失败** → 自动降级到 `ikuuu.top` → `ikuuu.pw`；仍失败就配 `IKUUU_PROXY` 或用 `HOST` 锁定。发布页域名（`ikuuu.win` / `ikuuu.eu`）永不作为签到目标。
- **`N 个点只覆盖 M 个格子`** → 九宫格题里模型把多个点给到同一格；行列数按格子几何还原（日志会打印 `九宫格 3×3`），频繁出现可换更强的视觉模型或调大 `IKUUU_VLM_VOTES`。
- **登录页打不开 / 密码错误 / 2FA / 邮箱验证码** → 分别检查代理与页面结构、核对 `ACCOUNTS`、关闭两步验证或调整账号安全设置（脚本无法代填）。
- 退出码：`0` = 全部成功或部分失败（青龙绿色），`1` = 全部失败或主流程异常（青龙红色）。

## 来源

改编自 [wuang-wu/Ikuuu](https://github.com/wuang-wu/Ikuuu)。
