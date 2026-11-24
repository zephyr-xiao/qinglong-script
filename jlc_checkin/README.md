# 嘉立创签到（jlc_checkin）

自动完成嘉立创（立创商城 m.jlc.com）每日签到领金豆，签到周期第七天自动领取 8 金豆券，签到后刷新金豆余额并随通知推送。AccessToken 认证，只依赖 `requests`，无需浏览器环境。

## 青龙任务命令

```text
task jlc_checkin/jlc_checkin.py
```

定时建议 `40 8 * * *`（每天上午 8:40，可自行调整）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `JLC_TOKEN` | 是 | — | AccessToken，多账号用换行分隔 |
| `JLC_PROXY` | 否 | 空 | HTTP 代理，如 `http://172.17.0.1:7890` |
| `JLC_NOTIFY` | 否 | `true` | 是否推送 |
| `JLC_NOTIFY_ONLY_FAIL` | 否 | `false` | 全部成功时静默 |
| `JLC_TIMEOUT` | 否 | `30` | 请求超时秒数 |
| `JLC_DEBUG` | 否 | `false` | 调试模式，输出接口响应细节 |

多账号格式（换行分隔）：

```text
账号1的AccessToken
账号2的AccessToken
```

## 依赖安装

青龙「依赖管理」→「Python」安装 `requests`。

## 凭证获取

1. **用 Chrome 浏览器**打开 https://m.jlc.com 并登录（必须 Chrome：Edge 的设备模拟抓不到该请求头）。
2. F12 打开开发者工具，按 **Ctrl+Shift+M** 切换移动设备模拟后刷新页面（PC 直连会跳转 PC 版页面，PC 端请求不发该请求头）。
3. 切到「网络」标签，刷新页面，点任意发往 `m.jlc.com` 的请求。
4. 在「请求标头」中找到 `X-JLC-AccessToken`，复制其值。

也可以用手机抓包工具（如 Stream / HttpCanary）从嘉立创 App 内抓取。**拿到 Token 后不要在网页或 App 上手动退出账号**，否则 Token 立即失效；正常登录状态下长期有效，推送提示「Token 已失效」时重新抓取并更新环境变量即可。

## 常见问题

- **提示「AccessToken 已失效」** → 抓包时或在其他设备登录导致 Token 刷新，重新抓取后更新 `JLC_TOKEN`。
- **显示「第七天签到，领券接口无收益」** → 第七天奖励需按活动规则领取，可能当日已被领取或不满足条件，次日观察即可。
- **一天可以跑多次吗** → 可以，已签到时自动跳过（幂等），无副作用。网络抖动会自动重试 3 次（线性退避）。
- 通知复用青龙 `notify.py`，不用在脚本里单独配推送。

## 来源

移植自 [Foticing/LC-AutoSign](https://github.com/Foticing/LC-AutoSign)，按青龙面板规范重写。
