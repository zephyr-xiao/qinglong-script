#!/usr/bin/env python3
"""夸克网盘每日签到（移动端签名参数版）

cron: 13 18 * * *
new Env('夸克签到')

为什么用 kps/sign/vcode 而不是 Cookie：
2026 年 11 月起，夸克对 PC 端 Cookie 的 growth/info 与 growth/sign 都返回
HTTP 200 + 空 data。旧脚本把空 dict 当假值直接跳过，于是"没签到"也显示成功。
移动端签名参数（kps/sign/vcode）路径仍然有效，本脚本只走这条路。

环境变量：
  QUARK_ACCOUNTS       必填。多账号用换行或 && 分隔，账号内用 ; 分隔字段：
                         user=张三; kps=xxx; sign=xxx; vcode=xxx
                         user=张三; url=https://drive-m.quark.cn/...?kps=..&sign=..&vcode=..
  QUARK_TIMEOUT        请求超时秒数，默认 15，允许 5-120
  QUARK_RANDOM_DELAY   整轮开始前随机延迟上限秒数，默认 0
  QUARK_CHECK          设为 true 等价 --check（只读自检，不提交签到）
  QUARK_PROBE          设为 true 等价 --probe（额外打印接口原始返回）

退出码：0 全部成功 / 1 有账号失败 / 2 配置错误
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import unquote, urlparse

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


INFO_URL = "https://drive-m.quark.cn/1/clouddrive/capacity/growth/info"
SIGN_URL = "https://drive-m.quark.cn/1/clouddrive/capacity/growth/sign"
REQUIRED_PARAMS = ("kps", "sign", "vcode")
# GET 允许重试的网关类状态码；业务 4xx 不重试
RETRY_STATUS = (429, 500, 502, 503, 504)
RETRY_DELAYS = (1, 2)
# 出现这些字段说明填的是浏览器 Cookie，不是移动端签名参数
COOKIE_HINTS = ("__pus", "__puus", "_UP_A4A_11_", "b-user-id", "tfstk")

ACCOUNT_HELP = """🔧 获取方法（抓包）：
1. 用 MuMu 模拟器（开启 Root）或已 Root 的手机，安装夸克 App 与 ProxyPin
2. 把 ProxyPin 的 CA 证书装成「系统证书」，不要只装用户证书
3. 打开夸克 App 进入「网盘 → 签到/领空间」页面
4. 在 ProxyPin 里搜索域名 drive-m.quark.cn
5. 复制该请求的完整 URL，粘到 QUARK_ACCOUNTS 的 url= 后面

示例（单账号）：
  user=张三; url=https://drive-m.quark.cn/1/clouddrive/capacity/growth/info?kps=..&sign=..&vcode=..
示例（多账号，换行分隔）：
  user=张三; kps=..; sign=..; vcode=..
  user=李四; kps=..; sign=..; vcode=.."""

# 账号分隔：换行，或后面紧跟 key=value / URL 的 &&（避免切坏 URL 里的查询串）
_ACCOUNT_SPLIT = re.compile(r"\r?\n+|&&(?=\s*(?:[A-Za-z_][\w-]*\s*=|https?://))")


class ConfigError(ValueError):
    """配置缺失或格式错误。"""


class AuthError(RuntimeError):
    """凭证失效，需要重新抓包。"""


class ApiError(RuntimeError):
    """接口返回与预期不符（含空 data、字段缺失、业务码非 0）。"""


class NetworkError(RuntimeError):
    """网络层失败，重试后仍不可用。"""


def load_notify():
    """按青龙常见布局查找 notify.py：脚本目录 → 父目录 → /ql/data/scripts → /ql/scripts。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (here, os.path.dirname(here), "/ql/data/scripts", "/ql/scripts"):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
        try:
            from notify import send as notify_func  # type: ignore

            return notify_func
        except Exception:
            continue
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
        return
    for attempt in range(1, 4):
        try:
            _NOTIFY(title, content)
            print("📨 通知推送成功")
            return
        except Exception as exc:
            print(f"⚠️  通知推送第 {attempt}/3 次失败：{type(exc).__name__}")
            if attempt < 3:
                time.sleep(3)
    print("❌ 通知推送失败，已跳过")


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


def human_size(value):
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value or 0)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.2f} {units[index]}"


def split_entries(raw):
    if raw is None or not raw.strip():
        raise ConfigError("未配置 QUARK_ACCOUNTS，或变量内容为空")
    entries = [part.strip() for part in _ACCOUNT_SPLIT.split(raw)]
    entries = [part for part in entries if part]
    if not entries:
        raise ConfigError("QUARK_ACCOUNTS 中没有可用的账号")
    return entries


def extract_url_params(url):
    """从抓包 URL 里取出 kps/sign/vcode。

    刻意不用 parse_qs：它按 application/x-www-form-urlencoded 解析，会把字面量
    "+" 变成空格，而夸克新版权限参数里确实存在字面量 "+"，一旦被改写就 401。
    """
    query = {}
    for part in urlparse(url).query.split("&"):
        if not part:
            continue
        key, separator, value = part.partition("=")
        key = unquote(key)
        if not key or key in query:
            continue
        query[key] = unquote(value) if separator else ""
    return {name: query.get(name, "") for name in REQUIRED_PARAMS}


