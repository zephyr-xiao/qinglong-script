# -*- coding: utf-8 -*-
"""
new Env('芯幻签到');
cron: 5 8 * * *

芯幻（xhcyv.com，ACG 社区）每日签到领积分。

站点契约（2026-10-21 真机实测）：
  登录  POST https://xhcyv.com/api/session/login       body {"account","password"}
        → 成功时 token 只出现在 Set-Cookie 的 x_bbs_token 里（JWT，有效期 3 天），
          响应 body 仅含 user 信息，不含 token。
  状态  GET  https://api.xhcyv.com/api/v1/users/me/signin
  签到  POST https://api.xhcyv.com/api/v1/users/me/signin
  用户  GET  https://api.xhcyv.com/api/v1/users/me
  验证码 GET https://api.xhcyv.com/api/v1/captcha
  鉴权  Authorization: Bearer <x_bbs_token>
  统一响应 {code, message, data}，code=0 为成功；签到重复返回 11006。

环境变量：
  XHCYV_ACCOUNTS     必填，账号#密码（也兼容 账号:密码 / 账号&密码），多账号换行分隔；
                     也可直接填 x_bbs_token（JWT，以 eyJ 开头）表示只用凭证
  XHCYV_ACCOUNT      单账号便捷写法（配合 XHCYV_PASSWORD）
  XHCYV_PASSWORD     单账号密码
  XHCYV_TOKEN        可选，直接提供 x_bbs_token，优先于账号密码，多账号换行分隔
  XHCYV_NOTIFY       true/false 默认 true，是否调用 notify.py 推送
  XHCYV_NOTIFY_ONLY_FAIL true/false 默认 false，全部成功时静默（需 NOTIFY=true）
  XHCYV_TIMEOUT      秒 默认 30，HTTP 超时
  XHCYV_DEBUG        true/false 默认 false，输出接口响应细节
  XHCYV_PROXY        代理地址 默认空，如 http://172.17.0.1:7890

作者: zephyr_xiao
"""

import base64
import json
import os
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

# 1. UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 问题）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 2. requests 缺失友好提示
try:
    import requests
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
except ImportError:
    print("❌ 缺少 requests 库，请在青龙面板「依赖管理」-「Python」中安装 requests")
    sys.exit(1)

# 3. notify.py 兼容（青龙运行时把 notify.py 加入 sys.path；本地干跑找不到不报错）
_qinglong_send = None
try:
    from notify import send as _qinglong_send  # type: ignore
except Exception:
    _scripts_dir = "/ql/data/scripts"
    if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
        try:
            from notify import send as _qinglong_send  # type: ignore
        except Exception:
            _qinglong_send = None


def send_notify(title: str, content: str) -> bool:
    """通过青龙 notify.py 推送，带 3 次重试。返回是否成功。"""
    if not _qinglong_send:
        print("ℹ️ 未找到 notify.py（仅在青龙环境内可用），跳过推送")
        return False
    max_attempts, retry_delay = 3, 3
    for attempt in range(1, max_attempts + 1):
        try:
            _qinglong_send(title, content)
            print("📨 已通过 notify.py 推送")
            return True
        except Exception as e:
            print(f"⚠️ 推送第 {attempt}/{max_attempts} 次失败: {e}")
            if attempt < max_attempts:
                print(f"   等待 {retry_delay} 秒后重试推送...")
                time.sleep(retry_delay)
            else:
                print("⚠️ notify.py 推送最终失败")
    return False


# ============================ 配置常量 ============================

SITE_URL = "https://xhcyv.com"
API_BASE = "https://api.xhcyv.com/api/v1"
LOGIN_URL = SITE_URL + "/api/session/login"      # 登录只在主站同源路由提供
CAPTCHA_PATH = "/captcha"
SIGNIN_PATH = "/users/me/signin"
USER_PATH = "/users/me"

