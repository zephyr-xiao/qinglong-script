#!/usr/bin/env python3
"""Breeze 每日免费积分领取

cron: 30 8 * * *
new Env('Breeze 积分签到')

BreezeBlue（Breeze TTS 2）每天北京时间 08:00 重置一次免费积分（默认 400），
只能在网页控制台点「领取」。官方开发者 API 没有领取端点，领取接口只存在于
网页端，因此本脚本用网页登录态 Cookie 调用：

  GET  /api/credits/summary       查余额 / 今日是否已领 / 下次重置
  POST /api/credits/daily-claim   领取（幂等，重复调用不会重复加积分）

环境变量：
  BREEZE_ACCOUNTS          必填。多账号用换行或 && 分隔，账号内用 ; 分隔字段：
                             user=主号; cookie=breeze_blue_session的值
                           cookie 支持三种写法：裸值、breeze_blue_session=值、
                           完整 Cookie 头（cookie 必须是最后一个字段）
  BREEZE_TIMEOUT           请求超时秒数，默认 20，允许 5-120
  BREEZE_NOTIFY            true/false，默认 true，是否走青龙 notify.py 推送
  BREEZE_NOTIFY_ONLY_FAIL  true/false，默认 true，全部成功时静默
  BREEZE_DEBUG             true/false，默认 false，打印接口响应片段（已截断）
  BREEZE_PROXY             代理地址，默认空（回退青龙全局代理，再回退直连）；
                           填 none/direct/off 表示强制直连
  BREEZE_CHECK             设为 true 等价 --check（只查状态，不领取）
  BREEZE_PROBE             设为 true 等价 --probe（额外打印接口响应片段）

凭证获取：浏览器登录 https://breezeblue.ai/app/home → DevTools → Application →
Cookies → https://breezeblue.ai → 复制 breeze_blue_session 的值。

作者: zephyr_xiao
退出码：0 全部成功（含今日已领）/ 1 有账号失败 / 2 配置错误
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:  # 青龙默认自带 requests；本地裸环境可能没有
    print("❌ 缺少 requests 库，请先安装：pip install requests")
    sys.exit(1)

# Windows 控制台与青龙容器的编码兜底，避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


BASE = "https://breezeblue.ai"
SUMMARY_PATH = "/api/credits/summary"
CLAIM_PATH = "/api/credits/daily-claim"
# 与原插件保持一致的 UA；Breeze 网页接口会校验来源，Origin/Referer 不能少
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
# 只对网关类状态码重试，业务 4xx 不重试
RETRY_STATUS = (429, 500, 502, 503, 504)
RETRY_DELAYS = (0, 1.5, 3.0)
GLOBAL_PROXY_KEYS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)

ACCOUNT_HELP = """🔧 Cookie 获取方法：
1. 浏览器登录 https://breezeblue.ai/app/home
2. 按 F12 打开 DevTools → Application（应用）→ Cookies → https://breezeblue.ai
3. 找到 breeze_blue_session，复制它的值（只复制值，不要复制整行）
4. 填成：user=主号; cookie=粘贴的值

示例（单账号）：
  user=主号; cookie=xxxxxxxx

示例（多账号，换行分隔）：
  user=主号; cookie=xxxxxxxx
  user=小号; cookie=yyyyyyyy

完整 Cookie 头也可以（cookie 必须是最后一个字段）：
  user=主号; cookie=breeze_blue_session=xxx; __cf_bm=yyy"""

# 账号分隔：换行，或后面紧跟 key=value 的 &&（避免切坏 Cookie 里的查询串）
_ACCOUNT_SPLIT = re.compile(r"\r?\n+|&&(?=\s*(?:[A-Za-z_][\w-]*\s*=))")
# cookie 字段放最后，值里允许出现 ;（完整 Cookie 头）；session= 作为别名
_COOKIE_FIELD = re.compile(r"(?:^|;)\s*(?:cookie|session)\s*=\s*", re.IGNORECASE)


class ConfigError(ValueError):
    """配置缺失或格式错误。"""


class AuthError(RuntimeError):
    """登录态失效，需要重新复制 Cookie。"""


class ApiError(RuntimeError):
    """接口返回与预期不符（含不可领取、领取未生效、非 JSON）。"""


class NetworkError(RuntimeError):
    """网络层失败，重试后仍不可用。"""


def load_notify():
    """按青龙常见布局查找 notify.py：脚本目录 → 父目录 → /ql/data/scripts → /ql/scripts。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (here, os.path.dirname(here), "/ql/data/scripts", "/ql/scripts")
    # 先移除再重插，保证 sys.path 顺序与候选顺序一致（脚本目录优先）
    for path in reversed(candidates):
        if not os.path.isdir(path):
            continue
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    found = any(
        os.path.isfile(os.path.join(path, "notify.py"))
        for path in candidates
        if os.path.isdir(path)
    )
    try:
        from notify import send as notify_func  # type: ignore

        return notify_func
    except Exception as exc:
        if found:
            print(f"ℹ️ notify.py 存在但导入失败：{type(exc).__name__}")
        return None


