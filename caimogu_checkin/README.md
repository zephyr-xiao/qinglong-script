# 采蘑菇论坛自动回帖签到

适配青龙面板的采蘑菇论坛（caimogu.cc）自动回帖脚本。采蘑菇没有按钮式签到，活跃度 = 当天在板块内的有效回复数，因此脚本用 **Playwright 驱动真实浏览器**，每天在指定板块回复若干帖子获取活跃度。

评论生成采用 **AI / 模板双模式**：配了 API Key 走 AI 生成自然评论，没配则用本地模板兜底。评论生成、REPLY/SKIP 判定、反套话过滤、跨天去重等逻辑均在本脚本内自洽。

## 功能特点

- **接口级成败判定（关键）**：回复提交后拦截站点 `/post/act/comment` 接口的响应，按 `status` 判定——`1` 成功、`-1001` 未登录、`887/888/889` 被判水贴、其他为失败。不再"点了按钮就算成功"，避免活跃度没刷到却报成功的假成功；
- **水贴保守中止**：站点有"踩蘑菇AI妹妹"水贴识别，且圈规明确"被删帖会扣影响力"，故一旦被判水贴（887/888/889）立即中止本轮并推送告警，不自动重试、不点"继续发布"；
- **AI / 模板双模式评论生成**：OpenAI 兼容接口（DeepSeek 等），prompt 强约束口语化、日常闲聊腔、禁套话、限 40 字内；请求最多重试 3 次（含首试），超时/网络错误/空返回均重试，全失败回退模板；
- **跨天评论去重**：把成功发出的评论按 4-gram 相似度记入 `replied_posts.json` 的 `recent_comments`（保留 30 天），生成时优先避开最近用过的说法，缓解模板池长期复用；
- **反套话词库三层**：硬禁词全局过滤、弱禁词仅拦开头、长度钳制 15-40 字，避免评论"一眼机器人"；
- **REPLY/SKIP 判定**：自动跳过水帖、签到帖、置顶帖、圈规帖，不给不适合的帖子硬凑回复；
- **帖子列表走接口**：候选帖改由 `/circle/act/post_list` 获取（免登录，直接拿到 `is_top`/`is_lock`/`tags` 等精确字段），接口不可用时回退 DOM 解析；
- **拟人化**：分钟级随机延迟（默认 60-180s）、评论逐字键入（真实按键事件）+ 随机停顿、浏览器基础指纹抹平（webdriver/plugins/chrome/WebGL/locale/时区）、模板随机抽取 + 同义词洗牌；
- **配置启动校验**：板块地址、回帖数、间隔等配置启动即校验，错在日志里一眼可见；
- **登录态检测加固**：以页面"退出/登录"入口 + 提交接口 `status=-1001` 双重判定，登录失效会明确提示；
- **防重复回帖**：`replied_posts.json` 记录当天已回帖 ID + 数量，断点续跑只补差额、中断不重复回同一帖；
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
- 标题按成败分档：`✅ 完成（N/N）` / `⚠️ 部分完成（M/N）` / `❌ 失败（M/N）` / `🚫 被判定水贴已中止（M/N）`；
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

### 推送显示「🚫 被判定水贴已中止」？

站点有"踩蘑菇AI妹妹"水贴识别，接口返回 `887/888/889` 时会保守中止本轮并推送该标题（`888` 硬拦、`889` 软警告、`887` 转后台审核）。含义是评论被判"无意义水贴"，圈规明确"被删帖会扣影响力"，因此脚本不自动重试。可考虑：换更自然的评论（配 AI Key 走 AI 模式）、降低 `CAIMOGU_REPLY_COUNT`、或换一个板块。

### `replied_posts.json` 里的 `recent_comments` 是什么？

跨天去重记录，保存最近 30 天成功发出的评论（最多 100 条），生成新评论时用来避开重复说法。该文件属运行期数据，已被 `.gitignore` 排除，可随时删除（删掉即重置去重记忆）。
