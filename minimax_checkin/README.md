# MiniMax Code 签到

MiniMax Code 每日积分自动领取（`agent.minimax.cn` 的 signin 接口），**不需要订阅、不需要安装任何客户端**——脚本自己走一遍标准 OAuth device flow 完成授权。

每日 400 积分，连签第 4 天、第 7 天各 1000，积分 30 天过期。

## 为什么用 device flow

2026-10-19 起，MiniMax 桌面端改成 Shared MCode OAuth 架构：登录态不再写在 webview 的 localStorage，而是落到

```
~/.minimax/auth/<buildEnv>/<region>/mcode-public/auth.json
```

里面是不透明的 `mmoat_` token（不是 JWT），访问签到接口要带 `Authorization: Bearer`。

关键在于这个 token 由 `account.minimax.cn` 的**标准 RFC 8628 device flow** 签发，`client_id` 是公开的 `mcode-public`。也就是说：授权流程本身不需要客户端参与，脚本照着协议走一遍就能拿到凭据。

另一条路（抓桌面端登录态）需要装客户端，且客户端一旦退出登录凭据就没了，不如直接授权干净。

## 功能特点

- 零客户端、零订阅、**零第三方依赖**（只用标准库 `urllib`）
- 自带 device flow 授权：`login` 打印链接 + user_code，浏览器确认即可
- access token 过期自动用 refresh token 续期，续期后**原子写回**凭据文件
- **刷新失败可自愈**：已换到新 token 但主文件写不进去时，落 `auth-refresh.pending.json`，下次运行按 CAS 校验后合并——不会出现「旧 token 已作废、新 token 又丢了」的废号局面
- 与客户端同名的 `auth.lock` 目录锁 + owner 校验，避免并发刷新互相踢下线
- 幂等：先查状态，已领直接跳过；claim 本身也幂等（重复领取返回 `claim_result=2`）
- 登录态失效单独识别（业务码 `1022100011` / HTTP 401/403），并区分「真失效」与「网络抖动」——后者不会提示重新授权
- `--check` 只读自检，不提交签到，也不推送
- 复用青龙 `notify.py` 推送，失败重试 3 次且不影响签到结果
- 日志与 DEBUG 输出全程脱敏，不会打印 token

## 青龙任务命令

```bash
task minimax_checkin/minimax_checkin.py
```

建议 cron：`20 7 * * *`（避开 7:10 雨云、7:15 百度网盘）。

## 首次授权（唯一需要人操作的一步）

`login` 需要在浏览器里确认 user_code，不能扔给青龙定时跑。两种做法：

**方式一：在青龙容器终端里跑**（推荐，凭据路径天然正确）

```bash
docker exec -it qinglong-web-1 bash
cd /ql/data/scripts
python3 minimax_checkin/minimax_checkin.py login
```

按提示在浏览器打开链接、确认授权，凭据直接写入 `.auth/auth.json`。

**方式二：本机授权后上传**

```bash
python minimax_checkin.py login            # 本机跑，生成 .auth/auth.json
scp -r .auth <青龙宿主机脚本目录>/minimax_checkin/
```

⚠️ **上传完必须删掉本机那份**，原因见下面「凭据只能存在一份」。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MINIMAX_AUTH_FILE` | 否 | `<脚本目录>/.auth/auth.json` | 凭据文件路径，青龙里建议指到 `/ql/data` 下的持久目录 |
| `MINIMAX_NOTIFY` | 否 | `true` | 是否走青龙 `notify.py` 推送 |
| `MINIMAX_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅失败时推送 |
| `MINIMAX_TIMEOUT` | 否 | `20` | HTTP 超时秒数，允许 5–120 |
| `MINIMAX_DEBUG` | 否 | `false` | 打印接口响应摘要（已脱敏 + 截断 400 字符） |
| `MINIMAX_DESKTOP_VERSION` | 否 | `3.1.6` | 模拟的桌面端版本号，参与签名 |
| `MINIMAX_USER_ID` | 否 | `0` | 签名参数 `user_id`，实测服务端不严格校验 |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常：领取成功 / 今日已签 / 可领未领（`status` 模式）/ 活动未开启 |
| 1 | 失败：登录态失效、网络不可达、接口异常、领取失败 |
| 2 | 配置错误：没有凭据、未知命令 |

## 三个必须知道的坑

### 1. 凭据只能存在一份

**refresh token 是轮换制**：服务端每次刷新都换发新的，旧的那个立即作废。如果同一份凭据在两台机器上各跑一次，先刷新的那个会把另一份的 refresh token 顶掉，另一方下次就是 `invalid_grant`，需要重新授权。

所以：授权在哪台机器上做，凭据就只留在那台。本机授权后上传到 NAS 的，本机那份必须删掉。

### 2. 未领取状态的 `status` 是 1，不是 2

`status` 接口返回的面板长这样：

