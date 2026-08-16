# 7x.hk / 8s.hk 签到

适配青龙面板的 7x.hk & 8s.hk（NewAPI 架构）自动签到脚本，仅依赖 `requests` + 标准库。

站点：
- `https://7x.hk/profile`
- `https://8s.hk/profile`

签到流程：① `GET /api/user/self` 验证登录 → ② `POST /api/user/checkin` 签到 → ③ `GET /api/user/checkin?month=YYYY-MM` 查本月统计。

## 功能特点

- 单脚本同时支持 **7x.hk** 和 **8s.hk** 两个站点；
- 使用 `session` Cookie 认证签到（NewAPI 标准登录态）；
- 支持多账号，使用 `&` 分隔；
- 可选 `new-api-user` 请求头，适配多用户场景；
- 签到前先验证 session 有效性，过期会明确提示；
- 支持"已签到"幂等识别（重复签到视为成功）；
- 支持代理，适合网络受限场景；
- 本月签到天数与累计额度统计；
- 支持青龙 `notify.py` 推送；
- Debug 模式可输出请求 URL、状态码和响应片段。

## 青龙任务命令

```text
task 7x8s_checkin/7x8s_checkin.py
```

建议定时：

```text
20 8 * * *
```

（每天 8:20 执行，避开整点拥堵）

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `S7XHK_SESSION` | 否 | - | 7x.hk 的 session 值，多账号用 `&` 分隔；不配则跳过该站 |
| `S7XHK_USER_ID` | 否 | 自动 | 7x.hk 的 `new-api-user` 头；留空时自动从 session 解码 |
| `S8SHK_SESSION` | 否 | - | 8s.hk 的 session 值，多账号用 `&` 分隔；不配则跳过该站 |
| `S8SHK_USER_ID` | 否 | 自动 | 8s.hk 的 `new-api-user` 头；留空时自动从 session 解码 |
| `S7X8S_PROXY` | 否 | - | HTTP/SOCKS 代理，网络受限时填写 |
| `S7X8S_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `S7X8S_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅当存在失败时才推送（需 `S7X8S_NOTIFY=true`，全部成功则静默） |
| `S7X8S_TIMEOUT` | 否 | `30` | HTTP 超时时间，单位秒 |
| `S7X8S_DEBUG` | 否 | `false` | 输出请求 URL、状态码和响应片段 |

> **提示**：最少只需配置一个站点即可运行。如只配 `S7XHK_SESSION`，脚本只处理 7x.hk，8s.hk 自动跳过。

代理示例：

```text
S7X8S_PROXY=http://172.17.0.1:7890
```

或：

```text
S7X8S_PROXY=socks5://172.17.0.1:7891
```

## session 获取方式

1. 浏览器登录 `https://7x.hk/`（或 8s.hk）；
2. 打开开发者工具（F12）；
3. 进入 **Network / 网络**；
4. 刷新页面或打开控制台页面，触发一个 `/api/` 开头的请求；
5. 在该请求的请求头（Request Headers）中找到 `Cookie` 字段；
6. 复制其中 `session=` 后面的值（只取 session 的值，不要带 `session=` 前缀，也不要带其他 cookie）；
7. 填入青龙环境变量 `S7XHK_SESSION`（或 `S8SHK_SESSION`）。

> session 形如一串长字符（例如 `eyJhbGciOi...` 或类似 token）。脚本会自动拼成 `Cookie: session=<值>` 发送。

> **user_id 自动解码**：NewAPI 的签到接口除 session 外还要求带 `new-api-user` 请求头（值为用户 ID）。脚本会自动从 session 内部的 gob 编码中解码出 user_id，**无需手动填写 `S7XHK_USER_ID` / `S8SHK_USER_ID`**。仅当自动解码失败（日志提示"未能从 session 解码 user_id"）时，才需手动配置。

多账号示例：

```text
S7XHK_SESSION=session值1&session值2&session值3
```

