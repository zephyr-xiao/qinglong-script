# -*- coding: utf-8 -*-
"""
new Env('福利吧每日签到');
cron: 20 8 * * *

福利吧论坛(wnflb2023.com)「每日签到」插件(fx_checkin)自动签到。

接口流程(2026-10-19 实测):
  1. GET  plugin.php?id=fx_checkin:list&type=0
     已登录页面里 JS 变量 fx_chk_menu=true 且顶栏图片 alt="已签到",
     并出现「您今日第 N 个签到,已连续签到 X 天,累计签到 Y 天!」
     游客页面 discuz_uid = '0',据此判定 Cookie 失效
  2. GET  plugin.php?id=fx_checkin:checkin&formhash=<hash>&<hash>&inajax=1
     签到入口由页面 JS 给出(showWindow 打开),必须带 formhash。
     重复签到返回「您今日已签到，无需重复签到！」(幂等)。
     成功文案以站点实际返回为准,脚本额外用「签到后页面是否变为已签到」做正向确认,
     不单凭关键词冒报成功。

认证方式(二选一,也可同时配置,各自独立跑):
  A. 账号密码 :WNFX_ACCOUNTS,格式 用户名#密码,多账号换行分隔
  B. Cookie   :WNFX_COOKIE,浏览器抓的完整 Cookie,多账号换行分隔
     账号密码模式登录成功后会缓存 Cookie 到脚本同目录 wnflb_fx_cookie.json,
     之后优先复用,缓存失效才重新登录。

     ⚠️ 该站登录失败次数按 IP 统计,连续失败 5 次后要求图片验证码。
        因此认证失败绝不自动重试(只有网络错误才重试);遇到验证码时用 ddddocr
        本地识别(识别错会重新取图,最多 3 次)。

环境变量:
  WNFX_ACCOUNTS           可选,账号密码「用户名#密码」,多账号换行分隔
  WNFX_COOKIE             可选,完整 Cookie 字符串,多账号换行分隔
  WNFX_HOST               可选,站点域名,默认 www.wnflb2023.com
  WNFX_PROXY              可选,HTTP 代理,如 http://172.17.0.1:7890
  WNFX_TIMEOUT            可选,HTTP 超时秒数,默认 25
  WNFX_SIGN_RETRY         可选,业务失败重试次数,默认 2(0 表示不重试)
  WNFX_SIGN_RETRY_DELAY   可选,每次重试前等待秒数,默认 5
  WNFX_NOTIFY             true/false 默认 true,是否调用青龙 notify.py 推送
  WNFX_NOTIFY_ONLY_FAIL   true/false 默认 false,全部成功时静默
  WNFX_DEBUG              true/false 默认 false,输出接口响应细节用于排错

依赖:requests
      可选:ddddocr —— 仅在站点要求登录验证码时才需要(平时不加载)。

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
from urllib.parse import urlsplit

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

LIST_PATH = "/plugin.php?id=fx_checkin:list&type=0"
CHECKIN_PATH = "/plugin.php?id=fx_checkin:checkin"

# 游客特征:Discuz 会在页面 JS 里输出 discuz_uid = '0'
GUEST_PATTERNS = (re.compile(r"discuz_uid\s*=\s*['\"]0['\"]"),)
GUEST_MARKERS = ("抱歉，您尚未登录",)

# 已签到特征(按可靠性排序)。fx_chk_menu=true 是页面 JS 自己用的开关,
# 为 true 时点击顶栏图标不会再弹签到窗口,是站点给出的权威状态。
# 「您今日第 N 个签到」用正则限定,避免排行榜/模板占位符误命中。
SIGNED_PATTERNS = (
    re.compile(r"var\s+fx_chk_menu\s*=\s*true"),
    re.compile(r"""alt=["']已签到["']"""),
)
SIGNED_STAT_RE = re.compile(r"您今日第\s*\d+\s*个签到")