TOKEN_COOKIE = "x_bbs_token"
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_FILE = SCRIPT_DIR / "xhcyv_token.json"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 站点业务码（取自前端源码的错误码表，已真机核对 11006 / 10002）
CODE_OK = 0
CODE_TOO_MANY = 10006
CODE_CAPTCHA_REQUIRED = 10007
CODE_CAPTCHA_WRONG = 10008
CODE_MAINTENANCE = 10009
CODE_UNAUTHORIZED = 10002
CODE_BAD_CREDENTIALS = 11003
CODE_BANNED = 11004
CODE_ALREADY_SIGNED = 11006
CODE_EMAIL_NOT_VERIFIED = 11008

NETWORK_RETRY_DELAYS = (3, 6)   # 仅连接类异常重试；已拿到 HTTP 响应不重试
TOKEN_MIN_REMAINING = 3600      # 缓存 token 剩余有效期不足 1 小时则视为不可用


# ============================ 通用工具函数 ============================

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except Exception:
        return default


def mask_account(s: str) -> str:
    """账号脱敏：保留前 3 后 2，避免日志与推送泄露完整账号。"""
    if not s:
        return ""
    if len(s) <= 8:
        return s[:2] + "*" * max(1, len(s) - 2)
    return s[:3] + "*" * 4 + s[-2:]


def mask_token(token: str) -> str:
    """token 脱敏：JWT 只留头尾，防止日志可直接复用凭证。"""
    if not token:
        return ""
    if len(token) <= 16:
        return token[:4] + "***"
    return token[:10] + "..." + token[-6:]


def jwt_exp(token: str) -> int:
    """解析 JWT payload 的 exp（Unix 秒）。非标准 JWT / 解析失败返回 0。"""
    try:
        parts = (token or "").split(".")
        if len(parts) != 3:
            return 0
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return int(data.get("exp") or 0)
    except Exception:
        return 0


def _looks_like_token(line: str) -> bool:
    """JWT 特征：三段以点分隔且首段是 eyJ（{"alg":...} 的 base64）。"""
    return line.startswith("eyJ") and line.count(".") == 2


def parse_accounts(raw: str):
    """
    解析账号配置，返回 [{"type","account","password","label"}, ...]。

    支持三种行格式：
      账号#密码 / 账号:密码 / 账号&密码 → 账密登录
      eyJ...（JWT）                     → 直接使用 x_bbs_token
    分隔符取行内最早出现的 "#"、":"、"&" 之一（推荐 "#"）；
    密码本身可含这三种符号，只要不早于分隔符出现即可。
    """
    if not raw or not raw.strip():
        return []
    entries = []
    for line in re.split(r"[\n]+", raw.strip()):
        line = line.strip()
        if not line:
            continue
        if _looks_like_token(line):
            entries.append({
                "type": "token",
                "token": line,
                "label": f"凭证 [{mask_token(line)}]",
            })
            continue
        # 取行内最早出现的分隔符：账号/密码自身含 # : & 时不会被后面的符号误切
        marks = [(line.find(sep), sep) for sep in ("#", ":", "&") if sep in line]
        if not marks:
            continue
        _, sep = min(marks)
        account, password = line.split(sep, 1)
        account, password = account.strip(), password.strip()
        if not account or not password:
            continue
        entries.append({
            "type": "credential",
            "account": account,
            "password": password,
            "label": f"{mask_account(account)}",
        })
    return entries


def collect_accounts():
    """按优先级汇总环境变量中的账号：XHCYV_ACCOUNTS > XHCYV_ACCOUNT/PASSWORD。"""
    entries = parse_accounts(os.getenv("XHCYV_ACCOUNTS") or "")
    single_account = (os.getenv("XHCYV_ACCOUNT") or "").strip()
    single_password = (os.getenv("XHCYV_PASSWORD") or "").strip()
    if single_account and single_password:
        entries.insert(0, {
            "type": "credential",
            "account": single_account,
            "password": single_password,
            "label": mask_account(single_account),
        })
    return entries


# ============================ 异常与请求层 ============================

