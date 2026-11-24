# 百度网盘签到

百度网盘有两套互相独立的签到体系，本脚本都做了：网页端会员成长值签到（含每日答题、成长值页广告任务）和 App 金币任务中心签到（含累计档位奖励），另有网页任务中心、积分中心与补签广告。注意「PC 签到」指网页端成长值签到，百度网盘 PC 客户端本身没有签到功能。App「赚积分」页模块默认关闭（数据源未定位）。

## 青龙任务命令

```text
task baiduwangpan_checkin/baiduwangpan_checkin.py
```

`--probe` / `--check` 为只读自检，不向百度写入数据；青龙「运行」按钮不转发额外参数，要带参数得用「调试」终端，或改用对应环境变量。

## 环境变量

只需 `BAIDU_COOKIE`，其余全部可省略。

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `BAIDU_COOKIE` | 是 | 空 | 整条 Cookie，多账号用换行分隔 |
| `BAIDU_CUID` | 否 | 从 cookie 的 `BAIDUCUID` 提取 | 设备标识，缺失时随机生成并写入 `.cuid` |
| `BAIDU_ENABLE_PC` | 否 | `true` | 网页端会员成长值签到 |
| `BAIDU_ENABLE_QUESTION` | 否 | `true` | 每日答题 |
| `BAIDU_ENABLE_APP` | 否 | `true` | App 金币任务中心签到 |
| `BAIDU_ENABLE_AWARDS` | 否 | `true` | 累计档位奖励领取 |
| `BAIDU_ENABLE_ADS` | 否 | `true` | 成长值页任务（广告 70 + 浏览类 13） |
| `BAIDU_ENABLE_TASKCENTER` | 否 | `true` | 任务中心任务 |
| `BAIDU_ENABLE_POINTS` | 否 | `true` | 积分中心任务 |
| `BAIDU_ENABLE_MAKEUP` | 否 | `true` | 补签广告（请求量最大，可单独关） |
| `BAIDU_TIMEOUT` | 否 | `15` | 单请求超时秒，范围 5-120 |
| `BAIDU_TRY_TYPE` | 否 | 空 | 等同 `--try-type`，写操作诊断，用完必须删 |
| `BAIDU_TRY_SOURCE` | 否 | `points` | 等同 `--source`，配合 `BAIDU_TRY_TYPE` |
| `BAIDU_ENABLE_ACT` | 否 | `false` | App「赚积分」页任务，数据源未定位，确认可用再开 |
| `BAIDU_ACT_TYPES` | 否 | `13,70` | App 任务 `task_type` 白名单，逗号分隔 |
| `BAIDU_ACT_ID` | 否 | `987141565000` | App「赚积分」活动 ID，来自页面 URL 的 `id` 参数 |
| `BAIDU_ACT_VERSION` | 否 | `10.0.154` | App 版本号，参与 `/act/task/*` 请求 |
| `BAIDU_ACT_DEVID` | 否 | 空（用 cuid） | App 端 `devuid`，格式如 `XXXX\|0` |
| `BAIDU_PROBE_ACT` | 否 | `false` | 等同 `--probe-act`（只读探测），用完必须删 |
| `BAIDU_UA` | 否 | 移动端 Chrome UA | 自定义 UA |
| `PRIVACY_MODE` | 否 | `true` | 通知里用户名打码 |
| `RANDOM_SIGNIN` | 否 | `true` | 启动时随机延迟 |
| `MAX_RANDOM_DELAY` | 否 | `3600` | 随机延迟上限秒 |

关掉某个模块写成字符串 `false`，例如 `BAIDU_ENABLE_MAKEUP=false`。

## 依赖安装

青龙「依赖管理」-「Python」安装 `requests`；缺失时脚本会直接提示并退出。

## 凭证获取

浏览器登录 <https://pan.baidu.com/> → F12 打开 Network → 刷新 → 任一请求的 Request Headers → 复制完整 Cookie，填入 `BAIDU_COOKIE`。

## 常见问题

- **Cookie 失效** → 业务码 `errno=-6`。脚本不认 HTTP 200，只认白名单业务码；响应同时带 `errno` 与 `error_code` 时两个都必须通过，任一冲突即判失败。
- **Cookie 过期被 302 到登录页** → 脚本设置 `allow_redirects=False`，避免把登录页 HTML 误判成签到成功。
- **成长值接口返回 `errno: null` + `error_code: 0`** → 实测如此，`null` 按「字段不存在」处理，不判格式异常。
- **签到成功但成长值没涨** → 成长值只用于如实展示，不作成功判据（签到本身可能 +0）；测量失败只记 warning，不参与账号成败。
- **任务类型 84 / 114 / 133 / 214 / 257 无法完成** → 需真实 App 参与，`tasksave` 返回 `errno=40002 user task does not exist`，已从白名单跳过。
- **累计奖励只领到部分档位** → 脚本按 `received` 是否推进循环领取，某次领取后计数不动立即停手报错，既不漏领也不重复领；每档 `prize` 是数组（实测 2 项）。
- **任务签名失效** → 任务完成需 MD5 签名（PC 端带盐、App 端不带盐且 `clienttype=110`）；站点若改盐，`tasksave` 返回业务错误，脚本明确报错并跳过该模块，不影响签到主流程。
- **App「赚积分」拉不到任务列表** → 该页数据源不在 `/api/taskscore/tasklist` 的 7 个 `task_from` 里；`task_from` 由服务端枚举校验（已知 7 个即全集），不存在的值一律返回 `errno=40001`，需 App 端抓包才能定位，模块默认关闭。
- **广告 / 补签不是真播放** → 脚本直接调 `tasksave` 标记完成，不需要真机或播放器。
- **请求量与自检** → 全模块约 30-60 个请求；首次先跑 `--probe`、`--check`（任一账号失败、接口报错或出现 `errno=-6` 时退出码为 1，可直接当部署自检）。
- **想验证某类任务能否自动完成** → 用 `BAIDU_TRY_TYPE=84`（等同 `--try-type`，只打一个任务、不重试，会消耗掉该任务）；用完必须删，否则每天只做这次试探、不执行签到。

## 来源

改编自 [awakaze/cf-netdisk-signin](https://github.com/awakaze/cf-netdisk-signin)（App 金币任务中心签到与累计奖励、严格业务码判定）和 [jujiunofly/baidu-signin](https://github.com/jujiunofly/baidu-signin)（登录态预检、PC 端任务签名盐、广告/任务中心/积分/补签接口）。
