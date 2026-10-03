# -*- coding: utf-8 -*-
"""
new Env('福利吧预注册签到');
cron: 15 8 * * *

福利吧论坛(wnflb2023.com)「游客预注册签到」插件(fuliba_prereg)每日签到。
连续签到 30 天可免费转为正式会员,漏签会中断连续天数。

认证方式:仅 Cookie 模式(浏览器 F12 抓取完整 Cookie)。
Cookie 失效时推送告警,提醒重新抓取。

接口流程(实测):
  1. GET  plugin.php?id=fuliba_prereg:checkin        -> 签到页,取 formhash
     游客访问会渲染预注册表单(无 formhash 可用),据此判定 Cookie 失效
  2. POST plugin.php?id=fuliba_prereg:checkin&ac=do  -> body: formhash=xxx
     成功/幂等均返回 302,Location 形如 ...&done=<码>&d=<连续天数>
       done=1   签到成功
       done=-2  今日已签到(幂等,按成功处理)
     formhash 失配时返回 200 提示页「请通过签到页上的按钮提交本操作」,重取页面重试一次
     Cookie 失效时返回 200 提示页「您尚未登录」

认证方式(二选一,也可同时配置,各自独立跑):
  A. Cookie 模式    :WNFLB_COOKIE,浏览器抓的完整 Cookie,多账号换行分隔
  B. 账号密码模式   :WNFLB_ACCOUNTS,格式 用户名#密码,多账号换行分隔
     首次登录成功后把 Cookie 缓存到脚本同目录 wnflb_cookie.json,
     之后优先复用缓存,缓存失效才重新登录。

     ⚠️ 登录失败次数是**按 IP 统计**的(实测),同一 IP 连续失败 5 次会被要求
        输入验证码。因此账号密码模式下:认证失败绝不自动重试(只有网络错误才
        重试),缓存命中时完全不走登录流程。

环境变量:
  WNFLB_COOKIE             可选,完整 Cookie 字符串,多账号用换行分隔
  WNFLB_ACCOUNTS           可选,账号密码「用户名#密码」,多账号用换行分隔
  WNFLB_HOST               可选,站点域名,默认 www.wnflb2023.com(有备用域名时填这里)
  WNFLB_PROXY              可选,HTTP 代理,如 http://172.17.0.1:7890
  WNFLB_TIMEOUT            可选,HTTP 超时秒数,默认 25
  WNFLB_NOTIFY             true/false 默认 true,是否调用青龙 notify.py 推送
  WNFLB_NOTIFY_ONLY_FAIL   true/false 默认 false,全部成功时静默
  WNFLB_DEBUG              true/false 默认 false,输出接口响应细节用于排错

依赖:requests
      可选:ddddocr —— 仅在站点要求输入登录验证码时才需要(平时不加载)。
      该站同一 IP 连续登录失败 5 次后登录会要求图片验证码,此时靠 ddddocr 本地识别;
      未安装则会在该场景下给出明确提示,并建议改用 Cookie 模式。

作者: zephyr_xiao
"""

import html
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# UTF-8 强制重配(解决 Windows GBK / 部分容器 locale 问题)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
except ImportError:
    print("❌ 缺少 requests 库,请在青龙面板「依赖管理」-「Python」中安装 requests")
    sys.exit(1)


# ============================ notify.py 兼容 ============================

_qinglong_send = None
try:
    from notify import send as _qinglong_send  # type: ignore
except Exception:
    # 青龙容器内 notify.py 在 /ql/data/scripts/,任务运行时 cwd 不一定在该目录
    _scripts_dir = "/ql/data/scripts"
    if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
        try:
            from notify import send as _qinglong_send  # type: ignore
        except Exception:
            _qinglong_send = None


def send_notify(title: str, content: str) -> bool:
    """通过青龙 notify.py 推送,带 3 次重试。返回是否成功。"""
    if not _qinglong_send:
        print("ℹ️ 未找到 notify.py(仅在青龙环境内可用),跳过推送")
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