# 签到结果文案(站点实测与同类插件通用文案)
ALREADY_MARKERS = ("无需重复签到", "今日已签到", "已经签到", "今日已签")
SUCCESS_MARKERS = ("签到成功", "签到完成", "恭喜签到", "签到获得")
LOGIN_MARKERS = ("尚未登录", "请先登录", "请登录后", "需要登录")
FORMHASH_MARKERS = ("表单验证串不符", "请求来路不正确", "formhash")

# ---- 账号密码登录(Discuz 标准流程) ----
LOGIN_PAGE_URL = (
    "/member.php?mod=logging&action=login&infloat=yes"
    "&handlekey=login&inajax=1&ajaxtarget=fwin_content_login"
)
LOGIN_OK_MARKERS = ("欢迎您回来", "登录成功", "现在将转入登录前页面")
LOGIN_FAIL_MARKER = "登录失败"
LOGIN_CAPTCHA_MARKERS = ("验证码", "seccode", "captcha")
LOGIN_QUESTION_MARKERS = ("安全提问", "请选择安全提问")
CAPTCHA_MAX_TRY = 3

# Cookie 缓存(脚本同目录;.gitignore 已忽略 *_cookie.json)
COOKIE_CACHE_FILE = Path(__file__).resolve().parent / "wnflb_fx_cookie.json"
# 运行锁,防止 cron 重叠执行
LOCK_FILE = Path(__file__).resolve().parent / "wnflb_fx_checkin.lock"
LOCK_STALE_SECONDS = 2 * 3600

MAX_RETRIES = 3
RETRY_BASE_DELAY = 5

# 业务失败重试(签到接口正常返回但页面仍未变为已签到)
SIGN_RETRY = 2          # 额外重试次数,0 表示不重试
SIGN_RETRY_DELAY = 5    # 每次重试前等待秒数(带 ±20% 抖动)

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
    """用户名脱敏:长度 >=3 时保留首尾各 1 位,中间打码。"""
    if not s:
        return ""
    if len(s) <= 2:
        return s[0] + "*"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def mask_proxy(proxy: str) -> str:
    """代理地址脱敏:去掉 user:pass@ 部分,避免把代理密码写进日志。"""
    try:
        parts = urlsplit(proxy)
        if parts.username or parts.password:
            netloc = parts.hostname or ""
            if parts.port:
                netloc += f":{parts.port}"
            return f"{parts.scheme}://***@{netloc}"
        return proxy
    except Exception:
        return "<代理地址>"


def _is_network_error(e: Exception) -> bool:
    """
    只有网络层错误值得重试;MissingSchema/TooManyRedirects 这类确定性错误
    重试只是白等,应立即抛出。SSLError 继承 ConnectionError,天然覆盖。
    """
    if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                      requests.exceptions.ChunkedEncodingError)):
        return True
    msg = str(e).lower()
    keywords = ("timeout", "connection", "ssl", "proxy", "max retries", "eof")
    return any(k in msg for k in keywords)


def rand_sleep(min_s: float, max_s: float):
    """随机休眠,用于请求间隔降低风控特征。"""
    time.sleep(random.uniform(min_s, max_s))


def http_request(session: requests.Session, method: str, url: str,
                 retry: bool = True, **kwargs) -> requests.Response:
    """
    带重试的请求。只对网络类异常重试;
    SSLError 首次出现时把会话降级为不校验证书再试(边缘节点证书异常的兜底)。

    retry=False 用于登录提交:该站登录失败按 IP 计数,请求超时/断连时服务端
    可能已经处理过,盲目重试会白扣失败次数。
    """
    last_exc = None
    attempts = MAX_RETRIES if retry else 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.request(method, url, timeout=TIMEOUT, **kwargs)
            if DEBUG:
                print(f"   🔍 {method} {url[:88]} → {resp.status_code} ({len(resp.content)}B)")
            return resp
        except Exception as e:
            if not _is_network_error(e):
                raise
            if isinstance(e, requests.exceptions.SSLError) and session.verify:
                session.verify = False
                print("   ⚠️ TLS 证书校验失败,已自动降级为不校验继续(如非预期请核查站点证书)")
            last_exc = e
            if attempt < attempts:
                delay = RETRY_BASE_DELAY * attempt
                print(f"   ⚠️ 请求失败({type(e).__name__}),{delay}s 后重试 {attempt}/{attempts - 1}")
                time.sleep(delay)
    raise last_exc


