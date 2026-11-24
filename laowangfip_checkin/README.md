# 老王FIP签到

老王论坛（laowang.vip）Discuz k_misign 插件每日签到。站点被墙，必须挂代理；纯 `requests` 实现登录、tncode 滑块验证与签到（OpenCV 差异法解缺口），无需浏览器。

候选域名（按序探测，首个可用即采用，主域名被拦自动降级）：`https://laowang.vip,https://laowangfip372.vip`

## 青龙任务命令

```text
task laowangfip_checkin/laowangfip_checkin.py
```

定时建议 `30 8 * * *`。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `LWFIP_ACCOUNTS` | 是 | - | `用户名#密码`，多账号用 `&` 或换行分隔 |
| `LWFIP_PROXY` | 否 | - | HTTP 代理，站点被墙建议配置；留空回退青龙全局代理（`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`） |
| `LWFIP_PROXY_REQUIRED` | 否 | `false` | `true` 时缺代理直接报错退出（默认仅告警并直连尝试） |
| `LWFIP_BASE_URLS` | 否 | `https://laowang.vip,https://laowangfip372.vip` | 候选域名，逗号分隔，按顺序探测 |
| `LWFIP_BASE_URL` | 否 | - | 兼容旧配置；设置后只用该域名、不做降级 |
| `LWFIP_NOTIFY` | 否 | `true` | 是否调用青龙 `notify.py` 推送 |
| `LWFIP_NOTIFY_ONLY_FAIL` | 否 | `false` | 仅在有账号失败时推送 |
| `LWFIP_TIMEOUT` | 否 | `30` | 请求超时，单位秒 |
| `LWFIP_DEBUG` | 否 | `false` | 输出验证码求解等调试日志 |
| `LWFIP_MAX_CAPTCHA_RETRY` | 否 | `5` | 单次滑块识别失败重试次数 |
| `LWFIP_MAX_RETRY` | 否 | `5` | 账号级任务重试次数（仅可重试错误触发） |
| `LWFIP_RETRY_INTERVAL` | 否 | `60` | 两次任务重试间隔，单位秒 |
| `LWFIP_COOKIE_CACHE` | 否 | `true` | 登录成功后 Cookie 落盘复用，后续直通签到跳过滑块 |

多账号示例：

```text
LWFIP_ACCOUNTS=user1#pass1&user2#pass2
```

## 依赖安装

要求 Python 3.8+（青龙容器自带 3.11）。在「依赖管理 → Python」安装：

```text
requests
opencv-python-headless
numpy
```

## 实现要点

- 站点的 `/captcha/tn_code.js` 是未混淆的经典实现：`sign` / `track` 只是「轨迹统计 → JSON → 与明文密钥逐字节 XOR → base64」，再用 FNV-1a 对 `trackStr + ts + offset + 密钥` 求签名，因此可全 Python 复现，无需浏览器。
- 取 `/captcha/tncode.php` 三联图（带缺口背景 / 拼图块 / 完整背景），用完整背景与带缺口背景做差异定位。站点用「双缺口」反爬，脚本按「真缺口与拼图块同行（y 对齐）」判别，并对一张图的多个候选逐个提交，全部失败才换图。
- 服务端行为风控（`error_track`）会拒绝点数过少或瞬移的轨迹，脚本用约 40 个点的余弦缓动轨迹规避。

## 常见问题

- **Cloudflare 403 打不开** → 判定看页面特征（`Just a moment...` 或 `window._cf_chl_opt`），不看状态码：同一出口下 `curl` 可能 403 而 `requests` 返回 200（TLS 指纹差异）。脚本启动时按 `LWFIP_BASE_URLS` 找第一个能解析出 `formhash` + `loginhash` 的域名，运行中再遇拦截立即换域名重试、不空等间隔。换代理或补 `is_agree` Cookie 都无效（实测 3/3 仍 403），CF 质询与站点年龄网关是两件独立的事。
- **年龄确认网关** → 站点根路径 `/` 是「性材料警告」页，需 `is_agree=1`。脚本按当前域名预置该 Cookie（不跨域泄漏），业务页命中网关时补 Cookie 重试一次，仍失败抛 `AgeGateBlocked` 换域名。该页没有 `formhash`，被误当登录页会报「formhash 缺失」。
- **哪些错误会重试** → 网络/超时/代理、滑块失败、签到流程异常、域名被拦截会重试；密码错误、用户名不存在、账号禁用等业务错误不重试（重试只会加剧风控）。间隔固定不做指数退避：默认 5 次重试、4 个间隔 × 60 秒 ≈ 4 分钟，避免任务跨 cron 周期。
- **验证码识别失败** → 脚本按 `LWFIP_MAX_CAPTCHA_RETRY` 重新取图重试；持续失败开 `LWFIP_DEBUG=true` 看「缺口 bbox」与「服务端返回」，站点若改成点选验证码需重新适配算法。
- **统计数据为空或只有「天」** → 脚本读签到页 `ul.countqian.cl` 各 `li` 的 `h4` 与 `input.hidnum`，DOM 改版会失效，开 `LWFIP_DEBUG=true` 检查签到页结构。
- **页面打不开或超时** → 配 `LWFIP_PROXY`（Docker 青龙常用 `http://172.17.0.1:7890`），代理建议开规则模式，国内域名直连、境外域名走代理。

## 会话 Cookie 复用

登录成功后 Cookie 存脚本目录 `cookie_<哈希>.json`，文件名按「账号 + 域名」哈希，不含明文，已被仓库 `.gitignore` 覆盖。

- 当天重试与后续 cron 任务直接带 Cookie 签到，不再重过滑块，登录频率大幅降低。
- **按域名隔离**：Cookie 与域名绑定，不同域名分别存放；共用一份会在切域名时互相覆盖且加载后无效。
- Cookie 失效自动重新登录并覆盖缓存；`LWFIP_COOKIE_CACHE=false` 可关闭。

## 本地调试

```bash
export LWFIP_ACCOUNTS="your_username#your_password"
export LWFIP_PROXY="http://127.0.0.1:7890"
export LWFIP_DEBUG=true
python laowangfip_checkin.py
```

## 执行报告

运行结束会输出汇总报告，含账号总计与成功/失败分组，成功项附连续签到、等级、积分、总天数等统计；推送失败会自动重试 3 次，不影响签到结果。
