# 采蘑菇论坛自动回帖签到

适配青龙面板的采蘑菇论坛（caimogu.cc）自动回帖脚本。采蘑菇没有按钮式签到，活跃度 = 当天在板块内的有效回复数，因此脚本用 **Playwright 驱动真实浏览器**，每天在指定板块回复若干帖子获取活跃度。

评论生成采用 **AI / 模板双模式**：配了 API Key 走 AI 生成自然评论，没配则用本地模板兜底。所有逻辑（评论生成、REPLY/SKIP 判定、反套话过滤）与 Windows 版 `caimogu_signin.py` 一致。

## 功能特点

- **AI / 模板双模式评论生成**：OpenAI 兼容接口（DeepSeek 等），prompt 强约束口语化、禁套话、限 40 字内；请求最多重试 3 次（含首试），超时/网络错误/空返回均重试，全失败回退模板；
- **反套话词库三层**：硬禁词全局过滤、弱禁词仅拦开头、长度钳制 15-40 字，避免评论"一眼机器人"；
- **REPLY/SKIP 判定**：自动跳过水帖、签到帖、置顶帖、圈规帖，不给不适合的帖子硬凑回复；
- **关键词提取优先级**：书名号/引号 → 游戏名库 → 复合模式 → 最长中文片段；
- **防重复回帖**：`replied_posts.json` 记录当天已回帖 ID + 数量，断点续跑只补差额、中断不重复回同一帖；
- **拟人化**：随机延迟 8-20s、模板随机抽取 + 同义词洗牌；
- 登录态失效自动检测，明确提示重新导入 Cookie；
- 支持青龙 `notify.py` 推送，标题按成败分档。

## 青龙任务命令

```text
task caimogu_checkin/caimogu_checkin.py
```

建议定时：

```text
8 8 * * *
```

（每天 8 点执行，与脚本头部 `cron:` 一致）

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `CAIMOGU_COOKIE` | 否* | - | 登录 Cookie 字符串（优先凭证，见下方获取方式） |
| `CAIMOGU_AUTH_FILE` | 否* | 脚本同目录 `auth_state.json` | Playwright storage_state 登录态文件路径（回退凭证） |
| `CAIMOGU_ACCOUNTS` | 否 | - | 自动登录账号，格式 `用户名#密码`（手机号/用户名均可），用于 cookie 失效时自动登录；旧写法 `CAIMOGU_USER` + `CAIMOGU_PASSWORD` 仍兼容 |
| `CAIMOGU_CIRCLE_URL` | 否 | `https://www.caimogu.cc/circle/308.html` | 签到板块地址 |
| `CAIMOGU_REPLY_COUNT` | 否 | `3` | 每天回帖数 |
| `CAIMOGU_MIN_DELAY` | 否 | `8` | 回帖最小间隔（秒） |
| `CAIMOGU_MAX_DELAY` | 否 | `20` | 回帖最大间隔（秒） |
| `CAIMOGU_DEEPSEEK_API_KEY` | 否 | 空 | AI 模式 API Key（留空走模板模式） |
| `CAIMOGU_DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com/v1` | AI 接口地址（任意 OpenAI 兼容） |
| `CAIMOGU_DEEPSEEK_MODEL` | 否 | `deepseek-chat` | AI 模型名 |
| `CAIMOGU_AI_TIMEOUT` | 否 | `120` | AI 接口读取超时秒数（连接超时固定 10s） |
| `CAIMOGU_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `CAIMOGU_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅当存在失败时才推送（需 `CAIMOGU_NOTIFY=true`） |
| `CAIMOGU_DEBUG` | 否 | `false` | 关键步骤截屏存 `_caimogu_debug_*.png` |
| `CAIMOGU_HEADFUL` | 否 | `false` | 显示真实浏览器窗口（本地调试用） |
| `CAIMOGU_CHROMIUM_PATH` | 否 | 自动搜索 | chromium 绝对路径 |
| `CAIMOGU_TIMEOUT_MS` | 否 | `90000` | Playwright 页面超时（毫秒） |
| `CAIMOGU_PROXY` | 否 | - | HTTP/SOCKS 代理，留空时自动回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`） |

> *凭据优先级：`CAIMOGU_COOKIE` > `auth_state.json` > 账号密码自动登录（`CAIMOGU_ACCOUNTS`）。配置 `CAIMOGU_ACCOUNTS` 后，即使 cookie 失效脚本也会自动登录并继续回帖。

## Cookie 获取方式

1. 浏览器登录 `https://www.caimogu.cc/`；
2. 按 F12 打开开发者工具 → **Network / 网络**；
3. 刷新页面，任选一个 `/api/` 或页面请求，查看 **Request Headers** 里的 `Cookie` 字段；
4. 把整个 Cookie 字符串（如 `CAIMOGU=xxx; cmg_token=yyy`）填入 `CAIMOGU_COOKIE`。

