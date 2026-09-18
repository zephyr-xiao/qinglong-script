# 青龙脚本合集

本目录收集适配青龙面板的 Python 定时任务脚本。每个脚本已放入同名文件夹，并提供独立 `README.md`。

## 脚本列表

| 脚本 | 说明 | 文档 | 青龙任务命令 |
|---|---|---|---|
| E-Hentai 自动签到 | E-Hentai 每日签到领 Exp/Credits，Cookie 认证，TLS 1.2 回退 + Cookie 过期预警 | [AkiyaKiko_EhentaiAutoSignIn/autosignin.py](AkiyaKiko_EhentaiAutoSignIn/autosignin.py) | `task AkiyaKiko_EhentaiAutoSignIn/autosignin.py` |
| 老王FIP签到（浏览器版） | Discuz + tncode 滑块 + 前端 JS 签名，使用 Playwright + OpenCV 自动登录签到 | [laowangfip_browser_checkin/README.md](laowangfip_browser_checkin/README.md) | `task laowangfip_browser_checkin/laowangfip_browser_checkin.py` |
| 全自动签到助手 | 多站点 API/Discuz 签到，支持内置站点与 `QZD_CUSTOM` 自定义站点，凭证 token/Cookie 缓存复用 | [quan_zidong_zhushou/README.md](quan_zidong_zhushou/README.md) | `task quan_zidong_zhushou/quan_zidong_zhushou.py` |
| whos.tv 签到 | Cookie 签到，带接口探测和代理支持 | [whos_tv_checkin/README.md](whos_tv_checkin/README.md) | `task whos_tv_checkin/whos_tv_checkin.py` |
| SophNet 签到 | www.sophnet.com 福利中心签到，refreshToken 换 accessToken，纯 API 免验证码 | [sophnet_checkin/README.md](sophnet_checkin/README.md) | `task sophnet_checkin/sophnet_checkin.py` |
| iKuuu 签到（Node.js） | ikuuu 机场签到，动态域名 + 多账号 + 状态分档，复用 `sendNotify.js` 全通道推送 | [wuang-wu_Ikuuu/README.md](wuang-wu_Ikuuu/README.md) | `task wuang-wu_Ikuuu/ikuuu.js` |
| gpt.qt.cool 签到 | gpt.qt.cool 签到，Playwright + OpenCV 滑动验证码自动识别，登录态 storage_state 缓存复用 | [gptqtcool_checkin/README.md](gptqtcool_checkin/README.md) | `task gptqtcool_checkin/gptqtcool_checkin.py` |
| 司机社签到 | xsijishe 司机社签到，Cookie 直签或邮箱 + 本地 ddddocr OCR 登录，Cookie 缓存复用 | [xsijishe_checkin/README.md](xsijishe_checkin/README.md) | `task xsijishe_checkin/xsijishe_checkin.py` |
| 采蘑菇论坛回帖签到 | caimogu.cc 自动回帖刷活跃度，Playwright 驱动 + AI/模板双模式评论生成（反套话词库、REPLY/SKIP 判定、防重复回帖） | [caimogu_checkin/README.md](caimogu_checkin/README.md) | `task caimogu_checkin/caimogu_checkin.py` |
| DZZI.AI 自动签到 | New API 站签到，多账号批量、已签到自动跳过（幂等） | [dzzi-auto-checkin/README.md](dzzi-auto-checkin/README.md) | `task dzzi-auto-checkin/checkin.py` |
| 雨云自动签到 | 雨云服务器签到 + 自动续费，多账号 + 验证码识别，专为青龙面板优化 | [Rainyun/README.md](Rainyun/README.md) | `task Rainyun/main.py` |
| 天翼云盘签到 | cloud189-sdk 登录签到，多账号 + 个人/家庭容量统计，复用 sendNotify.js 全通道推送 | [Cloud189Checkin/README.md](Cloud189Checkin/README.md) | `task Cloud189Checkin/src/app.js` |
| PT 签到（Node.js） | NovaHD / HDArea / BTSchool 三站签到，Cookie 失效自动登录兜底（视觉模型识别验证码），登录限次保护防封 IP | [pt-checkin/README.md](pt-checkin/README.md) | `task pt-checkin/pt_checkin.js` |
| TG签到（tg-signer） | Telegram 群/机器人自动签到，基于 tg-signer 封装，扫码登录多账号，支持发消息+点按钮+定时撤回 | [tg-signer-ql/README.md](tg-signer-ql/README.md) | `task tg-signer-ql/tg_signer_ql.py` |
| ZodGame 签到 | zodgame.xyz 每日签到 + BUX 广告任务（每个 +2 点币），Cookie 认证，幂等跳过，Cookie 失效推送告警 | [zodgame_checkin/README.md](zodgame_checkin/README.md) | `task zodgame_checkin/zodgame_checkin.py` |
| 嘉立创签到 | m.jlc.com 每日签到领金豆，第七天自动领 8 金豆券，AccessToken 认证，幂等跳过，Token 失效告警 | [jlc_checkin/README.md](jlc_checkin/README.md) | `task jlc_checkin/jlc_checkin.py` |
| 纸鸢下载签到 | mybt.kiteyuan.info 每日签到 + 访问任务，Casdoor 账号密码自动登录换 JWT，JWT 缓存复用（401 才重登），多账号 + 网络重试，幂等跳过 | [mybt-checkin-ql/README.md](mybt-checkin-ql/README.md) | `task mybt-checkin-ql/mybt_checkin.py` |