```json
{"data":{"days":[
  {"day_no":1,"points":400,"status":3,"is_today":true},
  {"day_no":2,"points":400,"status":1},
  {"day_no":4,"points":1000,"status":1}
]}}
```

- `status=3`：已领取
- `status=1`：未领取（未来天数实测都是 1）
- `status=2`：上游脚本假设的「可领取」态，实测未观察到

上游脚本按 `status != 2` 就判「今日不可领取」，会把当天的 `status=1` 直接跳过——**永远不会提交 claim，也就永远签不上**。本脚本只把 3 当已领，其余状态一律交给服务端裁决（claim 幂等，重复领返回 `claim_result=2`）。

### 3. 刷新写回失败会落 recovery 文件（带 CAS 校验）

刷新成功、但主凭据文件因权限/磁盘问题写不进去时，脚本会把新 grant 写到同目录的 `auth-refresh.pending.json`（0600），下次运行开头尝试合并回主文件。

合并用 **compare-and-swap**：只有主文件里目标记录的 refresh token 仍等于本次刷新用的旧 token 时才允许覆盖。如果主文件已经被别处更新过（重新 login、桌面端刷新、另一个进程刷新），说明 recovery 是过期数据，直接丢弃——否则会把已作废的旧凭据写回去，等于主动废号。主文件缺失或损坏时，recovery 是唯一真相源，会用它重建主文件。

这一步不能省：服务端已经轮换掉了旧 refresh token，新 token 若只存在内存里，进程一退出就彻底丢了，只能重新授权。`.gitignore` 已同时覆盖 `.auth/` 整个目录。

## 实测记录

2026-11-16 首次落地：

- device flow 端点 `POST /oauth2/device/code` 返回 200，`client_id=mcode-public` 被接受
- 授权后凭据为 `mmoat_` / `mmort_` 前缀，access token 有效期约 12 小时
- 强制过期后刷新：`generation` 1→2→3→4→5 递增，refresh token 每次轮换，`auth-state.json` 同步，无 recovery / 锁残留
- 容器内 `task` 命令跑通，Server酱 推送正常
- `--check` 自检确认不推送

## 测试

```bash
python -m unittest discover -s tests -v
```

59 个用例，全部打桩不发真实请求（`send_notify` 也统一 mock，不会真的发通知），覆盖：已签跳过、`status=1` 仍提交 claim（回归）、claim 幂等/业务码非 0/5xx、登录态失效、非对象 JSON、`days` 非数组、**空面板报错而非静默成功**、刷新轮换 generation、刷新请求失败不破坏原凭据、刷新网络异常归为可重试、`invalid_grant` 才判失效、**缺字段与 `error`/`error_description` 都不泄露 token**、**写回失败落 recovery 并可恢复**、**过期 recovery 不覆盖更新的主凭据（CAS 回归）**、**recovery 锁内重读（防误删新 recovery）**、**generation 不倒退**、**缺 `previousRefreshToken` 不合并**、**recordKey 指错时按 previous 回退**、**主文件缺失时用 recovery 重建**、**recovery 不污染其他客户端记录**、**state 写失败仍算刷新成功**、**优先选可续期记录**、锁路径为 `auth.lock`、stale 普通文件不死循环、release 不删他人锁、锁超时、锁冲突时 login 放弃写入、**首次 login 自动创建 `.auth` 目录**、签名头齐全、DEBUG 脱敏、device_login 成功/缺字段/非法过期值、退出码与 `--check` 不推送（带 auto 对照组）。

## 已知限制

以下是**刻意不处理**的边界，正常单进程签到不受影响：

- recovery 用固定文件名 `auth-refresh.pending.json`，同目录多个凭据文件会共用（脚本默认单凭据，不触发）。
- `recordKey` 按脚本自己的 auth 目录计算，与桌面端 `~/.minimax/auth` 的算法不同——这份 auth.json 仅供脚本使用，桌面端不会认。
- 没有 fsync 父目录；极端断电下 `os.replace` 可能未落盘（有 recovery 兜底）。
- recovery 的存在性检查在锁外，若它恰好在检查之后才被创建，本轮会跳过、下轮才消费（不会损坏数据）。
- 签名盐 `I*7Cf%WZ#S&%1RlZJ&C2` 是客户端公开常量，硬编码在脚本里；服务端若轮换需要同步更新。

## 来源与致谢

device flow 协议、签名算法与凭据格式来自 [Minatoxiaohu/agent-auto-signin](https://github.com/Minatoxiaohu/agent-auto-signin) 的 `minimax_signin.py`（逆向自 MiniMax Code 桌面端）。本脚本在其基础上做了青龙适配：删掉 macOS 专有的 leveldb / 配置文件回退、增加 `notify.py` 推送与 `--check`、凭据路径改为可配置，并修正了 `status` 判定。

MiniMax Code / MiniMax Agent 是 MiniMax 旗下产品，本项目与其无任何关联。签到属活动性质，规则可能随时变化，使用后果自负。
