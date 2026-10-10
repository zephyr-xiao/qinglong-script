# -*- coding: utf-8 -*-
"""
new Env('司机社签到');
cron: 10 9,20 * * *

司机社（多镜像域名，域名列表见 KNOWN_HOSTS）自动签到脚本。
支持双模式：Cookie 直接签到 或 邮箱密码 + 本地 OCR 验证码登录。
适配青龙面板，多账号串行签到，Cookie 自动缓存复用。
OCR 使用本地 ddddocr 库，无需部署外部服务。

域名发现：SIJISHE_HOST 手动锁定 > 内置实测可用域名列表；
两者都失败时才用 Playwright 渲染发布页兜底（可选依赖，非必需）。
本次运行中首个签到成功的域名会被提升为后续账号的首选，减少无效重试。

环境变量：
  SIJISHE_ACCOUNTS         必填，账号列表，支持三种格式：
                              1) 邮箱&密码：user@mail.com&mypassword
                              2) Cookie 字符串：直接填入浏览器抓取的完整 Cookie
                              3) 混合模式：不同行可以不同格式
                              ★ 多账号用换行分隔
  SIJISHE_NOTIFY           true/false  默认 true，是否调用青龙 notify.py 推送
  SIJISHE_NOTIFY_ONLY_FAIL true/false  默认 false，全部成功时静默
  SIJISHE_PROXY            代理地址    默认空，如 http://172.17.0.1:7890
  SIJISHE_HOST             可选，手动锁定签到域名（如 dlsjs.net）；
                             留空则使用内置实测可用域名列表，
                             全部失败时再用 Playwright 渲染发布页兜底
  SIJISHE_DEBUG            true/false  默认 false，输出接口响应细节用于排错

依赖：requests, beautifulsoup4；ddddocr（仅验证码防御路径需要）；
      playwright（可选，仅域名发现兜底需要，需额外安装 chromium）

定时建议：一天两次（9:00 和 20:00），避免签到遗漏
cron: 10 9,20 * * *

作者: zephyr_xiao
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

GUIDE_URL = "https://47447.net/"          # 发布页（返回 403 + Cloudflare 挑战，只能 Playwright 渲染）
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

# 同一账号连续登录失败多少次就中止重试。
# Discuz 的 seccode 是按登录失败次数触发的，连续失败会把账号推进验证码流程，
# 因此失败两次即停，不再继续遍历备用域名。
MAX_LOGIN_FAILURES = 2


# ============================ 通用工具函数 ============================

class LoginParamError(Exception):
    """登录/签到参数提取失败，疑似域名失效或反爬拦截，应切换备用域名重试。"""


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


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
    多账号用换行分隔，空行自动跳过。

    判定规则：Cookie 串必带分隔符（`;` 或 `=`），故先按「含 ; 或 =」判为 Cookie，
    否则才按「含 @ 且含 &」判为邮箱密码——避免含 @ 的 Cookie 值被误判成凭据账号。
    """
    if not raw or not raw.strip():
        return []

    # 按换行切分
    lines = [line.strip() for line in raw.strip().split("\n") if line.strip()]
    accounts = []

    for i, line in enumerate(lines):
        looks_like_cookie = (";" in line) or ("=" in line)
        # 不含 Cookie 分隔符、且同时含 @ 与 & → 邮箱密码模式
        if (not looks_like_cookie) and ("@" in line) and ("&" in line):
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

# 内置候选域名（2026-10-10 匿名实测：均能直接返回 Discuz 登录页；顺序按发布页优先级）
# 已移除的失效域名：xsijishe.ink、sjslt.cc（已变成 JS 跳转页，不再提供 Discuz 页面）、
#                   sjs47.com（DNS 解析与 SSL 握手均失败）
KNOWN_HOSTS = [
    "xsijishe.net",
    "dlsjs.net",
    "sjs96.com",
    "dlsjs.com",
    "sjs66.net",
    "xsijishe.com",
    "sjs66.com",
]


