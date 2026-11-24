# New API 站点自动签到

适配青龙面板的 New API 系列站点签到，支持多站点多账号，内置可萌中转站、哈基米 API 站、DZZI.AI，也可用 JSON 扩展任意 API 站或 Discuz 论坛。签到后展示账户余额（内置站已预配），并显示当日首签获得的额度；登录成功后缓存凭证跳过登录；DZZI 的算术图形验证码需配置视觉大模型（`QZD_VLM`）识别。

## 青龙任务命令

```text
task newapi_checkin/newapi_checkin.py
```

定时 `8 8 * * *`（脚本 docstring 已自带）。

## 环境变量

至少配置一个站点变量才会执行签到。

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `QZD_API456` | 否 | 空 | 可萌中转站账密 `用户名#密码`，多账号用 `&` 或换行分隔 |
| `QZD_GEMAI` | 否 | 空 | 哈基米 API 站账密，格式同上 |
| `QZD_DZZI` | 否 | 空 | DZZI.AI 账密，格式同上 |
| `QZD_CUSTOM` | 否 | 空 | JSON 数组，自定义站点，见下 |
| `QZD_NOTIFY` | 否 | `true` | 是否推送（复用青龙 `notify.py`） |
| `QZD_NOTIFY_ONLY_FAIL` | 否 | `false` | `true` 时仅在有失败时推送 |
| `QZD_TIMEOUT` | 否 | `30` | HTTP 超时秒 |
| `QZD_PROXY` | 否 | 空 | HTTP/SOCKS 代理，如 `http://172.17.0.1:7890` |
| `QZD_VLM` | DZZI 必填 | 空 | `base_url#api_key#model`，OpenAI 兼容视觉接口，模型需支持图片输入；`base_url` 写到 `/v1` 为止 |
| `QZD_VLM_TIMEOUT` | 否 | `90` | 视觉模型请求超时秒（思考型模型更慢） |
| `QZD_VLM_MAX_TOKENS` | 否 | `3000` | 视觉模型最大输出；思考型模型给小了会只思考、`content` 为空 |
| `QZD_VLM_PROXY` | 否 | 空 | 仅视觉模型请求用的代理；不填则直连，不继承 `QZD_PROXY` |
| `QZD_CAPTCHA_ATTEMPTS` | 否 | `3` | 图形验证码最多重试次数 |

内置站点：`api456`（api456.me，Bearer Token）、`gemai`（api.gemai.cc，New-Api-User Header）、`dzzi`（api.dzzi.ai，Bearer Token + 算术图形验证码）。

> 从旧的独立脚本 `dzzi-auto-checkin` 迁移：`DZZI_ACCOUNTS`（`用户名|密码`）改为 `QZD_DZZI`（`用户名#密码`），任务命令改为 `task newapi_checkin/newapi_checkin.py`。

`QZD_CUSTOM` 最小示例：

```json
[
  {
    "name": "示例站点", "type": "custom-api", "username": "testuser", "password": "test123",
    "api_config": {
      "login_url": "https://example.com/api/login", "login_method": "POST",
      "login_body_template": "{\"username\":\"{{username}}\",\"password\":\"{{password}}\"}",
      "login_content_type": "application/json", "token_path": "data.token",
      "signin_url": "https://example.com/api/checkin", "signin_method": "POST",
      "signin_body": "{}", "signin_content_type": "application/json",
      "auth_header_name": "Authorization", "auth_header_template": "Bearer {{token}}",
      "success_field": "success", "message_field": "message"
    }
  }
]
```

其余可选字段：`token_path_fallback`、`signin_headers`、`quota_info_url` / `quota_field`（默认 `data.quota`）/ `quota_per_unit`（默认 `500000`）/ `quota_currency`（默认 `$`）、`captcha_config`、`refresh_url`、`session_sid_path`、`token_type_path`；`type` 为 `discuz` 时通常只需 `base_url`。

凭证缓存：登录后 token、会话 Cookie、过期时间落盘到脚本同目录 `.token/{site}_{hash}.json`，命中则跳过登录直接签到；token 过期 / 401 / 登录态失效自动清缓存重登，配置了 `refresh_url` 的站点先用会话 Cookie 续期（不需再过验证码）。无开关，删 `.token/` 可强制重登（含敏感数据，勿提交）。

## 依赖安装

青龙环境一般已内置 `requests`；缺失时在「依赖管理」-「Python」安装 `requests`。

## 凭证获取

- 内置站点直接用账号密码，脚本自动登录并提取 Token / Cookie，无需手动抓取。
- DZZI.AI 必须配 `QZD_VLM`：三段用 `#` 连接 `base_url#api_key#model`。`base_url` 指向内网时不要配 `QZD_PROXY`，确需代理用 `QZD_VLM_PROXY`。

## 常见问题

- **DZZI 报「未配置 QZD_VLM」/「QZD_VLM 格式应为 base_url#api_key#model」** → 按上面三段格式补全；只影响 DZZI，其余站点照常签到。
- **「视觉模型返回空内容」** → 思考型模型的典型表现，token 预算被思考过程吃光；调大 `QZD_VLM_MAX_TOKENS`（如 `4096`）与 `QZD_VLM_TIMEOUT`（如 `120`）。
- **「图形验证码未通过（已重试 N 次）」** → 换更强的视觉模型或调大 `QZD_CAPTCHA_ATTEMPTS`；日志出现「视觉模型返回 HTTP xxx」多为欠费或模型不支持图片；下次运行若会话 Cookie 仍有效会走 refresh 续期，不再触发验证码。
- **日志报 401** → 脚本自动重新登录重试一次；反复出现可删 `.token/` 强制重登。
- **未设置任何站点变量** → 打印提示后退出；临时停用某站只需在青龙里禁用对应变量，不必删除。
- **缺少 `requests`** → 按「依赖安装」安装即可。

## 来源

改编自 [zhangguoguo1314/quan-zidong-zhushou](https://github.com/zhangguoguo1314/quan-zidong-zhushou)，去除原项目的 FastAPI / Vue / SQLite / 用户系统，仅保留签到能力。