def parse_account(entry, index):
    """解析单个账号条目，支持 `url=` 整条抓包链接或手填三参数。"""
    lowered = entry.lower()
    if any(hint.lower() in lowered for hint in COOKIE_HINTS):
        raise ConfigError(
            f"第 {index} 个账号填的是浏览器 Cookie。夸克已不接受 PC Cookie 签到，"
            "请改填 kps/sign/vcode（见下方抓包说明）"
        )

    if entry.startswith("http"):
        fields = {"url": entry}
    else:
        fields = {}
        for field in entry.split(";"):
            field = field.strip()
            if not field:
                continue
            if "=" not in field:
                raise ConfigError(f"第 {index} 个账号存在无效字段（缺少 =）")
            key, value = field.split("=", 1)
            key = key.strip().lower()
            if not key:
                raise ConfigError(f"第 {index} 个账号存在空字段名")
            fields[key] = value.strip()

    if fields.get("cookie"):
        raise ConfigError(
            f"第 {index} 个账号用了 cookie=，但夸克已不接受 PC Cookie 签到，"
            "请改填 kps/sign/vcode（见下方抓包说明）"
        )

    if fields.get("url"):
        for key, value in extract_url_params(fields["url"]).items():
            fields.setdefault(key, value)

    missing = [name for name in REQUIRED_PARAMS if not fields.get(name)]
    if missing:
        raise ConfigError(f"第 {index} 个账号缺少参数：{', '.join(missing)}")

    return {
        "label": fields.get("user") or f"账号{index}",
        "kps": fields["kps"],
        "sign": fields["sign"],
        "vcode": fields["vcode"],
    }


def parse_accounts(raw):
    entries = split_entries(raw)
    return [parse_account(entry, index) for index, entry in enumerate(entries, start=1)]


def load_config():
    return {
        "accounts": os.environ.get("QUARK_ACCOUNTS", ""),
        "timeout": env_int("QUARK_TIMEOUT", 15, 5, 120),
        "random_delay": env_int("QUARK_RANDOM_DELAY", 0, 0, 86400),
    }


