# -*- coding: utf-8 -*-
"""
new Env('尚香书苑签到');
cron: 23 8 * * *

尚香书苑 Discuz! X3.5 论坛每日签到（站点自研插件 sx_signin，2026-11-05 起替代已下线的 k_misign）。
站点主域名不定期更换（旧域名 301 到"最新地址"导航页），脚本启动时自动探测可用域名：
先试内置候选，再解析导航页里站点自维护的最新域名。
登录强制图片验证码（Discuz seccode，idhash 固定为 cS），本地 ddddocr 自动识别；
签到页内嵌 window.SIGNIN 配置，取 4 位图形验证码识别后 POST 到 sx_signin:ajax 换 JSON；
登录 Cookie JSON 缓存复用，失效自动重新登录。
网络默认直连（本站国内可达，走境外代理会被 Cloudflare 当数据中心 IP 发人机质询）；
站点偶发 Cloudflare 522（回源超时）等瞬时错误页会自动退避重试，不会误判成改版或掉登录；
Cloudflare 人机质询页（403 "Just a moment..."）单独识别，按出口 IP 风控处理，不清 Cookie 缓存。

环境变量：
  SXSY_ACCOUNTS          账号列表：用户名#密码，多账号用 & 或换行分隔；
                         也支持直接填 Cookie 字符串（不含 # 时按 Cookie 处理，不参与缓存）
  SXSY_NOTIFY            true/false 是否推送，默认 true
  SXSY_NOTIFY_ONLY_FAIL  true/false 仅失败时推送，默认 false
  SXSY_TIMEOUT           秒    请求超时，默认 30
  SXSY_DEBUG             true/false 调试模式，默认 false
  SXSY_PROXY             HTTP 代理（可选）；**默认直连**，不读全局 HTTPS_PROXY。
                         填 URL 走代理，填 direct/none/off 同样强制直连
依赖：requests 必装；ddddocr 两种模式都需要（登录与签到验证码都要识别）
作者: zephyr_xiao
"""
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

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
    print("❌ 缺少 requests 库，请在青龙面板「依赖管理」-「Python」中安装 requests")
    sys.exit(1)

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


# ---------------- 站点常量（均为 2026-10-30 实测） ----------------
# 站点主域名会不定期更换：旧域名 301 到一个纯 JS 的"最新地址"导航页（不是新站），
# 跟随重定向只会拿到导航页。所以 BASE 仅作默认值，运行时由 resolve_base() 探测后覆盖。
DEFAULT_BASE = "https://sxsy33.com"
DOMAIN_CANDIDATES = ("https://sxsy33.com", "https://sxsy45.com")
NAV_URL = "https://sxsy.pw"     # "最新地址"导航页：旧域名 301 的落点，页面内写着当前主域名
BASE = DEFAULT_BASE
SITE_NAME = "尚香书苑"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MODID = "member::logging"       # Discuz seccode 的 modid
# 2026-11-05 站点把签到插件从 k_misign 换成自研 sx_signin：
# 旧地址 plugin.php?id=k_misign:sign 现在返回「插件不存在或已关闭」，不要回退。
SIGN_URL = f"{BASE}/plugin.php?id=sx_signin:index"
SIGN_AJAX_URL = f"{BASE}/plugin.php?id=sx_signin:ajax"
SIGN_CAPTCHA_URL = f"{BASE}/plugin.php?id=sx_signin:captcha"
LOGIN_URL = f"{BASE}/member.php?mod=logging&action=login"
FORUM_URL = f"{BASE}/forum.php"

# 命名须匹配仓库 .gitignore 的 *_cookie.json 规则（含真实凭证，严禁入库）
COOKIE_CACHE = Path(__file__).resolve().parent / "sxsy45_cookie.json"
LOGIN_MAX_TRIES = 3             # 验证码 OCR 识别重试上限（含首次）

# 致命登录错误关键词：出现即停止重试（避免反复触发风控/封禁计数）
_LOGIN_FATAL_KEYWORDS = ("密码错误", "密码不正确", "登录失败次数过多", "密码错误次数过多",
                         "账户被", "帐号被", "被锁定", "被禁止")
SIGN_CAPTCHA_TRIES = 3          # 签到图形验证码识别重试上限（含首次）

# 站点间歇性返回 Cloudflare 522（回源超时）等错误页，属于瞬时故障：
# 必须退避重试，不能当成"Cookie 失效"或"站点改版"，否则会白白丢掉有效会话。
_SITE_ERROR_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504,
                                520, 521, 522, 523, 524, 525, 526})
HTTP_TRIES = 3                  # 单个 GET 请求最大尝试次数（含首次）
HTTP_BACKOFF = (5, 15)          # 各次重试前等待秒数，末项复用