如需手动指定 `new-api-user`（一般无需，自动解码即可），按相同顺序用 `&` 分隔，缺省位置留空：

```text
# 3 个 session，仅第 2 个账号手动带 user_id
S7XHK_SESSION=s1&s2&s3
S7XHK_USER_ID=&8411&
```

注意：session 中如果包含 `&` 字符（极罕见），会被当作账号分隔符，导致解析错误。如遇此情况，请单独配置。

## 额度说明

NewAPI 的额度（quota）为「单位额度」，换算关系为 **1 美元 = 500000 额度**。

脚本默认对额度数字做 K/M 缩写展示，不换算成美元。如需换算，可自行将额度除以 500000。

- 签到成功：`+<本次奖励额度>`
- 本月统计：`累计 <本月总额度>`

## 依赖安装

青龙环境一般已内置 `requests`。如缺失，在青龙「依赖管理」-「Python」中安装：

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
export S7XHK_SESSION="your_session_value"
export S8SHK_SESSION="your_session_value"
export S7X8S_NOTIFY=false
export S7X8S_DEBUG=true
python 7x8s_checkin.py
```

Windows PowerShell：

```powershell
$env:S7XHK_SESSION="your_session_value"
$env:S8SHK_SESSION="your_session_value"
$env:S7X8S_NOTIFY="false"
$env:S7X8S_DEBUG="true"
python .\7x8s_checkin.py
```

> ⚠️ 环境变量名以数字开头时，PowerShell 可能无法直接赋值。请确保使用正确语法或通过 bash 运行。

## 通知

- 青龙环境中自动复用青龙 `notify.py`；
- 设置 `S7X8S_NOTIFY=false` 可关闭推送；
- 标题按成败分档显示：`✅ 全部成功（N/N）` / `⚠️ 部分失败（M/N）` / `❌ 全部失败（0/N）`；
- 正文为 Markdown 分组格式，顶部带 ⏰ 执行时间、📊 账号统计，结果按站点分组、按「成功 / 失败」列出；
- 设置 `S7X8S_NOTIFY_ONLY_FAIL=true` 可在本次全部成功时静默不推送（仅在有失败时才通知）；
- 推送失败会自动重试最多 3 次；
- 本地运行找不到 `notify.py` 时会跳过推送。

## 常见问题

### 提示 Session 已过期？

session 具有时效性，过期后需重新登录，按「session 获取方式」重新复制 session 值并更新环境变量。

### 提示"缺少 user_id"或"未能从 session 解码 user_id"？

正常情况下脚本会自动从 session 解码出 user_id，无需配置。若出现此提示，说明 NewAPI 的 session 编码格式发生变化，自动解码失效。可手动获取 user_id：

1. 浏览器登录后按 F12 打开开发者工具；
2. 进入 Application / 应用 → Local Storage → 站点域名；
3. 找到 `user` 相关键，其中的 `id` 字段即为 user_id（数字）；
4. 填入 `S7XHK_USER_ID` 或 `S8SHK_USER_ID`。

同时建议反馈，便于更新解码逻辑。

### 请求超时或连接失败？

若站点网络受限，请在 `S7X8S_PROXY` 填写青龙容器内可访问的代理地址。Docker 青龙常见代理值：

```text
http://172.17.0.1:7890
```

### 不知道脚本发了哪些请求？

开启：

```text
S7X8S_DEBUG=true
```

脚本会输出每个请求的 URL、状态码和响应片段，便于排查接口变化或认证问题。

### 多账号怎么配置？

用 `&` 分隔多个 session 值：

```text
S7XHK_SESSION=session1&session2&session3
```

脚本会逐个签到并汇总，账号之间间隔 2 秒避免风控。

### 签到返回"今日已签到"算成功吗？

算。脚本已对"已签到 / 已经签到 / already / 重复签到"等幂等提示做识别，重复签到视为成功，不会报失败。
