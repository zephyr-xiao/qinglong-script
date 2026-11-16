# 夸克网盘签到

夸克网盘每日签到领空间（`drive-m.quark.cn` 的 growth 接口），走移动端签名参数 `kps`/`sign`/`vcode`，多账号、严格判定、失败可感知。

## 为什么不用 Cookie

2026 年 11 月起，夸克对 PC 端 Cookie 的 `growth/info` 与 `growth/sign` 都返回 **HTTP 200 + 空 `data`**：

```json
{"status":200,"code":0,"message":"","data":{}}
```

签到实际没有发生，但按 `if response.get("data"):` 写的旧脚本会把空 dict 当假值直接跳过，既不报错也不返回非零退出码——青龙标绿、推送照发，等于静默失效。本脚本把「空 data」当作硬错误处理，只走仍然有效的移动端签名参数路径。

## 功能特点

- 移动端签名参数签到（`pr=ucpro&fr=android&kps&sign&vcode`），不依赖 PC Cookie
- 凭证支持三种写法：完整抓包 URL、手填三参数、裸 URL
- 严格判定：空 `data`、缺 `cap_sign`、缺 `sign_daily_reward`、业务码非 0 全部算失败
- 幂等：先查 `cap_sign.sign_daily`，今日已签到直接跳过，不重复提交
- 提交签到网络异常时复查一次状态，避免「其实签上了」被误判为失败
- 多账号隔离：单个账号失败不影响后续账号，并计入退出码
- 凭证失效（HTTP 401 / code 31001 / `require login`）单独识别，直接提示重新抓包
- `--check` 只读自检，`--probe` 打印接口原始返回
- 复用青龙 `notify.py`，推送失败重试 3 次且不影响签到结果

## 青龙任务命令

```bash
task quark_checkin/quark_checkin.py
```

建议 cron：`13 18 * * *`（每天 18:13，与旧的夸克签到任务保持一致）。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `QUARK_ACCOUNTS` | ✅ | — | 账号配置，多账号用**换行**或 `&&` 分隔，账号内用 `;` 分隔字段 |
| `QUARK_TIMEOUT` | 否 | `15` | 请求超时秒数，允许 5–120，越界回退默认值并告警 |
| `QUARK_RANDOM_DELAY` | 否 | `0` | 整轮开始前随机延迟上限（秒），`0` 表示不延迟 |
| `QUARK_CHECK` | 否 | `false` | 设为 `true` 等价 `--check`（青龙「运行」按钮不转发命令行参数） |
| `QUARK_PROBE` | 否 | `false` | 设为 `true` 等价 `--probe`，用完请删除 |

## 凭证获取

### 三种写法

```text
# 1. 推荐：粘贴完整抓包 URL，脚本自动拆 kps/sign/vcode
user=张三; url=https://drive-m.quark.cn/1/clouddrive/capacity/growth/info?kps=..&sign=..&vcode=..

# 2. 手填三参数
user=张三; kps=..; sign=..; vcode=..

# 3. 裸 URL（不带 user=，标签显示为「账号1」）
https://drive-m.quark.cn/1/clouddrive/capacity/growth/info?kps=..&sign=..&vcode=..

# 多账号：换行分隔
user=张三; kps=..; sign=..; vcode=..
user=李四; kps=..; sign=..; vcode=..

# 多账号：&& 分隔（&& 后面必须紧跟 key= 或 http，否则不当作分隔符）
user=张三; kps=..; sign=..; vcode=.. && user=李四; kps=..; sign=..; vcode=..
```

> URL 里含有字面量 `+` 时**原样粘贴**，不要手动改成 `%2B` 或空格。脚本按 `&` 手工切分、只做 percent-decode，不走 `parse_qs`（它会把 `+` 变成空格导致 401）。

### 抓包步骤（MuMu 模拟器 + mitmproxy，已实测）

1. 安装并启动 MuMu 模拟器，`adb connect 127.0.0.1:16384` 后 `adb root` 拿到 root（MuMu 自带 SELinux Permissive）
2. 安装夸克 App，`adb install quark.apk`
3. 用 mitmproxy 生成 CA，转成 Android 系统证书格式：`openssl x509 -inform PEM -subject_hash_old -in mitmproxy-ca-cert.pem`，把证书改名成 `<hash>.0`
4. 系统分区只读，用 bind mount 绕过：把 `/system/etc/security/cacerts` 全量拷到 `/data/local/tmp/cacerts`，放进 mitmproxy 证书，再 `mount --bind` 回去
5. 模拟器设全局代理：`adb shell settings put global http_proxy 10.0.2.2:8888`，宿主机跑 `mitmdump --listen-port 8888 -w flows.mitm`
6. 夸克 App 登录 → 网盘 → 签到/领空间，触发 `drive-m.quark.cn` 请求
7. 从流量里取 `kps` / `sign` / `vcode`，填入 `QUARK_ACCOUNTS`

用 ProxyPin 也可以，思路相同：**CA 必须装成系统证书**，只装用户证书抓不到夸克。

### 参数位置

`kps` / `sign` / `vcode` 在两种位置都可能出现，两种鉴权方式都实测可用：

- **查询参数**：`...?pr=ucpro&fr=android&kps=..&sign=..&vcode=..`（`act/growth/reward` 等页面请求）
- **请求头**：`x-u-kps-wg` / `x-u-sign-wg` / `x-u-vcode`（接口请求，名字多一层 `-wg` 后缀）

