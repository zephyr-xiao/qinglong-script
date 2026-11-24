# 采蘑菇论坛自动回帖签到

适配青龙面板的采蘑菇论坛（caimogu.cc）自动回帖脚本。站点没有按钮式签到，活跃度 = 当天在板块内的有效回复数，因此脚本用 **Playwright 驱动真实浏览器**，每天在指定板块回复若干帖子；评论生成支持 AI / 模板双模式，配了 API Key 走 AI，没配则本地模板兜底。

回复提交后拦截 `/post/act/comment` 接口响应按 `status` 判定成败（`1` 成功、`-1001` 未登录、`887/888/889` 被判水贴、其他失败），避免「点了按钮就算成功」的假成功；判为水贴立即中止本轮。

## 青龙任务命令

```text
task caimogu_checkin/caimogu_checkin.py
```

建议定时 `8 8 * * *`（每天 8 点执行，与脚本头 `cron:` 一致）。不启动浏览器、只预览评论生成效果（无副作用）用 `--test`。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `CAIMOGU_COOKIE` | 否* | - | 登录 Cookie 字符串（优先凭证） |
| `CAIMOGU_AUTH_FILE` | 否* | 脚本同目录 `auth_state.json` | Playwright storage_state 登录态文件路径（回退凭证） |
| `CAIMOGU_ACCOUNTS` | 否 | - | 自动登录账号，格式 `用户名#密码`（手机号 / 用户名均可），用于 cookie 失效时自动登录；旧写法 `CAIMOGU_USER` + `CAIMOGU_PASSWORD` 仍兼容 |
| `CAIMOGU_CIRCLE_URL` | 否 | `https://www.caimogu.cc/circle/308.html` | 签到板块地址 |
| `CAIMOGU_REPLY_COUNT` | 否 | `3` | 每天回帖数 |
| `CAIMOGU_MIN_DELAY` | 否 | `60` | 回帖最小间隔（秒） |
| `CAIMOGU_MAX_DELAY` | 否 | `180` | 回帖最大间隔（秒） |
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
| `CAIMOGU_PROXY` | 否 | - | HTTP/SOCKS 代理，留空时回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`） |

\* 凭据优先级：`CAIMOGU_COOKIE` > `auth_state.json` > 账号密码自动登录（`CAIMOGU_ACCOUNTS`）。配置 `CAIMOGU_ACCOUNTS` 后，即使 cookie 失效脚本也会自动登录并继续回帖。

## 依赖安装

进入容器（`docker exec -it qinglong bash`）后执行（脚本会自动搜索 `/usr/bin/chromium`，无需配置 `CAIMOGU_CHROMIUM_PATH`）：

```bash
pip install playwright
apt update && apt install -y chromium libxss1 libnss3 libgbm1 libasound2
```

## 凭证获取

1. 浏览器登录 `https://www.caimogu.cc/`，`F12` → Network，刷新页面，任选一个 `/api/` 或页面请求，查看 Request Headers 里的 `Cookie`；
2. 把整个 Cookie 字符串（如 `CAIMOGU=xxx; cmg_token=yyy`）填入 `CAIMOGU_COOKIE`。过期后日志会提示登录状态已失效，重新登录更新即可；配置 `CAIMOGU_ACCOUNTS` 时脚本会自动用账号密码登录（无验证码），在同一次运行中继续回帖，并把新登录态持久化到 `auth_state.json`。

## 常见问题

**评论太生硬 / 重复** → 没配 `CAIMOGU_DEEPSEEK_API_KEY` 时用本地模板（模板有限，跑久会撞车）；填 Key 自动切换 AI 模式，AI 失败会回退模板不中断。跨天去重记录存于 `replied_posts.json` 的 `recent_comments`（最近 30 天、最多 100 条），属运行期数据，删除即重置去重记忆。

**提示登录状态失效** → Cookie 过期。重新复制 Cookie，或配置 `CAIMOGU_ACCOUNTS` 让脚本自动登录。站点真正校验发生在点击提交时：若弹出「您还未登录」，脚本会立即中止整轮，或配置账号密码后自动登录并重试当前帖；日志显示「检测到未登录（页面存在登录/注册入口）」则说明彻底未登录，脚本开始阶段直接退出。

**容器报 `Executable doesn't exist`** → 未安装 chromium，按「依赖安装」执行 `apt install -y chromium`，或设 `CAIMOGU_CHROMIUM_PATH` 指向浏览器绝对路径。

**某一天回帖数不足** → 置顶帖 / 水帖 / 签到帖 / 圈规帖会被跳过，只有 REPLY 判定通过的帖子才回复；`CAIMOGU_DEBUG=true` 可截屏排查。

**推送显示「🚫 被判定水贴已中止」** → 站点有「踩蘑菇AI妹妹」水贴识别，`887/888/889` 分别对应转后台审核 / 硬拦 / 软警告。圈规明确「被删帖会扣影响力」，脚本不自动重试、不点「继续发布」；可换更自然的评论（配 AI Key）、降低 `CAIMOGU_REPLY_COUNT` 或换板块。