_NOTIFY = load_notify()
if _NOTIFY:
    print("✅ 已加载 notify.py 通知模块")
else:
    print("ℹ️ 未找到 notify.py（仅在青龙环境内可用），通知只写日志")


def send_notify(title, content):
    """推送失败不影响签到结果，最多重试 3 次。"""
    if not _NOTIFY:
        print(f"📢 {title}\n📄 {content}")
        return False
    for attempt in range(1, 4):
        try:
            _NOTIFY(title, content)
            print("📨 通知推送成功")
            return True
        except Exception as exc:
            print(f"⚠️  通知推送第 {attempt}/3 次失败：{type(exc).__name__}")
            if attempt < 3:
                time.sleep(3)
    print("❌ 通知推送失败，已跳过")
    return False


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name, default, minimum, maximum):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        print(f"⚠️  {name}={raw!r} 不是整数，改用默认值 {default}")
        return default
    if not minimum <= value <= maximum:
        print(f"⚠️  {name}={value} 超出范围 {minimum}-{maximum}，改用默认值 {default}")
        return default
    return value


def resolve_proxy(*specific_keys):
    """解析代理地址，返回 (代理地址, 来源变量名)。

    优先级：脚本专属变量 > 青龙全局代理变量 > 空（直连）。
    专属变量填 none/direct/off 表示强制直连，不再回退全局代理。
    """
    for name in specific_keys:
        value = (os.environ.get(name) or "").strip()
        if value:
            if value.lower() in ("none", "direct", "off", "0", "false"):
                return "", name
            return value, name
    for name in GLOBAL_PROXY_KEYS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    return "", ""


def mask_proxy(url):
    """隐藏代理 URL 里的账号密码，避免写进日志。"""
    if not url or "@" not in url:
        return url or ""
    # 按最后一个 @ 切 host：密码里可能含裸 @
    prefix, _, host = url.rpartition("@")
    if "://" in prefix:
        scheme, _, userinfo = prefix.partition("://")
        scheme += "://"
    else:
        scheme, userinfo = "", prefix
    masked_user = "***:***" if ":" in userinfo else "***"
    return scheme + masked_user + "@" + host


def normalize_cookie(raw):
    """把用户填的 Cookie 统一成可直接放进请求头的字符串。

    支持裸 session 值、`breeze_blue_session=值`、完整 Cookie 头三种写法。
    """
    value = " ".join((raw or "").split())
    if not value:
        raise ConfigError("cookie 字段为空")
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if not value:
        raise ConfigError("cookie 字段为空")
    # 裸值（不含 cookie 名）补上默认 cookie 名；完整头里第一段没有名字时也补
    if "breeze_blue_session=" not in value:
        first, separator, rest = value.partition(";")
        candidate = first.strip()
        _, _, tail = candidate.partition("=")
        # 没有 =，或 = 后面只剩 base64 填充（如 abc==），都按裸 session 值处理
        if not candidate or "=" not in candidate or tail.strip("=") == "":
            value = "breeze_blue_session=" + candidate + separator + rest
    return value


def validate_cookie(cookie):
    """确认 Cookie 头里有非空的 breeze_blue_session，且每段都是 name=value。"""
    session_value = ""
    for part in cookie.split(";"):
        part = part.strip()
        if not part:
            continue
        name, separator, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not name:
            raise ConfigError("cookie 格式不正确：存在没有 = 或空名称的片段")
        if name.lower() == "breeze_blue_session":
            session_value = value
    if not session_value:
        raise ConfigError("cookie 里没有有效的 breeze_blue_session 值")


def split_entries(raw):
    if raw is None or not raw.strip():
        raise ConfigError("未配置 BREEZE_ACCOUNTS，或变量内容为空")
    entries = [part.strip() for part in _ACCOUNT_SPLIT.split(raw)]
    entries = [part for part in entries if part]
    if not entries:
        raise ConfigError("BREEZE_ACCOUNTS 中没有可用的账号")
    return entries