> Cookie 具有时效性，过期后日志会提示"登录状态已失效"，重新登录并更新环境变量即可，无需改动脚本。

## 依赖安装

青龙容器内执行：

```bash
docker exec -it qinglong bash
pip install playwright
apt update && apt install -y chromium libxss1 libnss3 libgbm1 libasound2
```

脚本会自动搜索 `/usr/bin/chromium`，无需配置 `CAIMOGU_CHROMIUM_PATH`。

## 本地调试

Linux / macOS：

```bash
export CAIMOGU_COOKIE="CAIMOGU=xxx; cmg_token=yyy"
export CAIMOGU_NOTIFY=false
python caimogu_checkin.py
```

Windows PowerShell：

```powershell
$env:CAIMOGU_COOKIE="CAIMOGU=xxx; cmg_token=yyy"
$env:CAIMOGU_NOTIFY="false"
python .\caimogu_checkin.py
```

不启动浏览器、只预览评论生成效果（无副作用）：

```bash
python caimogu_checkin.py --test
```

## 通知

- 青龙环境中自动复用青龙 `notify.py`；
- 设置 `CAIMOGU_NOTIFY=false` 可关闭推送；
- 标题按成败分档：`✅ 完成（N/N）` / `⚠️ 部分完成（M/N）` / `❌ 失败（M/N）`；
- 设置 `CAIMOGU_NOTIFY_ONLY_FAIL=true` 可全部成功时静默不推送；
- 推送失败自动重试最多 3 次；本地运行找不到 `notify.py` 时跳过推送。

## 常见问题

### 评论太生硬？

没配 `CAIMOGU_DEEPSEEK_API_KEY` 时用的是本地模板模式（模板有限，跑久了会撞车）。填上 API Key 自动切换 AI 模式，生成更自然的评论；AI 失败会自动回退模板，不会中断。

### 提示登录状态失效？

Cookie 过期了。有两种处理：
1. 按「Cookie 获取方式」重新登录复制 Cookie，更新 `CAIMOGU_COOKIE` 环境变量。
2. **配置 `CAIMOGU_ACCOUNTS` 后无需手动处理**——脚本检测到 cookie 失效时会自动用账号密码登录（无验证码），登录成功后在同一次运行中继续回帖，并把新登录态持久化到 `auth_state.json` 供后续复用。

> caimogu 对 token 过期并不同步在页面上渲染（回复框、回复按钮照常显示），真正的校验在点击提交时发生：若站点弹出「您还未登录」确认框，脚本会立即识别并中止整轮，或在配置了账号密码后自动登录并重试当前帖。若日志显示「检测到未登录（页面存在登录/注册入口）」，则说明彻底未登录，脚本会在开始阶段直接退出。

### 容器里报 Executable doesn't exist？

未安装 chromium。按「依赖安装」执行 `apt install -y chromium`，或设置 `CAIMOGU_CHROMIUM_PATH` 指向浏览器绝对路径。

### 某一天回帖数不足？

板块里置顶帖/水帖/圈规帖会被跳过，且只有 REPLY 判定通过的帖子才回复。日志会打印每条帖子的判定与评论，可开启 `CAIMOGU_DEBUG=true` 截屏排查。
