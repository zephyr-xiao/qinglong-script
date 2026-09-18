# -*- coding: utf-8 -*-
"""
new Env('司机社签到');
cron: 10 9,20 * * *

司机社(xsijishe.com / sjs47.com) 自动签到脚本。
支持双模式：Cookie 直接签到 或 邮箱密码 + 本地 OCR 验证码登录。
适配青龙面板，多账号串行签到，Cookie 自动缓存复用。
OCR 使用本地 ddddocr 库，无需部署外部服务。

环境变量：
  SIJISHE_ACCOUNTS         必填，账号列表，支持三种格式：
                              1) 邮箱&密码：user@mail.com&mypassword
                              2) Cookie 字符串：直接填入浏览器抓取的完整 Cookie
                              3) 混合模式：不同行可以不同格式
                              ★ 多账号用换行分隔
  SIJISHE_NOTIFY           true/false  默认 true，是否调用青龙 notify.py 推送
  SIJISHE_NOTIFY_ONLY_FAIL true/false  默认 false，全部成功时静默
  SIJISHE_PROXY            代理地址    默认空，如 http://172.17.0.1:7890
  SIJISHE_HOST              可选，手动指定签到域名（如 xsijishe.com），
                             留空则使用默认域名 xsijishe.com
  SIJISHE_DEBUG            true/false  默认 false，输出接口响应细节用于排错

依赖：requests, beautifulsoup4, ddddocr

定时建议：一天两次（9:00 和 20:00），避免签到遗漏
cron: 10 9,20 * * *

作者: 箫遥风
"""

import json
import os
import re
import sys
import time
import random
import base64
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path

# ❶ UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 问题）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ❷ 依赖检查
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

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("❌ 缺少 beautifulsoup4 库，请在青龙面板「依赖管理」-「Python」中安装 beautifulsoup4")
    sys.exit(1)

# ddddocr 延迟导入：仅邮箱密码模式需要，纯 Cookie 模式无需安装
# （与 README"仅邮箱密码模式需要"的说明保持一致）
_ocr = None


def get_ocr():
    """惰性初始化 ddddocr 单例（避免反复初始化）。纯 Cookie 模式永不触发。"""
    global _ocr
    if _ocr is None:
        try:
            import ddddocr
        except ImportError:
            print("❌ 缺少 ddddocr 库，请在青龙面板「依赖管理」-「Python」中安装 ddddocr")
            sys.exit(1)
        _ocr = ddddocr.DdddOcr(old=False)
    return _ocr

# Playwright 可选导入（用于绕过 Cloudflare 获取最新域名）
_sync_playwright = None
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
except ImportError:
    pass

# ❸ notify.py 兼容（青龙运行时自动注入 sys.path）
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

GUIDE_URL = "https://47447.net/"          # 发布页（需 Playwright 绕过 Cloudflare）
DEFAULT_HOST = "xsijishe.com"             # 默认域名
SCRIPT_DIR = Path(__file__).resolve().parent
COOKIE_FILE = SCRIPT_DIR / "xsijishe_cookie.json"   # Cookie 缓存文件

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0"
)

# Discuz! 登录关键 URL 模板（host 动态替换）
URL_LOGIN_PARAM = "/member.php?mod=logging&action=login&infloat=yes&frommessage&inajax=1&ajaxtarget=messagelogin"
URL_SECCODE_IMG = "/misc.php?mod=seccode&update={update}&idhash={seccodehash}"
URL_SECCODE_CHECK = "/misc.php?mod=seccode&action=check&inajax=1&modid=member::logging&idhash={seccodehash}&secverify={captcha}"
URL_LOGIN_DO = "/member.php?mod=logging&action=login&loginsubmit=yes&loginhash={loginhash}&inajax=1"
URL_CHECK_COOKIE = "/home.php?mod=space"
URL_SIGN_PAGE = "/k_misign-sign.html"
URL_SIGN_DO = "/k_misign-sign.html?operation=qiandao&format=button&formhash={formhash}&inajax=1&ajaxtarget=midaben_sign"

# OCR 验证码重试次数
MAX_CAPTCHA_RETRY = 3


# ============================ 通用工具函数 ============================

class LoginParamError(Exception):
    """登录/签到参数提取失败，疑似域名失效或反爬拦截，应切换备用域名重试。"""


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


def mask_email(email: str) -> str:
    """邮箱脱敏：保留首字符和域名，中间打码。"""
    if not email or "@" not in email:
        return email or "未知"
    user, domain = email.split("@", 1)
    if len(user) <= 2:
        return f"{user[0]}*@{domain}"
    return f"{user[0]}***{user[-1]}@{domain}"


def rand_sleep(min_ms: int, max_ms: int):
    """随机休眠（毫秒），用于多账号间隔和风控对抗。"""
    ms = random.randint(min_ms, max_ms)
    time.sleep(ms / 1000.0)