def html_to_text(page: str) -> str:
    """粗略提取页面可见文本,用于提示文案与排错输出。"""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


# ============================ 页面解析 ============================

def is_guest_page(page: str) -> bool:
    """游客页面:Discuz 输出 discuz_uid = '0',或直接出现未登录提示。"""
    if any(p.search(page) for p in GUEST_PATTERNS):
        return True
    return any(marker in page for marker in GUEST_MARKERS)


def is_signed_page(page: str) -> bool:
    """
    判断页面是否处于「今日已签到」状态。
    只认站点自己的状态开关与明确文案,不认站点全局模板里的「签到成功!」
    (那段模板在游客页也会输出占位符,不可作为判据)。
    """
    if any(p.search(page) for p in SIGNED_PATTERNS):
        return True
    return bool(SIGNED_STAT_RE.search(page))


def extract_formhash(page: str) -> str:
    """提取 formhash。Discuz 页面里是 <input type="hidden" name="formhash" value="xxx">。"""
    for pattern in (
        r'name=["\']formhash["\']\s+value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\']\s+name=["\']formhash["\']',
        r"formhash=([A-Za-z0-9]{6,})",
    ):
        m = re.search(pattern, page, re.I)
        if m:
            return m.group(1)
    return ""


def extract_discuz_message(page: str) -> str:
    """提取 Discuz 通用提示页(id=messagetext)的文案。"""
    m = re.search(r'id="messagetext"[^>]*>\s*<p>(.*?)</p>', page, re.S)
    if not m:
        m = re.search(r'<div class="alert_(?:error|info|right)"[^>]*>(.*?)</div>', page, re.S)
    if m:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    return ""


def extract_ajax_message(page: str) -> str:
    """
    提取 Discuz inajax 响应(root/CDATA)里的提示文案。
    形如: <root><![CDATA[提示信息 关闭 您今日已签到，无需重复签到！ 确定 ]]></root>
    """
    m = re.search(r"<!\[CDATA\[(.*?)\]\]>", page, re.S)
    if not m:
        return ""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", m.group(1), flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:提示信息|提示|消息)\s*(?:关闭)?\s*", "", text)
    text = re.sub(r"\s*(?:确定|关闭)\s*$", "", text)
    return text.strip()


def extract_message(page: str) -> str:
    """从签到响应里取提示文案,兼容 inajax 与普通提示页。"""
    return (extract_ajax_message(page)
            or extract_discuz_message(page)
            or html_to_text(page)[:160])


def parse_stats(page: str) -> str:
    """
    解析签到进度。
    已签到页面会输出:
      您今日第1008个签到,已连续签到1天,累计签到1天!
      已连续签到:<i>1</i>天，累计签到:<i>1</i>天
    """
    parts = []
    m = re.search(r"您今日第\s*(\d+)\s*个签到[，,]\s*已连续签到\s*(\d+)\s*天[，,]\s*"
                  r"累计签到\s*(\d+)\s*天", page)
    if m:
        return f"今日第 {m.group(1)} 名 │ 连续 {m.group(2)} 天 │ 累计 {m.group(3)} 天"

    m = re.search(r"今日第\s*(\d+)\s*个签到", page)
    if m:
        parts.append(f"今日第 {m.group(1)} 名")
    m = re.search(r"已连续签到[：:]?\s*(?:<i>)?\s*(\d+)\s*(?:</i>)?\s*天", page)
    if m:
        parts.append(f"连续 {m.group(1)} 天")
    m = re.search(r"累计签到[：:]?\s*(?:<i>)?\s*(\d+)\s*(?:</i>)?\s*天", page)
    if m:
        parts.append(f"累计 {m.group(1)} 天")
    return " │ ".join(parts)


