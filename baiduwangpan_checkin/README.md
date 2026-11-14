# 百度网盘签到（PC + App 双端）

百度网盘现在有**两套互相独立**的签到体系，各自加各自的奖励。本脚本把两套都做了，外加成长值页广告、任务中心、积分中心和补签广告。

| 体系 | 入口 | 本脚本对应模块 |
|---|---|---|
| A. 会员成长值（网页端） | `pan.baidu.com` 会员成长页 | 网页签到、每日答题、成长值任务 |
| B. 金币任务中心（App 端） | App「我的 → 任务中心」 | App 签到、累计档位奖励 |
| C. 任务中心（网页版） | 成长值页旁的任务中心 | 任务中心 |
| D. 积分中心 | 积分中心页 | 积分任务 |
| E. 补签 | 补签广告 | 补签广告 |
| F. App「赚积分」页 | App 内「赚积分」（AI 功能 / 日常 / 功能任务） | App任务（**默认关闭**，数据源未定位） |

> 「PC 签到」指的是网页端会员成长值签到，不是 PC 客户端签到——百度网盘 PC 客户端没有独立签到功能。

## 快速开始

```bash
# 青龙任务命令
task baiduwangpan_checkin/baiduwangpan_checkin.py
```

只需要配一个环境变量：

| 变量 | 必填 | 说明 |
|---|---|---|
| `BAIDU_COOKIE` | ✅ | 整条 cookie。多账号用**换行**分隔 |

获取 cookie：浏览器登录 <https://pan.baidu.com/> → F12 → Network → 刷新 → 任意请求的 Request Headers → 复制完整 Cookie。

## 环境变量

除 `BAIDU_COOKIE` 外全部可省略。

| 变量 | 默认 | 说明 |
|---|---|---|
| `BAIDU_CUID` | 从 cookie 的 `BAIDUCUID` 提取 | 设备标识，缺失时随机生成并写入 `.cuid` |
| `BAIDU_ENABLE_PC` | `true` | 网页端会员成长值签到 |
| `BAIDU_ENABLE_QUESTION` | `true` | 每日答题 |
| `BAIDU_ENABLE_APP` | `true` | App 金币任务中心签到 |
| `BAIDU_ENABLE_AWARDS` | `true` | 累计档位奖励领取 |
| `BAIDU_ENABLE_ADS` | `true` | 成长值页任务（广告 70 + 浏览类 13） |
| `BAIDU_ENABLE_TASKCENTER` | `true` | 任务中心任务 |
| `BAIDU_ENABLE_POINTS` | `true` | 积分中心任务 |
| `BAIDU_ENABLE_MAKEUP` | `true` | 补签广告（单次最多 15+ 轮请求） |
| `BAIDU_TIMEOUT` | `15` | 单请求超时秒，范围 5-120 |
| `BAIDU_TRY_TYPE` | 空 | 等同 `--try-type`，**写操作**诊断用，用完必须删 |
| `BAIDU_TRY_SOURCE` | `points` | 等同 `--source` |
| `BAIDU_ENABLE_ACT` | `false` | App「赚积分」页任务。**数据源未定位，实测拉不到任务列表**，确认可用后再开 |
| `BAIDU_ACT_TYPES` | `13,70` | App 任务的 `task_type` 白名单，逗号分隔 |
| `BAIDU_ACT_ID` | `987141565000` | App「赚积分」活动 ID，来自页面 URL 的 `id` 参数 |
| `BAIDU_ACT_VERSION` | `10.0.154` | App 版本号，参与 `/act/task/*` 请求 |
| `BAIDU_ACT_DEVID` | 空（用 cuid） | App 端 `devuid`，格式如 `XXXX\|0` |
| `BAIDU_PROBE_ACT` | `false` | 设为 `true` 等同 `--probe-act`，用完必须删 |
| `BAIDU_UA` | 移动端 Chrome UA | 自定义 UA |
| `PRIVACY_MODE` | `true` | 通知里用户名打码 |
| `RANDOM_SIGNIN` | `true` | 启动时随机延迟 |
| `MAX_RANDOM_DELAY` | `3600` | 随机延迟上限秒 |

关掉某个模块写成字符串 `false`，例如 `BAIDU_ENABLE_MAKEUP=false`。

## 命令行参数

| 参数 | 作用 |
|---|---|
| `--probe` | **只读**探测，打印各接口的真实返回结构，不签到不领奖 |
| `--probe-act` | **只读**探测 App 任务中心的真实来源：逐个试候选 `task_from`，并读任务中心完成记录反查 |
| `--check` | 只查登录态、成长值、累计档位、积分余额 |
| `--try-type <类型>` | **写操作**：对指定 `task_type` 的单个任务真发一次 `tasksave`，用来验证某类任务能否自动完成。会消耗掉这个任务 |
| `--source points\|taskcenter\|app` | 配合 `--try-type`，指定任务来源，默认 `points` |

`--probe` 和 `--check` 不向百度写入任何数据（只会在本地生成设备号缓存文件），适合首次部署和排障；任一账号失败、任一接口报错或出现 `errno=-6` 时退出码为 1，可以直接当部署自检用。

