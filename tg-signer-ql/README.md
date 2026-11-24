# tg-signer-ql（青龙面板版）

用你自己的 Telegram 账号，每天定时给机器人 / 群组发送签到命令、点击按钮，并把结果推送到青龙通知。单文件脚本，依赖自动安装，配置全靠环境变量；支持网页扫码登录与多账号。

## 青龙任务命令

```text
# TG签到（建议定时 26 8 * * *）
task tg-signer-ql/tg_signer_ql.py

# TG扫码登录（只手动跑，定时 0 0 1 1 *）
task tg-signer-ql/tg_signer_ql.py --qrlogin
```

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `TG_SIGN_CHATS` | 否 | — | 签到目标，格式 `聊天\|发送文本\|点击按钮\|N秒后删除`（后三段可省略），多目标用 `&` 或换行分隔，按钮可逗号分隔多个（按顺序点）；不设置时用内置默认且不覆盖手工配置 |
| `TG_PROXY` | 否 | 无 | 代理，如 `socks5://127.0.0.1:7890`（大陆服务器建议配置）；留空回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`）。TG 走 MTProto，`socks5` 最稳妥，`http` 需支持 CONNECT 隧道 |
| `TG_SESSION_STRING` | 否 | 无 | 会话字符串，多账号用 `&` 分隔（设置后优先于扫码文件） |
| `TG_2FA_PASSWORD` | 否 | 无 | 网页扫码时的两步验证密码 |
| `TG_QR_LOGIN` | 否 | 无 | 设 `true` 让主任务进入扫码模式（命令传参的备用方案） |
| `TG_API_ID` / `TG_API_HASH` | 否 | tg-signer 内置 | 自有 API 凭据 |
| `TG_ACCOUNT` | 否 | `my_account` | 文件会话账号名，多个用 `&` 分隔 |
| `TG_SIGN_TASK` | 否 | `ql_sign` | tg-signer 任务名 |
| `TG_SIGNER_DIR` | 否 | `脚本目录/tg_signer_data` | 数据目录 |
| `TG_SIGN_TIMEOUT` | 否 | `600` | 单账号超时（秒） |
| `TG_SIGN_DIALOGS` | 否 | `20` | 登录拉取的最近对话数 |
| `TG_SIGN_NOTIFY` | 否 | `true` | 设 `false` 关闭通知 |

## 依赖安装

依赖 `tg-signer`，首次运行会自动 pip 安装；也可在青龙「依赖管理 → Python3」提前添加 `tg-signer`（可选再加 `TgCrypto` 提速）。要求容器 Python ≥ 3.10。

## 凭证获取（扫码登录）

1. 手动运行「TG扫码登录」任务，打开运行日志；
2. 手机 Telegram 打开「设置 → 设备 → 连接桌面设备」，扫日志**最下方**的二维码（旧码 30 秒过期，日志不动就点刷新）；
3. 扫码成功即完成，签到任务会自动使用所有扫码过的账号，无需复制任何变量；多账号换个账号再运行一次；
4. 开了两步验证的账号先加 `TG_2FA_PASSWORD=云密码`，登录完可删除。

也支持容器终端扫码（`python3 tg_signer_ql.py --qrlogin`）或手机号验证码登录（`--login`）。session 文件 / 会话字符串等同账号完全控制权，不要泄露、不要提交仓库；建议设 `TG_SIGNER_DIR=/ql/data/tg_signer_data` 把数据目录移出 scripts 目录。

## 常见问题

**收不到消息 / 超时** → 检查 `TG_PROXY` 或青龙全局代理是否可用；确认目标在最近对话里（先手动发过一次 `/start`）。

**`@username` 解析失败** → 先给该机器人发一条 `/start`，或在 `TG_SIGN_CHATS` 里直接填数字 ID（任务日志的最近对话列表里能查到）。

**会话失效（401 AUTH_KEY_UNREGISTERED）** → 多半是在手机 Telegram「设备」里注销了该会话；脚本会自动把失效会话文件改名为 `.bad` 隔离，重新运行扫码登录任务即可。

**高级玩法（骰子 / AI 识图 / 算术题）** → 直接编辑 `数据目录/.signer/signs/<任务名>/config.json`，字段格式见 tg-signer 文档；不设置 `TG_SIGN_CHATS` 时脚本不会覆盖手工配置。

## 来源

改编自 [xuanvivo/tg-signer-ql](https://github.com/xuanvivo/tg-signer-ql)，核心签到能力来自 [amchii/tg-signer](https://github.com/amchii/tg-signer)。
