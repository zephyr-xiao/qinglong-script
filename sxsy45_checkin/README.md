# 尚香书苑签到 - 青龙面板脚本

自动完成尚香书苑论坛（sxsy45.com，Discuz! X3.5）`k_misign` 插件的每日签到。

该站**登录强制图片验证码**，且 k_misign 签到还叠加了一道**算术验证**（"签到验证：9 + 15 = ?"），两道都能自动通过，无需人工介入。

## 特性

- 两种认证方式：**Cookie 直签** 或 **账号密码登录**
- 账密模式自动识别登录验证码：本地 **ddddocr** OCR（识别错自动换图重试，最多 3 次）
- 登录 Cookie JSON 缓存复用：缓存有效完全不走登录流程，失效自动重登
- 签到算术验证自动计算（支持 `+ - × ÷`）
- 已签到自动跳过（以签到页有无 `JD_sign` 按钮为准，幂等可重复执行）
- 签到后输出统计：连续天数 / 等级 / 积分奖励 / 累计天数
- 多账号支持（`&` 或换行分隔）
- 复用青龙 `notify.py` 推送，`SXSY_NOTIFY_ONLY_FAIL=true` 可配置全成功时静默

## 认证方式怎么选

| 方式 | 环境变量 | 优点 | 注意 |
|---|---|---|---|
| 账号密码 | `SXSY_ACCOUNTS` 格式 `用户名#密码` | 免维护，Cookie 失效自动重登 | 需安装 `ddddocr` 依赖 |
| Cookie 直签 | `SXSY_ACCOUNTS` 直接填整段 Cookie（不含 `#`） | 不需要 ddddocr | Cookie 过期需手动重新抓取 |

两种格式都填在 `SXSY_ACCOUNTS` 一个变量里，脚本按是否含 `#` 自动区分。

> 登录验证码识别连续 3 次失败会放弃并提示改用 Cookie 模式（避免反复触发站点风控）。
> 账号密码错误属于致命错误，**不会自动重试**。

## 接口流程（2026-09-29 实测）

### 登录（仅账密模式）

| 步骤 | 请求 | 说明 |
|---|---|---|
| 1 | `GET member.php?mod=logging&action=login` | 取 `formhash` 与验证码入口 `idhash`（当前固定为 `cS`） |
| 2 | `GET misc.php?mod=seccode&action=update&idhash=<x>&modid=member::logging&inajax=1` | 返回 JS，内含真实 `seccodehash` |
| 3 | `GET misc.php?mod=seccode&update=<随机>&idhash=<x>` | 验证码 PNG，送 ddddocr 识别 |
| 4 | `POST member.php?mod=logging&action=login&loginsubmit=yes` | body 含账密 + `seccodehash` / `seccodemodid` / `seccodeverify` 三件套；响应含 `succeedhandle` 即成功 |

### 签到（k_misign）

| 步骤 | 请求 | 说明 |
|---|---|---|
| 1 | `GET plugin.php?id=k_misign:sign` | 有 `id="JD_sign"` 按钮 → 今天未签；无按钮且有 `lxdays` 统计 → 已签（幂等跳过）；被跳转登录页 → 登录态失效 |
| 2 | `GET plugin.php?id=k_misign:sign&operation=qiandao&formhash=<fh>&format=text` | 返回算术验证题 XML（`var q="签到验证：A - B = ?"`） |
| 3 | `GET ...&mathverify_answer=<计算结果>` | 答对即完成签到，返回结果 XML |

签到最终状态以**回查签到页 `JD_sign` 是否消失**为准（接口返回文案因插件版本而异，不作判据）。

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `SXSY_ACCOUNTS` | 账号列表：`用户名#密码`（多账号 `&` 或换行分隔），或整段 Cookie 字符串 | 必填 |
| `SXSY_NOTIFY` | 是否推送 | `true` |
| `SXSY_NOTIFY_ONLY_FAIL` | 仅失败时推送（全成功静默） | `false` |
| `SXSY_TIMEOUT` | 请求超时（秒） | `30` |
| `SXSY_DEBUG` | 调试模式（打印 OCR 结果、页面状态等） | `false` |
| `SXSY_PROXY` | HTTP 代理（可选）；留空时依次回退 `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` 全局变量，全空直连 | 空 |

> 注意：青龙「配置文件」里的 `ProxyUrl` 只供面板自身使用，脚本读不到；代理请配在「环境变量」页。

## 依赖安装

青龙面板 →「依赖管理」→「Python」：

- `requests`（必装）
- `ddddocr`（账密模式必装；只用 Cookie 模式可不装）

## 本地测试

```bash
cd sxsy45_checkin
python -m unittest discover -s tests -v   # 33 个离线单测，不发真实请求

# 真实干跑
SXSY_ACCOUNTS="用户名#密码" SXSY_NOTIFY=false python sxsy45_checkin.py
```

## 文件说明

| 文件 | 说明 |
|---|---|
| `sxsy45_checkin.py` | 主脚本（单文件，青龙上传这一个即可） |
| `sxsy45_cookie.json` | 登录 Cookie 缓存（运行时自动生成，含敏感凭证，已被 `.gitignore` 忽略，**不要分享**） |
| `tests/` | 离线单元测试（真机抓包样本断言） |
