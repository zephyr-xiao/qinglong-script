# -*- coding: utf-8 -*-
"""
new Env('纸鸢下载签到');
cron: 5 8 * * *

纸鸢下载（mybt.kiteyuan.info）自动签到，移植自 elongou-checkin/mybt-signin。
流程：Casdoor 账号密码自动登录换取站点 token → 每日签到 + 访问任务 → 青龙 notify.py 推送。
登录态按账号本地缓存复用，失效时先用 refresh_token 续期、失败才重新登录（缓存不存密码）。

环境变量：
  MYBT_ACCOUNT           账号：用户名#密码（多账号用 & 或换行分隔）
  MYBT_TOKEN             手动 JWT 兜底（无账号密码时必填；自动登录失败时回退）
  MYBT_COOKIE            附加 Cookie（如 cf_clearance=...）
  MYBT_TURNSTILE_TOKEN   登录触发人机验证时手动提供的验证码 token
  MYBT_DO_VISIT          true/false 是否执行访问任务，默认 true
  MYBT_NOTIFY            true/false 是否推送，默认 true
  MYBT_NOTIFY_ONLY_FAIL  true/false 仅失败时推送，默认 false
  MYBT_TIMEOUT           HTTP 超时秒数，默认 30
  MYBT_DEBUG             true/false 输出调试细节，默认 false
  MYBT_PROXY             HTTP/SOCKS 代理，如 http://172.17.0.1:7890
  MYBT_BASE_URL / MYBT_AUTH_URL / MYBT_SECRET   站点/认证地址/签名密钥（一般不用改）
作者: 箫遥风
"""
import base64
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

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

# 3. notify.py 兼容加载（青龙运行时把 notify.py 加入 sys.path；本地干跑找不到不报错）
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


# ============================ 基础配置 ============================

DEFAULT_BASE_URL = "https://mybt.kiteyuan.info"
DEFAULT_AUTH_URL = "https://auth.kiteyuan.info"
DEFAULT_SECRET = "change-this-secret"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# JWT 缓存：有效期减 12h 余量才复用；无 exp 时按保存时间 24h 内复用
TOKEN_MARGIN_SECONDS = 12 * 3600
TOKEN_FALLBACK_TTL_SECONDS = 24 * 3600
TOKEN_CACHE_FILE = Path(__file__).resolve().parent / "mybt_token.json"

ALREADY_KEYWORDS = [
    "已签到", "已经签到", "今日已签", "您已签到", "您今日已",
    "签到过", "重复签到", "连续签到", "已完成", "已领取",
    "already", "already signed", "signed in", "signed-in", "signed",
]
# ⚠️ 不要把"签到成功""签到完成"放入此列表！
#   这两个词是首次签到成功的标志，不是"重复签到"的幂等提示。


def env_str(name, default=""):
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


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


def dbg(msg):
    if env_bool("MYBT_DEBUG", False):
        print(f"  🔍 {msg}")


def mask(s: str) -> str:
    """账号脱敏：保留前 2 后 2，邮箱保留 @ 之后。"""
    if not s:
        return ""
    if "@" in s:
        head, tail = s.split("@", 1)
        if len(head) <= 2:
            return head[0] + "*@" + tail
        return head[:2] + "*" * (len(head) - 2) + "@" + tail
    if len(s) <= 2:
        return s[0] + "*"
    return s[:2] + "*" * max(1, len(s) - 4) + s[-2:]


def parse_credentials(env_value: str):
    """解析 'a#b&c#d' / 多行 -> [(user, pwd), ...]"""
    if not env_value:
        return []
    parts = re.split(r"[&\n]+", env_value.strip())
    accounts = []
    for raw in parts:
        raw = raw.strip()
        if not raw or "#" not in raw:
            continue
        u, p = raw.split("#", 1)
        u, p = u.strip(), p.strip()
        if u and p:
            accounts.append((u, p))
    return accounts


