# New API 站点自动签到（青龙面板版）

重写自 [zhangguoguo1314/quan-zidong-zhushou](https://github.com/zhangguoguo1314/quan-zidong-zhushou)，去除原项目的 FastAPI / Vue / SQLite / 用户系统，仅保留核心签到能力，适配青龙面板。

## 功能特点

- 单文件 Python 脚本，只依赖 `requests` + 标准库；
- 支持多站点、多账号；
- 内置站点：Liminality 贝之中转站、可萌中转站、哈基米 API 站、DZZI.AI；
- 支持 `QZD_CUSTOM` 扩展任意自定义 API 站（含 Discuz 论坛）；
- 自动登录、提取 Token / Cookie、签到、识别“今日已签到”；
- **余额展示**：签到后显示账户总余额；当日首签成功还会显示本次签到获得的额度（按签到前后余额差计算）。内置站已预配 `quota_info_url` 等字段，自定义站可在 `api_config` 中按需添加（`quota_info_url` / `quota_field` / `quota_per_unit` / `quota_currency`）；
- **凭证缓存复用**：登录成功后把 Token、会话 Cookie、过期时间落盘到脚本同目录 `.token/`，下次命中则跳过登录直接签到；token 过期 / 401 / 登录态失效自动清缓存重登；
- 401 自动重新登录重试一次；
- 中文错误分类：密码错误、账号不存在、接口 404、限流 429 等；
- 账号脱敏展示，避免日志泄露；
- 自动适配青龙原生 `notify.py` 推送。

## 青龙任务命令

```text
task newapi_checkin/newapi_checkin.py
```

建议定时：

```text
8 8 * * *
```

## 环境变量

至少配置一个站点变量才会执行签到。

| 变量名 | 必填 | 格式 | 示例 |
|---|---:|---|---|
| `QZD_BEIZHI` | 否 | `用户名#密码`，多账号用 `&` 或换行分隔 | `123#pwd123` |
| `QZD_API456` | 否 | `用户名#密码`，多账号用 `&` 或换行分隔 | `123#pwd123` |
| `QZD_GEMAI` | 否 | `用户名#密码`，多账号用 `&` 或换行分隔 | `zhangsan#pwd123` |
| `QZD_DZZI` | 否 | `用户名#密码`，多账号用 `&` 或换行分隔 | `zhangsan#pwd123` |
| `QZD_CUSTOM` | 否 | JSON 数组，见下方 | 见下方 |
| `QZD_NOTIFY` | 否 | `true` / `false`，默认 `true` | `false` |
| `QZD_NOTIFY_ONLY_FAIL` | 否 | `true` / `false`，默认 `false`，`true` 时仅在有失败时推送 | `true` |
| `QZD_TIMEOUT` | 否 | 秒，默认 `30` | `60` |
| `QZD_PROXY` | 否 | HTTP/SOCKS 代理 | `http://172.17.0.1:7890` |

> DZZI.AI 原为独立脚本 `dzzi-auto-checkin`，现已整合为本脚本的内置站点。原变量 `DZZI_ACCOUNTS`（格式 `用户名|密码`）请改为 `QZD_DZZI`（格式 `用户名#密码`），青龙任务命令也一并改为 `task newapi_checkin/newapi_checkin.py`。

## 内置站点

| key | 名称 | 站点地址 | 认证方式 |
|---|---|---|---|
| `beizhi` | Liminality 贝之中转站 | https://beizhi.sylu.cc | Bearer Token（短期 token + 过期时间缓存） |
| `api456` | 可萌中转站 | https://api456.me | New-Api-User Header + Session Cookie |
| `gemai` | 哈基米 API 站 | https://api.gemai.cc | New-Api-User Header |
| `dzzi` | DZZI.AI（大肘子API） | https://api.dzzi.ai | Bearer Token |

## `QZD_CUSTOM` 自定义站点

用于扩展没有内置预设的 API 站点。示例：

```json
[
  {
    "name": "示例站点",
    "type": "custom-api",
    "username": "your_username",
    "password": "your_password",
    "api_config": {
      "login_url": "https://example.com/api/login",
      "login_method": "POST",
      "login_body_template": "{\"username\": \"{{username}}\", \"password\": \"{{password}}\"}",
      "login_content_type": "application/json",
      "token_path": "data.token",
      "signin_url": "https://example.com/api/checkin",
      "signin_method": "POST",
      "signin_body": "{}",
      "signin_content_type": "application/json",
      "auth_header_name": "Authorization",
      "auth_header_template": "Bearer {{token}}",
      "success_field": "success",
      "message_field": "message"
    }
  }
]
```

常用字段说明：

| 字段 | 说明 |
|---|---|
| `name` | 站点名称，仅用于日志展示 |
| `type` | `custom-api` 或 `discuz` |
| `username` / `password` | 登录账号 |
| `api_config.login_url` | 登录接口；留空则跳过登录直接签到 |
| `api_config.login_method` | `POST` / `GET` |
| `api_config.login_body_template` | 登录请求体模板，支持 `{{username}}` / `{{password}}` |
| `api_config.login_content_type` | `application/json` 或 `application/x-www-form-urlencoded` |
| `api_config.token_path` | 从登录响应 JSON 中提取 token 的点号路径，例如 `data.token` |
| `api_config.token_path_fallback` | token 候选路径列表 |
| `api_config.signin_url` | 签到接口地址 |
| `api_config.signin_method` | `POST` / `GET` |
| `api_config.signin_body` | 签到请求体，可用模板变量 |
| `api_config.signin_headers` | 自定义请求头，值支持模板变量 |
| `api_config.auth_header_name` | 鉴权头名，默认 `Authorization` |
| `api_config.auth_header_template` | 鉴权头值模板，例如 `Bearer {{token}}` |
| `api_config.success_field` | 判定成功的字段路径 |
| `api_config.message_field` | 提取消息文本的字段路径 |
| `api_config.quota_info_url` | 余额查询接口（可选，配置后签到结果显示余额） |
| `api_config.quota_field` | 余额字段点号路径，默认 `data.quota` |
| `api_config.quota_per_unit` | 额度换算单位，默认 `500000`（New API 标准） |
| `api_config.quota_currency` | 货币符号，默认 `$` |

`type` 为 `discuz` 时，`api_config` 通常只需配置 `base_url`，其余流程由脚本自动处理。

## 凭证缓存

- 登录成功后将 token、会话 Cookie 快照、token 过期时间（如有）落盘到脚本同目录 `.token/{site}_{hash}.json`
- 下次运行命中缓存则**跳过登录直接签到**（Session Cookie 型站点同样生效）；token 过期 / 签到 401 / Cookie 失效自动清除缓存并重新登录
- 无开关，自动生效；如需强制重新登录，删除脚本同目录 `.token/` 即可（含敏感数据，勿提交/外传）

## 依赖安装

青龙环境一般已内置 `requests`。如果缺失，在青龙「依赖管理」-「Python」中安装：

```text
requests
```

或在容器内执行：

```bash
pip install requests
```

## 本地调试

Linux / macOS：

```bash
export QZD_BEIZHI="your_username#yourpass"
export QZD_NOTIFY=false
python newapi_checkin.py
```

Windows PowerShell：

```powershell
$env:QZD_BEIZHI="your_username#yourpass"
$env:QZD_NOTIFY="false"
python .\newapi_checkin.py
```

未设置任何站点变量时，脚本会打印提示后退出。

## 通知

- 青龙环境中自动复用青龙 `notify.py`；
- 设置 `QZD_NOTIFY=false` 可关闭推送；
- 本地运行找不到 `notify.py` 时会跳过推送，不影响签到。

## 常见问题

### 青龙日志显示缺少 `requests`？

在青龙「依赖管理」-「Python」中安装 `requests`。

### 怎么临时禁用某个站点？

在青龙「环境变量」里禁用对应变量即可，不需要删除。

### 本地跑单元测试

```bash
pip install pytest
python -m pytest tests -q
```

### 怎么扩展新的内置站点？

优先使用 `QZD_CUSTOM`。如果要内置到脚本中，可编辑 `SITE_PRESETS` 与 `ENV_PRESET_MAP`。