# ============================ 会话构建 ============================

def new_session(host: str, cookie_str: str = "") -> requests.Session:
    """构建会话。cookie_str 为空时用于账号密码登录。"""
    session = requests.Session()
    # trust_env 必须关掉:Windows 上 requests 会静默采用注册表系统代理,
    # 击穿下面这套优先级(实测系统代理会把该站路由到超时,而直连正常)。
    session.trust_env = False
    session.verify = True
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://{host}/",
    })
    if cookie_str:
        session.cookies.update(parse_cookies(cookie_str))
    proxy, proxy_src = resolve_proxy("WNFX_PROXY")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        print(f"🌐 使用代理: {mask_proxy(proxy)}(来源: {proxy_src})")
    return session


def session_cookies_to_str(session: requests.Session) -> str:
    """把会话里的 Cookie 导出成浏览器格式字符串(用于写缓存)。"""
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


# ============================ 凭证解析与 Cookie 缓存 ============================

def classify_credential(raw: str) -> tuple:
    """
    判定单条凭证类型,返回 (kind, payload)。
      - 含 '#'          -> ("account", (用户名, 密码))
      - 含 '='          -> ("cookie", cookie字符串,允许只有一个键值对)
      - 其他            -> ("unknown", raw)
    先判 '#' 是为了让密码里出现 ';' / '=' 时仍能按账号密码解析。
    """
    raw = raw.strip()
    if "#" in raw:
        user, pwd = raw.split("#", 1)
        return "account", (user.strip(), pwd)
    if "=" in raw:
        return "cookie", raw
    return "unknown", raw


def read_cookie_cache(host: str = "") -> dict:
    """
    读取 {用户名: {cookies, update_time, host}};文件缺失/损坏返回 {}。
    结构损坏的条目直接跳过(合法 JSON 但值不是 dict 时不能让脚本崩)。
    指定 host 时忽略其他站点的缓存,避免换镜像域名后把旧 Cookie 发到新域名。
    """
    if not COOKIE_CACHE_FILE.exists():
        return {}
    try:
        with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f).get("accounts", {}) or {}
    except Exception as e:
        print(f"⚠️ Cookie 缓存读取失败,忽略: {e}")
        return {}
    if not isinstance(accounts, dict):
        print("⚠️ Cookie 缓存结构异常(accounts 不是对象),已忽略")
        return {}
    valid = {}
    for user, entry in accounts.items():
        if not isinstance(entry, dict) or not entry.get("cookies"):
            continue
        cached_host = entry.get("host")
        if host and cached_host and cached_host != host:
            continue
        valid[user] = entry
    return valid


