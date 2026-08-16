# 青龙脚本合集

本目录收集适配青龙面板的 Python 定时任务脚本。每个脚本已放入同名文件夹，并提供独立 `README.md`。

## 脚本列表

| 脚本 | 说明 | 文档 | 青龙任务命令 |
|---|---|---|---|
| 7x.hk / 8s.hk 签到 | NewAPI 双站签到（7x.hk + 8s.hk），session 认证，单脚本双站 | [7x8s_checkin/README.md](7x8s_checkin/README.md) | `task 7x8s_checkin/7x8s_checkin.py` |
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