`sign` 会随时间轮换，`kps` 相对稳定；凭证整体失效后脚本会报 `AuthError`，需要重新抓包。

### 常见坑

- **只装用户证书抓不到**：夸克对系统证书有要求，实体机不 Root 基本抓不到，推荐用模拟器
- **`kps`/`sign` 被截断**：这两个值很长且含 `+`、`=`，务必整段复制，不要手敲
- **填了 Cookie**：脚本会直接报错并提示改填签名参数——夸克已不接受 PC Cookie 签到
- **URL 里有 `&&`**：会被当成账号分隔符切坏，请改用换行分隔

## 依赖安装

青龙默认自带 `requests`。本地裸环境：

```bash
pip install requests
```

## 本地调试

```bash
# Linux / macOS
export QUARK_ACCOUNTS='user=张三; kps=..; sign=..; vcode=..'
python quark_checkin.py --check   # 只读自检
python quark_checkin.py           # 真实签到
```

```powershell
# Windows PowerShell
$env:QUARK_ACCOUNTS = 'user=张三; kps=..; sign=..; vcode=..'
python quark_checkin.py --check
```

## 通知

优先使用青龙 `notify.py`，查找顺序：脚本目录 → 父目录 → `/ql/data/scripts` → `/ql/scripts`。找不到时只打日志，不影响签到与退出码。

推送标题三档：

- `✅ 夸克签到 全部成功（N/N）`
- `⚠️ 夸克签到 部分失败（K/N）`
- `❌ 夸克签到 全部失败（0/N）`

## 设计要点

1. **空 `data` 必须报错**：这是与旧脚本最大的区别。接口「200 但没数据」是失效的典型形态，不能当成功。
2. **POST 不重试**：签到不是幂等语义，重试可能重复计数。网络异常改为复查 `sign_daily`，确认后才算成功。
3. **凭证失效单独分类**：`AuthError` 会明确提示「请重新抓包」，而不是笼统报网络错误。
4. **错误信息不含凭证**：日志和推送只出现账号标签与错误原因，不回显 `kps`/`sign`/`vcode`。
5. **退出码三档**：`0` 全成功 / `1` 有失败 / `2` 配置错误，青龙列表一眼可辨。

## 已知限制

- **签名参数会过期**：夸克更新 App 或登录态变化后需重新抓包，脚本会报「凭证失效」而不是静默跳过
- **抓包门槛**：需要模拟器 + Root + 系统证书，比抓 Cookie 麻烦
- **无法用 Cookie 兜底**：PC Cookie 路径已被夸克关闭，不要指望降级

## 常见问题

### 提示「凭证失效（require login）」怎么办

`kps`/`sign`/`vcode` 已过期或复制不完整，重新抓包更新 `QUARK_ACCOUNTS`。

### 提示「返回空数据」

接口改版或账号状态异常。先用 `--probe` 看原始返回，再决定是否要更新脚本。

### 提示「缺少参数：sign, vcode」

抓包 URL 不完整，或粘贴时被截断。直接粘完整 URL 最省事。

### 多账号只有一个生效

检查分隔符：换行或 `&&`，且 `&&` 后面要紧跟 `user=` 或 URL。

## 实测记录（2026-11-16 真实账号）

- PC Cookie 调 `growth/info` 与 `growth/sign` 均返回 `200 + data:{}`，确认 Cookie 路径已关闭
- 同一 Cookie 的 `account/info` 仍返回昵称，说明 Cookie 本身有效，是接口不再接受
- `account/info` 返回的 `mobilekps` 单独或配合 Cookie 均返回 401，不能替代抓包
- MuMu 12 + mitmproxy 抓到 `x-u-kps-wg` / `x-u-sign-wg` / `x-u-vcode`，改成 query 参数同样可用
- 首跑签到成功 `+20.00 MB`，连签进度 `1/7`；二次执行返回「今日已签到」，未重复提交

## 测试

```bash
cd quark_checkin
python -m unittest discover -s tests -v
```

45 项单元测试，全部打桩，不发真实请求。覆盖：账号分隔（换行/`&&`/不切 URL）、三种凭证写法、字面量 `+` 与 `%2B` 解码、Cookie 误填拒绝、GET 网络异常与 429/5xx 重试、401 → `AuthError`、**空 `data` 必须报错**、非 JSON/非 dict 拒绝、POST 不重试、缺 `sign_daily_reward`、已签到跳过 POST、POST 网络异常复查（已签/未签两条路径）、缺 `cap_sign`、多账号隔离、退出码 0/1/2、`--check` 与 `QUARK_CHECK` 两种入口、错误信息不含凭证明文、推送失败不影响退出码。

## 来源与参考

| 来源 | 说明 |
|---|---|
| [Liu8Can/Quark_Auto_Check_In](https://github.com/Liu8Can/Quark_Auto_Check_In) | 签到接口、参数解析（含字面量 `+` 处理）、POST 复查思路 |
| [PLA888/kuake_qiandao](https://github.com/PLA888/kuake_qiandao) | `x-u-kps-wg` / `x-u-sign-wg` / `x-u-vcode` 请求头对应关系 |
| [ASign.js 夸克文档](https://as.js.cool/reference/quark) | 抓包位置与参数说明 |

本仓库版本在其基础上重写了错误判定、退出码、多账号隔离、自检模式与单元测试。