# ============================ 账号解析 ============================

def parse_accounts(raw: str) -> list[dict]:
    """
    解析 SIJISHE_ACCOUNTS 环境变量。
    支持格式：
      - 邮箱&密码  → {"type": "credential", "email": ..., "password": ..., "label": ...}
      - Cookie     → {"type": "cookie", "cookie": ..., "label": ...}
    多账号用换行或 & 分隔（& 仅在整行不含 @ 时生效，避免与邮箱冲突），
    连续空行自动跳过。
    """
    if not raw or not raw.strip():
        return []

    # 按换行切分
    lines = [line.strip() for line in raw.strip().split("\n") if line.strip()]
    accounts = []

    for i, line in enumerate(lines):
        # 含 @ 且含 & → 邮箱密码模式
        if "@" in line and "&" in line:
            parts = line.split("&", 1)
            email = parts[0].strip()
            password = parts[1].strip() if len(parts) > 1 else ""
            accounts.append({
                "type": "credential",
                "email": email,
                "password": password,
                "label": f"账号 {i + 1} [{mask_email(email)}]",
            })
        else:
            # 其他情况视为纯 Cookie
            accounts.append({
                "type": "cookie",
                "cookie": line.strip(),
                "label": f"账号 {i + 1} [Cookie]",
            })

    return accounts


# ============================ 域名获取 ============================

# 已知活跃域名列表（从发布页 47447.net 通过 Playwright 获取，作为无 Playwright 时的兜底）
# 顺序即优先级；镜像域名 DNS 记录不稳定（间歇污染），连接失败时自动跳过
KNOWN_HOSTS = [
    "sjs96.com",       # 发布页当前主推（Cloudflare 直连）
    "sjs66.net",       # 备用（DNS 间歇污染，失败自动跳过）
    "xsijishe.net",    # 备用（同上）
    "xsijishe.com",    # 历史域名（DNS 间歇污染）
    "sjs66.com",
    "xsijishe.ink",
    "sjslt.cc",
    "sjs47.com",
]


def _fetch_hosts_via_playwright() -> list[str]:
    """
    使用 Playwright 真浏览器渲染发布页，绕过 Cloudflare 获取最新域名列表。
    返回域名列表，失败返回空列表。
    """
    if not _sync_playwright:
        return []
    try:
        print("[域名加载] 🎭 使用 Playwright 渲染发布页获取最新域名...")
        with _sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.goto(GUIDE_URL, wait_until="domcontentloaded", timeout=15000)
                # 等待 Cloudflare JS 挑战完成 + "正在检测最快地址" 完成
                page.wait_for_timeout(8000)
                html = page.content()

                soup = BeautifulSoup(html, "html.parser")
                hosts = []
                for link in soup.find_all("a"):
                    href = link.get("href", "").strip()
                    if href.startswith("http"):
                        h = href.split("//")[1].split("/")[0]
                        # 过滤发布页自身
                        if h in ("47447.net", "www.47447.net"):
                            continue
                        if h not in hosts:
                            hosts.append(h)
                if hosts:
                    print(f"[域名加载] 🎉 Playwright 获取到 {len(hosts)} 个活跃域名: {', '.join(hosts)}")
                    return hosts
                print("[域名加载] ⚠️ Playwright 渲染完成但未找到域名链接")
            finally:
                browser.close()
    except Exception as e:
        print(f"[域名加载] ⚠️ Playwright 获取域名异常: {e}")
    return []


def get_latest_host(debug: bool) -> tuple[str, list[str]]:
    """
    获取签到域名列表，优先级：
      1. SIJISHE_HOST 环境变量（手动指定）
      2. Playwright 渲染发布页动态获取
      3. 内置已知域名列表
    返回 (首选域名, [备用域名列表])，打开代理时可以逐个尝试。
    """
    # ❶ 环境变量手动指定（最高优先级）
    manual_host = (os.getenv("SIJISHE_HOST") or "").strip()
    if manual_host:
        print(f"[域名加载] 使用手动指定的域名: {manual_host}")
        return manual_host, [manual_host]

    # ❷ 尝试 Playwright 动态获取
    live_hosts = _fetch_hosts_via_playwright()
    if live_hosts:
        print(f"[域名加载] 选用 Playwright 获取的域名: {live_hosts[0]}（备用: {', '.join(live_hosts[1:4])}）")
        return live_hosts[0], live_hosts

    # ❸ 回退内置已知列表
    print(f"[域名加载] 使用内置已知域名列表")
    return KNOWN_HOSTS[0], KNOWN_HOSTS


# ============================ Cookie 缓存 ============================

def read_cookie_cache() -> dict:
    """读取本地 Cookie 缓存文件。返回 {email: {cookies, update_time}, ...} 或 {}。"""
    try:
        if COOKIE_FILE.exists():
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("accounts", {})
    except Exception as e:
        print(f"⚠️ 读取 Cookie 缓存异常: {e}")
    return {}