def _fetch_hosts_via_playwright() -> list[str]:
    """
    使用 Playwright 真浏览器渲染发布页，绕过 Cloudflare 获取最新域名列表。
    仅在「内置候选域名全部失败」时作为兜底调用一次（未安装 Playwright 时直接返回空）。
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


def get_hosts(debug: bool) -> tuple[list[str], str]:
    """
    获取候选签到域名列表（按优先级排序），并返回来源说明。

    优先级：SIJISHE_HOST 手动锁定 > 内置实测可用列表。
    Playwright 不再每次启动就渲染发布页（成本高、青龙需装 chromium），
    只在「所有候选域名都失败」时由 run_one_account 兜底调用一次。
    """
    manual_host = (os.getenv("SIJISHE_HOST") or "").strip()
    if manual_host:
        print(f"[域名加载] 使用手动指定的域名: {manual_host}")
        return [manual_host], "手动指定（SIJISHE_HOST）"

    print(f"[域名加载] 使用内置实测可用域名列表（{len(KNOWN_HOSTS)} 个候选）")
    return list(KNOWN_HOSTS), "内置列表"


def _order_hosts(hosts: list[str], preferred: str | None) -> list[str]:
    """把本次运行已确认可用的域名提到最前，避免每个账号都从头重试一遍。"""
    if preferred and preferred in hosts:
        return [preferred] + [h for h in hosts if h != preferred]
    return list(hosts)


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


def _chmod_private(path: Path):
    """把缓存文件权限收紧到仅属主可读写（Windows 等非 POSIX 环境静默跳过）。"""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def write_cookie_cache(email: str, cookie_str: str):
    """写入/更新指定账号的 Cookie 缓存，并把文件权限收紧到 0600。"""
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
        _chmod_private(COOKIE_FILE)
    except Exception as e:
        print(f"⚠️ 写入 Cookie 缓存异常: {e}")


def remove_cached_cookie(email: str):
    """只清除指定账号的 Cookie 缓存，保留其它账号（缓存确认失效时调用）。"""
    try:
        if not COOKIE_FILE.exists():
            return
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        accounts = data.get("accounts") or {}
        if email not in accounts:
            return
        del accounts[email]
        data["accounts"] = accounts
        data["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _chmod_private(COOKIE_FILE)
        print(f"🗑️ 已清除账号 {mask_email(email)} 的失效 Cookie 缓存")
    except Exception as e:
        print(f"⚠️ 删除账号 Cookie 缓存异常: {e}")


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


def parse_login_welcome(inner: str) -> str:
    """
    从登录成功响应里提取「等级 / 昵称」用于展示，提取不到返回空串。

    站点模板会改字体色值（实测已从 #00FFCC 变为 #2B8BD6），因此不写死色值；
    提取结果里的 HTML 标签一并剔除，避免把标签打进日志与通知。
    """
    m = re.search(r'<font color="#[0-9A-Fa-f]{6}">(.*?)</font>\s*([^<，,]*)', inner)
    if m:
        level = m.group(1).strip()
        nickname = m.group(2).strip()
        return f"等级: {level}，昵称: {nickname}" if nickname else f"等级: {level}"
    # 兜底：欢迎语里没有等级标签时，只取昵称
    m = re.search(r"欢迎您回来，([^，]*)", inner)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return ""


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
                # 等级/昵称随站点模板变化，交给 parse_login_welcome 统一提取
                who = parse_login_welcome(text)
                print(f"✅ [登录] 登录成功！{who}" if who else "✅ [登录] 登录成功！")
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

    # 步骤 2：验证码（防御路径）
    # 2026-10-10 实测：登录页不返回 seccodehash（页面内 seccode/verify 关键字出现 0 次），
    # 正常登录无需验证码。但 Discuz 的 seccode 按登录失败次数触发，账号一旦被推进
    # 验证码流程就会走到这里，因此保留 OCR 链路（ddddocr 惰性导入，纯 Cookie 模式不触发）。
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
        # 响应原文完整落日志（不截断）：成功分支文案尚未真机实测钉死，
        # 保留原文便于事后核对站点文案变化（见 README「已知限制与待验证」）。
        print(f"   📥 [签到响应] HTTP {resp.status_code} 原文: {text}")

        # 解析 CDATA 内容
        m = re.search(r"<!\[CDATA\[(.*?)\]\]>", text, re.DOTALL)
        inner = m.group(1) if m else text

        # 签到成功（⚠️ 下面两个字面量与奖励正则属历史值：2026-10-10 评审实测签到页
        # HTML 中不存在「签到成功」「获得随机奖励」字样，即成功分支文案从未被真机验证。
        # 下次签到成功时请按日志里的响应原文核对，确认后把文案钉死在此处）
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

# SSL/连接类错误关键词（切换域名可恢复）。统一小写做大小写不敏感匹配：
# urllib3/requests 的异常消息大小写不定（如 ReadTimeout 与 "Read timed out"）。
_NETWORK_ERR_KEYWORDS = ("sslerror", "ssleoferror", "connectionerror", "maxretryerror",
                         "timeout", "timed out", "connection refused",
                         "eof occurred", "unexpected_eof", "connection aborted",
                         "filenotfounderror", "getaddrinfo failed", "connection reset",
                         "remotedisconnected")


def _is_network_error(err: Exception) -> bool:
    """判断是否为网络/SSL 类错误（切换域名可能恢复），而非业务逻辑错误。"""
    msg = str(err).lower()
    return any(kw in msg for kw in _NETWORK_ERR_KEYWORDS)


def _result(success: bool, reason: str, message: str, label: str, host: str) -> dict:
    """
    统一构造账号结果。

    reason 供上层区分「可重试的登录失败」与「业务失败」，取值：
      ok              签到成功或今日已签
      login_failed    登录失败（连续 2 次即熔断，避免触发站点验证码）
      cookie_invalid  提供的 Cookie 已失效
      sign_param_failed  拿不到签到 formhash
      sign_failed     签到接口明确返回失败
      unknown_type    账号类型无法识别
    """
    return {"success": success, "reason": reason, "message": message,
            "label": label, "host": host}


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
                return _result(False, "cookie_invalid", "❌ Cookie 已失效，请重新获取", label, host)
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
            return _result(False, "sign_param_failed", "❌ 获取签到参数失败", label, host)

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
        return _result(success, "ok" if success else "sign_failed",
                       f"{emoji} {result['message']}", label, host)

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
                cache_invalid = False
                try:
                    if check_cookie_valid(host, session):
                        print(f"   ✅ [{host}] 缓存 Cookie 有效，直接签到")
                        sign_hash = fetch_sign_hash(host, session, debug)
                        if sign_hash:
                            result = do_signin(host, sign_hash, session, debug)
                            success = result["status"] in ("success", "already")
                            emoji = "✅" if success else "❌"
                            print(f"   {emoji} {result['message']}")
                            return _result(success, "ok" if success else "sign_failed",
                                           f"{emoji} {result['message']}", label, host)
                        # 签到页提示未登录 → 缓存确已失效
                        cache_invalid = True
                    else:
                        cache_invalid = True
                except LoginParamError as e:
                    # 签到参数提取失败疑似域名异常 → 直接切换备用域名，无需在同一域名上重新登录
                    print(f"   ⚠️ [{host}] {e}，尝试切换备用域名...")
                    return None
                except Exception as e:
                    if _is_network_error(e):
                        print(f"   ⚠️ [{host}] 缓存 Cookie 检测网络异常，尝试重新登录...")
                    else:
                        print(f"   ⚠️ [{host}] 缓存 Cookie 检测异常，尝试重新登录...")

                # 缓存确认失效才清（网络异常时缓存未必失效，保留它）
                if cache_invalid:
                    remove_cached_cookie(email)
                # 缓存 Cookie 失效或网络错误 → 继续走登录流程

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
            return _result(False, "login_failed", "❌ 登录失败", label, host)

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
            return _result(False, "sign_param_failed", "❌ 获取签到参数失败", label, host)

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
        return _result(success, "ok" if success else "sign_failed",
                       f"{emoji} {result['message']}", label, host)

    return _result(False, "unknown_type", "❌ 未知账号类型", label, host)


def run_one_account(
    account: dict,
    hosts: list[str],
    cookie_cache: dict,
    debug: bool,
    proxy: str,
    run_state: dict | None = None,
) -> dict:
    """
    对单个账号执行签到完整流程，支持多域名容错切换。

    run_state 为本次运行的跨账号共享状态：
      preferred_host   本次运行已确认可用的域名，会提升为后续账号的首选（运行时学习）
      playwright_tried 是否已用 Playwright 兜底找过新域名（每次运行只做一次）
      allow_fallback   是否允许 Playwright 兜底（手动锁定 SIJISHE_HOST 时为 False）

    返回 {"success": bool, "reason": str, "message": str, "label": str, "host": str}
    """
    run_state = run_state if run_state is not None else {}
    run_state.setdefault("playwright_tried", False)
    run_state.setdefault("preferred_host", None)
    run_state.setdefault("allow_fallback", not bool((os.getenv("SIJISHE_HOST") or "").strip()))

    label = account["label"]
    print(f"\n{'─' * 50}")
    print(f"▶ {label} 开始处理...")
    print(f"{'─' * 50}")
    if debug:
        print(f"   [DEBUG] 代理: {proxy or '无'}")
        print(f"   [DEBUG] 候选域名: {hosts}")

    ordered = _order_hosts(hosts, run_state.get("preferred_host"))
    login_failures = 0
    last_result = None

    while True:
        for idx, host in enumerate(ordered):
            if idx > 0:
                print(f"   🔄 切换域名: {host}")
            try:
                result = _try_signin_with_host(account, host, cookie_cache, debug, proxy)
            except Exception as e:
                # 非网络类的未知异常，也尝试换域名
                print(f"   ⚠️ [{host}] 未知异常: {e}")
                last_result = _result(False, "exception", f"❌ [{host}] {e}", label, host)
                continue

            if result is None:
                # 网络/域名异常（含登录参数提取失败），换下一个备用域名
                last_result = _result(False, "host_unreachable", f"⚠️ [{host}] 网络或域名不可用", label, host)
                continue

            if result.get("success"):
                # 运行时学习：记录可用域名，后续账号直接优先使用
                run_state["preferred_host"] = host
                return result

            if result.get("reason") == "login_failed":
                login_failures += 1
                last_result = result
                if login_failures >= MAX_LOGIN_FAILURES:
                    print(f"   🛑 连续 {login_failures} 次登录失败，中止该账号重试"
                          f"（避免把账号推进站点验证码流程）")
                    aborted = dict(result)
                    aborted["reason"] = "login_aborted"
                    aborted["message"] = (f"❌ 连续 {login_failures} 次登录失败，已中止重试"
                                          f"（避免触发站点验证码）")
                    return aborted
                continue

            # 其它业务失败（Cookie 失效、签到接口明确失败等）→ 换域名无意义，直接返回
            return result

        # 所有候选域名都试过：必要时用 Playwright 兜底找一次新域名
        if run_state.get("allow_fallback") and not run_state.get("playwright_tried"):
            run_state["playwright_tried"] = True
            fresh_hosts = _fetch_hosts_via_playwright()
            new_hosts = [h for h in fresh_hosts if h not in ordered]
            if new_hosts:
                print(f"   🎭 候选域名全部不可用，Playwright 兜底取得新域名: {', '.join(new_hosts)}")
                ordered = new_hosts
                continue
        break

    if last_result:
        return last_result
    return _result(False, "all_hosts_failed", "❌ 所有域名均不可达", label,
                   hosts[0] if hosts else "")


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

    # 获取候选域名列表
    hosts, host_source = get_hosts(DEBUG)
    print(f"\n▶ 本次签到域名: {hosts[0]}（共 {len(hosts)} 个候选，来源: {host_source}）\n")

    # 串行签到
    results = []
    success_cnt = 0
    # 跨账号共享状态：域名排序学习 + Playwright 兜底只做一次
    run_state = {"preferred_host": None, "playwright_tried": False}

    for i, acc in enumerate(accounts):
        t0 = time.time()
        try:
            result = run_one_account(acc, hosts, cookie_cache, DEBUG, PROXY, run_state)
        except Exception:
            traceback.print_exc()
            result = _result(False, "script_error",
                             f"❌ 脚本异常: {traceback.format_exc(limit=1)}",
                             acc["label"], hosts[0] if hosts else "")
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
    used_host = run_state.get("preferred_host") or (hosts[0] if hosts else "未知")
    lines.append(f"🌐 签到域名: `{used_host}`")
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