# ============================ 常量 ============================

DEFAULT_HOST = "www.wnflb2023.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 签到页游客特征(渲染预注册表单,说明 Cookie 已失效)
GUEST_MARKERS = ("立即预注册并签到", "游客预注册 ·", 'name="password2"')
# 签到页已签到特征(按钮变为 disabled 的「今日已签到 ✓」)
ALREADY_SIGNED_MARK = "今日已签到"

# POST ac=do 的 302 Location 中 done 参数语义(实测)
DONE_SUCCESS = 1     # 签到成功
DONE_ALREADY = -2    # 今日已签到

# 提示页文案特征
ERR_NEED_LOGIN = "尚未登录"
ERR_BAD_FORMHASH = "请通过签到页上的按钮提交本操作"

# ---- 账号密码登录(Discuz 标准流程) ----
LOGIN_PAGE_URL = "/member.php?mod=logging&action=login&infloat=yes&handlekey=login&inajax=1&ajaxtarget=fwin_content_login"
# 登录成功特征
LOGIN_OK_MARKERS = ("欢迎您回来", "登录成功", "现在将转入登录前页面")
# 登录失败特征
LOGIN_FAIL_MARKER = "登录失败"
# 需要验证码的特征
LOGIN_CAPTCHA_MARKERS = ("验证码", "seccode", "captcha")
# 触发安全提问的特征
LOGIN_QUESTION_MARKERS = ("安全提问", "请选择安全提问")

# 需要验证码时最多识别重试几次
CAPTCHA_MAX_TRY = 3

# Cookie 缓存文件(脚本同目录;.gitignore 已忽略 *_cookie.json)
COOKIE_CACHE_FILE = Path(__file__).resolve().parent / "wnflb_cookie.json"

MAX_RETRIES = 3
RETRY_BASE_DELAY = 5

DEBUG = False
TIMEOUT = 25


# ============================ 工具函数 ============================

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


# 青龙面板「配置文件 / 环境变量」里配置的全局代理变量
GLOBAL_PROXY_KEYS = (
    "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)


def resolve_proxy(*specific_keys: str) -> tuple:
    """优先级:脚本专属变量 > 青龙全局代理变量 > 空(直连)。"""
    for name in (*specific_keys, *GLOBAL_PROXY_KEYS):
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def parse_cookies(cookie_str: str) -> dict:
    """浏览器复制的 Cookie 字符串转 dict(按首个 = 切分,容忍值内含 =)。"""
    cookies = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def mask_cookie(cookie_str: str) -> str:
    """Cookie 脱敏展示:仅保留键名,不泄露值。"""
    keys = [item.split("=", 1)[0].strip() for item in cookie_str.split(";") if "=" in item]
    return f"<{len(keys)} 项: {', '.join(keys[:4])}{'...' if len(keys) > 4 else ''}>"


def mask(s: str) -> str:
    """用户名脱敏:保留前 2 后 2。"""
    if not s:
        return ""
    if len(s) <= 2:
        return s[0] + "*"
    return s[:2] + "*" * max(1, len(s) - 4) + s[-2:]


def _is_network_error(e: Exception) -> bool:
    """网络层错误需向上抛由外层重试,业务异常就地消化。"""
    msg = str(e).lower()
    keywords = ("timeout", "connection", "ssl", "proxy", "max retries", "eof")
    return any(k in msg for k in keywords) or isinstance(e, requests.RequestException)


def rand_sleep(min_s: float, max_s: float):
    """随机休眠,用于请求间隔降低风控特征。"""
    time.sleep(random.uniform(min_s, max_s))


def http_request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """带重试的请求。站点有偶发 SSL EOF 抖动,必须重试兜底。"""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=TIMEOUT, **kwargs)
            if DEBUG:
                print(f"   🔍 {method} {url[:78]} → {resp.status_code} ({len(resp.content)}B)")
            return resp
        except Exception as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * attempt
                print(f"   ⚠️ 请求失败({type(e).__name__}),{delay}s 后重试 {attempt}/{MAX_RETRIES - 1}")
                time.sleep(delay)
    raise last_exc