def parse_cookie_str(s: str) -> dict:
    """'a=1; b=2' -> {'a':'1','b':'2'}"""
    jar = {}
    for part in (s or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar[k.strip()] = v.strip()
    return jar


def _is_already_signed_in(msg: str) -> bool:
    if not msg:
        return False
    return any(kw.lower() in msg.lower() for kw in ALREADY_KEYWORDS)


_NETWORK_ERR_KEYWORDS = (
    "SSLError", "ConnectionError", "Max retries exceeded", "Timeout", "timed out",
    "EOF occurred", "Connection reset", "RemoteDisconnected", "ProtocolError",
)


def _is_network_error(err) -> bool:
    return any(kw in str(err) for kw in _NETWORK_ERR_KEYWORDS)


def _call_with_retry(fn, attempts=3, delay=2):
    """网络层失败（status==0）重试；业务响应不重试（签到/访问幂等，重试安全）。"""
    for i in range(attempts):
        status, data, raw = fn()
        if status != 0 or i == attempts - 1:
            return status, data, raw
        dbg(f"网络异常，{delay}s 后重试 ({i + 2}/{attempts}): {raw}")
        time.sleep(delay)


def _login_with_retry(account, base_url, auth_url, attempts=3, delay=2):
    """仅对网络异常重试登录；密码错误/验证码拦截等业务错误立即抛出。"""
    last = None
    for i in range(attempts):
        try:
            return _casdoor_login(account, base_url, auth_url)
        except Exception as e:
            if not _is_network_error(e):
                raise
            last = e
            print(f"           ⚠️ 登录网络异常，{delay}s 后重试 ({i + 2}/{attempts})")
            time.sleep(delay)
    raise last


# ============================ 请求签名 ============================

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sort_query(query: str) -> str:
    """按 key 排序 query 参数，与站点前端签名逻辑一致；重复 key 取首次出现。"""
    if not query:
        return ""
    keys, values = [], {}
    for kv in query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
        else:
            k, v = kv, None
        if k not in values:
            keys.append(k)
        values[k] = v
    keys.sort()
    return "&".join(k if values[k] is None else f"{k}={values[k]}" for k in keys)


def build_sign_headers(method, api_path, body, secret):
    """生成 X-Sign / X-Timestamp。
    canonical = METHOD\\npath\\nsorted_query\\nsha256(body)\\nts"""
    path, query = api_path, ""
    if "?" in api_path:
        path, query = api_path.split("?", 1)
    query = sort_query(query)
    ts = str(int(time.time()))
    body = body or ""
    canonical = f"{method.upper()}\n{path}\n{query}\n{sha256_hex(body)}\n{ts}"
    dbg(f"sign canonical={canonical!r}")
    sign = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"X-Sign": sign, "X-Timestamp": ts}


# ============================ JWT / token 缓存 ============================

def _jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def jwt_exp(token: str) -> int:
    try:
        return int(_jwt_payload(token).get("exp") or 0)
    except Exception:
        return 0


def jwt_uid(token: str) -> str:
    data = _jwt_payload(token)
    return str(data.get("uid") or data.get("id") or "")


def _load_token_cache() -> dict:
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("accounts") or {}
    except Exception:
        return {}


def _save_token_cache(accounts_map: dict):
    try:
        payload = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": accounts_map,
        }
        with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 写入 token 缓存失败: {e}")