def parse_account(entry, index):
    """解析单个账号条目：`user=名字; cookie=值`，cookie 必须放最后。"""
    match = _COOKIE_FIELD.search(entry)
    if match:
        prefix = entry[: match.start()]
        raw_cookie = entry[match.end() :].strip()
    else:
        prefix = entry
        raw_cookie = ""

    fields = {}
    for field in prefix.split(";"):
        field = field.strip()
        if not field:
            continue
        if "=" not in field:
            # 不回显字段内容：用户可能把 Cookie 值误写成 cookie <值>
            raise ConfigError(f"第 {index} 个账号存在无效字段（缺少 =）")
        key, value = field.split("=", 1)
        key = key.strip().lower()
        if not key:
            raise ConfigError(f"第 {index} 个账号存在空字段名")
        fields[key] = value.strip()

    if not raw_cookie:
        raise ConfigError(f"第 {index} 个账号缺少 cookie 字段")
    # 只把第二个 cookie= 当作「又写了一个账号字段」；session/user 可能是正常 Cookie 名
    if re.search(r"(?:^|;)\s*cookie\s*=", raw_cookie, re.IGNORECASE):
        raise ConfigError(
            f"第 {index} 个账号的 cookie 必须是最后一个字段，后面不能再写 cookie="
        )

    cookie = normalize_cookie(raw_cookie)
    validate_cookie(cookie)
    return {
        "label": fields.get("user") or f"账号{index}",
        "cookie": cookie,
    }


def parse_accounts(raw):
    entries = split_entries(raw)
    return [parse_account(entry, index) for index, entry in enumerate(entries, start=1)]


def load_config():
    proxy, proxy_source = resolve_proxy("BREEZE_PROXY")
    if proxy_source and not proxy:
        print(f"🌐 已按 {proxy_source} 强制直连")
    elif proxy:
        print(f"🌐 使用代理：{mask_proxy(proxy)}（来自 {proxy_source}）")
    return {
        "accounts": os.environ.get("BREEZE_ACCOUNTS", ""),
        "timeout": env_int("BREEZE_TIMEOUT", 20, 5, 120),
        "proxy": proxy,
    }


def _snippet(value, limit=200):
    """把接口返回压成一行短文本，错误信息里用。"""
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value or "")
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _is_auth_error(status, data, text):
    if status in (401, 403):
        return True
    code = str(data.get("code") or "").upper()
    if code == "AUTH_REQUIRED":
        return True
    return "AUTH_REQUIRED" in (text or "")


