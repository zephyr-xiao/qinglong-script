# 司机社签到 - 青龙面板脚本

## 功能
自动完成 [司机社](https://xsijishe.com/)（sjs66.net / xsijishe.net 等多镜像）的每日签到。

> 💡 镜像域名会不定期更换/失效，脚本内置域名列表已按实测可用性排序，并在登录失败时自动切换到备用域名重试。

支持两种认证方式：
- **Cookie 模式**：直接填入浏览器抓取的 Cookie，轻量无依赖
- **邮箱密码模式**：填入邮箱&密码，本地 ddddocr 识别验证码，自动登录并缓存 Cookie

## 快速开始

### 1. 青龙面板安装依赖

在青龙「依赖管理」-「Python」中安装：
```
requests
beautifulsoup4
ddddocr
```

### 2. 青龙面板配置环境变量

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `SIJISHE_ACCOUNTS` | ✅ | 账号列表，见下方格式说明 |
| `SIJISHE_HOST` | ❌ | 手动锁定签到域名（优先于自动抓取/内置列表），如 `sjs66.com` |
| `SIJISHE_NOTIFY` | ❌ | 是否推送通知，默认 true |
| `SIJISHE_NOTIFY_ONLY_FAIL` | ❌ | 仅失败时推送，默认 false |
| `SIJISHE_PROXY` | ❌ | 代理地址，如 `http://172.17.0.1:7890` |
| `SIJISHE_DEBUG` | ❌ | 调试模式，默认 false |

### 3. 账号格式

**Cookie 模式**（推荐）：
```
SIJISHE_ACCOUNTS=你的完整Cookie字符串
```

**邮箱密码模式**：
```
SIJISHE_ACCOUNTS=your@email.com&yourpassword
```

**多账号**（换行分隔，支持混合）：
```
SIJISHE_ACCOUNTS=user1@mail.com&pass1
user2@mail.com&pass2
完整Cookie字符串_账号3
```

### 4. 获取 Cookie 的方法
1. 用 Chrome/Edge 打开 https://xsijishe.com/ 并登录
2. 按 F12 打开开发者工具
3. 切换到 Application（应用程序）→ Cookies
4. 复制所有 Cookie，格式如 `key1=value1; key2=value2; ...`

## 定时建议
```
cron: 10 9,20 * * *
```
一天两次（9:00 和 20:00），避免因临时故障导致签到遗漏。

## 依赖
- `requests`（青龙 Python 环境已预装）
- `beautifulsoup4`（青龙 Python 环境已预装）
- `dddddocr`（需手动安装，OCR 验证码识别，仅邮箱密码模式需要）

## 作者
箫遥风