class QuarkClient:
    """只封装签到相关的两个接口，所有异常都翻译成上面的四类。"""

    def __init__(self, account, timeout=15, session=None):
        self.account = account
        self.timeout = timeout
        self.session = session or requests.Session()

    def _params(self):
        return {
            "pr": "ucpro",
            "fr": "android",
            "kps": self.account["kps"],
            "sign": self.account["sign"],
            "vcode": self.account["vcode"],
        }

    @staticmethod
    def _parse(response, stage):
        """把响应翻成 data；任何"看起来成功但实际没数据"都算失败。"""
        try:
            payload = response.json()
        except ValueError:
            if response.status_code == 401:
                raise AuthError(f"{stage}凭证失效（HTTP 401），请重新抓包")
            raise ApiError(f"{stage}返回的不是 JSON（可能是风控页或登录页）")

        if not isinstance(payload, dict):
            raise ApiError(f"{stage}返回格式异常")

        code = payload.get("code")
        message = str(payload.get("message") or "")

        if response.status_code == 401 or code == 31001 or "require login" in message.lower():
            raise AuthError(f"{stage}凭证失效（{message or 'require login'}），请重新抓包")
        if response.status_code != 200:
            raise ApiError(f"{stage}失败：HTTP {response.status_code}")
        if code != 0:
            raise ApiError(f"{stage}失败：{message or ('code=' + str(code))}")

        data = payload.get("data")
        # 空 data 是本次修复的核心：旧脚本把 {} 当假值静默跳过，等于什么都没做
        if not isinstance(data, dict) or not data:
            raise ApiError(f"{stage}返回空数据（接口可能已改版，请更新脚本）")
        return data

    def get_growth_info(self):
        last_error = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                response = self.session.get(
                    INFO_URL, params=self._params(), timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise NetworkError(
                    f"查询成长信息网络异常（{type(exc).__name__}）"
                ) from exc
            if response.status_code in RETRY_STATUS and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            return self._parse(response, "查询成长信息")
        raise NetworkError("查询成长信息重试耗尽") from last_error

    def sign(self):
        """提交签到。刻意不重试：POST 不是幂等语义，失败靠复查状态确认。"""
        try:
            response = self.session.post(
                SIGN_URL,
                params=self._params(),
                json={"sign_cyclic": True},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NetworkError(f"提交签到网络异常（{type(exc).__name__}）") from exc
        data = self._parse(response, "提交签到")
        reward = data.get("sign_daily_reward")
        if not isinstance(reward, (int, float)):
            raise ApiError("提交签到返回缺少 sign_daily_reward")
        return reward


def _account_snapshot(account, info):
    cap_sign = info.get("cap_sign")
    if not isinstance(cap_sign, dict):
        raise ApiError("成长信息缺少 cap_sign 字段")
    composition = info.get("cap_composition")
    if not isinstance(composition, dict):
        composition = {}
    return {
        "label": account["label"],
        "vip": "88VIP" if info.get("88VIP") else "普通用户",
        "total": info.get("total_capacity") or 0,
        "accumulated": composition.get("sign_reward") or 0,
        "progress": cap_sign.get("sign_progress"),
        "target": cap_sign.get("sign_target"),
        "signed": bool(cap_sign.get("sign_daily")),
        "today_reward": cap_sign.get("sign_daily_reward") or 0,
    }


def _progress_text(progress, target):
    return f"（{progress}/{target}）"


def run_account(account, config, probe=False):
    client = QuarkClient(account, config["timeout"])
    info = client.get_growth_info()
    if probe:
        print("  🔍 成长信息：" + json.dumps(info, ensure_ascii=False)[:800])
    snapshot = _account_snapshot(account, info)

    head = (
        f"{snapshot['vip']} {snapshot['label']}｜"
        f"总容量 {human_size(snapshot['total'])}｜"
        f"签到累计 {human_size(snapshot['accumulated'])}"
    )

    if snapshot["signed"]:
        message = (
            f"{head}\n  ✅ 今日已签到 +{human_size(snapshot['today_reward'])}，"
            f"连签进度{_progress_text(snapshot['progress'], snapshot['target'])}"
        )
        return {"label": account["label"], "ok": True, "state": "already", "message": message}

    try:
        reward = client.sign()
    except NetworkError as exc:
        # POST 结果不确定时复查一次，避免把"其实签上了"误判成失败
        print("  ⚠️  提交签到结果不确定，复查一次")
        verified = _account_snapshot(account, client.get_growth_info())
        if not verified["signed"]:
            raise ApiError("签到结果无法确认（复查显示今日仍未签到）") from exc
        message = (
            f"{head}\n  ✅ 复查确认今日已签到 +{human_size(verified['today_reward'])}，"
            f"连签进度{_progress_text(verified['progress'], verified['target'])}"
        )
        return {"label": account["label"], "ok": True, "state": "verified", "message": message}

    next_progress = (
        snapshot["progress"] + 1 if isinstance(snapshot["progress"], int) else "?"
    )
    message = (
        f"{head}\n  ✅ 签到成功 +{human_size(reward)}，"
        f"连签进度{_progress_text(next_progress, snapshot['target'])}"
    )
    return {"label": account["label"], "ok": True, "state": "signed", "message": message}


def check_account(account, config, probe=False):
    """只读自检：验证凭证、签到状态与容量，不提交签到。"""
    client = QuarkClient(account, config["timeout"])
    info = client.get_growth_info()
    if probe:
        print("  🔍 成长信息：" + json.dumps(info, ensure_ascii=False)[:800])
    snapshot = _account_snapshot(account, info)
    status = "今日已签到" if snapshot["signed"] else "今日未签到"
    message = (
        f"{snapshot['vip']} {snapshot['label']}\n"
        f"  ✅ 凭证有效｜{status}\n"
        f"  💾 总容量 {human_size(snapshot['total'])}｜"
        f"签到累计 {human_size(snapshot['accumulated'])}｜"
        f"连签进度{_progress_text(snapshot['progress'], snapshot['target'])}"
    )
    return {"label": account["label"], "ok": True, "state": "checked", "message": message}


def sleep_between_accounts():
    time.sleep(random.uniform(2, 5))


def build_report(results, check_mode):
    total = len(results)
    ok = sum(1 for item in results if item["ok"])
    if total and ok == total:
        title = f"✅ 夸克签到 全部成功（{ok}/{total}）"
    elif ok == 0:
        title = f"❌ 夸克签到 全部失败（0/{total}）"
    else:
        title = f"⚠️ 夸克签到 部分失败（{ok}/{total}）"

    lines = [
        f"# 夸克签到 - {'自检报告' if check_mode else '执行报告'}",
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


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    check_mode = "--check" in argv or env_bool("QUARK_CHECK")
    probe = "--probe" in argv or env_bool("QUARK_PROBE")

    print("=" * 60)
    mode = "自检模式（只读）" if check_mode else "开始执行"
    print(f"🚀 夸克签到 {mode}  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    try:
        config = load_config()
        accounts = parse_accounts(config["accounts"])
    except ConfigError as exc:
        message = f"❌ {exc}\n\n{ACCOUNT_HELP}"
        print(message)
        send_notify("❌ 夸克签到 配置错误", message)
        return 2

    print(f"📋 共 {len(accounts)} 个账号")

    if config["random_delay"] > 0:
        delay = random.randint(0, config["random_delay"])
        if delay:
            print(f"⏰ 随机延迟 {delay} 秒后开始")
            time.sleep(delay)

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
    send_notify(title, content)

    ok = sum(1 for item in results if item["ok"])
    print(f"\n==== 完成：成功 {ok}/{len(results)} ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