def write_cookie_cache(email: str, cookie_str: str):
    """写入/更新 Cookie 缓存文件。"""
    try:
        existing = {}
        if COOKIE_FILE.exists():
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing["site"] = "司机社"
        existing["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if "accounts" not in existing:
            existing["accounts"] = {}
        existing["accounts"][email] = {
            "cookies": cookie_str,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 写入 Cookie 缓存异常: {e}")


def delete_cookie_cache():
    """删除整个 Cookie 缓存文件（所有 Cookie 都失效时）。"""
    try:
        if COOKIE_FILE.exists():
            COOKIE_FILE.unlink()
            print(f"🗑️ 已删除失效的 Cookie 缓存: {COOKIE_FILE}")
    except Exception as e:
        print(f"⚠️ 删除 Cookie 缓存异常: {e}")


def apply_cookies(session: requests.Session, cookie_str: str) -> None:
    """将 Cookie 字符串注入 session。"""
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            key, value = item.split("=", 1)
            session.cookies.set(key.strip(), value.strip())


def get_session_cookies_str(session: requests.Session) -> str:
    """从 session 提取 Cookie 字符串用于缓存。"""
    parts = []
    for cookie in session.cookies:
        parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts)


# ============================ 登录流程 ============================

def fetch_login_params(host: str, session: requests.Session, debug: bool) -> tuple[str | None, str | None, str | None]:
    """
    GET 登录页，提取 formhash、seccodehash、loginhash。
    返回 (formhash, seccodehash, loginhash)。
    formhash/loginhash 提取失败时抛 LoginParamError（上层切换域名重试）；
    seccodehash 为 None 属正常（当前无需验证码或 JS 动态加载），不影响登录。
    """
    try:
        url = f"https://{host}{URL_LOGIN_PARAM}"
        resp = session.get(
            url,
            headers={"User-Agent": DEFAULT_UA, "Host": host},
            timeout=15,
        )
        resp.raise_for_status()
        text = resp.text
        if debug:
            print(f"   [DEBUG] 登录参数页 HTTP {resp.status_code}, 长度 {len(text)}")
            # 提取所有疑似 hash 的内容
            hashes = re.findall(r'[a-zA-Z0-9]{5,10}', text)
            print(f"   [DEBUG] 疑似 hash: {list(set(hashes))[:20]}")

        # formhash: 8 位字母数字
        m = re.search(r'name="formhash"\s+value="([a-zA-Z0-9]{8})"', text)
        formhash = m.group(1) if m else None
        if not formhash:
            # 尝试更宽松的匹配
            m = re.search(r'formhash["\']?\s*[=:]\s*["\']?([a-zA-Z0-9]{8})', text)
            formhash = m.group(1) if m else None
        if not formhash and debug:
            print(f"   [DEBUG] formhash 缺失，响应片段: {text[:500]}")

        # seccodehash: seccode_ 后跟 5-8 位字母数字
        m = re.search(r"seccode_([a-zA-Z0-9]{5,8})", text)
        seccodehash = m.group(1) if m else None
        if not seccodehash:
            # 不是错误——可能当前不需要验证码，或者验证码由 JS 动态加载
            if debug:
                print("   [DEBUG] 未找到 seccodehash（可能无需验证码或 JS 动态加载）")

        # loginhash: main_messaqge_ 后跟 5 位字母数字（Discuz! 源码拼写如此）
        m = re.search(r"main_messaqge_([a-zA-Z0-9]{5})", text)
        loginhash = m.group(1) if m else None
        if not loginhash:
            # 尝试其他可能的 hash 格式
            m = re.search(r'loginhash["\']?\s*[=:]\s*["\']?([a-zA-Z0-9]{5,8})', text)
            loginhash = m.group(1) if m else None
        if not loginhash and debug:
            print(f"   [DEBUG] loginhash 缺失，响应片段: {text[:500]}")

        # formhash/loginhash 任一缺失 → 疑似域名失效或被反爬拦截，
        # 抛 LoginParamError 让上层切换备用域名重试（而非直接判定登录失败）
        if not formhash or not loginhash:
            raise LoginParamError(
                f"无法提取登录参数（formhash={'有' if formhash else '无'}，"
                f"loginhash={'有' if loginhash else '无'}），疑似域名或反爬问题"
            )

        if debug:
            print(f"   [DEBUG] formhash={formhash}, seccodehash={seccodehash}, loginhash={loginhash}")

        return formhash, seccodehash, loginhash
    except LoginParamError:
        raise
    except Exception as e:
        # 网络/SSL 类错误上抛（上层切换域名重试）；解析类异常按提取失败处理
        if _is_network_error(e):
            raise
        print(f"❌ [获取参数] 解析异常: {e}")
        return None, None, None


def fetch_captcha_image(host: str, seccodehash: str, session: requests.Session) -> str | None:
    """
    获取验证码图片并返回 base64 编码字符串。
    """
    try:
        update_id = random.randint(10000, 99999)
        url = f"https://{host}{URL_SECCODE_IMG.format(update=update_id, seccodehash=seccodehash)}"
        resp = session.get(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Host": host,
                "Referer": f"https://{host}/member.php?mod=logging&action=login",
            },
            timeout=15,
        )
        img_b64 = base64.b64encode(resp.content).decode("utf-8")
        return img_b64
    except Exception as e:
        # 网络/SSL 类错误上抛（上层切换域名重试）；其余按获取失败处理（外层会重试）
        if _is_network_error(e):
            raise
        print(f"❌ [获取验证码] 异常: {e}")
        return None