# Cloudflare 托管质询（403 "Just a moment..." / 503 challenge-platform）：
# 判定依据是出口 IP 被风控，与账号密码、Cookie 都无关。刷重试只会加重风控，
# 也不能据此认定"Cookie 失效"，所以单独一类、只给一次重试机会（代理节点有机会切换）。
CF_CHALLENGE_MARK = "Cloudflare 人机质询"
CF_CHALLENGE_TITLE_MARKERS = ("Just a moment", "Attention Required",
                              "Checking your browser", "Verify you are human")
CF_CHALLENGE_TRIES = 2          # 同一出口最多尝试次数（含首次）
CF_CHALLENGE_BACKOFF = 15       # 两次尝试之间的等待秒数


# ---------------- 通用工具 ----------------
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


def parse_credentials(env_value: str):
    """解析 'user#pwd&user2#pwd2' / 多行 -> [(user, pwd), ...]"""
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
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar[k.strip()] = v.strip()
    return jar


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


def parse_signin_config(html: str) -> dict:
    """解析签到页内嵌的 window.SIGNIN 配置；找不到或不是合法 JSON 时返回 {}。

    插件把 ajax 地址、formhash、今日是否已签、验证码开关都放在这个对象里，
    比从 HTML 结构里抠按钮可靠得多（旧插件改版时正是靠按钮 id 判定的，很容易失效）。
    """
    m = re.search(r"window\.SIGNIN\s*=\s*(\{.*?\});", html or "", re.S)
    if not m:
        return {}
    try:
        cfg = json.loads(m.group(1))
    except (ValueError, TypeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def fetch_sign_stats(html: str) -> str:
    """从签到页提取统计（连续/累计签到天数），无统计返回空串。"""
    parts = []
    m = re.search(r'id="signin-streak"[^>]*>(\d+)<', html or "")
    if m:
        parts.append(f"连续 {m.group(1)} 天")
    m = re.search(r'id="signin-total"[^>]*>(\d+)<', html or "")
    if m:
        parts.append(f"累计 {m.group(1)} 天")
    return "  │  ".join(parts)


# 本站国内可直连；走境外代理反而会被 Cloudflare 当成数据中心 IP 发人机质询，
# 所以默认直连。需要代理时显式设 SXSY_PROXY=<url>，或设 SXSY_PROXY=direct 强制直连。
DIRECT_PROXY_VALUES = ("direct", "none", "off", "0", "false", "-")

def resolve_proxy() -> tuple:
    """解析网络出口，返回 (代理地址, 来源说明)；地址为空表示直连。

    与通用约定的差异：**默认直连，不回退全局 HTTPS_PROXY**。原因是本站套 Cloudflare，
    经境外代理节点访问会被判为可疑数据中心 IP 而返回 403 质询（2026-10-28 实测），
    直连家庭宽带出口才稳定。
    """
    value = (os.getenv("SXSY_PROXY") or "").strip()
    if value:
        if value.lower() in DIRECT_PROXY_VALUES:
            return "", "SXSY_PROXY=直连"
        return value, "SXSY_PROXY"
    ignored = next(
        (name for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                           "ALL_PROXY", "all_proxy")
         if (os.getenv(name) or "").strip()),
        "",
    )
    return "", (f"默认直连，已忽略 {ignored}" if ignored else "默认直连")