def _fmt_number(value):
    if value is None:
        return "未知"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _as_bool(value):
    """接口字段严格转布尔：兼容 JSON 布尔、0/1 与字符串 true/false。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


class BreezeClient:
    """只封装积分查询与领取两个网页接口。"""

    def __init__(self, cookie, timeout=20, proxy="", session=None):
        self.cookie = cookie
        self.timeout = timeout
        self.session = session or requests.Session()
        # 代理只认显式配置与青龙全局变量，不受宿主机系统代理干扰
        self.session.trust_env = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _headers(self):
        return {
            "cookie": self.cookie,
            "user-agent": UA,
            "content-type": "application/json",
            "accept": "application/json",
            # Breeze 会校验来源，少了这两个会被 403 Request origin is not allowed
            "origin": BASE,
            "referer": BASE + "/app/home",
        }

    def _request(self, method, path):
        last_error = "未知错误"
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            if delay:
                time.sleep(delay)
            try:
                response = self.session.request(
                    method,
                    BASE + path,
                    headers=self._headers(),
                    data=b"{}" if method == "POST" else None,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < len(RETRY_DELAYS):
                    print(f"  ⚠️  请求 {path} 失败（第 {attempt} 次），准备重试")
                continue
            if response.status_code in RETRY_STATUS and attempt < len(RETRY_DELAYS):
                last_error = f"HTTP {response.status_code}"
                print(f"  ⚠️  接口返回 HTTP {response.status_code}，准备重试")
                continue
            return response
        raise NetworkError(f"请求 {path} 失败（已重试 {len(RETRY_DELAYS)} 次）：{last_error}")

    @staticmethod
    def _decode(response):
        text = response.text or ""
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError:
            data = {}
        # 接口正常只返回对象；数组/标量都按空响应处理，避免后续 .get 崩溃
        if not isinstance(data, dict):
            data = {}
        return data, text

    def summary(self):
        response = self._request("GET", SUMMARY_PATH)
        data, text = self._decode(response)
        if _is_auth_error(response.status_code, data, text):
            raise AuthError(
                f"登录态失效（HTTP {response.status_code}）："
                "请重新登录 breezeblue.ai，复制新的 breeze_blue_session 更新 BREEZE_ACCOUNTS"
            )
        if response.status_code != 200:
            raise ApiError(
                f"读取积分失败 HTTP {response.status_code}：{_snippet(data or text)}"
            )
        if not data:
            raise ApiError(f"读取积分失败：接口返回为空或格式不符：{_snippet(text)}")
        if data.get("ok") is False:
            raise ApiError(f"读取积分失败：{_snippet(data)}")
        return data

    def claim(self):
        response = self._request("POST", CLAIM_PATH)
        data, text = self._decode(response)
        if _is_auth_error(response.status_code, data, text):
            raise AuthError(
                f"登录态失效（HTTP {response.status_code}）："
                "请重新登录 breezeblue.ai，复制新的 breeze_blue_session 更新 BREEZE_ACCOUNTS"
            )
        # 部分实现会在已领过时返回 200/400/409 + already claimed，按成功处理。
        # 关键词必须足够明确，避免把「已领完」「not claimed today」误判成已领。
        lowered = (text or "").lower()
        already_claimed = (
            (
                any(word in lowered for word in ("already claimed", "已领取"))
                and "not already claimed" not in lowered
            )
            or _as_bool(data.get("already_claimed"))
            or str(data.get("code") or "").upper() == "ALREADY_CLAIMED"
        )
        if (
            response.status_code in (200, 400, 409)
            and already_claimed
            and not _as_bool(data.get("claimed"))
        ):
            return {
                "claimed": False,
                "already_claimed": True,
                "balance": data.get("balance"),
            }
        if response.status_code == 200 and data.get("ok") is not False:
            if not data:
                raise ApiError(f"领取失败：接口返回为空或格式不符：{_snippet(text)}")
            return data
        raise ApiError(f"领取失败 HTTP {response.status_code}：{_snippet(data or text)}")


def _status_text(summary):
    """把 summary 里的状态压成一句人类可读的话。"""
    parts = []
    balance = summary.get("balance")
    if balance is not None:
        parts.append(f"余额 {_fmt_number(balance)}")
    if "daily_claim_claimed_today" in summary:
        claimed_value = summary.get("daily_claim_claimed_today")
        if claimed_value is None:
            parts.append("今日状态未知")
        else:
            parts.append("今日已领" if _as_bool(claimed_value) else "今日未领")
    amount = summary.get("daily_claim_amount")
    if amount is not None:
        parts.append(f"每日额度 {_fmt_number(amount)}")
    next_reset = summary.get("daily_claim_next_reset_at")
    if next_reset:
        parts.append(f"下次重置 {next_reset}")
    return "｜".join(parts)


def _status_suffix(summary):
    """把 summary 状态压成「｜余额 …」形式的后缀，空状态返回空串。"""
    text = _status_text(summary)
    return f"｜{text}" if text else ""


def run_account(account, config, probe=False):
    """查询并领取单个账号，返回结果字典。"""
    client = BreezeClient(
        account["cookie"], timeout=config["timeout"], proxy=config["proxy"]
    )
    summary = client.summary()
    if probe:
        print(f"  [DEBUG] summary: {_snippet(summary, 800)}")

    if _as_bool(summary.get("daily_claim_claimed_today")):
        message = f"ℹ️ 今天已经领过了{_status_suffix(summary)}"
        return {"label": account["label"], "ok": True, "message": message}

    available = summary.get("daily_claim_available")
    if available not in (None, "") and not _as_bool(available):
        raise ApiError(
            f"当前不可领取（daily_claim_available=false）{_status_suffix(summary)}"
        )

    try:
        data = client.claim()
    except (NetworkError, ApiError) as exc:
        # 领取请求可能已经在服务端生效，先复查再判失败，避免误报
        print("  ⚠️  领取请求失败，复查今日领取状态")
        try:
            latest = client.summary()
        except (AuthError, ApiError, NetworkError):
            raise exc
        if _as_bool(latest.get("daily_claim_claimed_today")):
            message = f"✅ 领取请求异常但积分已到账{_status_suffix(latest)}"
            return {"label": account["label"], "ok": True, "message": message}
        raise

    if probe:
        print(f"  [DEBUG] claim: {_snippet(data, 800)}")

    claimed = _as_bool(data.get("claimed"))
    credits = data.get("credits_issued")
    balance = data.get("balance")
    next_reset = data.get("daily_claim_next_reset_at")
    suffix = f"｜余额 {_fmt_number(balance)}"
    if next_reset:
        suffix += f"｜下次重置 {next_reset}"

    if claimed:
        message = f"✅ 领到 +{_fmt_number(credits)}{suffix}"
    elif _as_bool(data.get("daily_claim_claimed_today")) or _as_bool(
        data.get("already_claimed")
    ):
        message = f"ℹ️ 今天已经领过了{suffix}"
    else:
        raise ApiError(f"领取接口未返回 claimed=true{suffix}")

    return {"label": account["label"], "ok": True, "message": message}


def check_account(account, config, probe=False):
    """只读自检：查询登录态、余额与今日领取状态，不提交领取。"""
    client = BreezeClient(
        account["cookie"], timeout=config["timeout"], proxy=config["proxy"]
    )
    summary = client.summary()
    if probe:
        print(f"  [DEBUG] summary: {_snippet(summary, 800)}")
    return {
        "label": account["label"],
        "ok": True,
        "message": "✅ 登录态有效" + _status_suffix(summary),
    }


def sleep_between_accounts():
    time.sleep(random.uniform(1, 2))


def build_report(results, check_mode):
    total = len(results)
    ok = sum(1 for item in results if item["ok"])
    if total and ok == total:
        title = f"✅ Breeze 积分签到 全部成功（{ok}/{total}）"
    elif ok == 0:
        title = f"❌ Breeze 积分签到 全部失败（0/{total}）"
    else:
        title = f"⚠️ Breeze 积分签到 部分失败（{ok}/{total}）"

    lines = [
        f"# Breeze 积分签到 - {'自检报告' if check_mode else '执行报告'}",
        "",
        f"⏰ 执行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"📊 总计 {total} 个账号，✅ 成功 {ok}，❌ 失败 {total - ok}",
        "",
    ]
    ok_lines = [f"- **[{item['label']}]** {item['message']}" for item in results if item["ok"]]
    fail_lines = [
        f"- **[{item['label']}]** ❌ {item['error']}" for item in results if not item["ok"]
    ]
    if ok_lines:
        lines.append("## 成功")
        lines.extend(ok_lines)
        lines.append("")
    if fail_lines:
        lines.append("## 失败")
        lines.extend(fail_lines)
    return title, "\n".join(lines).rstrip()


def maybe_notify(title, content, ok, total, force=False):
    """按开关推送：失败必推；全成功时默认静默。"""
    if not env_bool("BREEZE_NOTIFY", True):
        print("ℹ️ BREEZE_NOTIFY=false，已禁用推送")
        return
    if not force and env_bool("BREEZE_NOTIFY_ONLY_FAIL", True) and ok == total:
        print("ℹ️ 全部成功且 BREEZE_NOTIFY_ONLY_FAIL=true，跳过推送")
        return
    send_notify(title, content)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    check_mode = "--check" in argv or env_bool("BREEZE_CHECK")
    probe = "--probe" in argv or env_bool("BREEZE_PROBE") or env_bool("BREEZE_DEBUG")

    print("=" * 60)
    mode = "自检模式（只读）" if check_mode else "开始执行"
    print(f"🚀 Breeze 积分签到 {mode}  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    try:
        config = load_config()
        accounts = parse_accounts(config["accounts"])
    except ConfigError as exc:
        message = f"❌ {exc}\n\n{ACCOUNT_HELP}"
        print(message)
        maybe_notify("❌ Breeze 积分签到 配置错误", message, 0, 0, force=True)
        return 2

    print(f"📋 共 {len(accounts)} 个账号")

    results = []
    for index, account in enumerate(accounts):
        if index:
            sleep_between_accounts()
        print(f"\n---- 账号 {index + 1}/{len(accounts)}：{account['label']} ----")
        try:
            if check_mode:
                result = check_account(account, config, probe)
            else:
                result = run_account(account, config, probe)
            print("  " + result["message"])
        except (AuthError, ApiError, NetworkError, ConfigError) as exc:
            result = {"label": account["label"], "ok": False, "error": str(exc)}
            print(f"  ❌ {exc}")
        except Exception as exc:  # 单账号异常不能中断整轮
            result = {
                "label": account["label"],
                "ok": False,
                "error": f"未知错误：{type(exc).__name__}",
            }
            print(f"  ❌ 未知错误：{type(exc).__name__}")
        results.append(result)

    title, content = build_report(results, check_mode)
    ok = sum(1 for item in results if item["ok"])
    maybe_notify(title, content, ok, len(results))

    print(f"\n==== 完成：成功 {ok}/{len(results)} ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