def ocr_captcha(img_base64: str) -> str | None:
    """
    使用本地 ddddocr 库识别验证码文字。
    返回识别结果字符串，失败返回 None。
    """
    try:
        img_bytes = base64.b64decode(img_base64)
        result = get_ocr().classification(img_bytes)
        if result:
            return result.strip()
        print("❌ [OCR识别] ddddocr 返回空结果")
        return None
    except Exception as e:
        print(f"❌ [OCR识别] 异常: {e}")
        return None


def verify_captcha(host: str, captcha: str, seccodehash: str, session: requests.Session) -> bool:
    """
    向服务器校验验证码是否正确。
    """
    try:
        url = f"https://{host}{URL_SECCODE_CHECK.format(seccodehash=seccodehash, captcha=captcha)}"
        resp = session.get(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Host": host,
                "Referer": f"https://{host}/member.php?mod=logging&action=login",
            },
            timeout=15,
        )
        resp.raise_for_status()
        m = re.search(r"<!\[CDATA\[(.*?)\]\]>", resp.text)
        if m and "succeed" in m.group(1):
            return True
        return False
    except Exception as e:
        # 网络/SSL 类错误上抛（上层切换域名重试）；其余按校验失败处理（外层会重试）
        if _is_network_error(e):
            raise
        print(f"❌ [校验验证码] 异常: {e}")
        return False


def do_login(
    host: str,
    email: str,
    password: str,
    formhash: str,
    loginhash: str,
    session: requests.Session,
    seccodehash: str | None = None,
    captcha: str | None = None,
) -> bool:
    """
    执行 Discuz! 登录。
    如果 seccodehash 和 captcha 为 None，则跳过验证码字段。
    返回 True 表示登录成功。
    """
    try:
        url = f"https://{host}{URL_LOGIN_DO.format(loginhash=loginhash)}"
        # 构造登录表单（loginfield=email 表示用邮箱登录）
        raw_payload = (
            f"formhash={formhash}"
            f"&referer=https://{host}/home.php?mod=spacecp&ac=credit&showcredit=1"
            f"&loginfield=email"
            f"&username={email}"
            f"&password={password}"
            f"&questionid=0"
            f"&answer="
        )
        # 仅当有验证码时才附加验证码字段
        if seccodehash and captcha:
            raw_payload += (
                f"&seccodehash={seccodehash}"
                f"&seccodemodid=member::logging"
                f"&seccodeverify={captcha}"
            )
        raw_payload += "&cookietime=2592000"
        # URL 编码（保留 = 和 &）
        payload = urllib.parse.quote(raw_payload, safe="=&")

        resp = session.post(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Host": host,
                "Referer": f"https://{host}/home.php?mod=spacecp&ac=credit&showcredit=1",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=payload,
            timeout=15,
        )
        resp.raise_for_status()

        # Discuz! AJAX 登录返回 CDATA 包裹的 JavaScript
        m = re.search(r"<!\[CDATA\[(.*?)\]\]>", resp.text)
        if m:
            text = m.group(1)
            if "欢迎您回来" in text:
                # 尝试提取用户等级和昵称
                user_m = re.search(r'<font color="#00FFCC">(.*?)</font>\s*(.*)', text)
                if user_m:
                    level = user_m.group(1)
                    nickname = user_m.group(2).strip()
                    print(f"✅ [登录] 登录成功！等级: {level}，昵称: {nickname}")
                else:
                    name_m = re.search(r"欢迎您回来，(.*?)，现在将转入", text)
                    name = name_m.group(1) if name_m else email
                    print(f"✅ [登录] 登录成功！用户: {name}")
                return True
            else:
                print(f"❌ [登录] 登录失败，响应: {text[:200]}")
                return False
        else:
            print(f"❌ [登录] 响应格式异常: {resp.text[:200]}")
            return False
    except Exception as e:
        # 网络/SSL 类错误上抛（上层切换域名重试）；其余按登录失败处理
        if _is_network_error(e):
            raise
        print(f"❌ [登录] 异常: {e}")
        return False


