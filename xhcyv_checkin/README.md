# 芯幻签到

适配青龙面板的芯幻（xhcyv.com，二次元 ACG 社区）每日签到脚本。纯 API 签到，不依赖浏览器 / Playwright / OpenCV，仅需 `requests` + 标准库；账号密码自动换取 `x_bbs_token`（JWT，有效期 3 天）并本地缓存复用。

## 青龙任务命令

```text
task xhcyv_checkin/xhcyv_checkin.py
```

建议定时 `5 8 * * *`（每天 8:05，与脚本头 `cron:` 一致；站点按北京时间划分自然日）。部署后先跑一次自检，**不会提交签到**；出现 `✅ 自检通过` 且退出码为 0 才算通过：

```text
task xhcyv_checkin/xhcyv_checkin.py --check
```

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `XHCYV_ACCOUNTS` | 是 | - | 账号密码，格式 `账号#密码`（兼容 `账号:密码` / `账号&密码`），多账号用换行分隔；每行也可以直接填 `x_bbs_token` |
| `XHCYV_ACCOUNT` | 否 | - | 单账号便捷写法，需同时配置 `XHCYV_PASSWORD`（会排在 `XHCYV_ACCOUNTS` 之前执行） |
| `XHCYV_PASSWORD` | 否 | - | 配合 `XHCYV_ACCOUNT` 使用 |
| `XHCYV_TOKEN` | 否 | - | 直接提供 `x_bbs_token`，优先于账号密码；多账号用 `&` 或换行分隔 |
| `XHCYV_PROXY` | 否 | - | HTTP/SOCKS 代理，网络受限时填写（未填时回退 `HTTPS_PROXY` / `HTTP_PROXY`） |
| `XHCYV_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `XHCYV_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅当存在失败时才推送（需 `XHCYV_NOTIFY=true`） |
| `XHCYV_TIMEOUT` | 否 | `30` | HTTP 超时时间，单位秒 |
| `XHCYV_DEBUG` | 否 | `false` | 输出各接口状态码与响应片段 |

## 依赖安装

青龙环境一般已内置 `requests`，缺失时在「依赖管理 → Python」安装 `requests`。只有登录被要求图形验证码时才需要 `ddddocr`（可选）。

## 凭证获取

1. 浏览器登录 `https://xhcyv.com/login`；
2. `F12` → Application → Cookies → `https://xhcyv.com`，找到 `x_bbs_token`，复制其值（`eyJ` 开头的三段 JWT）；
3. 填入 `XHCYV_TOKEN`（或 `XHCYV_ACCOUNTS` 的某一整行）。

`x_bbs_token` 是 HttpOnly Cookie，控制台 `document.cookie` 读不到，必须从 DevTools 的 Cookies 面板复制。

## 常见问题

**提示“账号或密码错误”（11003）** → 账号与邮箱都能填。分隔符 `#`、`:`、`&` 按行内最早出现者切分，推荐 `#`；密码以分隔符开头导致误切时，改用 `XHCYV_ACCOUNT` / `XHCYV_PASSWORD` 分开配置。

**提示“该账号已开启两步验证（2FA）”** → 脚本无法自动完成验证器验证。在站点「设置 → 安全」关闭 2FA，或改用 `XHCYV_TOKEN` 模式（到期后重新复制）。

**提示“登录需要图形验证码，但未安装 ddddocr”（10007）** → 在「依赖管理 → Python」安装 `ddddocr` 后重试，脚本会自动取码识别；识别一次仍失败会提示改用 `XHCYV_TOKEN`。

**提示“凭证已失效”** → 账密模式会自动清缓存并重登，通常看不到；`XHCYV_TOKEN` 模式 token 只有 3 天有效期，重新从浏览器复制 `x_bbs_token` 即可。

**签到返回“今日已签到”算成功吗** → 算。脚本签到前先查 `signed`，`code=11006` 也按幂等成功处理，不会重复提交。

**请求超时或连接失败** → 填 `XHCYV_PROXY`（如 `http://172.17.0.1:7890`）。脚本刻意设 `trust_env=False`，只认 `XHCYV_PROXY` 与 `HTTPS_PROXY` / `HTTP_PROXY`，避免 Windows 系统代理导致“本机能跑、容器超时”。

**token 缓存文件在哪** → `xhcyv_checkin/xhcyv_token.json`，权限 0600、原子写入，已被 `.gitignore` 的 `*_token.json` 覆盖，不要提交。