def html_to_text(html: str) -> str:
    """粗略提取页面可见文本,用于提示文案与排错输出。"""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


# ============================ 页面解析 ============================

def is_guest_page(html: str) -> bool:
    """游客访问会渲染预注册表单(含确认密码字段),据此判定 Cookie 失效。"""
    return any(marker in html for marker in GUEST_MARKERS)


def extract_formhash(html: str) -> str:
    """提取 formhash。Discuz 页面里是 <input type="hidden" name="formhash" value="xxx">。"""
    for pattern in (
        r'name="formhash"\s+value="([^"]+)"',
        r'value="([^"]+)"\s+name="formhash"',
        r'formhash=([a-z0-9]{6,})',
    ):
        m = re.search(pattern, html, re.I)
        if m:
            return m.group(1)
    return ""


def extract_toast(html: str) -> str:
    """提取插件结果提示 div.flp-toast 的文案。"""
    m = re.search(r'class="flp-toast"[^>]*>(.*?)</div>', html, re.S)
    if m:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    return ""


def extract_discuz_message(html: str) -> str:
    """提取 Discuz 通用提示页(id=messagetext)的文案。"""
    m = re.search(r'id="messagetext"[^>]*>\s*<p>(.*?)</p>', html, re.S)
    if not m:
        m = re.search(r'<div class="alert_(?:error|info)"[^>]*>(.*?)</div>', html, re.S)
    if m:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    return ""


def parse_stats(html: str) -> str:
    """解析签到进度:连续天数 / 进度条 / 距转正还差几天。"""
    parts = []
    m = re.search(r"已连续签到\s*(\d+)\s*天", html)
    if m:
        parts.append(f"连续 {m.group(1)} 天")
    m = re.search(r'flp-bignum">\s*(\d+)\s*<span>/\s*(\d+)\s*天', html)
    if m:
        parts.append(f"进度 {m.group(1)}/{m.group(2)}")
    m = re.search(r"距离转正还差\s*<b>(\d+)</b>\s*天", html)
    if m:
        parts.append(f"还差 {m.group(1)} 天转正")
    return "  │  ".join(parts)


# ============================ 会话构建 ============================

def new_session(host: str, cookie_str: str = "") -> requests.Session:
    """构建会话。cookie_str 为空时用于账号密码登录。"""
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://{host}/",
    })
    if cookie_str:
        session.cookies.update(parse_cookies(cookie_str))
    proxy, proxy_src = resolve_proxy("WNFLB_PROXY")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        print(f"🌐 使用代理: {proxy}(来源: {proxy_src})")
    return session


def session_cookies_to_str(session: requests.Session) -> str:
    """把会话里的 Cookie 导出成浏览器格式字符串(用于写缓存)。"""
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


# ============================ 凭证解析与 Cookie 缓存 ============================

def classify_credential(raw: str) -> tuple:
    """
    判定单条凭证类型,返回 (kind, payload)。
      - 含 ';' 与 '='  -> ("cookie", cookie字符串)
      - 含 '#'          -> ("account", (用户名, 密码))
      - 其他            -> ("unknown", raw)
    Cookie 值经 URL 编码不会出现裸 '#',故用 '#' 区分是安全的。
    """
    raw = raw.strip()
    if ";" in raw and "=" in raw:
        return "cookie", raw
    if "#" in raw:
        user, pwd = raw.split("#", 1)
        return "account", (user.strip(), pwd.strip())
    return "unknown", raw


def read_cookie_cache() -> dict:
    """读取 {用户名: {cookies, update_time}};文件缺失/损坏返回 {}。"""
    if not COOKIE_CACHE_FILE.exists():
        return {}
    try:
        with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("accounts", {}) or {}
    except Exception as e:
        print(f"⚠️ Cookie 缓存读取失败,忽略: {e}")
        return {}


