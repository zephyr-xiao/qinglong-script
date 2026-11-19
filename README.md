# 青龙脚本合集

适配青龙面板的定时任务脚本合集。每个脚本一个子目录，完整环境变量与用法见子目录 `README.md`。

## 脚本列表

| 脚本 | 说明 | 青龙任务命令 |
|---|---|---|
| [E-Hentai 自动签到](AkiyaKiko_EhentaiAutoSignIn/autosignin.py) | E-Hentai 每日签到领 Exp/Credits | `task AkiyaKiko_EhentaiAutoSignIn/autosignin.py` |
| [老王FIP签到](laowangfip_checkin/README.md) | Discuz + tncode 滑块自动登录签到 | `task laowangfip_checkin/laowangfip_checkin.py` |
| [New API 站点自动签到](newapi_checkin/README.md) | 多站点 API/Discuz 签到，支持自定义站点 | `task newapi_checkin/newapi_checkin.py` |
| [whos.tv 签到](whos_tv_checkin/README.md) | Patchright 过 Cloudflare 挑战，Cookie/账密双认证 | `xvfb-run -a task whos_tv_checkin/whos_tv_checkin.py` |
| [SophNet 签到](sophnet_checkin/README.md) | 福利中心签到，refreshToken 换 accessToken | `task sophnet_checkin/sophnet_checkin.py` |
| [iKuuu 签到（Node.js）](wuang-wu_Ikuuu/README.md) | 机场签到，Geetest 点选由视觉模型过码 | `task wuang-wu_Ikuuu/ikuuu.js` |
| [gpt.qt.cool 签到](gptqtcool_checkin/README.md) | Playwright + OpenCV 滑块，登录态缓存复用 | `task gptqtcool_checkin/gptqtcool_checkin.py` |
| [司机社签到](xsijishe_checkin/README.md) | Discuz 多镜像签到，域名自动切换学习 | `task xsijishe_checkin/xsijishe_checkin.py` |
| [采蘑菇论坛回帖签到](caimogu_checkin/README.md) | 自动回帖刷活跃度，AI/模板评论生成 | `task caimogu_checkin/caimogu_checkin.py` |
| [雨云自动签到](Rainyun/README.md) | 服务器签到 + 自动续费 | `task Rainyun/main.py` |
| [天翼云盘签到](Cloud189Checkin/README.md) | cloud189-sdk 登录签到 + 容量统计 | `task Cloud189Checkin/src/app.js` |
| [PT 签到（Node.js）](pt-checkin/README.md) | 四站 PT 签到，Cookie 失效自动登录 | `task pt-checkin/pt_checkin.js` |
| [TG签到（tg-signer）](tg-signer-ql/README.md) | Telegram 群/机器人自动签到 | `task tg-signer-ql/tg_signer_ql.py` |
| [ZodGame 签到](zodgame_checkin/README.md) | 每日签到 + BUX 广告任务 | `task zodgame_checkin/zodgame_checkin.py` |
| [嘉立创签到](jlc_checkin/README.md) | 每日签到领金豆，第 7 天自动领券 | `task jlc_checkin/jlc_checkin.py` |
| [纸鸢下载签到](mybt-checkin-ql/README.md) | 每日签到 + 访问任务，Casdoor 登录 | `task mybt-checkin-ql/mybt_checkin.py` |
| [福利吧预注册签到](wnflb_checkin/README.md) | 游客预注册，连续签到 30 天转正 | `task wnflb_checkin/wnflb_checkin.py` |
| [福利吧每日签到](wnflb_fx_checkin/README.md) | 每日签到插件，幂等跳过 + 排名统计 | `task wnflb_fx_checkin/wnflb_fx_checkin.py` |
| [尚香书苑签到](sxsy45_checkin/README.md) | Discuz sx_signin 签到，域名自动发现 | `task sxsy45_checkin/sxsy45_checkin.py` |
| [维咔VikACG签到](vikacg_checkin/README.md) | 钱包签到领积分，JWT 双 token 自动续期 | `task vikacg_checkin/vikacg_checkin.py` |
| [芯幻签到](xhcyv_checkin/README.md) | ACG 社区签到，JWT 缓存 + 验证码识别 | `task xhcyv_checkin/xhcyv_checkin.py` |
| [百度网盘签到](baiduwangpan_checkin/README.md) | 网页端成长值 + App 金币任务中心 | `task baiduwangpan_checkin/baiduwangpan_checkin.py` |
| [夸克网盘签到](quark_checkin/README.md) | 移动端签名参数签到领空间 | `task quark_checkin/quark_checkin.py` |
| [MiniMax Code 签到](minimax_checkin/README.md) | 每日 400 积分，自带 OAuth device flow | `task minimax_checkin/minimax_checkin.py` |