def save_token_cache(username: str, session):
    """缓存登录态，绝不存密码。

    session 为登录/续期接口返回的字典 {"token", "refresh_token", "expires_in"}；
    同时兼容直接传 token 字符串的旧调用方式。
    """
    if not username:
        return
    if isinstance(session, str):
        session = {"token": session}
    session = session or {}
    token = str(session.get("token") or "").strip()
    if not token:
        return
    # 该 token 的 payload 非标准 JWT，jwt_exp() 解析不出 exp，改用 expires_in 换算
    expires_in = int(session.get("expires_in") or 0)
    exp = int(time.time()) + expires_in if expires_in > 0 else jwt_exp(token)
    m = _load_token_cache()
    m[username] = {
        "token": token,
        "refresh_token": str(session.get("refresh_token") or "").strip(),
        "exp": exp,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_token_cache(m)


def token_reusable(entry: dict, now=None) -> bool:
    """本地 TTL 只是减请求次数的优化，凭证是否有效以服务端 401 为准。"""
    entry = entry or {}
    token = entry.get("token") or ""
    if not token:
        return False
    now = time.time() if now is None else now
    exp = entry.get("exp") or jwt_exp(token)
    if exp:
        return exp - now > TOKEN_MARGIN_SECONDS
    try:
        saved = datetime.strptime(str(entry.get("saved_at") or ""), "%Y-%m-%d %H:%M:%S")
        return (now - saved.timestamp()) < TOKEN_FALLBACK_TTL_SECONDS
    except Exception:
        return False


def read_reusable_cached_token(username: str) -> str:
    if not username:
        return ""
    entry = _load_token_cache().get(username) or {}
    return entry.get("token") if token_reusable(entry) else ""


def read_cached_refresh_token(username: str) -> str:
    """取缓存的 refresh_token，用于免登录续期、降低风控概率。"""
    if not username:
        return ""
    return (_load_token_cache().get(username) or {}).get("refresh_token") or ""


def refresh_site_token(base_url, refresh_token, timeout=30, proxy=""):
    """用 refresh_token 换新 token，失败返回 {}。

    /auth/refresh 与 /auth/session 同属站点免签名白名单，无需 X-Sign。
    """
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        return {}
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Content-Type": "application/json"})
        if proxy:
            sess.proxies.update({"http": proxy, "https": proxy})
        r = sess.post(f"{base_url.rstrip('/')}/api/auth/refresh",
                      data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
                      timeout=timeout)
        data = _json_or(r.text)
        token = str(data.get("token") or "").strip()
        if not token:
            dbg(f"refresh 未返回 token: HTTP {r.status_code} {r.text[:200]!r}")
            return {}
        return {
            "token": token,
            "refresh_token": str(data.get("refresh_token") or "").strip(),
            "expires_in": int(data.get("expires_in") or 0),
        }
    except Exception as e:
        dbg(f"refresh_token 续期异常: {type(e).__name__}: {e}")
        return {}


# ============================ Casdoor 登录 ============================

def _json_or(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _extract_token(url: str, body_text: str) -> str:
    q = parse_qs(urlparse(url).query)
    token = (q.get("token") or [""])[0].strip()
    if token:
        return token
    m = re.search(r"[?&#]token=([A-Za-z0-9_\-\.]+)", (url or "") + "\n" + (body_text or ""))
    return m.group(1) if m else ""


class CasdoorLogin:
    """Casdoor 账号密码登录，换取站点 JWT（移植自原脚本 CasdoorLogin）。"""

    def __init__(self, base_url, auth_url, username, password, turnstile_token="",
                 timeout=30, proxy=""):
        self.base_url = base_url.rstrip("/")
        self.auth_url = auth_url.rstrip("/")
        self.username = username
        self.password = password
        self.turnstile_token = turnstile_token
        self.timeout = timeout
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if proxy:
            self.sess.proxies.update({"http": proxy, "https": proxy})

    def login(self):
        # 1) 站点 Casdoor 登录入口，跟随重定向拿到 authorize 参数
        r = self.sess.get(f"{self.base_url}/api/auth/casdoor/login", timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"start oauth failed HTTP {r.status_code}: {r.text[:200]!r}")
        authorize_url = r.url
        dbg(f"authorize_url={authorize_url}")
        qs = parse_qs(urlparse(authorize_url).query)
        client_id = (qs.get("client_id") or [""])[0]
        redirect_uri = (qs.get("redirect_uri") or [""])[0]
        scope = (qs.get("scope") or ["openid profile email"])[0]
        state = (qs.get("state") or [""])[0]
        if not client_id or not redirect_uri:
            raise RuntimeError(f"oauth params incomplete: {authorize_url}")

        # 2) 应用信息（organization / application 名）
        app_q = {"clientId": client_id, "responseType": "code", "redirectUri": redirect_uri,
                 "type": "code", "scope": scope, "state": state}
        r2 = self.sess.get(f"{self.auth_url}/api/get-app-login?{urlencode(app_q)}",
                           timeout=self.timeout)
        app = _json_or(r2.text).get("data") or {}
        org = app.get("organization") or "KiteYuan Users Organization"
        app_name = app.get("name") or "KiteYuan KiteMagnet"
        dbg(f"Casdoor app={app_name} org={org} enablePassword={app.get('enablePassword')}")

        # 3) 提交账号密码登录 → OAuth code
        login_payload = {"application": app_name, "organization": org,
                         "username": self.username, "password": self.password,
                         "autoSignin": True, "type": "code", "signinMethod": "Password"}
        if self.turnstile_token:
            login_payload["captchaType"] = "Cloudflare Turnstile"
            login_payload["captchaToken"] = self.turnstile_token
            login_payload["captchaCode"] = self.turnstile_token
        login_url = f"{self.auth_url}/api/login?{urlencode(app_q)}"
        r3 = self.sess.post(
            login_url,
            data=json.dumps(login_payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": self.auth_url,
                     "Referer": authorize_url},
            timeout=self.timeout,
        )
        login_json = _json_or(r3.text)
        if not login_json and r3.status_code >= 400:
            raise RuntimeError(f"login non-json HTTP {r3.status_code}: {r3.text[:300]}")
        if login_json.get("status") != "ok":
            msg = login_json.get("msg") or login_json.get("message") or r3.text[:300]
            low = str(msg).lower()
            if "captcha" in low or "turnstile" in low or "人机" in str(msg):
                raise RuntimeError(f"login requires captcha/turnstile: {msg}")
            raise RuntimeError(f"login failed: {msg}")
        data, data2 = login_json.get("data"), login_json.get("data2")
        code, redirect = None, None
        if isinstance(data, str) and data:
            if data.startswith("http"):
                redirect = data
            else:
                code = data
        if isinstance(data, dict):
            code = data.get("code") or data.get("authCode")
        if isinstance(data2, str) and data2:
            if data2.startswith("http"):
                redirect = data2
            elif len(data2) < 200 and not code:
                code = data2
        if redirect and not code:
            code = (parse_qs(urlparse(redirect).query).get("code") or [""])[0]
        if not code:
            dbg(f"login ok but no code in response, raw={login_json}")
            r = self.sess.get(authorize_url, timeout=self.timeout)
            m = re.search(r"[?&#]token=([A-Za-z0-9_\-\.]+)", r.url + "\n" + r.text)
            if m:
                token = m.group(1)
                return token, jwt_uid(token)
            m = re.search(r"code=([A-Za-z0-9_\-\.]+)", r.url + "\n" + r.text)
            if m:
                code = m.group(1)
            else:
                raise RuntimeError(
                    f"cannot extract oauth code after login: final={r.url} body={r.text[:200]!r}")

        # 4) callback 换取站点登录态
        return self._exchange_for_session(code, state)

    def _exchange_for_session(self, code, state):
        """用 OAuth code 换站点登录态，返回 {token, refresh_token, expires_in, uid}。

        callback 有两种响应形态：直接携带 token，或只返回一次性 login_code
        （须再调 POST /api/auth/session 换会话 token），两者都要支持。
        注意 OAuth code 是一次性的，因此这里只请求一次 callback，不做重放。
        """
        callback = f"{self.base_url}/api/auth/casdoor/callback?" + urlencode({"code": code, "state": state})
        r4 = self.sess.get(callback, timeout=self.timeout, allow_redirects=False)
        location = r4.headers.get("Location", "") or ""

        # 形态一：callback 响应里直接带 token
        token = _extract_token(location, r4.text)
        if token:
            dbg("callback 直接返回 token")
            return {"token": token, "refresh_token": "", "expires_in": 0, "uid": jwt_uid(token)}

        if not location:
            # 未发生重定向（callback 就地 200），再从最终 URL / body 里找一次
            token = _extract_token(r4.url, r4.text)
            if token:
                return {"token": token, "refresh_token": "", "expires_in": 0, "uid": jwt_uid(token)}
            raise RuntimeError(
                f"callback 未返回登录票据. status={r4.status_code} final={r4.url} body={r4.text[:200]!r}")

        params = parse_qs(urlparse(location).query)
        err = (params.get("error") or [""])[0]
        if err:
            # 服务端原文比 "final=.../login" 更有诊断价值
            raise RuntimeError(f"callback 返回错误: {err}")
        login_code = (params.get("login_code") or [""])[0]
        if not login_code:
            raise RuntimeError(
                f"callback 未返回 login_code/token. location={location!r} body={r4.text[:200]!r}")
        dbg(f"callback login_code={login_code}")

        # 形态二：login_code 换会话 token（该接口免签名）
        r5 = self.sess.post(
            f"{self.base_url}/api/auth/session",
            data=json.dumps({"login_code": login_code}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Origin": self.base_url, "Referer": location},
            timeout=self.timeout,
        )
        data = _json_or(r5.text)
        token = str(data.get("token") or "").strip()
        if not token:
            raise RuntimeError(
                f"login_code 换 token 失败. HTTP {r5.status_code} body={r5.text[:300]!r}")
        return {
            "token": token,
            "refresh_token": str(data.get("refresh_token") or "").strip(),
            "expires_in": int(data.get("expires_in") or 0),
            "uid": jwt_uid(token),
        }


# ============================ 站点 API 客户端 ============================

class MybtClient:
    def __init__(self, base_url, token, user_id="", secret=DEFAULT_SECRET,
                 cookie="", timeout=30, proxy=""):
        self.base_url = base_url.rstrip("/")
        self.token = (token or "").removeprefix("Bearer ").strip()
        self.user_id = user_id or ""
        self.secret = secret or DEFAULT_SECRET
        self.timeout = timeout
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": UA})
        if proxy:
            self.sess.proxies.update({"http": proxy, "https": proxy})
        if cookie:
            for k, v in parse_cookie_str(cookie).items():
                self.sess.cookies.set(k, v)

    def set_sign_secret(self, secret):
        if secret:
            self.secret = secret

    def request(self, method, path, body_obj=None):
        rel = path if path.startswith("/") else f"/{path}"
        if not rel.startswith("/api/"):
            rel = "/api" + rel
        body = None if body_obj is None else json.dumps(
            body_obj, ensure_ascii=False, separators=(",", ":"))
        headers = {
            "Accept": "application/json, */*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
        }
        headers.update(build_sign_headers(method, rel, body or "", self.secret))
        data = None if body is None else body.encode("utf-8")
        try:
            resp = self.sess.request(method.upper(), self.base_url + rel, data=data,
                                     headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            return 0, None, f"network error: {type(e).__name__}: {e}"
        raw = resp.text
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = raw
        return resp.status_code, parsed, raw

    def me(self):
        return self.request("GET", "/auth/me")

    def signin(self):
        return self.request("POST", "/auth/points/tasks/signin", {})

    def visit(self):
        return self.request("POST", "/auth/points/tasks/visit", {})


# ============================ 响应判定 ============================

def summarize(action, status, data, raw):
    """把接口响应归纳为 (是否成功, 展示消息)。"""
    if status == 0:
        return False, f"{action}: {raw}"
    if isinstance(data, dict):
        err = str(data.get("error") or data.get("message") or data.get("msg") or "")
        if status >= 400:
            if _is_already_signed_in(err):
                return True, f"{action}: {err}"
            return False, f"{action}: HTTP {status} - {err or raw[:200]}"
        if err and _is_already_signed_in(err):
            return True, f"{action}: {err}"
        if data.get("success") is False:
            return False, f"{action}: {err or raw[:200]}"
        added = data.get("added")
        if added is not None:
            return True, f"{action}: 成功，获得 {added} 积分"
        return True, f"{action}: 成功 - {json.dumps(data, ensure_ascii=False)[:300]}"
    if 200 <= status < 300:
        return True, f"{action}: HTTP {status}"
    return False, f"{action}: HTTP {status} - {raw[:200]}"


# ============================ 任务编排 ============================

def collect_accounts():
    """汇总环境变量里的账号。返回 [{username, password, token}, ...]"""
    accounts, seen = [], set()
    for u, p in parse_credentials(os.environ.get("MYBT_ACCOUNT", "")):
        if u.lower() not in seen:
            seen.add(u.lower())
            accounts.append({"username": u, "password": p, "token": ""})
    if not accounts:
        t = os.environ.get("MYBT_TOKEN", "").strip()
        if t:
            accounts.append({"username": "", "password": "", "token": t})
    return accounts


def account_label(task):
    return mask(task["username"]) if task.get("username") else "MYBT_TOKEN"


def _casdoor_login(account, base_url, auth_url):
    return CasdoorLogin(
        base_url, auth_url,
        account["username"], account["password"],
        turnstile_token=env_str("MYBT_TURNSTILE_TOKEN"),
        timeout=env_int("MYBT_TIMEOUT", 30),
        proxy=env_str("MYBT_PROXY"),
    ).login()


def _run_chain(client, do_visit):
    """登录态检查 → 签到 → 访问任务 → 积分统计。
    返回 (是否全部成功, 消息列表, 是否登录态失效)。"""
    lines = []
    status, data, raw = _call_with_retry(client.me)
    ok, msg = summarize("登录态检查", status, data, raw)
    if ok:
        # me() 成功响应含 sign_secret/邮箱等敏感信息，报告里只显示"正常"
        msg = "登录态检查: 正常"
    lines.append(msg)
    print(f"           {msg}")
    if not ok:
        return False, lines, status == 401
    if isinstance(data, dict):
        if data.get("sign_secret"):
            client.set_sign_secret(data["sign_secret"])
        user = data.get("user")
        if isinstance(user, dict):
            info = f"用户: {user.get('username')} 当前积分: {user.get('points')}"
            lines.append(info)
            print(f"           {info}")
            if user.get("id"):
                client.user_id = str(user.get("id"))
    ok_all = True
    status, data, raw = _call_with_retry(client.signin)
    ok, msg = summarize("签到", status, data, raw)
    lines.append(msg)
    print(f"           {msg}")
    ok_all = ok_all and ok
    if do_visit:
        status, data, raw = _call_with_retry(client.visit)
        ok, msg = summarize("访问任务", status, data, raw)
        lines.append(msg)
        print(f"           {msg}")
        ok_all = ok_all and ok
    status, data, raw = _call_with_retry(client.me)
    if status == 200 and isinstance(data, dict) and isinstance(data.get("user"), dict):
        after = f"签到后积分: {data['user'].get('points')}"
        lines.append(after)
        print(f"           {after}")
    return ok_all, lines, False


def run_one_task(account):
    base_url = env_str("MYBT_BASE_URL", DEFAULT_BASE_URL)
    auth_url = env_str("MYBT_AUTH_URL", DEFAULT_AUTH_URL)
    secret = env_str("MYBT_SECRET", DEFAULT_SECRET)
    cookie = env_str("MYBT_COOKIE")
    do_visit = env_bool("MYBT_DO_VISIT", True)
    username = account.get("username") or ""
    password = account.get("password") or ""
    has_cred = bool(username and password)
    label = account_label(account)
    fallback_token = (account.get("token") or env_str("MYBT_TOKEN")).strip()

    lines = []

    def note(msg):
        lines.append(msg)
        print(f"           {msg}")

    token = read_reusable_cached_token(username) if username else ""
    if token:
        note("♻️ 使用缓存登录态")
    elif has_cred:
        try:
            session = _login_with_retry(account, base_url, auth_url)
            token = session["token"]
            note("✅ 自动登录成功")
            save_token_cache(username, session)
        except Exception as e:
            if not fallback_token:
                return {"success": False, "label": label,
                        "message": f"自动登录失败且无 MYBT_TOKEN 兜底: {e}"}
            token = fallback_token
            note(f"⚠️ 自动登录失败({e})，回退 MYBT_TOKEN")
    else:
        if not fallback_token:
            return {"success": False, "label": label,
                    "message": "未配置账号密码，也未提供 MYBT_TOKEN"}
        token = fallback_token
        note("🔑 使用 MYBT_TOKEN")

    # 登录态失效（401）时最多补救一次，防死循环
    for attempt in (1, 2):
        client = MybtClient(base_url, token, user_id=jwt_uid(token), secret=secret,
                            cookie=cookie, timeout=env_int("MYBT_TIMEOUT", 30),
                            proxy=env_str("MYBT_PROXY"))
        ok_all, chain_lines, auth_dead = _run_chain(client, do_visit)
        lines.extend(chain_lines)
        if not auth_dead:
            return {"success": ok_all, "label": label, "message": "；".join(lines)}
        if attempt > 1:
            break

        # 先试 refresh_token 续期：不触碰 Casdoor 登录流程，对被风控更友好；失败再完整重登
        session = {}
        if username:
            session = refresh_site_token(base_url, read_cached_refresh_token(username),
                                         timeout=env_int("MYBT_TIMEOUT", 30),
                                         proxy=env_str("MYBT_PROXY"))
        if session.get("token"):
            note("♻️ 登录态失效，已用 refresh_token 续期")
            save_token_cache(username, session)
        elif has_cred:
            note("♻️ 登录态失效，重新登录")
            try:
                session = _login_with_retry(account, base_url, auth_url)
                note("✅ 重新登录成功")
                save_token_cache(username, session)
            except Exception as e:
                return {"success": False, "label": label,
                        "message": f"登录态失效且重新登录失败: {e}"}
        else:
            break
        token = session["token"]
    return {"success": False, "label": label, "message": "；".join(lines) or "登录态失效（HTTP 401）"}


# ============================ 主流程 ============================

def main():
    title = "纸鸢下载签到"
    notify_enabled = env_bool("MYBT_NOTIFY", True)
    notify_only_fail = env_bool("MYBT_NOTIFY_ONLY_FAIL", False)
    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"🌐 {env_str('MYBT_BASE_URL', DEFAULT_BASE_URL)}")
    print("=" * 60)

    tasks = collect_accounts()
    if not tasks:
        print("⚠️ 未配置任何账号，请设置 MYBT_ACCOUNT（格式：用户名#密码）或 MYBT_TOKEN")
        return

    print(f"📋 共发现 {len(tasks)} 个账号\n")
    results, success_cnt = [], 0
    for idx, task in enumerate(tasks, 1):
        print(f"[{idx}/{len(tasks)}] 🔄 纸鸢下载 [{account_label(task)}] ...")
        t0 = time.time()
        try:
            result = run_one_task(task)
        except Exception:
            traceback.print_exc()
            result = {"success": False, "label": account_label(task), "message": "脚本异常"}
        cost = time.time() - t0

        if result.get("success"):
            success_cnt += 1
            print(f"           ✅ 完成 ({cost:.1f}s)\n")
        else:
            print(f"           ❌ 失败 ({cost:.1f}s)\n")
        results.append(result)

        # 风控间隔（必须！多账号别一把梭）
        if idx < len(tasks):
            time.sleep(random.uniform(1, 2))

    # 汇总报告（Markdown 格式给 notify.py；标题按成败分档）
    total = len(tasks)
    if success_cnt == total:
        title = f"✅ {title} 全部成功（{success_cnt}/{total}）"
    elif success_cnt == 0:
        title = f"❌ {title} 全部失败（0/{total}）"
    else:
        title = f"⚠️ {title} 部分失败（{success_cnt}/{total}）"

    _label_prefix = re.compile(r"^账号\s*\d+\s*[:：]\s*")
    ok_lines = [f"- **[{r.get('label', f'账号 {i}')}]** "
                f"{_label_prefix.sub('', r.get('message') or r.get('error', ''), count=1)}"
                for i, r in enumerate(results, 1) if r.get("success")]
    fail_lines = [f"- **[{r.get('label', f'账号 {i}')}]** "
                  f"{_label_prefix.sub('', r.get('message') or r.get('error', ''), count=1)}"
                  for i, r in enumerate(results, 1) if not r.get("success")]

    lines = [f"# {title} - 执行报告", ""]
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

    # 推送（默认全成功也推；配 MYBT_NOTIFY_ONLY_FAIL=true 则仅失败时推）
    if notify_enabled:
        if notify_only_fail and success_cnt == total:
            print("ℹ️ MYBT_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(title, report)
    else:
        print("ℹ️ MYBT_NOTIFY=false，已禁用推送")

    # 退出码：全失败 exit(1)（青龙任务红色标记）；部分失败/全成功 exit(0)
    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