def write_cookie_cache(username: str, cookie_str: str):
    """写入指定用户的 Cookie 缓存(只存凭证,绝不存密码)。"""
    data = {}
    if COOKIE_CACHE_FILE.exists():
        try:
            with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data.setdefault("accounts", {})[username] = {
        "cookies": cookie_str,
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(COOKIE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Cookie 缓存写入失败(不影响签到): {e}")


# ============================ 账号密码登录 ============================

_OCR = None
_OCR_ERR = None


def _load_ocr():
    """
    惰性加载 ddddocr(仅在站点要求验证码时用到,平时不加载)。
    返回 (ocr 实例, 错误信息);成功时错误信息为 ""。
    """
    global _OCR, _OCR_ERR
    if _OCR is not None:
        return _OCR, ""
    if _OCR_ERR is not None:
        return None, _OCR_ERR
    try:
        import ddddocr
        _OCR = ddddocr.DdddOcr(old=False, show_ad=False)
        return _OCR, ""
    except ImportError:
        _OCR_ERR = "未安装 ddddocr"
        return None, _OCR_ERR
    except Exception as e:
        _OCR_ERR = f"ddddocr 初始化失败({type(e).__name__})"
        return None, _OCR_ERR


def classify_login_response(text: str, session: requests.Session) -> tuple:
    """
    判定登录结果。返回 (state, detail):
      state = "ok"      -> detail 为 ""
      state = "fail"    -> detail 为失败原因
      state = "captcha" -> detail 为带 auth 的验证码页 URL
    """
    # ⚠️ 必须最先判「需要验证码」:该状态下服务端**也会下发 auth Cookie**,
    #    若用 Cookie 判成功会把「待验证码」误判成登录成功(实测踩过)。
    if any(k in text for k in LOGIN_CAPTCHA_MARKERS):
        m = re.search(r"location\.href='([^']+)'", text)
        return "captcha", html.unescape(m.group(1)) if m else ""

    # 成功:响应含欢迎语,或拿到了 auth Cookie
    if any(k in text for k in LOGIN_OK_MARKERS) or \
            any(c.name.endswith("_auth") and c.value for c in session.cookies):
        return "ok", ""

    if LOGIN_FAIL_MARKER in text:
        m = re.search(r"还可以尝试\s*(\d+)\s*次", text)
        left = m.group(1) if m else "?"
        return "fail", (f"账号或密码错误(该 IP 剩余尝试 {left} 次)。"
                        f"该站登录失败按 IP 计数,请核对密码后再跑,脚本不会自动重试")
    if any(k in text for k in LOGIN_QUESTION_MARKERS):
        return "fail", "该账号设置了安全提问,账号密码模式无法登录,请改用 Cookie 模式"
    if ERR_NEED_LOGIN in text:
        return "fail", "登录态未建立(服务端未下发 auth Cookie)"
    return "fail", f"登录失败: {html_to_text(text)[:120] or '响应为空'}"


def _post_login(session: requests.Session, host: str, username: str, password: str,
                formhash: str, loginhash: str, extra: dict = None,
                referer: str = "") -> requests.Response:
    """提交登录表单(第一步与验证码步骤共用)。"""
    url = (f"https://{host}/member.php?mod=logging&action=login"
           f"&loginsubmit=yes&handlekey=login&loginhash={loginhash}&inajax=1")
    data = {
        "formhash": formhash,
        "referer": f"https://{host}/",
        "username": username,
        "password": password,
        "questionid": "0",   # 无安全提问
        "answer": "",
        "cookietime": "2592000",
    }
    if extra:
        data.update(extra)
    return http_request(session, "POST", url, data=data,
                        headers={"Referer": referer or f"https://{host}{LOGIN_PAGE_URL}"})


def _complete_captcha_login(session: requests.Session, host: str, username: str,
                            password: str, auth_url: str) -> tuple:
    """
    完成「需要验证码」的登录:取验证码图 -> ddddocr 识别 -> 带验证码二次提交。
    返回 (是否成功, 错误信息)。识别错会重新取图重试,最多 CAPTCHA_MAX_TRY 次。
    """
    ocr, ocr_err = _load_ocr()
    if ocr is None:
        return False, (f"站点要求输入登录验证码,但 {ocr_err}。"
                       f"请先在青龙「依赖管理」-「Python」安装 ddddocr,或改用 Cookie 模式")

    for attempt in range(1, CAPTCHA_MAX_TRY + 1):
        page_url = auth_url if auth_url.startswith("http") \
            else f"https://{host}/{auth_url.lstrip('/')}"
        try:
            page = http_request(session, "GET", page_url).text
        except Exception as e:
            return False, f"验证码页获取失败: {type(e).__name__}"

        formhash = extract_formhash(page)
        m_auth = re.search(r'name="auth" value="([^"]+)"', page)
        m_idhash = re.search(r"updateseccode\('([^']+)'", page)
        m_lh = re.search(r"loginhash=([A-Za-z0-9]+)", page)
        if not (formhash and m_auth and m_idhash and m_lh):
            return False, "验证码页结构解析失败(站点可能已改版)"

        idhash = m_idhash.group(1)
        img_url = (f"https://{host}/misc.php?mod=seccode"
                   f"&update={int(time.time())}&idhash={idhash}")
        try:
            img = http_request(session, "GET", img_url, headers={"Referer": page_url})
        except Exception as e:
            return False, f"验证码图片获取失败: {type(e).__name__}"

        code = (ocr.classification(img.content) or "").strip()
        if DEBUG:
            print(f"   🔍 验证码 idhash={idhash} 识别={code!r} ({len(img.content)}B)")
        if not code:
            print(f"   ⚠️ 第 {attempt}/{CAPTCHA_MAX_TRY} 次验证码识别为空,重试...")
            continue

        resp = _post_login(session, host, username, password, formhash, m_lh.group(1),
                           extra={"auth": m_auth.group(1), "seccodehash": idhash,
                                  "seccodemodid": "member::logging",
                                  "seccodeverify": code},
                           referer=page_url)
        state, detail = classify_login_response(resp.text, session)
        if state == "ok":
            return True, ""
        if state == "captcha":
            # 验证码识别错了,服务端给了新的 auth,继续重试
            print(f"   ⚠️ 第 {attempt}/{CAPTCHA_MAX_TRY} 次验证码未通过,重试...")
            if detail:
                auth_url = detail
            rand_sleep(1, 2)
            continue
        return False, detail

    return False, (f"验证码连续 {CAPTCHA_MAX_TRY} 次未通过(OCR 识别率不足),"
                   f"建议改用 Cookie 模式")


def login_with_password(host: str, username: str, password: str) -> tuple:
    """
    Discuz 账号密码登录。返回 (session, 错误信息);成功时错误信息为 ""。
    认证失败不重试(只有网络层错误由 http_request 内部重试)。
    """
    session = new_session(host)
    login_url = f"https://{host}{LOGIN_PAGE_URL}"

    try:
        resp = http_request(session, "GET", login_url)
    except Exception as e:
        return None, f"登录页获取失败: {type(e).__name__}"

    formhash = extract_formhash(resp.text)
    m = re.search(r"loginhash=([A-Za-z0-9]+)", resp.text)
    loginhash = m.group(1) if m else ""
    if not formhash or not loginhash:
        return None, "登录页未取到 formhash/loginhash(页面结构可能已变化)"

    try:
        resp2 = _post_login(session, host, username, password, formhash, loginhash,
                            referer=resp.url)
    except Exception as e:
        return None, f"登录请求失败: {type(e).__name__}"

    if DEBUG:
        # 登录响应是 JS 片段(errorhandle_login(...)),不能用 html_to_text 剥标签
        print(f"   🔍 登录响应: {' '.join(resp2.text.split())[:160]}")

    state, detail = classify_login_response(resp2.text, session)
    if state == "ok":
        return session, ""
    if state == "captcha":
        print("   🧩 站点要求输入验证码,尝试 ddddocr 自动识别...")
        ok, err = _complete_captcha_login(session, host, username, password, detail)
        return (session, "") if ok else (None, err)
    return None, detail


# ============================ 签到核心 ============================

def _parse_done_code(location: str) -> tuple:
    """从 302 Location 解析 (done 码, 连续天数),解析不到返回 (None, None)。"""
    done = re.search(r"[?&]done=(-?\d+)", location)
    days = re.search(r"[?&]d=(\d+)", location)
    return (int(done.group(1)) if done else None,
            int(days.group(1)) if days else None)


def do_checkin(session: requests.Session, host: str, page_html: str) -> dict:
    """
    执行签到。page_html 为已获取的签到页(避免重复请求)。
    返回 {success, duplicate, cookie_invalid, message, stats}
    """
    plugin_url = f"https://{host}/plugin.php?id=fuliba_prereg:checkin"
    action_url = plugin_url + "&ac=do"

    # 幂等:页面已含「今日已签到」按钮特征则直接跳过 POST
    if ALREADY_SIGNED_MARK in page_html:
        return {"success": True, "duplicate": True, "cookie_invalid": False,
                "message": "今日已签到", "stats": parse_stats(page_html)}

    formhash = extract_formhash(page_html)
    if not formhash:
        return {"success": False, "duplicate": False, "cookie_invalid": False,
                "message": "签到页未找到 formhash(页面结构可能已变化)", "stats": ""}

    # POST 不自动跟随重定向,改从 Location 读结果码
    for attempt in (1, 2):
        resp = http_request(
            session, "POST", action_url,
            headers={"Referer": plugin_url,
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"formhash": formhash},
            allow_redirects=False,
        )

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            done, days = _parse_done_code(location)
            if DEBUG:
                print(f"   🔍 302 Location: {location}")

            # 跟随到结果页取提示文案与最新进度
            result_html = ""
            try:
                followed = http_request(session, "GET", location or plugin_url,
                                        allow_redirects=True)
                result_html = followed.text
            except Exception as e:
                if DEBUG:
                    print(f"   🔍 结果页获取失败: {e}")

            toast = extract_toast(result_html)
            stats = parse_stats(result_html) or parse_stats(page_html)

            if done == DONE_SUCCESS:
                return {"success": True, "duplicate": False, "cookie_invalid": False,
                        "message": toast or (f"签到成功,已连续 {days} 天" if days else "签到成功"),
                        "stats": stats}
            if done == DONE_ALREADY:
                return {"success": True, "duplicate": True, "cookie_invalid": False,
                        "message": toast or "今日已签到", "stats": stats}
            return {"success": False, "duplicate": False, "cookie_invalid": False,
                    "message": toast or f"签到返回未知状态码 done={done}", "stats": stats}

        # 非 302:Discuz 提示页
        text = html_to_text(resp.text)
        if ERR_NEED_LOGIN in text:
            return {"success": False, "duplicate": False, "cookie_invalid": True,
                    "message": "Cookie 已失效,请重新抓取", "stats": ""}
        if ERR_BAD_FORMHASH in text and attempt == 1:
            # formhash 过期:重新取页面后重试一次
            print("   🔄 formhash 已过期,重新获取页面后重试...")
            rand_sleep(1, 2)
            fresh = http_request(session, "GET", plugin_url)
            if is_guest_page(fresh.text):
                return {"success": False, "duplicate": False, "cookie_invalid": True,
                        "message": "Cookie 已失效,请重新抓取", "stats": ""}
            formhash = extract_formhash(fresh.text)
            if not formhash:
                return {"success": False, "duplicate": False, "cookie_invalid": False,
                        "message": "重试时仍无法提取 formhash", "stats": ""}
            continue
        msg = extract_discuz_message(resp.text) or text[:120] or f"HTTP {resp.status_code}"
        if ERR_BAD_FORMHASH in text:
            msg = f"formhash 被拒绝,重试后仍失败(签到页结构可能已变化): {msg}"
        return {"success": False, "duplicate": False, "cookie_invalid": False,
                "message": f"签到失败: {msg}", "stats": ""}

    return {"success": False, "duplicate": False, "cookie_invalid": False,
            "message": "签到失败: formhash 重试后仍被拒绝", "stats": ""}


# ============================ 单账号流程 ============================

def _checkin_with_session(session: requests.Session, host: str) -> dict:
    """已建会话的签到流程。游客态直接标记 cookie_invalid。"""
    plugin_url = f"https://{host}/plugin.php?id=fuliba_prereg:checkin"
    resp = http_request(session, "GET", plugin_url)
    if is_guest_page(resp.text):
        return {"success": False, "duplicate": False, "cookie_invalid": True,
                "message": "Cookie 已失效,请重新抓取", "stats": ""}
    return do_checkin(session, host, resp.text)


def _fill_result(result: dict, sign: dict):
    """把签到结果写回 result 并打印。"""
    result["cookie_invalid"] = sign.get("cookie_invalid", False)
    result["duplicate"] = sign.get("duplicate", False)
    result["stats"] = sign.get("stats", "")
    result["success"] = sign.get("success", False)
    if result["success"]:
        tag = "🔁" if sign.get("duplicate") else "✅"
        result["message"] = sign.get("message", "签到成功")
        print(f"   {tag} {result['message']}")
        if result["stats"]:
            print(f"   📊 {result['stats']}")
    else:
        result["message"] = sign.get("message", "签到失败")
        print(f"   ❌ {result['message']}")


def run_one_account(cred: dict, index: int, host: str) -> dict:
    label = f"账号{index + 1}"
    result = {
        "label": label,
        "success": False,
        "duplicate": False,
        "cookie_invalid": False,
        "auth_failed": False,
        "message": "",
        "stats": "",
    }

    # ---- Cookie 模式 ----
    if cred["kind"] == "cookie":
        cookie_str = cred["payload"]
        print(f"\n======== {label} [Cookie] ({mask_cookie(cookie_str)}) ========")
        try:
            sign = _checkin_with_session(new_session(host, cookie_str), host)
        except Exception as e:
            result["message"] = f"网络异常: {type(e).__name__}: {e}"[:100]
            print(f"   ❌ {result['message']}")
            return result
        _fill_result(result, sign)
        return result

    # ---- 账号密码模式 ----
    username, password = cred["payload"]
    print(f"\n======== {label} [账密] ({mask(username)}) ========")

    # 1. 优先复用 Cookie 缓存:命中就完全不走登录,规避 IP 级失败计数
    cached = read_cookie_cache().get(username, {}).get("cookies", "")
    if cached:
        try:
            sign = _checkin_with_session(new_session(host, cached), host)
        except Exception as e:
            sign = {"success": False, "cookie_invalid": False,
                    "message": f"网络异常: {type(e).__name__}", "stats": ""}
        if not sign.get("cookie_invalid"):
            print("   ♻️ 复用缓存 Cookie")
            _fill_result(result, sign)
            return result
        print("   ♻️ 缓存 Cookie 已失效,重新登录...")

    # 2. 登录(认证失败不重试,只有网络错误在 http_request 内部重试)
    rand_sleep(1, 2)
    session, err = login_with_password(host, username, password)
    if err:
        result["message"] = err
        result["auth_failed"] = True
        print(f"   ❌ {err}")
        return result
    print("   🔑 登录成功")
    write_cookie_cache(username, session_cookies_to_str(session))

    # 3. 签到
    try:
        sign = _checkin_with_session(session, host)
    except Exception as e:
        result["message"] = f"网络异常: {type(e).__name__}: {e}"[:100]
        print(f"   ❌ {result['message']}")
        return result
    _fill_result(result, sign)
    return result


def collect_tasks() -> list:
    """解析 WNFLB_COOKIE / WNFLB_ACCOUNTS 两个环境变量,返回任务列表。"""
    tasks = []

    for line in os.getenv("WNFLB_COOKIE", "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        kind, payload = classify_credential(line)
        if kind != "cookie":
            print(f"⚠️ WNFLB_COOKIE 中该行不像 Cookie(缺 ';' 或 '='),已跳过: {line[:20]}...")
            continue
        tasks.append({"kind": "cookie", "payload": payload})

    for line in os.getenv("WNFLB_ACCOUNTS", "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        kind, payload = classify_credential(line)
        if kind != "account":
            print(f"⚠️ WNFLB_ACCOUNTS 中该行格式应为「用户名#密码」,已跳过: {line[:20]}...")
            continue
        user, pwd = payload
        if not user or not pwd:
            print(f"⚠️ WNFLB_ACCOUNTS 中该行用户名或密码为空,已跳过: {line[:20]}...")
            continue
        tasks.append({"kind": "account", "payload": payload})

    return tasks


# ============================ 主流程 ============================

def main():
    global DEBUG, TIMEOUT
    DEBUG = env_bool("WNFLB_DEBUG", False)
    TIMEOUT = env_int("WNFLB_TIMEOUT", 25)

    notify_enabled = env_bool("WNFLB_NOTIFY", True)
    notify_only_fail = env_bool("WNFLB_NOTIFY_ONLY_FAIL", False)

    host = (os.getenv("WNFLB_HOST") or DEFAULT_HOST).strip().rstrip("/")
    host = re.sub(r"^https?://", "", host)

    tasks = collect_tasks()
    if not tasks:
        print("❌ 未配置任何凭证。请设置 WNFLB_COOKIE 或 WNFLB_ACCOUNTS 环境变量")
        sys.exit(1)

    print("=" * 60)
    print(f"🚀 福利吧预注册签到  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"🌐 站点: {host}   账号数: {len(tasks)}")
    print("=" * 60)

    results = []
    for i, cred in enumerate(tasks):
        try:
            r = run_one_account(cred, i, host)
        except Exception:
            traceback.print_exc()
            r = {"label": f"账号{i + 1}", "success": False, "duplicate": False,
                 "cookie_invalid": False, "auth_failed": False, "stats": "",
                 "message": traceback.format_exc().splitlines()[-1][:100]}
            print(f"   ❌ {r['message']}")
        results.append(r)
        # 多账号间隔(风控)
        if i < len(tasks) - 1:
            rand_sleep(2, 4)

    total = len(results)
    success_cnt = sum(1 for r in results if r["success"])
    invalid_cnt = sum(1 for r in results if r["cookie_invalid"])

    if success_cnt == total:
        title = f"✅ 福利吧签到 全部成功({success_cnt}/{total})"
    elif invalid_cnt and invalid_cnt == total:
        title = f"❌ 福利吧签到 Cookie 全部失效({invalid_cnt}/{total})"
    elif success_cnt == 0:
        title = f"❌ 福利吧签到 全部失败(0/{total})"
    else:
        title = f"⚠️ 福利吧签到 部分失败({success_cnt}/{total})"

    lines = [f"# {title}", ""]
    lines.append(f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"📊 总计 {total} 个账号,✅ 成功 {success_cnt},❌ 失败 {total - success_cnt}")
    lines.append("")

    ok_lines = []
    fail_lines = []
    for r in results:
        line = f"- **[{r['label']}]** {r['message']}"
        if r["stats"]:
            line += f"\n  - 📊 {r['stats']}"
        if r["cookie_invalid"]:
            line += "\n  - 💡 请浏览器登录后 F12 重新抓取 Cookie 更新 `WNFLB_COOKIE`"
        if r.get("auth_failed"):
            line += "\n  - 💡 请核对 `WNFLB_ACCOUNTS` 的密码(该站登录失败按 IP 计数,勿反复试)"
        (ok_lines if r["success"] else fail_lines).append(line)

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

    if notify_enabled:
        if notify_only_fail and success_cnt == total:
            print("ℹ️ WNFLB_NOTIFY_ONLY_FAIL=true 且全部成功,跳过推送")
        else:
            send_notify(title, report)
    else:
        print("ℹ️ WNFLB_NOTIFY=false,已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