`--try-type` 是诊断用的写操作：只打一个任务、不重试，返回待领奖时才补一次领奖，最后以重新拉取的列表和积分余额为准。用来判断某个 `task_type`（例如 84）能不能进白名单。

> 青龙的「运行」按钮执行的是任务里保存的命令，**不会转发额外参数**。要带参数得用「调试」终端；或者改用 `BAIDU_TRY_TYPE=84` 环境变量后正常点「运行」。**环境变量用完必须删掉**，否则每天只会做这一次试探、不执行签到。

## 通知

脚本按以下顺序查找青龙的 `notify.py`，找到就调用 `send(title, content)`：

1. 脚本自己所在目录
2. 上一级目录（青龙默认的 `/ql/data/scripts`）
3. `/ql/data/scripts`
4. `/ql/scripts`

**找不到时只打印日志，不影响签到。** 日志里的两种提示：

- `✅ 已加载notify.py通知模块` —— 通知生效
- `⚠️ 未加载通知模块，通知只写日志` —— 青龙里没有 `notify.py`，需要自行放一个到 `/ql/data/scripts/`

> 脚本放在子目录里时，Python 的 `sys.path` 只含该子目录，父目录默认不可导入，所以这里做了多路径回退。

## 设计要点

**1. 严格业务码判定，不认 HTTP 200**

百度经常返回 HTTP 200 但业务失败（`errno=-6` 表示 cookie 失效）。脚本要求业务码必须是整数且在允许列表内，缺失或格式异常一律报错——绝不把「没看懂」当成成功。

部分响应会同时带 `errno` 和 `error_code`，两个字段的语义优先级在两个参考实现里还相反。脚本采取保守策略：**两个字段都必须通过**，任一冲突即判失败。

**2. 不跟随重定向**

`allow_redirects=False`。cookie 失效时百度会 302 到登录页，跟过去会拿到 200 的 HTML，被误判成签到成功。

**3. 每一步操作前后测成长值**

只用于**如实展示**，不作成功判据——签到本身可能 +0，用 delta 判成功会全错。操作抛错后也会回读余额，因为请求可能已经到达服务端。成长值读不到时**照常执行签到**，只把该步标成「未测量」，测量类问题统一进 `warnings`，不参与账号成败判定。

**4. 幂等复核**

- 网页签到：`421001` + `repeat`/`已签`/`重复` → 已签到；消息语义不明则报错
- App 签到：`signed_today == 1` 跳过；请求成功后**再查一次**确认
- 累计奖励：每档 `prize` 是数组（实测 2 项），按「`received` 在推进就继续领」循环；某次领取后计数不动立即停手报错，既不漏领也不重复领
- 任务：`task_status == 1` 跳过；`== 6`（待领奖）直接调领奖，不重复完成
- 补签：`task_finish_count >= count_max` 跳过

**5. 单模块失败不影响其它模块**

每个模块独立 try/except，失败项进汇总通知。HTTP 层只在网络异常时重试 3 次，业务失败不重试。

## 接口清单

| 模块 | 接口 |
|---|---|
| 登录态 | `/api/loginstatus` |
| 成长值/等级 | `/rest/2.0/membership/user?method=query` |
| 网页签到 | `/rest/2.0/membership/level?method=signin` |
| 每日答题 | `/act/v2/membergrowv2/getdailyquestion` + `answerquestion` |
| App 签到 | `/coins/taskcenter/signinlist` / `tasklist` / `signin` |
| 累计奖励 | `/coins/taskcenter/signinaward?day=N` |
| 任务列表/完成 | `/api/taskscore/tasklist` + `/api/taskscore/tasksave` |
| 积分中心 | `/coins/center/tasklist` + `/coins/points/balance` |
| 补签广告 | `/api/taskscore/tasklist?task_from=egg_checkin_makeup_task` |

任务完成需要 MD5 签名：

```
token = md5(f"{task_id}_{uk}_{rand}_{ts}_{SALT}")
```

## 已知风险

1. **累计奖励一次领几个奖品未实测** — 实测档位为 7/15/30/60/90 天、每档 `prize` 2 项、`received` 上限 2。脚本按「`received` 推进就继续领」循环处理，一次全领和一次领一个都能覆盖；若某次领取后计数不动会立即停手报错，不会重复领。
2. **任务签名盐可能变更** — 若失效，`tasksave` 会返回业务错误，脚本明确报错并跳过该模块，不影响签到主流程。
3. **请求量** — 全模块跑一轮约 30-60 个请求。首次建议先 `--probe`、`--check`，确认无误再全开；`BAIDU_ENABLE_MAKEUP` 请求量最大，可单独关。
4. **广告/补签不是真实播放** — 脚本直接调 `tasksave` 标记任务完成，不需要真机或播放器。
5. **App「赚积分」模块未接通** — `BAIDU_ENABLE_ACT` 默认 `false`。App 页里的 AI/功能任务数据源已确认不在 `/api/taskscore/tasklist` 的 7 个 `task_from` 里，需要 App 端抓包才能定位；代码保留待启用，不影响其它模块。
6. **去第三方 App 的任务无解** — `task_type=84/114/133/214/257` 调 `tasksave` 返回 `errno=40002 user task does not exist`，服务端要求用户先真实参与，无法伪造。

