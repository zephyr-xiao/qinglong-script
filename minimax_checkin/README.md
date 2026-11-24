# MiniMax Code 签到

MiniMax Code（agent.minimax.cn）每日积分自动领取，纯标准库实现，不需要订阅、不需要安装客户端——脚本自己走一遍 RFC 8628 device flow 完成授权。每日 400 积分，连签第 4、7 天各 1000，积分 30 天过期。

## 青龙任务命令

```bash
task minimax_checkin/minimax_checkin.py
```

定时建议 `20 7 * * *`。可用子命令：`auto`（默认，查状态后按需领取）、`status`、`login`（重新授权）、`--check`（只读自检，不提交也不推送）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MINIMAX_AUTH_FILE` | 否 | `<脚本目录>/.auth/auth.json` | 凭据文件路径，青龙里建议指到 `/ql/data` 下的持久目录 |
| `MINIMAX_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `MINIMAX_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅失败时推送 |
| `MINIMAX_TIMEOUT` | 否 | `20` | HTTP 超时秒数，允许 5–120 |
| `MINIMAX_DEBUG` | 否 | `false` | 打印接口响应摘要（已脱敏并截断 400 字符） |
| `MINIMAX_DESKTOP_VERSION` | 否 | `3.1.6` | 模拟的桌面端版本号，参与签名 |
| `MINIMAX_USER_ID` | 否 | `0` | 签名参数 `user_id`，实测服务端不严格校验 |

退出码：`0` 正常（领取成功 / 今日已签 / 可领未领 / 活动未开启）/ `1` 失败（登录态失效、网络不可达、接口异常、领取失败）/ `2` 配置错误（无凭据、未知命令）。

通知复用青龙 `notify.py`，推送失败重试 3 次且不影响签到结果；本地找不到时只打日志。

## 凭证获取

首次授权是唯一需要人工的一步，`login` 要在浏览器里确认 user_code，不能交给定时任务跑。推荐在青龙容器内执行（凭据路径天然正确）：

```bash
docker exec -it qinglong-web-1 bash
cd /ql/data/scripts
python3 minimax_checkin/minimax_checkin.py login
```

也可在本机 `login` 生成 `.auth/auth.json` 后上传到脚本目录，但上传完必须删掉本机那份，原因见下。

## 常见问题

- **凭据只能存在一份** → refresh token 是轮换制，每次刷新旧 token 立即作废。同一份凭据在两台机器各跑一次，先刷新的一方会把另一方顶掉，后者报 `invalid_grant` 需要重新授权。授权在哪台机器做，凭据就只留在那台。
- **未领取的 `status` 是 1，不是 2** → `status=3` 已领取、`status=1` 未领取。上游脚本按 `status != 2` 判「今日不可领取」，会永远跳过 claim；本脚本只把 3 当已领，其余交给服务端裁决（claim 幂等，重复领返回 `claim_result=2`）。
- **刷新写回失败会落 recovery** → 刷新成功但主文件因权限/磁盘问题写不进去时，新 grant 写入同目录 `auth-refresh.pending.json`（0600），下次运行开头尝试合并。合并用 CAS：只有主文件里的 refresh token 仍等于本次刷新用的旧 token 才允许覆盖，否则丢弃过期 recovery；主文件缺失或损坏时用它重建。少了这步，旧 token 已作废、新 token 只存内存，进程一退就只能重新授权。
- **登录态失效怎么分辨** → 业务码 `1022100011` / HTTP 401/403 才算真失效，网络抖动不会提示重新授权。
- **并发刷新会互相踢下线吗** → 有与客户端同名的 `auth.lock` 目录锁 + owner 校验，避免并发刷新冲突。
- **这份 `auth.json` 桌面端能用吗** → 不能。签名盐是客户端公开常量、硬编码在脚本里，服务端若轮换需同步更新；`recordKey` 算法与桌面端 `~/.minimax/auth` 不同，凭据仅供本脚本使用。

## 来源

改编自 [Minatoxiaohu/agent-auto-signin](https://github.com/Minatoxiaohu/agent-auto-signin)（逆向自 MiniMax Code 桌面端），本脚本做了青龙适配：去掉 macOS 专有的 leveldb / 配置文件回退、增加 `notify.py` 推送与 `--check`、凭据路径改为可配置，并修正 `status` 判定。