def login_with_retry(
    host: str,
    email: str,
    password: str,
    session: requests.Session,
    debug: bool,
) -> bool:
    """
    完整登录流程。seccodehash 不存在时跳过验证码；存在时执行 OCR 识别与重试。
    返回 True 表示登录成功，session 中已包含有效 Cookie。
    """
    # 步骤 1：获取登录页参数
    print("   📋 获取登录参数...")
    # 提取失败会抛 LoginParamError（由上层切换到备用域名重试），此处返回时必含 formhash/loginhash
    formhash, seccodehash, loginhash = fetch_login_params(host, session, debug)

    captcha_text = None

    # 步骤 2：如果有 seccodehash，则需要验证码
    if seccodehash:
        print("   🔐 需要验证码，开始 OCR 识别...")
        for attempt in range(1, MAX_CAPTCHA_RETRY + 1):
            print(f"   🔐 验证码识别 第 {attempt}/{MAX_CAPTCHA_RETRY} 次...")

            img_b64 = fetch_captcha_image(host, seccodehash, session)
            if not img_b64:
                continue

            captcha_text = ocr_captcha(img_b64)
            if not captcha_text:
                print(f"   ⚠️ OCR 识别失败，重试...")
                rand_sleep(2000, 4000)
                continue
            print(f"   🔤 识别结果: {captcha_text}")

            if verify_captcha(host, captcha_text, seccodehash, session):
                print(f"   ✅ 验证码校验通过")
                break
            else:
                print(f"   ⚠️ 验证码校验不通过")
                if attempt < MAX_CAPTCHA_RETRY:
                    rand_sleep(3000, 6000)
        else:
            print("   ❌ 验证码识别重试耗尽，登录失败")
            return False
    else:
        print("   ℹ️ 无需验证码，直接登录")
        captcha_text = None

    # 步骤 3：执行登录
    print("   🔑 执行登录...")
    if not do_login(host, email, password, formhash, loginhash, session, seccodehash, captcha_text):
        return False

    # 步骤 4：缓存 Cookie
    cookie_str = get_session_cookies_str(session)
    if cookie_str:
        write_cookie_cache(email, cookie_str)
        print("   💾 Cookie 已缓存")

    return True


# ============================ Cookie 有效性 ============================

def check_cookie_valid(host: str, session: requests.Session) -> bool:
    """
    访问个人空间页检测 Cookie 是否仍有效。
    页面含"请先登录"表示已失效。
    """
    try:
        url = f"https://{host}{URL_CHECK_COOKIE}"
        resp = session.get(
            url,
            headers={"User-Agent": DEFAULT_UA, "Host": host},
            timeout=15,
        )
        resp.raise_for_status()
        if "请先登录" in resp.text:
            return False
        return True
    except Exception as e:
        # 网络/SSL 类错误上抛（上层会切换域名重试）；页面结构等业务异常按 Cookie 失效处理
        if _is_network_error(e):
            raise
        print(f"⚠️ [Cookie检测] 异常: {e}")
        return False


# ============================ 签到流程 ============================

def fetch_sign_hash(host: str, session: requests.Session, debug: bool) -> str | None:
    """
    GET 签到页面，提取 formhash。
    """
    try:
        url = f"https://{host}{URL_SIGN_PAGE}"
        resp = session.get(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Host": host,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15,
        )
        resp.raise_for_status()
        if debug:
            print(f"   [DEBUG] 签到页 HTTP {resp.status_code}, 长度 {len(resp.text)}")

        m = re.search(r"formhash=([a-zA-Z0-9]{8})", resp.text)
        if m:
            sign_hash = m.group(1)
            if debug:
                print(f"   [DEBUG] 签到 formhash={sign_hash}")
            return sign_hash

        # 页面含"请先登录" → Cookie 已失效，属于业务失败，不切换域名
        if "请先登录" in resp.text:
            print("   ❌ [签到参数] 未登录，Cookie 已失效")
            return None
        # 不含 formhash 且未提示未登录 → 疑似当前镜像域名异常，切换备用域名重试
        print("   ⚠️ [签到参数] 无法提取签到 formhash，疑似域名问题，尝试切换备用域名")
        raise LoginParamError("无法提取签到 formhash")
    except LoginParamError:
        raise
    except Exception as e:
        # 网络/SSL 类错误上抛（上层切换域名重试）；解析类异常返回 None 按业务失败处理
        if _is_network_error(e):
            raise
        print(f"   ❌ [签到参数] 解析异常: {e}")
        return None