## 实测记录（2026-11-14 真实账号）

- `/rest/2.0/membership/user` 返回 **`errno: null` + `error_code: 0`**，所以 `null` 必须当「字段不存在」处理，不能判成格式异常
- 累计奖励档位：**7 / 15 / 30 / 60 / 90 天**，每档 `prize` 2 项，`received` 上限 2
- 补签广告是 6 个 `task_type=70` 任务，`count_max` 分别 20/15/10/7/5/3，合计 **60 次**
- 积分中心任务分值 200–800，完成任务后余额变动与 `task_score` 之和完全一致
- 任务中心 `task_type` **84 / 114 / 133 / 214 / 257** 需要真实 App、付费或群共享，已被白名单跳过
- `task_type=84` 用 `--try-type` 实测：`tasksave` 返回 **`errno=40002 / user task does not exist`**，服务端没有该用户的任务记录，必须先真实参与才有得领，确认无法自动完成
- App「赚积分」页（`lifeactivity/task/mainpage`）用的是另一套接口：`/act/task/tasklist?id=<活动ID>&task_from=netdisk_activity`，返回 `data.new_all` / `data.fresh` / `data.daily`，且 `task_type` 藏在 `task_extra` 的 JSON 字符串里；请求必须带 `clienttype=1&channel=android&rand&devuid&time&version&_t`，缺一个就返回 `errno=1011`
- App 端 `tasksave` 的签名与 PC 端不同：**`md5(task_id_uk_rand_time)`，不带盐**，且 `clienttype=110`
- `/act/*` 这组接口会校验 Referer，脚本按活动页 URL 覆盖 Referer；仍返回 `errno=1011` 时用 `--probe-act` 跑参数矩阵定位
- **`task_from` 是服务端枚举校验的**：不存在的值一律返回 `errno=40001`。实测 `task_sys_ai_new / task_sys_ai_task / task_sys_svip / task_sys_growth_task / svip_task / netdisk_activity / svipday2606` 全部 40001，已知的 7 个就是全集
- `clienttype` / `web` / `channel`（`0/1`、`1/5`、`110/5`、`channel=android`）**不影响返回的任务集合**：`task_sys_ai` 恒为 1 条，7 分类合并恒为 40 条
- `/act/v2/taskcenter/recordlist` 需要未知参数（无参/带 uk 均 `errno=2 参数错误`）
- **结论**：App「赚积分」页里的 AI 功能任务（体验AI学习服务、云一朵整理、AI笔记、AI拍一拍…）和功能任务（打开push开关、分享文件、书城/短剧30s）**无法通过现有接口获取**，需要 App 端抓包才能定位真实来源。`act` 模块因此默认关闭
- 成长值页除广告（`70`）外，浏览类（`13`）也能自动完成；填表（`86`）、扫码（`126`）类跳过
- 成长值页广告 `task_score=6`，实测成长值 +5（以 `addGrowScore` 为准，不以 `task_score` 为准）

## 测试

```bash
cd baiduwangpan_checkin
python -m unittest discover -s tests -v
```

80 项单元测试，全部打桩，不发真实请求。覆盖业务码格式（含 null 视为缺失、非法类型拒绝、两字段冲突按失败）、`421001` 五种语义分支、302 拒绝、Referer 覆盖、网络异常脱敏、App 签到三态、累计奖励单奖品/多奖品/不推进三种路径与并发已领、任务 DONE/WAIT/其他、成长值页类型白名单、签名公式（PC 带盐 / App 不带盐）、补签循环上限与计数停滞、测量失败只记 warning 不判异常、答题 WAIT 补领、多账号中止计入退出码、App 任务去重与白名单、`--try-type` 只发一次请求、命令行与环境变量两种诊断入口。

## 来源与参考

| 来源 | 贡献 |
|---|---|
| 本仓库原有的单文件 `baiduwangpan_checkin.py` | 骨架：青龙 `notify` 通知、多账号、随机延迟、隐私打码、答题与用户信息查询 |
| [awakaze/cf-netdisk-signin](https://github.com/awakaze/cf-netdisk-signin) | App 金币任务中心签到与累计档位奖励的接口路径、严格业务码判定、幂等复核思路 |
| [jujiunofly/baidu-signin](https://github.com/jujiunofly/baidu-signin) | 登录态预检、PC 端任务签名盐、广告/任务中心/积分/补签接口 |

本脚本在此基础上面向青龙面板重写：多账号、`notify.py` 通知、全环境变量配置、`--probe` / `--check` / `--probe-act` 自检。

接口协议部分是对百度网盘公开 H5 / App 接口的实测逆向（含前端 JS 分析），与上述项目**无代码级复用**；`task_from` 枚举、App 端无盐签名、`errno` 语义等结论均来自真实账号实测，记录在「实测记录」章节。