def write_cookie_cache(username: str, cookie_str: str, host: str = ""):
    """
    写入指定用户的 Cookie 缓存(只存凭证,绝不存密码)。
    走 .part 临时文件 + os.replace 原子替换;临时文件先收 0600。
    """
    data = {}
    if COOKIE_CACHE_FILE.exists():
        try:
            with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        data["accounts"] = accounts
    accounts[username] = {
        "cookies": cookie_str,
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": host,
    }
    tmp = COOKIE_CACHE_FILE.with_name(COOKIE_CACHE_FILE.name + ".part")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, COOKIE_CACHE_FILE)
    except Exception as e:
        print(f"⚠️ Cookie 缓存写入失败(不影响签到): {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ============================ 账号密码登录 ============================

_OCR = None
_OCR_ERR = None


def _load_ocr():
    """惰性加载 ddddocr(仅站点要求验证码时用到)。返回 (ocr, 错误信息)。"""
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
    # ⚠️ 必须最先判「需要验证码」:该状态下服务端也会下发 auth Cookie,
    #    若用 Cookie 判成功会把「待验证码」误判成登录成功。
    if any(k in text for k in LOGIN_CAPTCHA_MARKERS):
        m = re.search(r"location\.href='([^']+)'", text)
        return "captcha", html.unescape(m.group(1)) if m else ""

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
    # 登录提交不重试:失败次数按 IP 统计,超时/断连时服务端可能已计入失败
    return http_request(session, "POST", url, data=data, retry=False,
                        headers={"Referer": referer or f"https://{host}{LOGIN_PAGE_URL}"})


def _complete_captcha_login(session: requests.Session, host: str, username: str,
                            password: str, auth_url: str) -> tuple:
    """完成「需要验证码」的登录:取图 -> ddddocr -> 带验证码二次提交。"""
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
            print(f"   ⚠️ 第 {attempt}/{CAPTCHA_MAX_TRY} 次验证码未通过,重试...")
            if detail:
                auth_url = detail
            rand_sleep(1, 2)
            continue
        return False, detail

    return False, (f"验证码连续 {CAPTCHA_MAX_TRY} 次未通过(OCR 识别率不足),"
                   f"建议改用 Cookie 模式")


def login_with_password(host: str, username: str, password: str) -> tuple:
    """Discuz 账号密码登录。返回 (session, 错误信息);成功时错误信息为 ""。"""
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

def _classify_sign_message(msg: str) -> str:
    """把签到提示文案归类为 already / success / login / formhash / unknown。"""
    if any(k in msg for k in ALREADY_MARKERS):
        return "already"
    if any(k in msg for k in SUCCESS_MARKERS):
        return "success"
    if any(k in msg for k in LOGIN_MARKERS):
        return "login"
    if any(k in msg for k in FORMHASH_MARKERS):
        return "formhash"
    return "unknown"


def _fetch_list_page(session: requests.Session, host: str) -> str:
    resp = http_request(session, "GET", f"https://{host}{LIST_PATH}")
    return resp.text


def _sign_once(session: requests.Session, host: str, page_html: str) -> dict:
    """
    单次签到尝试(内部含 formhash 过期重取一次)。
    返回结果字典;retryable=True 表示属于「业务失败」,外层可安排重试。
    """
    if is_guest_page(page_html):
        return {"success": False, "duplicate": False, "cookie_invalid": True,
                "message": "Cookie 已失效,请重新抓取", "stats": ""}

    # 幂等:页面已是「今日已签到」状态则直接跳过,不再打签到接口
    if is_signed_page(page_html):
        return {"success": True, "duplicate": True, "cookie_invalid": False,
                "message": "今日已签到", "stats": parse_stats(page_html)}

    formhash = extract_formhash(page_html)
    if not formhash:
        return {"success": False, "duplicate": False, "cookie_invalid": False,
                "message": "签到页未找到 formhash(页面结构可能已变化)", "stats": ""}

    list_url = f"https://{host}{LIST_PATH}"

    for attempt in (1, 2):
        sign_url = (f"https://{host}{CHECKIN_PATH}"
                    f"&formhash={formhash}&{formhash}&inajax=1")
        resp = http_request(session, "GET", sign_url,
                            headers={"Referer": list_url, "X-Requested-With": "XMLHttpRequest"})
        msg = extract_message(resp.text)
        kind = _classify_sign_message(msg)
        if DEBUG:
            print(f"   🔍 签到响应: {msg[:160]}")

        if kind == "already":
            # 站点确认今天已签过,拉一次列表页补齐统计信息
            stats = ""
            try:
                stats = parse_stats(_fetch_list_page(session, host))
            except Exception:
                pass
            return {"success": True, "duplicate": True, "cookie_invalid": False,
                    "message": msg or "今日已签到", "stats": stats}

        if kind == "login":
            return {"success": False, "duplicate": False, "cookie_invalid": True,
                    "message": "Cookie 已失效,请重新抓取", "stats": ""}

        if kind == "formhash" and attempt == 1:
            # formhash 过期:重新取页面后重试一次
            print("   🔄 formhash 已过期,重新获取页面后重试...")
            rand_sleep(1, 2)
            fresh = _fetch_list_page(session, host)
            if is_guest_page(fresh):
                return {"success": False, "duplicate": False, "cookie_invalid": True,
                        "message": "Cookie 已失效,请重新抓取", "stats": ""}
            if is_signed_page(fresh):
                return {"success": True, "duplicate": True, "cookie_invalid": False,
                        "message": "今日已签到", "stats": parse_stats(fresh)}
            formhash = extract_formhash(fresh)
            if not formhash:
                return {"success": False, "duplicate": False, "cookie_invalid": False,
                        "message": "重试时仍无法提取 formhash", "stats": ""}
            continue

        # 关键:不单凭文案冒报成功。以「签到后页面是否变为已签到」作为正向证据。
        try:
            after = _fetch_list_page(session, host)
        except Exception as e:
            return {"success": False, "duplicate": False, "cookie_invalid": False,
                    "message": f"签到结果无法确认(结果页获取失败: {type(e).__name__})",
                    "stats": "", "retryable": True}

        if is_guest_page(after):
            return {"success": False, "duplicate": False, "cookie_invalid": True,
                    "message": "Cookie 已失效,请重新抓取", "stats": ""}

        if is_signed_page(after):
            return {"success": True, "duplicate": False, "cookie_invalid": False,
                    "message": msg or "签到成功", "stats": parse_stats(after)}

        return {"success": False, "duplicate": False, "cookie_invalid": False,
                "message": f"签到失败: {msg or '页面未显示已签到状态'}",
                "stats": parse_stats(after), "retryable": True}

    return {"success": False, "duplicate": False, "cookie_invalid": False,
            "message": "签到失败: formhash 重试后仍被拒绝", "stats": ""}


def do_checkin(session: requests.Session, host: str, page_html: str) -> dict:
    """
    执行签到,业务失败时按 SIGN_RETRY 自动重试。
    page_html 为已获取的签到列表页(避免重复请求)。
    返回 {success, duplicate, cookie_invalid, message, stats}
    """
    for attempt in range(0, SIGN_RETRY + 1):
        result = _sign_once(session, host, page_html)
        retryable = result.pop("retryable", False)

        if not retryable or attempt >= SIGN_RETRY:
            return result

        print(f"   ⚠️ {result['message']};{SIGN_RETRY_DELAY}s 后重试"
              f"({attempt + 1}/{SIGN_RETRY})")
        # 抖动等待,避免多个账号/多次重试形成固定节奏
        rand_sleep(SIGN_RETRY_DELAY * 0.8, SIGN_RETRY_DELAY * 1.2)

        try:
            page_html = _fetch_list_page(session, host)
        except Exception as e:
            return {"success": False, "duplicate": False, "cookie_invalid": False,
                    "message": (f"签到失败(重试前刷新页面失败: {type(e).__name__});"
                                f"上一次结果: {result['message']}"),
                    "stats": result.get("stats", "")}

        if is_guest_page(page_html):
            return {"success": False, "duplicate": False, "cookie_invalid": True,
                    "message": "Cookie 已失效,请重新抓取", "stats": ""}
        if is_signed_page(page_html):
            # 上一次尝试可能已实际签到成功,只是当时回查没看到
            return {"success": True, "duplicate": False, "cookie_invalid": False,
                    "message": "签到成功(重试前页面已显示已签到)",
                    "stats": parse_stats(page_html)}

    return {"success": False, "duplicate": False, "cookie_invalid": False,
            "message": "签到失败: 重试次数已用尽", "stats": ""}


# ============================ 单账号流程 ============================

def _checkin_with_session(session: requests.Session, host: str) -> dict:
    """已建会话的签到流程。"""
    page = _fetch_list_page(session, host)
    return do_checkin(session, host, page)


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
    cached = read_cookie_cache(host).get(username, {}).get("cookies", "")
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
    write_cookie_cache(username, session_cookies_to_str(session), host)

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
    """解析 WNFX_ACCOUNTS / WNFX_COOKIE 两个环境变量,返回任务列表。"""
    tasks = []

    for lineno, line in enumerate(os.getenv("WNFX_ACCOUNTS", "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        kind, payload = classify_credential(line)
        if kind != "account":
            print(f"⚠️ WNFX_ACCOUNTS 第 {lineno} 行格式应为「用户名#密码」,已跳过")
            continue
        user, pwd = payload
        if not user or not pwd:
            print(f"⚠️ WNFX_ACCOUNTS 第 {lineno} 行用户名或密码为空,已跳过")
            continue
        tasks.append({"kind": "account", "payload": payload})

    for lineno, line in enumerate(os.getenv("WNFX_COOKIE", "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        kind, payload = classify_credential(line)
        if kind != "cookie":
            print(f"⚠️ WNFX_COOKIE 第 {lineno} 行不像 Cookie(缺 '='),已跳过")
            continue
        tasks.append({"kind": "cookie", "payload": payload})

    return tasks


# ============================ 运行锁 ============================

def acquire_lock() -> bool:
    """
    创建运行锁。进程崩溃遗留的锁超过 LOCK_STALE_SECONDS 视为过期。
    返回 False 表示已有实例在跑。
    """
    try:
        if LOCK_FILE.exists():
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
                print(f"⚠️ 检测到运行锁({int(age)}s 前创建),疑似已有实例在跑,本次退出")
                return False
            print(f"⚠️ 发现过期运行锁({int(age)}s 前),已清理")
            LOCK_FILE.unlink(missing_ok=True)
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n{datetime.now():%Y-%m-%d %H:%M:%S}\n")
        return True
    except FileExistsError:
        print("⚠️ 检测到运行锁,疑似已有实例在跑,本次退出")
        return False
    except Exception as e:
        # 锁只是防重叠,创建失败不应阻断签到
        print(f"⚠️ 运行锁创建失败(继续执行): {e}")
        return True


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ============================ 主流程 ============================

def main():
    global DEBUG, TIMEOUT, SIGN_RETRY, SIGN_RETRY_DELAY
    DEBUG = env_bool("WNFX_DEBUG", False)
    TIMEOUT = max(1, env_int("WNFX_TIMEOUT", 25))
    SIGN_RETRY = max(0, env_int("WNFX_SIGN_RETRY", 2))
    SIGN_RETRY_DELAY = max(1, env_int("WNFX_SIGN_RETRY_DELAY", 5))

    notify_enabled = env_bool("WNFX_NOTIFY", True)
    notify_only_fail = env_bool("WNFX_NOTIFY_ONLY_FAIL", False)

    host = (os.getenv("WNFX_HOST") or DEFAULT_HOST).strip().rstrip("/")
    # 只保留域名/端口,避免用户误填协议、路径后拼出错误 URL
    host = re.sub(r"^https?://", "", host, flags=re.I).split("/")[0].strip()
    if not host:
        host = DEFAULT_HOST

    tasks = collect_tasks()
    if not tasks:
        print("❌ 未配置任何凭证。请设置 WNFX_ACCOUNTS(用户名#密码)或 WNFX_COOKIE 环境变量")
        sys.exit(1)

    if not acquire_lock():
        sys.exit(0)

    print("=" * 60)
    print(f"🚀 福利吧每日签到(fx_checkin)  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"🌐 站点: {host}   账号数: {len(tasks)}")
    print("=" * 60)

    results = []
    try:
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
    finally:
        release_lock()

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
            line += "\n  - 💡 请浏览器登录后 F12 重新抓取 Cookie 更新 `WNFX_COOKIE`"
        if r.get("auth_failed"):
            line += "\n  - 💡 请核对 `WNFX_ACCOUNTS` 的密码(该站登录失败按 IP 计数,勿反复试)"
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
            print("ℹ️ WNFX_NOTIFY_ONLY_FAIL=true 且全部成功,跳过推送")
        else:
            send_notify(title, report)
    else:
        print("ℹ️ WNFX_NOTIFY=false,已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
