# New API 站点自动签到（青龙面板版）

重写自 [zhangguoguo1314/quan-zidong-zhushou](https://github.com/zhangguoguo1314/quan-zidong-zhushou)，去除原项目的 FastAPI / Vue / SQLite / 用户系统，仅保留核心签到能力，适配青龙面板。

## 功能特点

- 单文件 Python 脚本，只依赖 `requests` + 标准库；
- 支持多站点、多账号；
- 内置站点：可萌中转站、哈基米 API 站、DZZI.AI；
- 支持 `QZD_CUSTOM` 扩展任意自定义 API 站（含 Discuz 论坛）；
- 自动登录、提取 Token / Cookie、签到、识别“今日已签到”；
- **余额展示**：签到后显示账户总余额；当日首签成功还会显示本次签到获得的额度（按签到前后余额差计算）。内置站已预配 `quota_info_url` 等字段，自定义站可在 `api_config` 中按需添加（`quota_info_url` / `quota_field` / `quota_per_unit` / `quota_currency`）；
- **凭证缓存复用**：登录成功后把 Token、会话 Cookie、过期时间落盘到脚本同目录 `.token/`，下次命中则跳过登录直接签到；token 过期 / 401 / 登录态失效自动清缓存重登；
- **登录图形验证码**：DZZI.AI 开启了算术图形验证码，脚本会拉图交给视觉大模型算答案，答案由本地复算校验；识别失败/答案不被接受时自动换图重试（次数由 `QZD_CAPTCHA_ATTEMPTS` 控制）；
- **会话自动续期**：token 过期时先用缓存的会话 Cookie 调 `/api/user/auth/refresh` 换新 token（该接口不需要验证码），只有会话彻底失效才重新过验证码；
- 401 自动重新登录重试一次；
- TLS 默认校验证书，仅 SSL 握手失败时自动降级并告警一次；`trust_env` 关闭，代理只认 `QZD_PROXY` > 青龙全局代理 > 直连，不受系统代理干扰；
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
| `QZD_API456` | 否 | `用户名#密码`，多账号用 `&` 或换行分隔 | `123#pwd123` |
| `QZD_GEMAI` | 否 | `用户名#密码`，多账号用 `&` 或换行分隔 | `zhangsan#pwd123` |
| `QZD_DZZI` | 否 | `用户名#密码`，多账号用 `&` 或换行分隔 | `zhangsan#pwd123` |
| `QZD_CUSTOM` | 否 | JSON 数组，见下方 | 见下方 |
| `QZD_NOTIFY` | 否 | `true` / `false`，默认 `true` | `false` |
| `QZD_NOTIFY_ONLY_FAIL` | 否 | `true` / `false`，默认 `false`，`true` 时仅在有失败时推送 | `true` |
| `QZD_TIMEOUT` | 否 | 秒，默认 `30` | `60` |
| `QZD_PROXY` | 否 | HTTP/SOCKS 代理 | `http://172.17.0.1:7890` |
| `QZD_VLM` | DZZI 必填 | `base_url#api_key#model`：OpenAI 兼容接口 + 密钥 + 支持图片输入的模型 | `http://your-host:3000/v1#sk-xxxxxx#your-vision-model` |
| `QZD_VLM_TIMEOUT` | 否 | 秒，默认 `90`，视觉模型请求超时（思考型模型更慢） | `120` |
| `QZD_VLM_MAX_TOKENS` | 否 | 默认 `3000`，视觉模型最大输出；**思考型模型给小了会只思考、`content` 为空** | `4096` |
| `QZD_VLM_PROXY` | 否 | 仅视觉模型请求使用的代理；不填则直连（内网接口无需代理） | `http://172.17.0.1:7890` |
| `QZD_CAPTCHA_ATTEMPTS` | 否 | 默认 `3`，图形验证码最多重试次数 | `5` |

> `QZD_VLM` 只对 **DZZI.AI** 生效：它开启了算术图形验证码，没配或格式不对时该站点会明确报「未配置 QZD_VLM」/「QZD_VLM 格式应为 base_url#api_key#model」，其余站点照常签到。`base_url` 写到 `/v1` 为止，脚本自己拼 `/chat/completions`；视觉模型请求单独建会话，不继承 `QZD_PROXY`，避免把内网接口误走代理。

> DZZI.AI 原为独立脚本 `dzzi-auto-checkin`，现已整合为本脚本的内置站点。原变量 `DZZI_ACCOUNTS`（格式 `用户名|密码`）请改为 `QZD_DZZI`（格式 `用户名#密码`），青龙任务命令也一并改为 `task newapi_checkin/newapi_checkin.py`。

## 内置站点

| key | 名称 | 站点地址 | 认证方式 |
|---|---|---|---|
| `api456` | 可萌中转站 | https://api456.me | Bearer Token（站点已升级 New API 新版 JWT 鉴权） |
| `gemai` | 哈基米 API 站 | https://api.gemai.cc | New-Api-User Header |
| `dzzi` | DZZI.AI（大肘子API） | https://api.dzzi.ai | Bearer Token（登录需算术图形验证码，由视觉模型识别） |

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
| `api_config.captcha_config` | 登录图形验证码配置（可选），字段见下方示例 |
| `api_config.refresh_url` | 会话续期接口（可选），token 过期时先用 Cookie 换新 token |
| `api_config.session_sid_path` | 登录响应里会话 sid 的点号路径（续期时作为 `X-Auth-Session` 头） |
| `api_config.token_type_path` | 登录响应里 token 类型的点号路径（仅记录进缓存） |

`captcha_config` 只影响登录环节，DZZI 的现成配置是：

```json
{
  "url": "https://api.dzzi.ai/api/captcha",
  "id_field": "data.captcha_id",
  "image_field": "data.image",
  "login_query": {
    "turnstile": "",
    "captcha_id": "{{captcha_id}}",
    "captcha": "{{captcha}}"
  },
  "error_codes": ["CAPTCHA_INVALID", "CAPTCHA_REQUIRED"]
}
```

`login_query` 是拼到登录 URL 上的 query 参数模板，空值也会保留（DZZI 要求 `turnstile` 键存在）；`error_codes` 命中即视为「验证码没通过」，会换一张图重试。

`type` 为 `discuz` 时，`api_config` 通常只需配置 `base_url`，其余流程由脚本自动处理。

## 凭证缓存

- 登录成功后将 token、会话 Cookie 快照、token 过期时间（如有）落盘到脚本同目录 `.token/{site}_{hash}.json`
- 配置了 `refresh_url` 的站点（目前是 DZZI）还会记录 `session_sid` 与 `token_type`：token 过期时先用会话 Cookie 调续期接口换新 token，**不需要再过验证码**
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
export QZD_API456="your_username#yourpass"
export QZD_NOTIFY=false
python newapi_checkin.py
```

Windows PowerShell：

```powershell
$env:QZD_API456="your_username#yourpass"
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

### DZZI 报「未配置 QZD_VLM」？

DZZI 开启了算术图形验证码，必须配一个支持图片输入的 OpenAI 兼容接口，三段用 `#` 连起来填在一个变量里：

```text
QZD_VLM = http://your-host:3000/v1#sk-xxxxxx#your-vision-model
          └──── base_url ────┘ │       │ │                   │
                              api_key └─┘     model ────────┘
```

`base_url` 指内网地址时不要配 `QZD_PROXY`（脚本对视觉模型请求单独建会话，不继承代理；确实需要走代理时用 `QZD_VLM_PROXY`）。

### 日志报「视觉模型返回空内容」？

这是**思考型模型**（如 nemotron-vl 系列）的典型表现：思考过程把 token 预算吃光，`content` 还没开始输出就被 `max_tokens` 截断。默认值已经放宽到 `3000`，仍为空就把 `QZD_VLM_MAX_TOKENS` 调到 `4096`；同时可把 `QZD_VLM_TIMEOUT` 调到 `120`（思考模型响应更慢）。脚本也会尝试从 `reasoning_content` 里捞答案，但首选还是给足 token 预算。

### DZZI 报「图形验证码未通过（已重试 N 次）」？

说明该轮所有验证码都被站点判错，常见原因：

- 视觉模型看错/答错：换更强的视觉模型，或把 `QZD_CAPTCHA_ATTEMPTS` 调到 `5`；
- 视觉模型接口返回非 200：看日志里的 `视觉模型返回 HTTP xxx`（欠费、模型不支持图片输入都会 400）；
- 站点把验证码限制调严了：看日志里「拉取验证码失败」的具体提示。

只影响当次登录；下次运行若会话 Cookie 仍有效，会走 `refresh` 续期，不再触发验证码。

### 本地跑单元测试

```bash
pip install pytest
python -m pytest tests -q
```

### 怎么扩展新的内置站点？

优先使用 `QZD_CUSTOM`。如果要内置到脚本中，可编辑 `SITE_PRESETS` 与 `ENV_PRESET_MAP`。
