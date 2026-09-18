# ZodGame 签到 - 青龙面板脚本

自动完成 ZodGame 论坛(zodgame.xyz)的每日收益:

- **每日签到**(dsu_paulsign 插件):随机心情签到,领取酱油奖励
- **BUX 广告任务**(jnbux 插件):自动完成全部广告任务,每个 +2 点币(点币仅累积,不自动兑换)

## 特性

- Cookie 认证,轻量无浏览器依赖(仅 `requests`)
- 已签到/任务已完成自动跳过,可安全重复执行(幂等)
- 多账号支持(换行分隔)
- Cookie 失效自动检测,推送告警提醒重新抓取
- 网络抖动自动重试(站点有偶发 SSL EOF)
- 复用青龙 `notify.py` 推送

## 快速开始

### 1. 青龙面板安装依赖

在青龙「依赖管理」-「Python」中安装:

```
requests
```

### 2. 配置环境变量

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `ZODGAME_COOKIE` | ✅ | 完整 Cookie 字符串,多账号用换行分隔 |
| `ZODGAME_PROXY` | ❌ | HTTP 代理,如 `http://172.17.0.1:7890`(站点直连不稳时使用) |
| `ZODGAME_NOTIFY` | ❌ | 是否推送,默认 `true` |
| `ZODGAME_NOTIFY_ONLY_FAIL` | ❌ | 全部成功时静默,默认 `false` |
| `ZODGAME_DEBUG` | ❌ | 调试模式,默认 `false` |

### 3. 获取 Cookie

1. 用浏览器打开 https://zodgame.xyz/ 并登录
2. 按 `F12` 打开开发者工具,切换到「网络」(Network)标签
3. 刷新页面,点击任意 `forum.php` 请求
4. 在「请求标头」中找到 `Cookie:`,复制完整值(包含 `qhMq_2132_auth`、`cf_clearance` 等所有键值对)

> 💡 关键 Cookie 是 `qhMq_2132_auth`(登录态)和 `cf_clearance`(Cloudflare 放行)。
> 论坛 Cookie 长期有效;若推送提示「Cookie 已失效」,重新抓取更新环境变量即可。

多账号格式(换行分隔):

```
账号1的完整Cookie字符串
账号2的完整Cookie字符串
```

### 4. 添加青龙任务

- 命令:`task zodgame_checkin/zodgame_checkin.py`
- 定时:`30 9 * * *`(每天上午 9:30,可自行调整)

## 运行示例

```
🚀 ZodGame 签到启动,共 1 个账号

======== 账号1 (<18 项: qhMq_2132_saltkey, ...>) ========
   📝 签到: 签到成功,获得 酱油 3 瓶
   📺 BUX: 完成 4 个任务 (+8 点币),余额 22.0 点币

# ✅ ZodGame 全部成功(1/1)
```

## 常见问题

### Q: 提示「Cookie 已失效」?
A: 浏览器重新登录论坛,F12 抓取新 Cookie 更新 `ZODGAME_COOKIE` 环境变量。

### Q: 签到显示成功但没奖励?
A: 签到奖励随机(酱油/榨菜等),数量随机,属正常现象。

### Q: BUX 任务有时只完成部分?
A: 广告任务依赖 `onlyhash` 时效,单任务领奖失败不影响其他任务,次日会重新执行。

## 免责声明

本脚本仅供学习和个人使用,请勿用于商业用途。使用产生的后果由使用者自行承担。