class ApiError(RuntimeError):
    """站点业务错误：携带业务码、HTTP 状态与原始 message。"""

    def __init__(self, message: str, code: int = -1, http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

    @property
    def is_unauthorized(self) -> bool:
        return self.code == CODE_UNAUTHORIZED or self.http_status == 401


class NetworkError(RuntimeError):
    """网络请求重试耗尽。"""


def describe_network_error(e: Exception) -> str:
    """把 requests 异常翻译成可排查的中文描述（判断顺序从特殊到一般）。"""
    text = f"{type(e).__name__}: {e}"
    low = text.lower()
    if "proxy" in low:
        return f"代理连接失败（{text}），请检查 XHCYV_PROXY 是否可从容器访问"
    if "ssl" in low or "certificate" in low:
        return f"TLS 握手/证书异常（{text}），请检查系统时间或中间设备"
    if "timeout" in low or "timed out" in low:
        return f"请求超时（{text}），站点可能限速或网络不通"
    if "connection" in low or "reset" in low or "refused" in low:
        return f"连接被拒绝/重置（{text}），站点可能临时不可达"
    return f"网络异常（{text}）"


def resolve_proxy() -> str:
    """代理优先级：XHCYV_PROXY > 标准 HTTPS_PROXY/HTTP_PROXY。"""
    for name in ("XHCYV_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def build_session(timeout: int, proxy: str) -> requests.Session:
    """
    构造 session。trust_env=False 是刻意的：Windows 下 requests 会自动读取
    注册表里的系统代理（Clash 等），导致脚本在青龙能跑、本机干跑却超时；
    代理改由 resolve_proxy() 显式传入。
    """
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": SITE_URL,
        "Referer": SITE_URL + "/",
    })
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    _orig_request = session.request

    def _request(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return _orig_request(method, url, **kwargs)

    session.request = _request
    return session


def api_call(session, method: str, url: str, debug: bool, token: str = None,
             body: dict = None):
    """
    发请求并解析统一响应。成功返回 data（可能为 None）。
    业务 code != 0 抛 ApiError；连接类异常按 NETWORK_RETRY_DELAYS 重试后抛 NetworkError。
    """
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    last_exc = None
    for attempt in range(len(NETWORK_RETRY_DELAYS) + 1):
        try:
            resp = session.request(method, url, headers=headers, json=body)
            break
        except requests.RequestException as e:
            last_exc = e
            if attempt < len(NETWORK_RETRY_DELAYS):
                delay = NETWORK_RETRY_DELAYS[attempt]
                print(f"   ⚠️ {describe_network_error(e)}，{delay} 秒后重试"
                      f"（{attempt + 1}/{len(NETWORK_RETRY_DELAYS)}）")
                time.sleep(delay)
    else:
        raise NetworkError(describe_network_error(last_exc))

    if debug:
        print(f"   [DEBUG] {method} {url} → HTTP {resp.status_code}: {resp.text[:400]}")

    if resp.status_code >= 500:
        raise ApiError(f"站点服务端故障（HTTP {resp.status_code}），请稍后重试",
                       code=-1, http_status=resp.status_code)

    try:
        payload = resp.json()
    except Exception:
        snippet = (resp.text or "")[:200].replace("\n", " ")
        raise ApiError(f"响应非 JSON（HTTP {resp.status_code}）: {snippet}",
                       code=-1, http_status=resp.status_code)

    if not isinstance(payload, dict):
        raise ApiError(f"响应格式异常（非对象）: {str(payload)[:200]}",
                       code=-1, http_status=resp.status_code)

    code = payload.get("code")
    message = payload.get("message") or ""
    if code != CODE_OK:
        raise ApiError(f"业务错误 code={code}: {message}", code=code,
                       http_status=resp.status_code)
    return payload.get("data")


# ============================ 凭证缓存 ============================

def read_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            accounts = data.get("accounts")
            return accounts if isinstance(accounts, dict) else {}
    except Exception as e:
        print(f"⚠️ 读取 token 缓存异常: {e}")
    return {}


def _write_cache_atomic(data: dict) -> None:
    """原子写入：先写同目录临时文件再 os.replace，避免中断留下半截 JSON。"""
    tmp_path = None
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".xhcyv_token.", dir=str(CACHE_FILE.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except Exception:
            pass
        os.replace(tmp_path, CACHE_FILE)
    except Exception as e:
        print(f"⚠️ 写入 token 缓存异常: {e}")
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def save_token(cache: dict, key: str, token: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cache[key] = {
        "token": token,
        "exp": jwt_exp(token),
        "update_time": now,
    }
    _write_cache_atomic({
        "site": "芯幻 xhcyv.com",
        "update_time": now,
        "accounts": cache,
    })


def drop_token(cache: dict, key: str, label: str = "") -> None:
    if key in cache:
        cache.pop(key, None)
        _write_cache_atomic({
            "site": "芯幻 xhcyv.com",
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": cache,
        })
        print(f"🗑️ 已清除账号 {label or mask_account(key)} 的失效 token 缓存")


def cached_token_usable(cache: dict, key: str) -> str:
    """取缓存 token；剩余有效期不足 1 小时按过期处理（避免签到前刚好失效）。"""
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return ""
    token = str(entry.get("token") or "").strip()
    if not token:
        return ""
    try:
        exp = int(entry.get("exp") or jwt_exp(token))
    except (TypeError, ValueError):
        exp = jwt_exp(token)
    if exp and time.time() > exp - TOKEN_MIN_REMAINING:
        print(f"ℹ️ 缓存 token 即将过期（{time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))}），改为重新登录")
        return ""
    return token


def token_expiry_text(token: str) -> str:
    exp = jwt_exp(token)
    if not exp:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))


# ============================ 认证 ============================

_ddddocr_engine = None


def solve_captcha(session, debug: bool):
    """
    取图形验证码并尝试用 ddddocr 识别。返回 (captcha_id, code)。
    未安装 ddddocr 或识别失败抛 ApiError，由调用方转成可读提示。
    """
    global _ddddocr_engine
    data = api_call(session, "GET", API_BASE + CAPTCHA_PATH, debug) or {}
    captcha_id = data.get("id") or ""
    image = data.get("image") or ""
    if not captcha_id or not image:
        raise ApiError("验证码接口未返回 id/image，站点可能已改版")

    if _ddddocr_engine is None:
        try:
            import ddddocr  # type: ignore
            _ddddocr_engine = ddddocr.DdddOcr(show_ad=False)
        except ImportError:
            raise ApiError("登录需要图形验证码，但未安装 ddddocr；"
                           "请在青龙「依赖管理」-「Python」安装 ddddocr 后重试")
        except Exception as e:
            raise ApiError(f"初始化 ddddocr 失败: {e}")

    raw = image.split(",", 1)[1] if image.startswith("data:") else image
    try:
        code = _ddddocr_engine.classification(base64.b64decode(raw)) or ""
    except Exception as e:
        raise ApiError(f"验证码识别异常: {e}")
    code = code.strip()
    if not code:
        raise ApiError("验证码识别结果为空")
    if debug:
        print(f"   [DEBUG] 验证码识别结果: {code}")
    return captcha_id, code


def do_login(session, account: str, password: str, debug: bool) -> dict:
    """
    账密登录。成功返回 {"token": ..., "user": ...}。
    token 只存在于 Set-Cookie 的 x_bbs_token，响应 body 不含 token。
    首次返回 10007（需要验证码）时自动取码重试，最多 2 轮。
    """
    captcha_payload = {}
    for attempt in range(1, 3):
        body = {"account": account, "password": password, **captcha_payload}
        try:
            data = api_call(session, "POST", LOGIN_URL, debug, body=body) or {}
        except ApiError as e:
            if e.code in (CODE_CAPTCHA_REQUIRED, CODE_CAPTCHA_WRONG):
                if attempt >= 2:
                    raise ApiError("图形验证码多次识别失败，请稍后重试或改用 XHCYV_TOKEN 模式")
                print(f"   🔐 站点要求图形验证码（code={e.code}），尝试自动识别 ...")
                cid, code = solve_captcha(session, debug)
                captcha_payload = {"captcha_id": cid, "captcha_code": code}
                continue
            raise

        if data.get("two_factor_required") and data.get("challenge"):
            raise ApiError("该账号已开启两步验证（2FA），脚本无法自动登录；"
                           "请在站点关闭 2FA 或改用 XHCYV_TOKEN 模式")

        token = session.cookies.get(TOKEN_COOKIE) or ""
        if not token:
            # 兜底：部分代理/网关会改写 Set-Cookie，直接扫响应头
            for value in (session.cookies.get_dict() or {}).values():
                if _looks_like_token(value):
                    token = value
                    break
        if not token:
            raise ApiError("登录成功但未拿到 x_bbs_token Cookie，站点登录流程可能已改版")
        return {"token": token, "user": data.get("user") or {}}

    raise ApiError("登录流程异常结束")


# ============================ 签到 ============================

def get_signin_status(session, token: str, debug: bool) -> dict:
    return api_call(session, "GET", API_BASE + SIGNIN_PATH, debug, token=token) or {}


def get_user_info(session, token: str, debug: bool) -> dict:
    return api_call(session, "GET", API_BASE + USER_PATH, debug, token=token) or {}


def do_signin(session, token: str, debug: bool) -> dict:
    """执行签到。返回 {"already": bool, ...}；失败抛 ApiError。"""
    try:
        data = api_call(session, "POST", API_BASE + SIGNIN_PATH, debug, token=token) or {}
        return {"already": False, **data}
    except ApiError as e:
        if e.code == CODE_ALREADY_SIGNED:
            return {"already": True}
        raise


def fmt_signin_result(result: dict) -> str:
    """把签到返回格式化成推送用的摘要。"""
    if result.get("already"):
        return "今日已签到"
    parts = []
    points = result.get("points")
    if points is not None:
        parts.append(f"+{points} 积分")
    day_order = result.get("day_order")
    if day_order:
        parts.append(f"今日第 {day_order} 位")
    streak = result.get("streak")
    if streak:
        parts.append(f"连签 {streak} 天")
    bonus = result.get("bonus")
    if bonus:
        parts.append(f"连签奖励 +{bonus}")
    member_bonus = result.get("member_bonus")
    if member_bonus:
        parts.append(f"会员加成 +{member_bonus}")
    return "签到成功 " + "，".join(parts) if parts else "签到成功"


def fmt_status(status: dict) -> str:
    """已签到状态摘要：优先用 record，缺字段就退回 streak。"""
    record = status.get("record") or {}
    parts = []
    if record.get("points") is not None:
        parts.append(f"+{record['points']} 积分")
    if record.get("day_order"):
        parts.append(f"今日第 {record['day_order']} 位")
    streak = record.get("streak") or status.get("streak")
    if streak:
        parts.append(f"连签 {streak} 天")
    return "今日已签到 " + "，".join(parts) if parts else "今日已签到"


def humanize_api_error(e: ApiError) -> str:
    """把常见业务码翻译成可操作的中文提示；未收录的码保留原始 code/message。"""
    mapping = {
        CODE_UNAUTHORIZED: "未登录或凭证已失效",
        CODE_BANNED: "账号已被封禁",
        CODE_TOO_MANY: "请求过于频繁（10006），请稍后重试",
        CODE_MAINTENANCE: "站点维护中，请稍后重试",
    }
    return mapping.get(e.code) or str(e)


def fmt_points_suffix(user: dict) -> str:
    points = user.get("points")
    if points is None:
        return ""
    level = user.get("level_name") or ""
    return f"（当前 {points} 积分{('，' + level) if level else ''}）"


# ============================ 单账号流程 ============================

def run_one_account(entry: dict, cache: dict, timeout: int, debug: bool,
                    check_only: bool = False) -> dict:
    """
    单账号完整流程。返回 {"success", "message"/"error", "label"}。
    check_only=True 时只验证凭证与签到状态，不提交签到。
    """
    label = entry["label"]
    print(f"🔄 {label} 开始{'自检' if check_only else '签到'} ...")
    session = build_session(timeout, resolve_proxy())

    token = ""
    status = None
    cache_key = ""
    if entry["type"] == "token":
        token = entry["token"]
        cache_key = "token:" + mask_token(token)
    else:
        cache_key = entry["account"]
        token = cached_token_usable(cache, cache_key)

    # 1) 先用缓存/直填 token 查状态；token 失效再登录
    if token:
        try:
            status = get_signin_status(session, token, debug)
            print(f"   ✅ 复用凭证（有效期至 {token_expiry_text(token) or '未知'}）")
        except ApiError as e:
            if not e.is_unauthorized:
                msg = humanize_api_error(e)
                print(f"   ❌ {msg}")
                return {"success": False, "error": msg, "label": label}
            if entry["type"] == "credential":
                print("   ⚠️ 凭证已失效，重新登录")
                drop_token(cache, cache_key, label)
            else:
                print("   ⚠️ 凭证已失效")
            token = ""
        except NetworkError as e:
            print(f"   ❌ {e}")
            return {"success": False, "error": str(e), "label": label}
    # 2) 登录拿新 token
    if not token:
        if entry["type"] == "token":
            return {"success": False, "label": label,
                    "error": "XHCYV_TOKEN 提供的凭证已失效，请重新从浏览器复制 x_bbs_token"}
        try:
            login_data = do_login(session, entry["account"], entry["password"], debug)
        except (ApiError, NetworkError) as e:
            msg = humanize_api_error(e) if isinstance(e, ApiError) else str(e)
            if isinstance(e, ApiError) and e.code == CODE_BAD_CREDENTIALS:
                msg = "账号或密码错误"
            elif isinstance(e, ApiError) and e.code == CODE_EMAIL_NOT_VERIFIED:
                msg = "邮箱尚未验证，请先在站点完成邮箱验证"
            print(f"   ❌ {msg}")
            return {"success": False, "error": msg, "label": label}
        token = login_data["token"]
        save_token(cache, cache_key, token)
        user = login_data.get("user") or {}
        print(f"   ✅ 登录成功{fmt_points_suffix(user)}"
              f"（有效期至 {token_expiry_text(token) or '未知'}）")
        try:
            status = get_signin_status(session, token, debug)
        except ApiError as e:
            if e.is_unauthorized:
                drop_token(cache, cache_key, label)
                msg = "登录后凭证立即失效，请检查账号是否被限制"
            else:
                msg = humanize_api_error(e)
            print(f"   ❌ 查询签到状态失败: {msg}")
            return {"success": False, "error": f"查询签到状态失败: {msg}", "label": label}
        except NetworkError as e:
            print(f"   ❌ 查询签到状态失败: {e}")
            return {"success": False, "error": f"查询签到状态失败: {e}", "label": label}

    status = status or {}

    # 3) 已签到 → 幂等成功
    if status.get("signed"):
        msg = fmt_status(status)
        print(f"   ✅ {msg}")
        return {"success": True, "message": msg, "label": label}

    if check_only:
        print("   ✅ 凭证有效，今日尚未签到")
        return {"success": True, "message": "凭证有效，今日尚未签到", "label": label}

    # 4) 提交签到
    try:
        result = do_signin(session, token, debug)
    except ApiError as e:
        if e.is_unauthorized:
            if entry["type"] == "credential":
                drop_token(cache, cache_key, label)
            return {"success": False, "label": label,
                    "error": "签到过程中凭证失效，请检查账号状态"}
        msg = humanize_api_error(e)
        print(f"   ❌ {msg}")
        return {"success": False, "error": msg, "label": label}
    except NetworkError as e:
        print(f"   ❌ {e}")
        return {"success": False, "error": str(e), "label": label}

    msg = fmt_signin_result(result)
    try:
        user = get_user_info(session, token, debug)
        msg += fmt_points_suffix(user)
    except (ApiError, NetworkError):
        pass
    print(f"   ✅ {msg}")
    return {"success": True, "message": msg, "label": label}


# ============================ 自检 ============================

def run_check(entries: list, cache: dict, timeout: int, debug: bool) -> int:
    """--check：只验证配置、登录/凭证与签到状态，不提交签到。"""
    print("🔍 自检模式：仅验证配置与凭证，不会提交签到\n")
    ok_cnt = 0
    for entry in entries:
        try:
            result = run_one_account(entry, cache, timeout, debug, check_only=True)
        except Exception as e:
            traceback.print_exc()
            result = {"success": False, "error": f"脚本异常: {e}", "label": entry["label"]}
        if result.get("success"):
            ok_cnt += 1
    print("\n" + "=" * 60)
    if ok_cnt == len(entries):
        print(f"✅ 自检通过（{ok_cnt}/{len(entries)}）")
        return 0
    print(f"❌ 自检失败（{ok_cnt}/{len(entries)}），请按上方提示修正")
    return 1


# ============================ 入口 ============================

def main():
    title = "芯幻签到"
    NOTIFY_ENABLED = env_bool("XHCYV_NOTIFY", True)
    NOTIFY_ONLY_FAIL = env_bool("XHCYV_NOTIFY_ONLY_FAIL", False)
    TIMEOUT = max(1, env_int("XHCYV_TIMEOUT", 30))
    DEBUG = env_bool("XHCYV_DEBUG", False)
    CHECK_ONLY = "--check" in sys.argv[1:]

    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    entries = collect_accounts()
    # XHCYV_TOKEN 优先：按行/&拆分后覆盖为纯凭证账号
    raw_tokens = (os.getenv("XHCYV_TOKEN") or "").strip()
    if raw_tokens:
        token_entries = [
            {"type": "token", "token": t, "label": f"凭证 [{mask_token(t)}]"}
            for t in re.split(r"[&\n]+", raw_tokens)
            if t.strip()
        ]
        entries = token_entries + entries

    if not entries:
        print("❌ 未配置任何账号")
        print("   请设置 XHCYV_ACCOUNTS=账号#密码（多账号换行分隔），")
        print("   或 XHCYV_ACCOUNT / XHCYV_PASSWORD，或 XHCYV_TOKEN=x_bbs_token")
        sys.exit(1)

    print(f"📋 共发现 {len(entries)} 个账号/凭证")
    cache = read_cache()
    if cache:
        print(f"📦 已加载 {len(cache)} 条 token 缓存")

    if CHECK_ONLY:
        sys.exit(run_check(entries, cache, TIMEOUT, DEBUG))

    results, success_cnt = [], 0
    for idx, entry in enumerate(entries, 1):
        print(f"\n[{idx}/{len(entries)}] ", end="")
        try:
            result = run_one_account(entry, cache, TIMEOUT, DEBUG)
        except Exception:
            traceback.print_exc()
            result = {"success": False, "error": "脚本异常", "label": entry["label"]}
        if result.get("success"):
            success_cnt += 1
        results.append(result)
        # 多账号间隔，避免同 IP 短时间连续请求触发风控
        if idx < len(entries):
            time.sleep(1)

    total = len(entries)
    if success_cnt == total:
        report_title = f"✅ {title} 全部成功（{success_cnt}/{total}）"
    elif success_cnt == 0:
        report_title = f"❌ {title} 全部失败（0/{total}）"
    else:
        report_title = f"⚠️ {title} 部分失败（{success_cnt}/{total}）"

    ok_lines = [
        f"- **[{r.get('label', f'账号 {i}')}]** {r.get('message')}"
        for i, r in enumerate(results, 1) if r.get("success")
    ]
    fail_lines = [
        f"- **[{r.get('label', f'账号 {i}')}]** {r.get('error')}"
        for i, r in enumerate(results, 1) if not r.get("success")
    ]

    lines = [f"# {report_title} - 执行报告", ""]
    lines.append(f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"📊 总计 {total} 个账号，✅ 成功 {success_cnt}，❌ 失败 {total - success_cnt}")
    lines.append("")
    if ok_lines:
        lines.append("## 成功")
        lines.extend(ok_lines)
        lines.append("")
    if fail_lines:
        lines.append("## 失败")
        lines.extend(fail_lines)
        lines.append("")
    report = "\n".join(lines).strip()
    print("\n" + report)

    if NOTIFY_ENABLED:
        if NOTIFY_ONLY_FAIL and success_cnt == total:
            print("ℹ️ XHCYV_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(report_title, report)
    else:
        print("ℹ️ XHCYV_NOTIFY=false，已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
