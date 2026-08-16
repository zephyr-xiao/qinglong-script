# DZZI.AI 自动签到（青龙面板 / GitHub Actions / 本地通用）

DZZI.AI（New API 站）自动签到脚本。多账号批量签到，签到前先查状态、已签到自动跳过（幂等），推送走青龙 `notify.py` 全渠道。

## 文件结构

| 文件 | 说明 |
|---|---|
| `checkin.py` | 主脚本（推送走青龙面板内置 notify，本地无青龙环境时回退内置推送） |
| `.env` | 本地账号配置（⚠️ 含明文密码，勿提交/外传） |
| `.token/` | access_token 缓存目录（运行后自动生成，文件名用账号 hash；勿提交/外传） |

> 通知模块：青龙面板新版已内置 `notify.py`（运行时自动注入脚本环境），脚本目录**无需自带**。本地/非青龙环境运行时不加载面板 notify，回退 `checkin.py` 内置的飞书/Server酱/Telegram/Bark 推送。

## 登录态缓存

- 登录成功后将 `access_token` 落盘到脚本同目录 `.token/{hash}.json`
- 下次运行命中缓存则**跳过登录请求直接签到**；token 失效（401/未登录）自动清除缓存并重新登录后重试一次
- 无开关，自动生效；如需强制重新登录，删除 `.token/` 目录即可

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `DZZI_ACCOUNTS` | ✅ | 账号列表。格式：`user1\|pwd1`（换行分隔多账号）或 JSON 数组 `[{"username":"a","password":"b"}]` |
| `DZZI_NOTIFY` | 否 | 是否推送结果，默认 `true` |
| `NOTIFIER` | 否 | 无青龙环境时的回退推送方式：`feishu` / `serverchan` / `telegram` / `bark` |
| `NOTIFIER_TOKEN` | 否 | 回退推送的 token |

> 青龙面板：在「环境变量」页面配置 `DZZI_ACCOUNTS`；推送渠道直接配置青龙标准通知变量（`BARK_PUSH`、`PUSH_KEY`、`TG_BOT_TOKEN` 等，见面板内置通知模块）即可，无需额外设置。

## 本地运行

```bash
# 在项目目录内
python checkin.py
```

脚本自动读取同目录 `.env`（若无账号环境变量时）。测试可临时禁用推送：

```bash
DZZI_NOTIFY=false python checkin.py
```

## 青龙面板部署

1. 上传 `checkin.py` 到脚本目录（通知走面板内置模块，无需额外文件）
2. 「定时任务」→ 新建任务：命令 `task checkin.py`，定时规则与脚本头 `cron: 8 8 * * *`（每天 8:08）保持一致
3. 或使用 `ql repo` 拉取仓库，青龙会按脚本头 `cron:` 自动建任务

## 退出码

- `0`：全部账号签到成功（或已签到跳过）
- `1`：未配置账号
- `2`：存在失败账号

## 接口说明

对接 QuantumNous/new-api 的 controller/checkin.go：
- 登录：`POST /api/user/login`
- 状态：`GET /api/user/checkin`
- 签到：`POST /api/user/checkin`

dzzi 站点实测：登录后需带 `Authorization: Bearer <access_token>` 访问签到接口（老版本 `New-Api-User` 头已顺带兼容）。