def do_signin(host: str, sign_hash: str, session: requests.Session, debug: bool) -> dict:
    """
    执行签到请求。返回 {"status": ..., "message": ...}

    status 取值：
      success    — 签到成功，获得奖励
      already    — 今日已签到（幂等成功）
      fail       — 签到失败
    """
    try:
        url = f"https://{host}{URL_SIGN_DO.format(formhash=sign_hash)}"
        resp = session.get(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Host": host,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15,
        )
        resp.raise_for_status()
        text = resp.text
        if debug:
            print(f"   [DEBUG] 签到响应 HTTP {resp.status_code}: {text[:300]}")

        # 解析 CDATA 内容
        m = re.search(r"<!\[CDATA\[(.*?)\]\]>", text, re.DOTALL)
        inner = m.group(1) if m else text

        # 签到成功
        if "签到成功" in inner:
            reward_m = re.search(r"获得随机奖励\s*(.*?)\s*车票\s*和\s*(.*?)\s*[。.]", inner)
            if reward_m:
                msg = f"签到成功，获得 {reward_m.group(1)} 车票和 {reward_m.group(2)}"
            else:
                msg = "签到成功"
            # 附加签到统计
            stats = fetch_sign_stats(host, session, debug)
            if stats:
                msg += f"\n      📊 {stats}"
            return {"status": "success", "message": msg}

        # 今日已签到
        if "今日已签" in inner:
            stats = fetch_sign_stats(host, session, debug)
            msg = "今日已签到"
            if stats:
                msg += f"\n      📊 {stats}"
            return {"status": "already", "message": msg}

        # 其他失败
        snippet = inner.strip()[:100] if inner.strip() else "空响应"
        return {"status": "fail", "message": f"签到失败: {snippet}"}

    except Exception as e:
        # 网络/SSL 类错误上抛（上层切换域名重试）；其余按签到失败处理
        if _is_network_error(e):
            raise
        return {"status": "fail", "message": f"签到请求异常: {e}"}


def fetch_sign_stats(host: str, session: requests.Session, debug: bool) -> str:
    """
    签到后访问签到页，提取签到统计数据用于展示。
    解析 <ul class="countqian cl"> 中的 #lxdays #lxlevel #lxreward #lxtdays。
    返回格式化字符串，如 "连续 1 天 | Lv.1 | 积分 130 | 累计 2 天"，失败返回空字符串。
    """
    try:
        url = f"https://{host}{URL_SIGN_PAGE}"
        resp = session.get(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Host": host,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15,
        )
        resp.raise_for_status()
        text = resp.text

        parts = []
        # 连续签到天数
        m = re.search(r'id="lxdays"\s+value="(\d+)"', text)
        if m:
            parts.append(f"连续 {m.group(1)} 天")
        # 签到等级
        m = re.search(r'id="lxlevel"\s+value="(\d+)"', text)
        if m:
            parts.append(f"Lv.{m.group(1)}")
        # 积分奖励
        m = re.search(r'id="lxreward"\s+value="(\d+)"', text)
        if m:
            parts.append(f"积分 {m.group(1)}")
        # 累计总天数
        m = re.search(r'id="lxtdays"\s+value="(\d+)"', text)
        if m:
            parts.append(f"累计 {m.group(1)} 天")

        if parts:
            stats = "  │  ".join(parts)
            if debug:
                print(f"   [DEBUG] 签到统计: {stats}")
            return stats
    except Exception as e:
        if debug:
            print(f"   [DEBUG] 获取签到统计异常: {e}")
    return ""


# ============================ 单账号执行 ============================

# SSL/连接类错误关键词（切换域名可恢复）
_NETWORK_ERR_KEYWORDS = ("SSLError", "SSLEOFError", "ConnectionError", "MaxRetryError",
                         "Timeout", "timed out", "connection refused", "Connection refused",
                         "EOF occurred", "UNEXPECTED_EOF", "Connection aborted",
                         "FileNotFoundError", "getaddrinfo failed", "Connection reset",
                         "RemoteDisconnected")


def _is_network_error(err: Exception) -> bool:
    """判断是否为网络/SSL 类错误（切换域名可能恢复），而非业务逻辑错误。"""
    msg = str(err)
    return any(kw in msg for kw in _NETWORK_ERR_KEYWORDS)


