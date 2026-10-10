# 纸鸢下载签到（青龙面板版）

纸鸢下载（[mybt.kiteyuan.info](https://mybt.kiteyuan.info)）自动签到脚本，移植自
[elongou-checkin/mybt-signin](https://github.com/elongou-checkin/mybt-signin)，
按青龙面板规范改造：Casdoor 账号密码自动登录换会话 token → 每日签到 + 访问任务 → 青龙 notify.py 推送。

> 声明：仅供学习交流，与纸鸢下载 / KiteYuan 官方无关。账号密码只放青龙环境变量，勿提交到仓库。请合理设置运行频率，勿滥用。

## 功能

| 能力 | 说明 |
|------|------|
| 自动登录 | Casdoor 账号密码走 OAuth 换取站点 token，无需手动抓 token |
| 改版适配 | 兼容站点 2026 新链路（`login_code` + `POST /api/auth/session`）与旧链路（callback 直出 token） |
| 每日签到 | `POST /api/auth/points/tasks/signin` |
| 访问任务 | `POST /api/auth/points/tasks/visit`（`MYBT_DO_VISIT` 可关） |
| 请求签名 | 自动生成 `X-Sign` / `X-Timestamp`（HMAC-SHA256），secret 从 `/auth/me` 动态获取 |
| 判定口径 | 分层判定：HTTP 码 → 业务码 → 文案。`401/403` 与业务层「未登录」无条件触发续期/重登，绝不被「已签到」类文案吞掉；无法确认为成功的响应单列「未确认」，按保守口径计为未成功 |
| 登录态缓存 | token + refresh_token 按账号缓存到 `mybt_token.json`（不存密码），原子写入；过期或 401/403 才续期/重登 |
| 会话续期 | 401/403 时先用 `refresh_token` 免登录续期，失败才走完整 Casdoor 登录 |
| 多账号 | `MYBT_ACCOUNT` 多账号支持，账号间自动加 3–5 秒风控间隔 |
| 结果通知 | 通过青龙 `notify.py` 推送（面板里配好的推送渠道直接复用），成功/未确认/失败分组展示 |
| 网络重试 | 网络异常自动重试 3 次；密码错误/验证码拦截不重试 |

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `MYBT_ACCOUNT` | 与 `MYBT_TOKEN` 二选一 | 账号：`用户名#密码`（多账号用 `&` 或换行分隔；**密码不能含 `&` 或换行**，否则会被截断，`#` 可以） |
| `MYBT_TOKEN` | 兜底 | 手动 JWT；无账号密码时必填，自动登录失败时回退 |
| `MYBT_COOKIE` | 否 | 附加 Cookie，如 `cf_clearance=...` |
| `MYBT_TURNSTILE_TOKEN` | 否 | 登录触发人机验证时手动提供的验证码 token |
| `MYBT_DO_VISIT` | 否 | 是否执行访问任务，默认 `true` |
| `MYBT_NOTIFY` | 否 | 是否推送，默认 `true` |
| `MYBT_NOTIFY_ONLY_FAIL` | 否 | 仅失败时推送（全成功静默），默认 `false` |
| `MYBT_EXIT_STRICT` | 否 | 只要有账号未成功（含「未确认」）就 `exit 1`，默认 `false`（仅全失败才 exit 1） |
| `MYBT_TIMEOUT` | 否 | HTTP 超时秒数，默认 `30` |
| `MYBT_DEBUG` | 否 | 输出调试细节（签名串等），默认 `false` |
| `MYBT_PROXY` | 否 | HTTP/SOCKS 代理，如 `http://172.17.0.1:7890` |
| `MYBT_BASE_URL` / `MYBT_AUTH_URL` | 否 | 站点地址 / 认证地址，一般不用改 |
| `MYBT_SECRET` | 否 | 签名密钥，默认留空：由 `/auth/me` 的 `sign_secret` 自动回填，仅站点收紧为全接口验签时才需显式提供 |

## 青龙部署

1. 「依赖管理」→「Python」确认已安装 `requests`（青龙镜像一般自带）
2. 「脚本管理」→ 上传 `mybt_checkin.py`
3. 「环境变量」→ 新建 `MYBT_ACCOUNT`，值为 `用户名#密码`（多账号用 `&` 连接或换行）
4. 「定时任务」→ 新建：
   - 名称：`纸鸢下载签到`
   - 命令：`task mybt-checkin-ql/mybt_checkin.py`（不是 `python mybt_checkin.py`）
   - 定时规则：`5 8 * * *`（每天 08:05，可自行调整，建议与脚本头部 `cron:` 保持一致）

## 本地运行

```powershell
$env:MYBT_ACCOUNT="用户名#密码"
$env:MYBT_NOTIFY="false"
python mybt_checkin.py
```

## 与原脚本的差异

| 项 | 原脚本 | 本版 |
|----|--------|------|
| 运行平台 | GitHub Actions / 本地 | 青龙面板（也可本地直接跑） |
| 通知 | 内置 WxPusher / PushPlus / 微信测试号 | 青龙 `notify.py`，渠道由面板统一管理 |
| 账号数量 | 单账号 | 多账号（`MYBT_ACCOUNT`） |
| 登录频率 | 每次运行都完整走 Casdoor 登录 | 登录态缓存复用（token 有标准 `exp` 时留 12h 余量，否则按保存时间 24h 内复用），401/403 先 refresh 续期、失败才重登 |
| 站点登录链路 | 一段式：callback 直接返回 token | 两段式：`login_code` → `POST /api/auth/session`，旧链路保留兼容 |
| 响应判定 | 关键字宽松匹配，HTTP 200 基本判成功 | 分层判定（HTTP 码 → 业务码 → 文案）+「未确认」档位，401/403 优先 |
| 网络抖动 | 直接失败 | 网络层自动重试 3 次（仅网络错误，业务错误不重试） |
| HTTP 客户端 | urllib 标准库 | requests（青龙生态主流，依赖管理一键安装） |

## 已知限制

- 登录页若启用 **Cloudflare Turnstile**，账号密码登录会被拦截（报错提示 `站点登录触发了人机验证（Cloudflare Turnstile）`），需提供 `MYBT_TURNSTILE_TOKEN` 或配 `MYBT_TOKEN` 兜底；本脚本**不做自动过码**
- **「未确认」的含义**：站点返回 HTTP 200 但响应形态无法确认为成功（业务码非零、或字段不认识）时，脚本按保守口径计为未成功，并在报告里单列「未确认」。这是为了避免"站点其实失败了、脚本却报成功"的静默误报；如果真实成功响应被误判成「未确认」，把该响应样例反馈后调整成功标志字段即可
- 站点 2026 年把登录从「callback 直出 token」改为「callback 返回 `login_code` → `POST /api/auth/session` 换 token」，本版按新链路实现并保留旧链路兼容分支。若站点再次重构接口或签名算法，脚本仍会失效，需对照前端 bundle 同步更新
- `mybt_token.json` 是登录态缓存（只存 token / refresh_token，不存密码），采用原子写入（先写 `.tmp` 再替换），可随时删除，下次运行自动重建

## 项目结构

```text
mybt-checkin-ql/
├── mybt_checkin.py            # 青龙脚本（单文件）
├── tests/
│   └── test_mybt_checkin.py   # 单元测试（纯函数 + mock，不发真实请求）
├── mybt_token.json            # 运行后自动生成的登录态缓存（可删，已被 .gitignore 覆盖）
└── README.md
```

## 本地测试

```bash
python -m unittest discover -s tests -v
```
