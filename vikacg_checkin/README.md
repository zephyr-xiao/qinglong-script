# 维咔VikACG签到（vikacg_checkin）

维咔VikACG[V站]（https://www.vikacg.cc）每日钱包签到领积分，纯 API 免验证码，青龙面板 Python 脚本。

## 站点与接口说明

- 站点为 Nuxt 3 SSR 应用（Moonlight 主题），同源 API：`https://www.vikacg.cc/api/vikacg/v1/*`
- 登录即发 JWT 双 token（**有效期 30 天**），`Authorization: Bearer <token>` 鉴权
- 签到端点 `userMission` 幂等友好：未签返回 200 + 签到数据；已签返回 **HTTP 409「用户今天已签到」**（按成功处理）
- **服务端校验浏览器特征头**（`Sec-Fetch-*`、`Sec-Ch-Ua*`、`X-Client-Name: VikACG Moonlight`、`Architecture: AixPot`、`X-Device-Code`、`X-Client-Code` 等），缺失直接报「非法的客户端，请下载官方版本」——脚本已内置完整头模拟，勿删改 `BASE_HEADERS`
- 设备指纹（`X-Device-Code` / `X-Client-Code`，UUIDv4）与 token 绑定，随缓存持久化复用，避免每次运行换设备触发风控

## 部署

1. 青龙面板 →「脚本管理」→ 上传 `vikacg_checkin.py`
2. 「环境变量」→ 新建 `VIKACG_ACCOUNTS`（见下）
3. 「定时任务」→ 新建：命令 `task vikacg_checkin/vikacg_checkin.py`，定时规则 `23 8 * * *`（与脚本头 cron 一致即可）

依赖：`requests`（青龙一般自带，缺失去「依赖管理 - Python」安装）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `VIKACG_ACCOUNTS` | ✅ | — | 账号：`邮箱#密码`，多账号用 `&` 或换行分隔 |
| `VIKACG_PROXY` | 建议 | 空 | HTTP/SOCKS 代理，如 `http://172.17.0.1:7890`。**站点在 Cloudflare 后，大陆直连 TLS 常被掐断，一般需要代理**；也自动读取全局 `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`（优先级：专属变量 > 全局变量 > 直连）。SOCKS 代理需额外安装 `pip install requests[socks]` |
| `VIKACG_INSECURE` | — | `false` | 跳过 TLS 证书校验（仅自签代理等特殊场景），默认严格校验 |
| `VIKACG_NOTIFY` | — | `true` | 是否通过 notify.py 推送 |
| `VIKACG_NOTIFY_ONLY_FAIL` | — | `false` | 仅失败时推送（全成功静默） |
| `VIKACG_TIMEOUT` | — | `30` | HTTP 超时秒数 |
| `VIKACG_DEBUG` | — | `false` | 输出 token 来源等调试细节 |
| `VIKACG_BASE_URL` | — | `https://www.vikacg.cc` | 站点地址（备用域名时改） |

示例：

```
VIKACG_ACCOUNTS=aa****8514@gmail.com#password1&bb****66@gmail.com#password2
```

## 凭证缓存与降级链

登录态缓存在脚本同目录 `vikacg_token.json`（**只存 token / refreshToken / 设备指纹，绝不存密码**），按账号隔离：

1. 缓存 token 有效（JWT exp 剩余 > 12h）→ 直接签到
2. 过期 → 先 `POST /refreshToken` 静默续期（不触碰登录流程，对风控友好）
3. 续期失败 → 才重新账密登录并覆盖缓存

签到时遇 401 同样走「续期 → 重登」降级链，且**只重试一次**，防死循环。

## 常见问题

- **报「非法的客户端，请下载官方版本」**：请求头被改或站点更新了客户端校验。脚本内置的浏览器模拟头是逆向当前前端 bundle 得出的，请勿精简；若站点改版需重新抓包对照。
- **全部请求超时/连接失败**：大概率直连被墙，配 `VIKACG_PROXY`。日志会打印代理来源，代理没生效一眼可查。
- **提示密码错误**：检查 `VIKACG_ACCOUNTS` 里 `#` 分隔是否正确；密码本身含 `#` 时按首个 `#` 切分。
- **换密码后**：删掉 `vikacg_token.json` 中该账号的条目（或整个文件）即可，下次运行自动重登。

## 单元测试

```bash
cd vikacg_checkin
python -m unittest discover -s tests -v
```