def _try_signin_with_host(
    account: dict,
    host: str,
    cookie_cache: dict,
    debug: bool,
    proxy: str,
) -> dict | None:
    """
    在指定 host 上尝试完成签到。返回结果 dict；网络/域名异常（含登录参数提取失败）返回 None（可换域名重试）。
    """
    label = account["label"]

    session = requests.Session()
    session.verify = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    # ---- 方式 A：Cookie 模式 ----
    if account["type"] == "cookie":
        cookie_str = account["cookie"]
        apply_cookies(session, cookie_str)

        try:
            if not check_cookie_valid(host, session):
                print(f"   ❌ [{host}] Cookie 已失效，请重新获取")
                return {"success": False, "message": "❌ Cookie 已失效，请重新获取", "label": label}
        except Exception as e:
            if _is_network_error(e):
                print(f"   ⚠️ [{host}] Cookie 检测网络异常: {e}")
                return None  # 换域名重试
            raise

        print(f"   ✅ [{host}] Cookie 有效")

        try:
            sign_hash = fetch_sign_hash(host, session, debug)
        except LoginParamError as e:
            print(f"   ⚠️ [{host}] {e}，尝试切换备用域名...")
            return None
        except Exception as e:
            if _is_network_error(e):
                print(f"   ⚠️ [{host}] 获取签到参数网络异常: {e}")
                return None
            raise

        if not sign_hash:
            return {"success": False, "message": "❌ 获取签到参数失败", "label": label}

        try:
            result = do_signin(host, sign_hash, session, debug)
        except Exception as e:
            if _is_network_error(e):
                print(f"   ⚠️ [{host}] 签到请求网络异常: {e}")
                return None
            raise

        success = result["status"] in ("success", "already")
        emoji = "✅" if success else "❌"
        print(f"   {emoji} {result['message']}")
        return {"success": success, "message": f"{emoji} {result['message']}", "label": label}

    # ---- 方式 B：邮箱密码模式 ----
    if account["type"] == "credential":
        email = account["email"]
        password = account["password"]

        # 先检查 Cookie 缓存
        cached = cookie_cache.get(email)
        if cached:
            cached_cookie = cached.get("cookies", "")
            if cached_cookie:
                print("   📦 发现 Cookie 缓存，尝试复用...")
                apply_cookies(session, cached_cookie)
                try:
                    if check_cookie_valid(host, session):
                        print(f"   ✅ [{host}] 缓存 Cookie 有效，直接签到")
                        sign_hash = fetch_sign_hash(host, session, debug)
                        if sign_hash:
                            result = do_signin(host, sign_hash, session, debug)
                            success = result["status"] in ("success", "already")
                            emoji = "✅" if success else "❌"
                            print(f"   {emoji} {result['message']}")
                            return {"success": success, "message": f"{emoji} {result['message']}", "label": label}
                except LoginParamError as e:
                    # 签到参数提取失败疑似域名异常 → 直接切换备用域名，无需在同一域名上重新登录
                    print(f"   ⚠️ [{host}] {e}，尝试切换备用域名...")
                    return None
                except Exception as e:
                    if _is_network_error(e):
                        print(f"   ⚠️ [{host}] 缓存 Cookie 检测网络异常，尝试重新登录...")
                    else:
                        print(f"   ⚠️ 缓存 Cookie 已失效，将重新登录...")
                # Cookie 失效或网络错误 → 继续走登录流程

        # 执行登录
        print(f"   🔐 [{host}] 使用邮箱密码登录...")
        try:
            login_ok = login_with_retry(host, email, password, session, debug)
        except LoginParamError as e:
            print(f"   ⚠️ [{host}] {e}，尝试切换备用域名...")
            return None
        except Exception as e:
            if _is_network_error(e):
                print(f"   ⚠️ [{host}] 登录网络异常: {e}")
                return None
            raise

        if not login_ok:
            return {"success": False, "message": "❌ 登录失败", "label": label}

        # 签到
        try:
            sign_hash = fetch_sign_hash(host, session, debug)
        except LoginParamError as e:
            print(f"   ⚠️ [{host}] {e}，尝试切换备用域名...")
            return None
        except Exception as e:
            if _is_network_error(e):
                print(f"   ⚠️ [{host}] 获取签到参数网络异常: {e}")
                return None
            raise

        if not sign_hash:
            return {"success": False, "message": "❌ 获取签到参数失败", "label": label}

        try:
            result = do_signin(host, sign_hash, session, debug)
        except Exception as e:
            if _is_network_error(e):
                print(f"   ⚠️ [{host}] 签到请求网络异常: {e}")
                return None
            raise

        success = result["status"] in ("success", "already")
        emoji = "✅" if success else "❌"
        print(f"   {emoji} {result['message']}")
        return {"success": success, "message": f"{emoji} {result['message']}", "label": label}

    return {"success": False, "message": "❌ 未知账号类型", "label": label}


def run_one_account(
    account: dict,
    hosts: list[str],
    cookie_cache: dict,
    debug: bool,
    proxy: str,
) -> dict:
    """
    对单个账号执行签到完整流程，支持多域名容错切换。
    返回 {"success": bool, "message": str, "label": str}
    """
    label = account["label"]
    print(f"\n{'─' * 50}")
    print(f"▶ {label} 开始处理...")
    print(f"{'─' * 50}")
    if debug:
        print(f"   [DEBUG] 代理: {proxy or '无'}")
        print(f"   [DEBUG] 域名列表: {hosts}")

    last_result = None
    for idx, host in enumerate(hosts):
        if idx > 0:
            print(f"   🔄 切换域名: {host}")
        try:
            result = _try_signin_with_host(account, host, cookie_cache, debug, proxy)
            if result is None:
                # 网络/域名异常（含登录参数提取失败），换下一个备用域名
                last_result = {"success": False, "message": f"⚠️ [{host}] 网络或域名不可用", "label": label}
                continue
            return result
        except Exception as e:
            # 非网络类的未知异常，也尝试换域名
            print(f"   ⚠️ [{host}] 未知异常: {e}")
            last_result = {"success": False, "message": f"❌ [{host}] {e}", "label": label}
            continue

    # 所有域名都失败
    if last_result:
        return last_result
    return {"success": False, "message": "❌ 所有域名均不可达", "label": label}


