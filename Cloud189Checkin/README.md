# 天翼云盘签到（Cloud189Checkin）

青龙面板自动完成天翼云盘（189 网盘）每日签到，Node.js 实现，基于 `cloud189-sdk` 登录，复用目录内 `sendNotify.js` 推送全通道。

## 青龙任务命令

```text
task Cloud189Checkin/src/app.js
```

首次部署需先安装依赖（见「依赖安装」），并在青龙「环境变量」中配置 `TY_ACCOUNTS`。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `TY_ACCOUNTS` | 是 | — | 账号列表，JSON 数组或单对象（见下） |
| `TY_USERNAME_n` / `TY_PASSWORD_n` | 否 | — | 旧版逐对账号格式，未配 `TY_ACCOUNTS` 时生效 |
| `CLOUD189_VERBOSE` | 否 | — | `1` = 开启 cloud189-sdk 调试日志，排查登录 / 签到问题 |

`TY_ACCOUNTS` 格式：

```text
[{"userName":"138xxxx","password":"xxx"},{"userName":"139xxxx","password":"yyy"}]   // 多账号数组（推荐）
{"userName":"138xxxx","password":"xxx"}                                            // 单账号对象
```

## 依赖安装

青龙「依赖管理」→ Node 依赖，输入 `cloud189-sdk dotenv log4js superagent undici`；或进容器执行 `cd /ql/scripts/Cloud189Checkin && npm install`。

`undici@8` 需要 `node >= 22.19`；青龙 Node 低于 22 时把 `undici` 降级为 `^6`（`npm install undici@6`）后再运行。

本地调试：`npm install` 后设好 `TY_ACCOUNTS` 再 `node src/app.js`（PowerShell 示例：`$env:TY_ACCOUNTS='[{"userName":"138xxxx","password":"xxx"}]'`）。

## 运行行为

多账号串行，单账号失败不中断整体；签到前后各取一次空间信息，推送个人 / 家庭容量增量（M/G）；登录凭证存 `.token/{userName}.json` 复用；当日已签到输出「今日已签到过」，不重复领奖励。退出码分档：全部账号失败才 `exit 1`（青龙红色），部分失败正常退出（绿色）。

## 常见问题

- **`主流程异常` 或启动即报错** → 依赖未装全，确认 `cloud189-sdk` 等已安装。
- **日志无任何输出** → `dotenv` 或 `log4js` 加载失败，重新 `npm install`。
- **`请求失败: 4xx`** → 账号密码错误或触发风控，检查 `TY_ACCOUNTS`。
- **`请求失败: ECONNRESET` / `ETIMEDOUT`** → 网络不通，容器出网受限时配代理或改本地运行。
- **推送收不到** → 检查青龙「环境变量」是否配置了推送 key，一个都没配时脚本照常运行、只是不推送；注意旧自研命名 `WX_PUSHER_APP_TOKEN` / `WX_PUSHER_UID` 已废弃，改用官方 `WXPUSHER_APP_TOKEN` / `WXPUSHER_UIDS`。
- 通知复用青龙 `sendNotify.js`，通道与官方 Notify 一致（`PUSH_KEY`、`TG_BOT_TOKEN` + `TG_USER_ID`、`DD_BOT_TOKEN` / `DD_BOT_SECRET`、`QYWX_AM` / `QYWX_KEY`、`BARK_PUSH`、`PUSH_PLUS_TOKEN` 等）。

## 来源

改编自 [wes-lin/Cloud189Checkin](https://github.com/wes-lin/Cloud189Checkin)，登录依赖 [wes-lin/cloud189-sdk](https://github.com/wes-lin/cloud189-sdk)。
