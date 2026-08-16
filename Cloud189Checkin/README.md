# 天翼云盘自动签到（Node.js）

青龙面板自动签到脚本，针对天翼云盘（189 网盘）。Node.js 实现，基于 `cloud189-sdk` 登录，复用目录内 `sendNotify.js` 推送全通道。

## 脚本文件

| 文件 | 说明 |
|---|---|
| `src/app.js` | 签到主入口 |
| `sendNotify.js` | 青龙官方 Notify（推送通道），勿删 |
| `accounts.js` | 账号解析（从环境变量读取） |
| `src/logger.js` | 日志配置（控制台 + `.logs/` 文件，运行结束自动清理） |

## 青龙任务命令

```text
task Cloud189Checkin/src/app.js
```

> 首次部署需先安装依赖（见下方「依赖安装」），并在青龙「环境变量」中配置 `TY_ACCOUNTS`。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `TY_ACCOUNTS` | ✅ | 账号列表，支持 JSON 数组或单对象（见下） |
| `TY_USERNAME_n` / `TY_PASSWORD_n` | | 旧版账号格式（逐对），未配 `TY_ACCOUNTS` 时生效 |
| `CLOUD189_VERBOSE` | | `1` = 开启 cloud189-sdk 调试日志（排查登录/签到问题） |

### TY_ACCOUNTS 格式

```json
// 1) 多账号 JSON 数组（推荐）
[
  {"userName":"138xxxx","password":"xxx"},
  {"userName":"139xxxx","password":"yyy"}
]
```

```json
// 2) 单账号 JSON 对象
{"userName":"138xxxx","password":"xxx"}
```

## 推送通道

复用同目录 `sendNotify.js`，在青龙「环境变量」里配置以下任意通道即可（与青龙官方 Notify 一致）：

- `PUSH_KEY`（Server 酱）/ `TG_BOT_TOKEN` + `TG_USER_ID`（Telegram）
- `DD_BOT_TOKEN` / `DD_BOT_SECRET`（钉钉）
- `QYWX_AM` / `QYWX_KEY`（企业微信）
- `BARK_PUSH`（Bark）
- `WXPUSHER_APP_TOKEN` + `WXPUSHER_UIDS`（wxpusher）
- `PUSH_PLUS_TOKEN`（pushplus）
- ……

> ⚠️ **变量名注意**：本项目早期自研推送用的 `WX_PUSHER_APP_TOKEN` / `WX_PUSHER_UID` 已废弃，青龙版统一改用官方的 `WXPUSHER_APP_TOKEN` / `WXPUSHER_UIDS`，配置时请迁移。

未配置任何通道时脚本照常运行，只是不推送。

## 依赖安装

青龙容器内的 Node 依赖需单独安装（`cloud189-sdk` 不在青龙内置依赖里）：

1. **青龙「依赖管理」→ Node 依赖**，输入：
   ```
   cloud189-sdk dotenv log4js superagent undici
   ```
2. 或进入容器脚本目录执行：
   ```bash
   cd /ql/scripts/Cloud189Checkin && npm install
   ```

> **Node 版本要求**：`undici@8` 需要 `node >= 22.19`。若青龙 Node 版本低于 22，请把 `undici` 降级为 `^6`（`npm install undici@6`）后再运行。

## 功能特性

- **多账号串行**：逐账号登录签到，账号间互不影响，单账号失败不中断整体。
- **已签到幂等**：当日已签到则输出「今日已签到过」，不会重复给奖励。
- **容量对比**：签到前后各取一次空间信息，推送个人 / 家庭容量增量（M/G）。
- **凭证复用**：登录凭证存 `.token/{userName}.json`，避免每次重复登录。
- **退出码分档**：全部账号失败时 `exit 1`（青龙任务红色标记），部分失败正常退出（绿色）。

## 本地调试

```bash
# 先安装依赖
npm install

# 配置账号后运行（PowerShell 示例）
$env:TY_ACCOUNTS='[{"userName":"138xxxx","password":"xxx"}]'
node src/app.js
```

## 排错指引

| 日志现象 | 排查方向 |
|---|---|
| `主流程异常` 或启动即报错 | 依赖未装全，确认 `cloud189-sdk` 等已安装 |
| 日志无任何输出 | `dotenv` 或 `log4js` 加载失败，重新 `npm install` |
| `请求失败: 4xx` | 账号密码错误或风控，检查 `TY_ACCOUNTS` |
| `请求失败: ECONNRESET/ETIMEDOUT` | 网络不通，青龙容器出网受限时配置代理或用本地跑 |
| 推送收不到 | 检查青龙「环境变量」是否已配置对应推送 key，未配置则跳过 |