def build_session(proxy: str, timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": BASE + "/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    else:
        # 直连时必须关掉 trust_env，否则 requests 仍会从环境变量读 HTTPS_PROXY 走代理
        s.trust_env = False
    s._timeout = timeout
    return s


# ---------------- HTTP 重试层 ----------------
def page_title(html: str) -> str:
    """提取页面标题（用于错误诊断，截断避免刷屏）。"""
    m = re.search(r"<title>(.*?)</title>", html or "", re.S | re.I)
    return m.group(1).strip()[:60] if m else ""


def is_site_error(resp) -> bool:
    """是否为 CDN/源站临时错误页。

    Cloudflare 回源超时会返回 520~526（本站实测 522），状态码是首要判据；
    个别 CDN 用 200 包装错误页，再按标题兜底识别。
    """
    if resp.status_code in _SITE_ERROR_STATUS:
        return True
    return bool(re.search(
        r"\b5\d{2}:\s*(Connection timed out|Web server is down|Origin is unreachable)",
        page_title(resp.text), re.I))


def is_cf_challenge(resp) -> bool:
    """是否为 Cloudflare 人机质询页（出口 IP 被风控）。

    质询页既没有 formhash 也没有 discuz_uid，若当成正常页面处理，会被误读成
    "Cookie 失效/站点改版"，进而清掉有效缓存去登录，而登录同样会被拦。
    """
    if resp is None:
        return False
    headers = getattr(resp, "headers", None) or {}
    if str(headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    if resp.status_code not in (403, 429, 503):
        return False
    if any(mark in page_title(resp.text) for mark in CF_CHALLENGE_TITLE_MARKERS):
        return True
    return "challenge-platform" in (resp.text or "")


def _is_challenge_err(err: str) -> bool:
    """get_retry 的错误描述是否来自 CF 人机质询。"""
    return CF_CHALLENGE_MARK in (err or "")


def get_retry(session: requests.Session, url: str, headers: dict = None,
              debug: bool = False, label: str = "请求",
              tries: int = HTTP_TRIES) -> tuple:
    """GET 带退避重试，返回 (响应, 错误描述)；成功时错误描述为空串。

    网络异常与 CDN/源站 5xx（Cloudflare 522 等）都重试——这类故障通常持续几秒
    到几十秒，一次成功请求就能继续流程，比直接判失败划算得多。
    CF 人机质询单独处理：绑定出口 IP，同一出口连打只会加重风控，故最多试
    CF_CHALLENGE_TRIES 次；仍被拦则返回带 CF_CHALLENGE_MARK 的错误，交调用方定性。
    """
    last_err = ""
    cf_hits = 0
    for attempt in range(1, tries + 1):
        try:
            r = session.get(url, timeout=session._timeout, headers=headers)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        else:
            if is_cf_challenge(r):
                cf_hits += 1
                last_err = f"{CF_CHALLENGE_MARK}（HTTP {r.status_code}）"
                if debug:
                    print(f"    [debug] {label} 命中 CF 质询页（第 {cf_hits} 次）")
                if cf_hits >= CF_CHALLENGE_TRIES or attempt >= tries:
                    print(f"    ⛔ {label} 命中 {last_err}，停止重试")
                    return None, last_err
                print(f"    ⏳ {label} 命中 {last_err}，{CF_CHALLENGE_BACKOFF}s 后重试")
                time.sleep(CF_CHALLENGE_BACKOFF)
                continue
            if not is_site_error(r):
                if r.status_code < 400:
                    return r, ""
                # 4xx（以及未列入瞬时故障的 5xx）不是可重试的临时错误：原样报出状态码与标题，
                # 避免调用方把 WAF 拦截页/404 误读成"未登录/页面改版"
                title = page_title(r.text)
                last_err = f"HTTP {r.status_code}" + (f"「{title}」" if title else "")
                print(f"    ⛔ {label} 返回 {last_err}，不是可用页面")
                return None, last_err
            title = page_title(r.text)
            last_err = f"HTTP {r.status_code}" + (f"「{title}」" if title else "")
            if debug:
                print(f"    [debug] {label} 第 {attempt}/{tries} 次返回错误页: {last_err}")
        if attempt < tries:
            delay = HTTP_BACKOFF[min(attempt - 1, len(HTTP_BACKOFF) - 1)]
            print(f"    ⏳ {label} 暂时不可用（{last_err}），{delay}s 后重试（{attempt}/{tries}）")
            time.sleep(delay)
    return None, last_err


# ---------------- 域名发现 ----------------
# 站点换域名时旧域名会 301 到"最新地址"导航页：HTTP 200、标题正常，但没有 formhash。
# requests 默认跟随重定向，于是登录流程拿到的是导航页，报出来是"登录页无 formhash"，
# 看着像改版或风控，实际只是域名换了。运行前先探测可用域名，避免整轮签到白跑。
def apply_base(base: str) -> None:
    """切换站点根地址，并同步所有派生 URL。"""
    global BASE, SIGN_URL, SIGN_AJAX_URL, SIGN_CAPTCHA_URL, LOGIN_URL, FORUM_URL
    BASE = base.rstrip("/")
    SIGN_URL = f"{BASE}/plugin.php?id=sx_signin:index"
    SIGN_AJAX_URL = f"{BASE}/plugin.php?id=sx_signin:ajax"
    SIGN_CAPTCHA_URL = f"{BASE}/plugin.php?id=sx_signin:captcha"
    LOGIN_URL = f"{BASE}/member.php?mod=logging&action=login"
    FORUM_URL = f"{BASE}/forum.php"


_PROBE_TIMEOUT = 15             # 域名探测超时上限（秒）：站点响应不稳，探测不必等满用户配置


def _login_page_probe(session: requests.Session, base: str,
                      debug: bool = False) -> tuple:
    """探测 base 是否为可用站点，返回 (状态, 说明, 页面文本)。

    状态语义：True 可用；False 不可用；None 出口 IP 被 CF 质询（换域名同样会被拦）。
    只对网络异常重试一次（本站偶发读超时很常见）；HTTP 层结果不重试，
    4xx/5xx 的语义要原样保留，且逐个别重试比单点长等更快出结果。
    """
    url = f"{base.rstrip('/')}/member.php?mod=logging&action=login"
    timeout = min(session._timeout, _PROBE_TIMEOUT)
    r, last_err = None, ""
    for attempt in (1, 2):
        try:
            r = session.get(url, timeout=timeout)
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if debug:
                print(f"    [debug] 探测 {base} 第 {attempt} 次异常: {last_err}")
            if attempt == 1:
                time.sleep(2)
    if r is None:
        return False, last_err, ""
    if is_cf_challenge(r):
        return None, f"{CF_CHALLENGE_MARK}（HTTP {r.status_code}）", ""
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}", ""
    if re.search(r'name="formhash"\s+value="[a-f0-9]+"', r.text):
        return True, f"HTTP {r.status_code}", r.text
    return False, f"HTTP {r.status_code} 无 formhash（标题「{page_title(r.text) or '无'}」）", r.text


def _domain_from_nav(html: str) -> str:
    """从"最新地址"导航页解析站点当前主域名（站点自己维护的 CONFIG）。"""
    m = (re.search(r'websiteUrl\s*:\s*"([^"]+)"', html or "")
         or re.search(r'latestDomain\s*:\s*"([^"]+)"', html or ""))
    if not m:
        return ""
    url = m.group(1).strip().rstrip("/")
    return url if url.startswith("http") else "https://" + url


def resolve_base(session: requests.Session, debug: bool = False) -> tuple:
    """确定本次运行使用的站点域名，返回 (base, 说明)；找不到返回 ("", 原因汇总)。

    顺序：候选域名逐个探测 -> 探测中若落到导航页则解析其最新域名 -> 主动再取一次导航页。
    命中 CF 质询时立即返回该域名：同一出口换域名一样被拦，继续探测只是徒增风控。
    """
    notes, nav_domains = [], []

    def _try(base: str):
        state, note, html = _login_page_probe(session, base, debug)
        if state is True:
            return base, note
        if state is None:
            return base, note
        notes.append(f"{base} {note}")
        found = _domain_from_nav(html)
        if found:
            nav_domains.append(found)
        return None

    for base in DOMAIN_CANDIDATES:
        hit = _try(base)
        if hit:
            return hit

    # 候选都不可用：导航页是站点自维护的最新域名来源，主动再取一次
    r, err = get_retry(session, NAV_URL, debug=debug, label="最新地址页", tries=2)
    if r is not None:
        found = _domain_from_nav(r.text)
        if found:
            nav_domains.append(found)
    else:
        notes.append(f"{NAV_URL} {err}")

    tried = {b.rstrip("/").lower() for b in DOMAIN_CANDIDATES}
    for base in dict.fromkeys(nav_domains):
        if base.rstrip("/").lower() in tried:
            continue
        hit = _try(base)
        if hit:
            return hit
    return "", "；".join(notes)


# ---------------- Cookie 缓存（仅账密模式使用） ----------------
def read_cookie_cache() -> dict:
    if COOKIE_CACHE.exists():
        try:
            with open(COOKIE_CACHE, "r", encoding="utf-8") as f:
                return json.load(f).get("accounts", {})
        except Exception:
            pass
    return {}


def write_cookie_cache(username: str, cookie_str: str):
    """写入指定用户的 Cookie 缓存（只存凭证，绝不存密码）。
    两段写 + os.replace 原子替换：写一半崩溃只丢本次，不损坏已有全部账号的缓存；
    临时文件先收 0600 再替换（Windows 下 chmod 失败不影响功能）。"""
    data = {}
    if COOKIE_CACHE.exists():
        try:
            with open(COOKIE_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        data = {"accounts": {}}
    data["accounts"][username] = {
        "cookies": cookie_str,
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = COOKIE_CACHE.with_name(COOKIE_CACHE.name + ".part")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, COOKIE_CACHE)
    except Exception as e:
        print(f"⚠️ Cookie 缓存写入失败: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------- 登录层 ----------------
_OCR_INSTANCE = None
_OCR_READY = False


def get_ocr():
    """懒加载 ddddocr 实例；库缺失或初始化失败返回 None。

    登录（Discuz seccode）和签到（sx_signin 4 位图形码）都要用它，
    所以做成进程内单例，多账号不重复初始化。
    """
    global _OCR_INSTANCE, _OCR_READY
    if _OCR_READY:
        return _OCR_INSTANCE
    _OCR_READY = True
    try:
        import ddddocr
    except ImportError:
        _OCR_INSTANCE = None
    else:
        try:
            _OCR_INSTANCE = ddddocr.DdddOcr(old=False)
        except Exception as e:
            print(f"⚠️ ddddocr 初始化失败: {e}")
            _OCR_INSTANCE = None
    return _OCR_INSTANCE


def has_auth_cookie(session: requests.Session) -> bool:
    """会话里出现 Discuz 的 *_auth Cookie 才算真正登录成功。

    Discuz 登录页与登录成功页都含 succeedlocation 模板元素，用它判成功会把
    "登录页被原样返回"误判成成功；auth Cookie 是服务端下发的确定信号。
    """
    return any(c.name.endswith("_auth") and c.value for c in session.cookies)


def is_login_page(html: str) -> bool:
    """页面是否为登录页 / 未登录状态。

    不能用 'member.php?mod=logging' 子串判断：已登录页面的「退出」链接正是
    member.php?mod=logging&action=logout，会被误伤（2026-11-05 失败就是这个原因）。
    """
    if not html:
        return False
    return 'id="loginform_' in html or bool(re.search(r"discuz_uid\s*=\s*'0'", html))


def check_cookie_valid(session: requests.Session, debug: bool = False):
    """GET forum.php 判登录态：discuz_uid > 0 即有效。

    返回 True/False；站点临时故障（重试后仍拿不到正常页面）返回 None，
    调用方据此保留缓存而不是误判为"Cookie 失效"。
    """
    r, err = get_retry(session, FORUM_URL, debug=debug, label="论坛首页")
    if r is None:
        if _is_challenge_err(err):
            print(f"    ⚠️ 论坛首页被拦截（{err}）：出口 IP 被风控，与 Cookie 无关，保留缓存先试签到")
        else:
            print(f"    ⚠️ 论坛首页暂时不可用（{err}）")
        return None
    m = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
    uid = m.group(1) if m else "0"
    if debug:
        print(f"    [debug] forum.php uid={uid}, HTTP {r.status_code}")
    return uid not in ("", "0")


def _ocr(session: requests.Session, idhash: str, ocr, debug: bool) -> str:
    """下载 seccode 图片并识别一次，返回识别文本；图片拿不到时返回空串（交给登录重试）。"""
    r, err = get_retry(
        session,
        f"{BASE}/misc.php?mod=seccode&update={time.time()}&idhash={idhash}",
        debug=debug, label="验证码图片", tries=2)
    if r is None:
        print(f"    ⚠️ 验证码图片暂时不可用（{err}）")
        return ""
    if debug:
        print(f"    [debug] 验证码图片 HTTP {r.status_code}, {len(r.content)}B")
    return ocr.classification(r.content)


def discuz_login(session: requests.Session, username: str, password: str,
                 debug: bool = False, ocr=None) -> tuple:
    """账密登录（含 seccode OCR）。返回 (是否成功, 消息)。成功时会话已带登录 Cookie。"""
    if ocr is None:
        ocr = get_ocr()
    if ocr is None:
        return False, ("缺少 ddddocr 库：登录与签到都需要识别图形验证码，"
                       "请在「依赖管理」-「Python」安装 ddddocr（或改用 Cookie 模式）")

    login_referer = LOGIN_URL
    last_reason = "未知原因"
    for attempt in range(1, LOGIN_MAX_TRIES + 1):
        r, err = get_retry(session, login_referer, debug=debug,
                           label="登录页", tries=2)
        if r is None:
            if _is_challenge_err(err):
                # 质询绑定出口 IP：再打只会加重风控，也不该耗掉登录重试次数
                return False, (f"登录页被拦截（{err}）：出口 IP 被 Cloudflare 风控，"
                               f"与账号密码无关；稍后重试，或用 SXSY_PROXY=direct 强制直连")
            last_reason = f"登录页暂时不可用（{err}）"
            print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次 {last_reason}，重试...")
            time.sleep(2)
            continue

        m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', r.text)
        formhash = m.group(1) if m else ""
        m = re.search(r"updateseccode\('([a-zA-Z0-9]+)'", r.text)
        idhash = m.group(1) if m else ""
        if not formhash:
            # 排除瞬时错误页后仍无 formhash，才可能是改版或风控页；带上状态码与标题便于定位
            last_reason = (f"登录页无 formhash（HTTP {r.status_code}"
                           f"，标题「{page_title(r.text) or '无'}」）")
            print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次 {last_reason}，重试...")
            time.sleep(3)
            continue

        data = {
            "formhash": formhash,
            "referer": FORUM_URL,
            "loginfield": "username",
            "username": username,
            "password": password,
            "questionid": "0",
            "answer": "",
            "cookietime": "2592000",
        }
        if idhash:
            # seccode 链路：update 接口拿 seccodehash -> 取图 -> OCR
            ru, err = get_retry(
                session,
                f"{BASE}/misc.php?mod=seccode&action=update&idhash={idhash}"
                f"&{time.time()}&modid={MODID}&inajax=1",
                debug=debug, label="验证码接口", tries=2,
            )
            if ru is None:
                if _is_challenge_err(err):
                    return False, (f"验证码接口被拦截（{err}）：出口 IP 被 Cloudflare 风控，"
                                   f"与账号密码无关；稍后重试，或用 SXSY_PROXY=direct 强制直连")
                last_reason = f"验证码接口暂时不可用（{err}）"
                print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次 {last_reason}，重试...")
                time.sleep(2)
                continue
            hm = re.search(r'name="seccodehash"\s+type="hidden"\s+value="([a-zA-Z0-9]+)"', ru.text)
            seccodehash = hm.group(1) if hm else ""
            if not seccodehash:
                last_reason = "验证码 update 接口未返回 seccodehash（站点可能改版）"
                print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次 {last_reason}，重试...")
                time.sleep(2)
                continue
            try:
                code = _ocr(session, idhash, ocr, debug)
            except Exception as e:
                return False, f"验证码识别异常: {e}"
            if debug:
                print(f"    [debug] 第 {attempt} 次 OCR: {code!r}")
            if code:
                data["seccodehash"] = seccodehash
                data["seccodemodid"] = MODID
                data["seccodeverify"] = code
        # 无 idhash 说明站点关闭验证码，直接裸登录

        try:
            r2 = session.post(
                f"{LOGIN_URL}&loginsubmit=yes",
                data=data,
                timeout=session._timeout,
                headers={"Referer": login_referer},
                allow_redirects=False,
            )
        except Exception as e:
            return False, f"登录请求失败: {e}"

        body = r2.text
        # succeedlocation 在登录页模板里也有，不能单独作成功判据（会误判）
        if "succeedhandle" in body or has_auth_cookie(session):
            return True, "登录成功"

        if is_cf_challenge(r2):
            # POST 同样会被质询：继续重试只会被拦，直接如实报出
            return False, (f"登录接口被拦截（{CF_CHALLENGE_MARK}，HTTP {r2.status_code}）："
                           f"出口 IP 被 Cloudflare 风控，与账号密码无关；稍后重试，或用 SXSY_PROXY=direct 强制直连")

        if is_site_error(r2):
            last_reason = f"登录接口返回错误页（HTTP {r2.status_code}）"
            print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次 {last_reason}，重试...")
            time.sleep(3)
            continue

        # 失败原因分类
        if any(kw in body for kw in _LOGIN_FATAL_KEYWORDS):
            err = re.search(r"show_error\('([^']{0,120})", body)
            return False, f"账号密码错误或账号受限，停止重试: {err.group(1) if err else '见日志'}"
        if "验证码" in body:
            last_reason = "验证码识别错误"
            print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次验证码识别错误，重试...")
            time.sleep(2)
            continue
        err = re.search(r"show_error\('([^']{0,120})", body)
        last_reason = f"登录未成功: {err.group(1) if err else body[:100]}"
        print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次{last_reason}，重试...")
        time.sleep(2)

    return False, f"登录失败（{last_reason}）"


# ---------------- 签到层 ----------------
def _get_sign_page(session: requests.Session, debug: bool = False) -> tuple:
    """GET 签到页，返回 (HTML, 错误描述)；成功时错误描述为空串。"""
    r, err = get_retry(session, SIGN_URL, headers={"Referer": FORUM_URL},
                       debug=debug, label="签到页")
    if r is None:
        print(f"⚠️ 签到页请求失败: {err}")
        return "", err
    if debug:
        print(f"    [debug] 签到页 HTTP {r.status_code}, {len(r.text)}B")
    return r.text, ""


def _sign_captcha_code(session: requests.Session, cfg: dict, ocr, debug: bool = False) -> str:
    """下载签到图形验证码并识别一次，返回识别文本；拿不到图返回空串（交给重试）。"""
    url = cfg.get("captchaUrl") or SIGN_CAPTCHA_URL
    r, err = get_retry(session, f"{url}&t={int(time.time() * 1000)}",
                       headers={"Referer": SIGN_URL}, debug=debug,
                       label="签到验证码", tries=2)
    if r is None:
        print(f"    ⚠️ 签到验证码暂时不可用（{err}）")
        return ""
    if debug:
        print(f"    [debug] 签到验证码 HTTP {r.status_code}, {len(r.content)}B")
    try:
        return (ocr.classification(r.content) or "").strip()
    except Exception as e:
        print(f"    ⚠️ 签到验证码识别异常: {e}")
        return ""


def _signin_result_text(data: dict) -> str:
    """把接口返回的 data 拼成一行摘要（本次奖励 / 连续 / 累计）。"""
    parts = []
    if data.get("credit") is not None:
        parts.append(f"本次奖励 +{data['credit']}")
    if data.get("streak") is not None:
        parts.append(f"连续 {data['streak']} 天")
    if data.get("total") is not None:
        parts.append(f"累计 {data['total']} 天")
    return " / ".join(parts) or "接口返回成功"


def do_signin(session: requests.Session, debug: bool = False, ocr=None) -> dict:
    """
    sx_signin 签到（2026-11-05 起站点自研插件，替代已下线的 k_misign）。流程（均实测）：
      1. GET 签到页，解析 window.SIGNIN；signed=1 说明今天已签，幂等跳过
      2. GET captchaUrl 取 4 位图形验证码，ddddocr 识别
      3. POST ajax（op=signin&formhash=..&seccode=..）-> JSON
         code=0 成功 / code=2 今天已签 / code=11 验证码错误（换图重试）
      4. 回查签到页 signed=1 作为最终判据
    返回 {"state": signed_today|success|failed, "message": str, "stats": str}
    """
    html, err = _get_sign_page(session, debug)
    if not html:
        return {"state": "failed", "message": f"签到页请求失败：{err}", "stats": ""}

    if is_login_page(html):
        return {"state": "failed", "message": "登录态失效（签到页被跳转登录）", "stats": ""}

    cfg = parse_signin_config(html)
    if not cfg:
        return {"state": "failed",
                "message": f"签到页结构变化：未找到 SIGNIN 配置（标题「{page_title(html) or '无'}」）",
                "stats": ""}
    if cfg.get("signed"):
        return {"state": "signed_today", "message": "今日已签到", "stats": fetch_sign_stats(html)}

    formhash = cfg.get("formhash") or ""
    if not formhash:
        return {"state": "failed", "message": "签到页未提供 formhash（插件可能改版）", "stats": ""}

    if ocr is None:
        ocr = get_ocr()
    if ocr is None:
        return {"state": "failed",
                "message": "缺少 ddddocr 库：签到需要识别图形验证码，请在「依赖管理」-「Python」安装 ddddocr",
                "stats": ""}

    ajax = cfg.get("ajax") or SIGN_AJAX_URL
    last_reason = "未知原因"
    for attempt in range(1, SIGN_CAPTCHA_TRIES + 1):
        code = _sign_captcha_code(session, cfg, ocr, debug)
        if not code:
            last_reason = "验证码图片获取失败或识别为空"
            print(f"    ⚠️ 第 {attempt}/{SIGN_CAPTCHA_TRIES} 次{last_reason}"
                  + ("，重试..." if attempt < SIGN_CAPTCHA_TRIES else ""))
            time.sleep(1)
            continue

        try:
            r = session.post(
                ajax,
                data={"op": "signin", "formhash": formhash, "seccode": code},
                headers={"Referer": SIGN_URL,
                         "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=session._timeout,
            )
        except Exception as e:
            return {"state": "failed", "message": f"签到接口请求失败: {e}", "stats": ""}

        if debug:
            print(f"    [debug] 签到接口 HTTP {r.status_code}: {r.text[:200]}")
        try:
            res = r.json()
        except ValueError:
            # 掉登录或被 WAF 拦时返回的是 HTML，按页面内容如实分类
            if is_login_page(r.text):
                return {"state": "failed", "message": "登录态失效（签到接口被跳转登录）", "stats": ""}
            return {"state": "failed",
                    "message": (f"签到接口返回非 JSON（HTTP {r.status_code}"
                                f"，标题「{page_title(r.text) or '无'}」）"),
                    "stats": ""}
        if not isinstance(res, dict):
            return {"state": "failed",
                    "message": f"签到接口返回格式异常: {str(res)[:100]}", "stats": ""}

        code_no = res.get("code")
        data = res.get("data") or {}
        detail = _signin_result_text(data)

        if code_no == 0:
            html2, _ = _get_sign_page(session, debug)
            if not html2:
                # 回查页拿不到时不能直接当成功：接口明确返回成功才算，并标注未复核
                return {"state": "success",
                        "message": f"签到成功（{detail}，回查页暂不可用）", "stats": ""}
            if parse_signin_config(html2).get("signed"):
                return {"state": "success", "message": f"签到成功（{detail}）",
                        "stats": fetch_sign_stats(html2)}
            return {"state": "failed",
                    "message": f"签到接口返回成功但回查仍未签到（{detail}）",
                    "stats": fetch_sign_stats(html2)}

        if code_no == 2:
            html2, _ = _get_sign_page(session, debug)
            return {"state": "signed_today",
                    "message": f"今日已签到（{res.get('msg') or '接口提示已签到'}）",
                    "stats": fetch_sign_stats(html2 or html)}

        if code_no == 11:
            last_reason = f"验证码识别错误（{res.get('msg') or 'code=11'}）"
            print(f"    ⚠️ 第 {attempt}/{SIGN_CAPTCHA_TRIES} 次{last_reason}"
                  + ("，换图重试..." if attempt < SIGN_CAPTCHA_TRIES else ""))
            time.sleep(1)
            continue

        last_reason = f"接口返回 code={code_no}：{res.get('msg') or '无提示'}"
        break

    return {"state": "failed", "message": f"签到失败（{last_reason}）", "stats": ""}


# ---------------- 单账号执行 ----------------
def _account_label(cred) -> str:
    """任务标识：账密 tuple -> 脱敏用户名；Cookie 字符串 -> 'Cookie'。"""
    if isinstance(cred, tuple):
        return mask(cred[0])
    return "Cookie"


def run_one_account(cred, proxy: str, timeout: int, debug: bool) -> dict:
    """
    cred: (username, password) tuple（账密模式，带缓存）或整段 Cookie 字符串（直签模式）。
    返回 {"success": bool, "message": str, "label": str}
    """
    if isinstance(cred, tuple):
        username, password = cred
        label = mask(username)
        session = build_session(proxy, timeout)

        # 1) Cookie 缓存优先
        cached = read_cookie_cache().get(username, {}).get("cookies", "")
        if cached:
            for k, v in parse_cookie_str(cached).items():
                session.cookies.set(k, v)
            valid = check_cookie_valid(session, debug)
            if valid is True:
                print("    💾 缓存 Cookie 有效，跳过登录")
            elif valid is False:
                print("    ⚠️ 缓存 Cookie 失效，重新登录")
                session.cookies.clear()
            else:
                # 站点瞬时故障或出口被 CF 质询、无法判断登录态：保留缓存先试签到，避免白白丢会话
                print("    ⚠️ 无法确认 Cookie 状态（站点临时故障/出口被拦），先按缓存继续")

        # 2) 缓存缺失/失效 -> 账密登录
        if not session.cookies:
            ok, msg = discuz_login(session, username, password, debug)
            if not ok:
                return {"success": False, "message": msg, "label": label}
            write_cookie_cache(username, "; ".join(f"{c.name}={c.value}" for c in session.cookies))

        result = do_signin(session, debug)

        # 3) 登录态失效则重登一次再签（只重试一次，防死循环）
        if result["state"] == "failed" and "登录态失效" in result["message"]:
            print("    🔄 登录态失效，重新登录后重试一次")
            session.cookies.clear()
            ok, msg = discuz_login(session, username, password, debug)
            if not ok:
                return {"success": False, "message": msg, "label": label}
            write_cookie_cache(username, "; ".join(f"{c.name}={c.value}" for c in session.cookies))
            result = do_signin(session, debug)
    else:
        # Cookie 直签模式：不缓存、不自动登录
        label = "Cookie"
        session = build_session(proxy, timeout)
        for k, v in parse_cookie_str(cred).items():
            session.cookies.set(k, v)
        result = do_signin(session, debug)
        if result["state"] == "failed" and "登录态失效" in result["message"]:
            result["message"] += "；Cookie 模式失效请重新抓取 Cookie 更新环境变量"

    success = result["state"] in ("success", "signed_today")
    icon = "✅" if result["state"] == "success" else "🔁" if result["state"] == "signed_today" else "❌"
    message = f"{icon} {result['message']}"
    if result.get("stats"):
        message += f"\n      📊 {result['stats']}"
    return {"success": success, "message": message, "label": label}


# ---------------- 主流程 ----------------
def main():
    title = f"{SITE_NAME}签到"
    notify_enabled = env_bool("SXSY_NOTIFY", True)
    notify_only_fail = env_bool("SXSY_NOTIFY_ONLY_FAIL", False)
    debug = env_bool("SXSY_DEBUG", False)
    timeout = env_int("SXSY_TIMEOUT", 30)
    proxy, proxy_src = resolve_proxy()

    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"🌐 网络: {proxy if proxy else '直连'} （{proxy_src}）")

    # 站点会换主域名，先确认本次用哪个，再进入账号流程
    probe_session = build_session(proxy, timeout)
    base, base_note = resolve_base(probe_session, debug)
    if base:
        apply_base(base)
        print(f"🌐 站点域名: {BASE}（{base_note}）")
    else:
        print(f"⚠️ 未确认可用域名，按默认 {BASE} 继续（{base_note}）")
    print("=" * 60)

    env_value = os.getenv("SXSY_ACCOUNTS", "").strip()
    tasks = parse_credentials(env_value)
    # 无 # 的非空片段按 Cookie 直签处理
    cookie_tasks = [p.strip() for p in re.split(r"[&\n]+", env_value) if p.strip() and "#" not in p]
    tasks = tasks + cookie_tasks

    if not tasks:
        print("⚠️ 未配置账号，请设置环境变量 SXSY_ACCOUNTS（格式：用户名#密码，多账号 & 或换行分隔）")
        return

    print(f"📋 共发现 {len(tasks)} 个账号\n")
    results, success_cnt = [], 0

    for idx, cred in enumerate(tasks, 1):
        print(f"[{idx}/{len(tasks)}] 🔄 {SITE_NAME} [{_account_label(cred)}] ...")
        t0 = time.time()
        try:
            result = run_one_account(cred, proxy, timeout, debug)
        except Exception:
            traceback.print_exc()
            result = {"success": False, "message": "脚本异常", "label": f"账号 {idx}"}
        cost = time.time() - t0

        if result.get("success"):
            success_cnt += 1
            print(f"    {result['message']} ({cost:.1f}s)")
        else:
            print(f"    {result['message']} ({cost:.1f}s)")
        results.append(result)

        if idx < len(tasks):
            time.sleep(2)  # 风控间隔

    total = len(tasks)
    if success_cnt == total:
        title = f"✅ {title} 全部成功（{success_cnt}/{total}）"
    elif success_cnt == 0:
        title = f"❌ {title} 全部失败（0/{total}）"
    else:
        title = f"⚠️ {title} 部分失败（{success_cnt}/{total}）"

    _label_prefix = re.compile(r"^账号\s*\d+\s*[:：]\s*")
    ok_lines = [f"- **[{r.get('label', f'账号 {i}')}]** {_label_prefix.sub('', (r.get('message') or '').replace(chr(10), ' '), count=1)}"
                for i, r in enumerate(results, 1) if r.get("success")]
    fail_lines = [f"- **[{r.get('label', f'账号 {i}')}]** {_label_prefix.sub('', (r.get('message') or '').replace(chr(10), ' '), count=1)}"
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

    if notify_enabled:
        if notify_only_fail and success_cnt == total:
            print("ℹ️ SXSY_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(title, report)
    else:
        print("ℹ️ SXSY_NOTIFY=false，已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