## 迁移提醒

脚本已从根目录移动到子目录。若青龙中已有旧任务，需要同步修改命令：

```text
# 旧
 task xxx.py

# 新
 task xxx/xxx.py
```

例如：

```text
task laowangfip_browser_checkin/laowangfip_browser_checkin.py
task quan_zidong_zhushou/quan_zidong_zhushou.py
task whos_tv_checkin/whos_tv_checkin.py
```

如果使用 `ql repo` 拉库，请确认订阅过滤规则能扫描到子目录内的 `.py` 文件。

## 通知说明

脚本在青龙环境中会尽量复用青龙自带的 `notify.py`，支持你在青龙里已经配置好的通知渠道，例如 Server酱、PushPlus、企业微信、钉钉、飞书、Bark、Telegram 等。

各脚本都提供独立的通知开关环境变量，详见对应子目录 README。

## 通用运行方式

青龙面板：

1. 上传或订阅本目录脚本。
2. 在「环境变量」中配置对应脚本的变量。
3. 在「定时任务」中新建任务，命令使用上方表格中的新路径。
4. 先手动运行一次，确认日志正常后再启用定时。

本地调试：

```bash
python path/to/script.py
```

Windows PowerShell 中设置环境变量示例：

```powershell
$env:XXX_NOTIFY="false"
python path\to\script.py
```

## 代理配置（重要）

脚本的代理取值优先级固定为三段：

1. 脚本专属变量（如 `LWFIP_PROXY`、`IKUUU_PROXY`、`TG_PROXY`）
2. 青龙全局代理：`HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`（大小写均可）
3. 都为空则直连（不报错退出）

### 注意：`config.sh` 里的 `ProxyUrl` 对脚本无效

`ProxyUrl` 是**青龙面板自身**的配置项，只用于面板拉库、安装依赖，**不会注入任务脚本的运行环境**。
脚本读不到它，因此「配了 ProxyUrl 却仍提示未配置代理」是正常现象，不是脚本的问题。

要让所有脚本共用一份代理配置，任选其一。

**方式一：青龙「环境变量」页（推荐）**

新增两条，值改成你自己的代理地址：

| 名称 | 值 |
|---|---|
| `HTTP_PROXY` | `http://192.168.5.5:7890` |
| `HTTPS_PROXY` | `http://192.168.5.5:7890` |

> 代理跑在宿主机上时容器内常用 `http://172.17.0.1:7890`；跑在局域网其它机器上则填其实际 IP。
> 保存后新运行的任务即生效。

**方式二：容器级环境变量（docker 部署）**

在 `docker-compose.yml` 的青龙服务下追加：

```yaml
environment:
  - HTTP_PROXY=http://192.168.5.5:7890
  - HTTPS_PROXY=http://192.168.5.5:7890
  - NO_PROXY=localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12
```

`NO_PROXY` 用于排除内网，避免青龙访问数据库、NAS 时也被绕进代理。

> 建议代理软件开启**规则模式**：国内域名直连、境外域名走代理。否则国内站脚本（嘉立创、雨云、司机社等）会因绕行而变慢甚至失败。

### 代理没生效怎么排查

运行同目录的诊断脚本 `proxy_env_probe.py`，它会列出任务环境里所有含 proxy 的变量、
脚本的代理解析结果，并实测直连与代理各自的出口 IP：

```text
task proxy_env_probe.py
```

判断标准：输出第 1 节若没有 `HTTP_PROXY` / `HTTPS_PROXY`，说明「环境变量」页里没配；
第 4 节「代理」一行能显示出 IP 且与「直连」不同，说明代理链路可用。