## 使用方式

1. 上传或订阅本仓库脚本到青龙
2. 「环境变量」配置脚本所需变量（见子目录 README）
3. 「定时任务」新建任务，命令见上表（形如 `task 目录/脚本.py`；从旧版根目录迁移的任务需同步改路径）
4. 先手动运行一次确认日志正常，再启用定时

本地调试：`python path/to/script.py`。

通知复用青龙自带 `notify.py`（Server酱、PushPlus、企业微信、钉钉、飞书、Bark、Telegram 等），各脚本有独立开关，详见子目录 README。

## 代理配置

取值优先级：脚本专属变量（如 `IKUUU_PROXY`）> 青龙全局 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` > 直连。

`config.sh` 的 `ProxyUrl` 只作用于面板自身（拉库、装依赖），不会注入任务环境。要全局生效，在「环境变量」页加 `HTTP_PROXY`、`HTTPS_PROXY`（如 `http://192.168.1.100:7890`；代理跑在宿主机时容器内常用 `http://172.17.0.1:7890`），新运行的任务即生效。

建议代理开规则模式：国内站（嘉立创、雨云等）直连；司机社、老王FIP 等被 DNS 污染/SNI 阻断的站点必须走代理。

## 来源与致谢

部分脚本改写/移植自开源项目（其余为本仓库原创）：

| 脚本 | 原项目 |
|---|---|
| 雨云自动签到 | [SerendipityR-2022](https://github.com/SerendipityR-2022/Rainyun-Qiandao) → [fatekey](https://github.com/fatekey/Rainyun-Qiandao) → [Jielumoon](https://github.com/Jielumoon/Rainyun-Qiandao) |
| 天翼云盘签到 | [wes-lin/Cloud189Checkin](https://github.com/wes-lin/Cloud189Checkin)（登录依赖 [cloud189-sdk](https://github.com/wes-lin/cloud189-sdk)） |
| iKuuu 签到 | [wuang-wu/Ikuuu](https://github.com/wuang-wu/Ikuuu) |
| E-Hentai 自动签到 | [AkiyaKiko/EhentaiAutoSignIn](https://github.com/AkiyaKiko/EhentaiAutoSignIn) |
| 嘉立创签到 | [Foticing/LC-AutoSign](https://github.com/Foticing/LC-AutoSign) |
| 纸鸢下载签到 | [elongou-checkin/mybt-signin](https://github.com/elongou-checkin/mybt-signin) |
| New API 站点签到 | [zhangguoguo1314/quan-zidong-zhushou](https://github.com/zhangguoguo1314/quan-zidong-zhushou) |
| TG 签到 | [xuanvivo/tg-signer-ql](https://github.com/xuanvivo/tg-signer-ql)（上游 [amchii/tg-signer](https://github.com/amchii/tg-signer)） |
| 百度网盘签到 | [awakaze/cf-netdisk-signin](https://github.com/awakaze/cf-netdisk-signin) + [jujiunofly/baidu-signin](https://github.com/jujiunofly/baidu-signin) |
| 夸克网盘签到 | [Liu8Can/Quark_Auto_Check_In](https://github.com/Liu8Can/Quark_Auto_Check_In) + [PLA888/kuake_qiandao](https://github.com/PLA888/kuake_qiandao) |
| MiniMax Code 签到 | [Minatoxiaohu/agent-auto-signin](https://github.com/Minatoxiaohu/agent-auto-signin) |
