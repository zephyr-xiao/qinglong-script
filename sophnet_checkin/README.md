# SophNet 签到

SophNet 云算力平台（www.sophnet.com）福利中心每日签到领 Token。纯 API 实现（`requests` + 标准库），无需浏览器与验证码；前提是提供 refreshToken（accessToken 仅约 2 小时有效，refreshToken 约 14 天有效）。

## 青龙任务命令

```text
task sophnet_checkin/sophnet_checkin.py
```

定时建议 `10 8 * * *`（每天 8:10，避开整点拥堵）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `SOPHNET_REFRESH_TOKEN` | 是 | - | refreshToken，多账号用 `&` 或换行分隔 |
| `SOPHNET_PROXY` | 否 | - | HTTP/SOCKS 代理，网络受限时填写；留空则直连，`trust_env` 关闭不读系统代理 |
| `SOPHNET_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `SOPHNET_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅当存在失败时才推送（需 `SOPHNET_NOTIFY=true`） |
| `SOPHNET_INTERACT` | 否 | `false` | 是否把「使用bot交互 N 分钟」三项任务做到发奖 |
| `SOPHNET_TIMEOUT` | 否 | `30` | HTTP 超时，单位秒 |
| `SOPHNET_DEBUG` | 否 | `false` | 输出各接口状态码与响应片段（凭证自动脱敏） |

多账号示例：

```text
SOPHNET_REFRESH_TOKEN=token1&token2&token3
```

通知复用青龙 `notify.py`：标题按全部成功 / 部分失败 / 全部失败分档，推送失败自动重试 3 次。

## 依赖安装

青龙一般已内置 `requests`；缺失时在「依赖管理 → Python」安装：

```text
requests
```

## 凭证获取

1. 浏览器登录 https://www.sophnet.com/（需通过阿里云验证码）；
2. F12 打开开发者工具，在 Console 执行 `localStorage.getItem('sophnet-auth-tab-sync-v1')`；
3. 取返回 JSON 中 `p.refreshToken` 的值（一串以 `Utbx...` 开头的长字符），填入 `SOPHNET_REFRESH_TOKEN`。

返回 `null` 说明当前标签未登录，先完成登录并停留在站内任意页面再执行；也可在 Application → Local Storage → www.sophnet.com 下找到同名键，取其 JSON 的 `p.refreshToken`。

## 交互任务（`SOPHNET_INTERACT`）

开启后签到结束会调用交互上报接口，把「使用bot交互 3/10/30 分钟」三项任务（阈值 180/600/1800 秒）做到发奖，合计约 78 万 Token/天。上报秒数取服务端下发的最高 `threshold`，不硬编码。

- 服务端限制：单次上报 ≤1800 秒、两次上报间隔 ≥10 秒、每日累计上限 7200 秒；超过最高阈值不产生额外奖励，因此只报到最高档。
- 三项已领取或当日进度已达标时直接跳过，不发请求，保证幂等。
- `recharge_100` 属充值类型，必须真实支付，脚本不处理。

该功能等价于把当天 bot 使用时长按固定值上报，平台若加风控风险在账号侧，请自行评估是否开启。

## 常见问题

- **提示 refreshToken 可能已失效** → 约 14 天过期，按「凭证获取」重新抓取并更新变量。
- **提示未登录或 accessToken 失效（HTTP 401）** → 多为 refreshToken 失效或被撤销；正常流程会先用 refreshToken 换新 accessToken。
- **请求超时或连接失败** → 填 `SOPHNET_PROXY`，Docker 青龙常用 `http://172.17.0.1:7890` 或 `socks5://172.17.0.1:7891`。
- **「今日已签到」算失败吗** → 不算。脚本识别 `status=1` 与「已签到 / already」等幂等文案，重复签到视为成功；签到前还会先查 `todayCheckedIn`。
- **开了 `SOPHNET_INTERACT` 却没上报** → 三项已领取或进度已达标会跳过；被限流时日志会打印「上报失败（本次已报 N 秒）」，下次定时任务补齐。
- **想排查接口变化** → `SOPHNET_DEBUG=true`，日志中 token 类字段统一前 6 后 4 脱敏，可放心贴出。