# ============================ 主入口 ============================

def main():
    title = "司机社签到"

    NOTIFY_ENABLED = env_bool("SIJISHE_NOTIFY", True)
    NOTIFY_ONLY_FAIL = env_bool("SIJISHE_NOTIFY_ONLY_FAIL", False)
    DEBUG = env_bool("SIJISHE_DEBUG", False)
    PROXY = (os.getenv("SIJISHE_PROXY") or "").strip()

    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    # 解析账号
    raw_accounts = (os.getenv("SIJISHE_ACCOUNTS") or "").strip()
    if not raw_accounts:
        print("❌ 未配置环境变量 SIJISHE_ACCOUNTS")
        print("   支持格式：邮箱&密码 或 Cookie，多账号用换行分隔")
        sys.exit(1)

    accounts = parse_accounts(raw_accounts)
    if not accounts:
        print("❌ SIJISHE_ACCOUNTS 解析后无有效账号")
        sys.exit(1)

    cred_count = sum(1 for a in accounts if a["type"] == "credential")
    cookie_count = sum(1 for a in accounts if a["type"] == "cookie")
    print(f"📋 共解析到 {len(accounts)} 个账号（邮箱密码: {cred_count}，Cookie: {cookie_count}）")

    # 加载 Cookie 缓存
    cookie_cache = read_cookie_cache()
    if cookie_cache:
        print(f"📦 已加载 {len(cookie_cache)} 条 Cookie 缓存")

    # 获取域名列表
    primary_host, all_hosts = get_latest_host(DEBUG)
    # 把首选域名放第一位，其余备用
    hosts = [primary_host] + [h for h in all_hosts if h != primary_host]
    print(f"\n▶ 本次签到域名: {primary_host}（另有 {len(hosts) - 1} 个备用）\n")

    # 串行签到
    results = []
    success_cnt = 0

    for i, acc in enumerate(accounts):
        t0 = time.time()
        try:
            result = run_one_account(acc, hosts, cookie_cache, DEBUG, PROXY)
        except Exception:
            traceback.print_exc()
            result = {
                "success": False,
                "message": f"❌ 脚本异常: {traceback.format_exc(limit=1)}",
                "label": acc["label"],
            }
        cost = time.time() - t0
        if DEBUG:
            print(f"   ⏱️ 耗时 {cost:.1f}s")
        if result.get("success"):
            success_cnt += 1
        results.append(result)

        # 多账号间隔（2-6 秒随机）
        if i < len(accounts) - 1:
            gap_s = random.randint(2, 6)
            if DEBUG:
                print(f"\n⏳ 账号间隔 {gap_s}s...")
            time.sleep(gap_s)

    # 汇总报告
    total = len(accounts)
    fail_cnt = total - success_cnt

    if success_cnt == total:
        report_title = f"✅ {title} 全部成功（{success_cnt}/{total}）"
    elif success_cnt == 0:
        report_title = f"❌ {title} 全部失败（0/{total}）"
    else:
        report_title = f"⚠️ {title} 部分失败（{success_cnt}/{total}）"

    # 构造通知正文（Markdown）
    ok_lines = [
        f"- **[{r['label']}]** {r['message']}"
        for r in results if r["success"]
    ]
    fail_lines = [
        f"- **[{r['label']}]** {r['message']}"
        for r in results if not r["success"]
    ]

    lines = [f"# {report_title} - 执行报告", ""]
    lines.append(f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"🌐 签到域名: `{primary_host}`")
    lines.append(f"📊 总计 {total} 个账号，✅ 成功 {success_cnt}，❌ 失败 {fail_cnt}")
    lines.append("")
    if ok_lines:
        lines.append("## ✅ 成功")
        lines.extend(ok_lines)
        lines.append("")
    if fail_lines:
        lines.append("## ❌ 失败")
        lines.extend(fail_lines)
        lines.append("")
    report = "\n".join(lines).strip()

    print("\n" + report)

    # 推送
    if NOTIFY_ENABLED:
        if NOTIFY_ONLY_FAIL and fail_cnt == 0:
            print("ℹ️ SIJISHE_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(report_title, report)
    else:
        print("ℹ️ SIJISHE_NOTIFY=false，已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
