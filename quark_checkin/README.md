# 夸克网盘签到

夸克网盘每日签到领空间，走 `drive-m.quark.cn` 的 growth 接口，使用移动端签名参数 `kps` / `sign` / `vcode`。前提：必须抓包拿到移动端签名参数，PC Cookie 已不能用于签到。

2026 年 11 月起，PC Cookie 调 `growth/info`、`growth/sign` 均返回 HTTP 200 + 空 `data`，签到实际没有发生；旧脚本把空 dict 当假值跳过，会静默失效还标绿。本脚本把空 `data` 当硬错误，只走仍有效的移动端签名参数路径。

## 青龙任务命令

```bash
task quark_checkin/quark_checkin.py
```

定时建议 `13 18 * * *`。退出码：`0` 全部成功 / `1` 有账号失败 / `2` 配置错误。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `QUARK_ACCOUNTS` | 是 | - | 账号配置，多账号用**换行**或 `&&` 分隔，账号内用 `;` 分隔字段 |
| `QUARK_TIMEOUT` | 否 | `15` | 请求超时秒数，允许 5–120，越界回退默认值并告警 |
| `QUARK_RANDOM_DELAY` | 否 | `0` | 整轮开始前随机延迟上限（秒），`0` 表示不延迟 |
| `QUARK_CHECK` | 否 | `false` | 等价 `--check`（青龙「运行」按钮不转发命令行参数） |
| `QUARK_PROBE` | 否 | `false` | 等价 `--probe`，打印接口原始返回，用完请删除 |

多账号示例（`&&` 后必须紧跟 `user=` 或 URL，否则不当作分隔符）：

```text
user=张三; kps=..; sign=..; vcode=..
user=李四; kps=..; sign=..; vcode=..

user=张三; kps=..; sign=..; vcode=.. && user=李四; kps=..; sign=..; vcode=..
```

## 依赖安装

青龙自带 `requests`；本地裸环境执行 `pip install requests`。

## 凭证获取

三种写法，账号内字段用 `;` 分隔：

```text
# 1. 推荐：粘贴完整抓包 URL，脚本自动拆 kps/sign/vcode
user=张三; url=https://drive-m.quark.cn/1/clouddrive/capacity/growth/info?kps=..&sign=..&vcode=..

# 2. 手填三参数
user=张三; kps=..; sign=..; vcode=..

# 3. 裸 URL（不带 user=，标签显示为「账号1」）
https://drive-m.quark.cn/1/clouddrive/capacity/growth/info?kps=..&sign=..&vcode=..
```

抓包步骤（MuMu 模拟器 + mitmproxy，已实测）：

1. MuMu 模拟器开启 Root：`adb connect 127.0.0.1:16384` 后 `adb root`（MuMu 自带 SELinux Permissive）；
2. 安装夸克 App；
3. 用 mitmproxy 生成 CA，执行 `openssl x509 -inform PEM -subject_hash_old -in mitmproxy-ca-cert.pem` 得到哈希，把证书改名成 `<hash>.0`；
4. 系统分区只读，把 `/system/etc/security/cacerts` 全量拷到 `/data/local/tmp/cacerts`，放入证书后再 `mount --bind` 回去；
5. 设全局代理 `adb shell settings put global http_proxy 10.0.2.2:8888`，宿主机跑 `mitmdump --listen-port 8888 -w flows.mitm`；
6. App 登录 → 网盘 → 签到/领空间，从 `drive-m.quark.cn` 请求里取 `kps` / `sign` / `vcode`。

用 ProxyPin 同理：CA 必须装成**系统证书**，只装用户证书抓不到夸克。

参数位置：查询参数 `...?pr=ucpro&fr=android&kps=..&sign=..&vcode=..`，或请求头 `x-u-kps-wg` / `x-u-sign-wg` / `x-u-vcode`，两种都实测可用。`sign` 会随时间轮换，`kps` 相对稳定，整体失效后脚本报 `AuthError` 提示重新抓包。URL 里含字面量 `+` 时原样粘贴，不要改成 `%2B` 或空格（脚本按 `&` 手工切分、只做 percent-decode，走 `parse_qs` 会把 `+` 变空格导致 401）。

## 常见问题

- **凭证失效（require login / HTTP 401 / code 31001）** → `kps`/`sign`/`vcode` 过期或复制不完整，重新抓包更新 `QUARK_ACCOUNTS`。
- **返回空数据** → 接口改版或账号状态异常，先 `--probe` 看原始返回，再决定是否更新脚本。
- **缺少参数：sign, vcode** → 抓包 URL 不完整或被截断，直接粘完整 URL 最省事。
- **多账号只有一个生效** → 检查分隔符：换行或 `&&`，且 `&&` 后要紧跟 `user=` 或 URL。
- **`kps`/`sign` 被截断** → 两个值很长且含 `+`、`=`，务必整段复制，不要手敲。
- **填了 Cookie** → 脚本直接报错并提示改填签名参数，夸克已不接受 PC Cookie 签到。
- **怎么确认真的签上了** → 脚本严格校验：空 `data`、缺 `cap_sign`、缺 `sign_daily_reward`、业务码非 0 都算失败；提交时网络异常会复查 `sign_daily` 一次。
- **想用 `mobilekps` 或 Cookie 替代抓包** → `account/info` 返回的 `mobilekps` 单独或配合 Cookie 均返回 401，PC Cookie 路径已关闭，只能抓移动端签名参数。
- **推送标题** → 全部成功 / 部分失败 / 全部失败三档；找不到 `notify.py` 时只打日志，不影响签到与退出码。

## 来源

改编自 [Liu8Can/Quark_Auto_Check_In](https://github.com/Liu8Can/Quark_Auto_Check_In)、[PLA888/kuake_qiandao](https://github.com/PLA888/kuake_qiandao) 与 [ASign.js 夸克文档](https://as.js.cool/reference/quark)，本仓库重写了错误判定、退出码、多账号隔离、自检模式与单元测试。
