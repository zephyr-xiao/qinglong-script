# -*- coding: utf-8 -*-
"""
new Env('尚香书苑签到');
cron: 23 8 * * *

尚香书苑（sxsy45.com）Discuz! X3.5 论坛 k_misign 插件每日签到。
登录强制图片验证码（Discuz seccode，idhash 固定为 cS），本地 ddddocr 自动识别；
k_misign 签到需通过算术验证（"签到验证：A - B = ?"），脚本解析后自动计算二次请求；
登录 Cookie JSON 缓存复用，失效自动重新登录。

环境变量：
  SXSY_ACCOUNTS          账号列表：用户名#密码，多账号用 & 或换行分隔；
                         也支持直接填 Cookie 字符串（不含 # 时按 Cookie 处理，不参与缓存）
  SXSY_NOTIFY            true/false 是否推送，默认 true
  SXSY_NOTIFY_ONLY_FAIL  true/false 仅失败时推送，默认 false
  SXSY_TIMEOUT           秒    请求超时，默认 30
  SXSY_DEBUG             true/false 调试模式，默认 false
  SXSY_PROXY             HTTP 代理（可选，如 http://172.17.0.1:7890）
依赖：requests 必装；ddddocr 仅账密登录时需要（Cookie 模式无需安装）
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
from urllib.parse import quote

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


# ---------------- 站点常量（均为 2026-09-29 实测） ----------------
BASE = "https://sxsy45.com"
SITE_NAME = "尚香书苑"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MODID = "member::logging"       # Discuz seccode 的 modid
SIGN_URL = f"{BASE}/plugin.php?id=k_misign:sign"
LOGIN_URL = f"{BASE}/member.php?mod=logging&action=login"
FORUM_URL = f"{BASE}/forum.php"

# 命名须匹配仓库 .gitignore 的 *_cookie.json 规则（含真实凭证，严禁入库）
COOKIE_CACHE = Path(__file__).resolve().parent / "sxsy45_cookie.json"
LOGIN_MAX_TRIES = 3             # 验证码 OCR 识别重试上限（含首次）

# 致命登录错误关键词：出现即停止重试（避免反复触发风控/封禁计数）
_LOGIN_FATAL_KEYWORDS = ("密码错误", "密码不正确", "登录失败次数过多", "账户被", "帐号被", "被锁定", "被禁止")


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


def solve_math(question: str):
    """解析 k_misign 算术验证题 '签到验证：9 + 15 = ?'，返回整数答案；解析失败返回 None。"""
    m = re.search(r"(\d+)\s*([+\-×xX*/÷])\s*(\d+)", question or "")
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op in ("×", "x", "X", "*"):
        return a * b
    if op in ("÷", "/"):
        if b == 0:
            return None
        return a // b if a % b == 0 else a / b
    return None


def fetch_sign_stats(html: str) -> str:
    """从签到页提取统计（连续天数/等级/奖励/累计天数），无统计返回空串。"""
    parts = []
    m = re.search(r'id="lxdays"\s+value="(\d+)"', html)
    if m:
        parts.append(f"连续 {m.group(1)} 天")
    m = re.search(r'id="lxlevel"\s+value="(\d+)"', html)
    if m:
        parts.append(f"Lv.{m.group(1)}")
    m = re.search(r'id="lxreward"\s+value="(\d+)"', html)
    if m:
        parts.append(f"积分+{m.group(1)}")
    m = re.search(r'id="lxtdays"\s+value="(\d+)"', html)
    if m:
        parts.append(f"累计 {m.group(1)} 天")
    return "  │  ".join(parts)


def build_session(proxy: str, timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": BASE + "/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    s._timeout = timeout
    return s


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
    data = {}
    if COOKIE_CACHE.exists():
        try:
            with open(COOKIE_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data.setdefault("accounts", {})[username] = {
        "cookies": cookie_str,
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(COOKIE_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Cookie 缓存写入失败: {e}")


# ---------------- 登录层 ----------------
def check_cookie_valid(session: requests.Session, debug: bool = False) -> bool:
    """GET forum.php 判登录态：discuz_uid > 0 即有效。"""
    try:
        r = session.get(FORUM_URL, timeout=session._timeout)
    except Exception as e:
        print(f"⚠️ Cookie 检测请求失败: {e}")
        return False
    m = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
    uid = m.group(1) if m else "0"
    if debug:
        print(f"    [debug] forum.php uid={uid}")
    return uid not in ("", "0")


def _ocr(session: requests.Session, idhash: str, ocr, debug: bool) -> str:
    """下载 seccode 图片并识别一次，返回识别文本。"""
    r = session.get(
        f"{BASE}/misc.php?mod=seccode&update={time.time()}&idhash={idhash}",
        timeout=session._timeout,
    )
    if debug:
        print(f"    [debug] 验证码图片 HTTP {r.status_code}, {len(r.content)}B")
    return ocr.classification(r.content)


def discuz_login(session: requests.Session, username: str, password: str,
                 debug: bool = False) -> tuple:
    """账密登录（含 seccode OCR）。返回 (是否成功, 消息)。成功时会话已带登录 Cookie。"""
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(old=False)
    except ImportError:
        return False, "缺少 ddddocr 库：账密登录需要它识别验证码，请在「依赖管理」-「Python」安装 ddddocr（或改用 Cookie 模式）"

    login_referer = LOGIN_URL
    for attempt in range(1, LOGIN_MAX_TRIES + 1):
        try:
            r = session.get(login_referer, timeout=session._timeout)
        except Exception as e:
            return False, f"登录页请求失败: {e}"

        m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', r.text)
        formhash = m.group(1) if m else ""
        m = re.search(r"updateseccode\('([a-zA-Z0-9]+)'", r.text)
        idhash = m.group(1) if m else ""
        if not formhash:
            return False, "登录页无 formhash（可能站点改版或被风控拦截）"

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
            try:
                ru = session.get(
                    f"{BASE}/misc.php?mod=seccode&action=update&idhash={idhash}"
                    f"&{time.time()}&modid={MODID}&inajax=1",
                    timeout=session._timeout,
                )
            except Exception as e:
                return False, f"验证码 update 接口请求失败: {e}"
            hm = re.search(r'name="seccodehash"\s+type="hidden"\s+value="([a-zA-Z0-9]+)"', ru.text)
            seccodehash = hm.group(1) if hm else ""
            if not seccodehash:
                return False, "验证码 update 接口未返回 seccodehash（站点可能改版）"
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
        if "succeedhandle" in body or "succeedlocation" in body:
            return True, "登录成功"

        # 失败原因分类
        if any(kw in body for kw in _LOGIN_FATAL_KEYWORDS):
            err = re.search(r"show_error\('([^']{0,120})", body)
            return False, f"账号密码错误或账号受限，停止重试: {err.group(1) if err else '见日志'}"
        if "验证码" in body:
            print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次验证码识别错误，重试...")
            time.sleep(2)
            continue
        err = re.search(r"show_error\('([^']{0,120})", body)
        print(f"    ⚠️ 第 {attempt}/{LOGIN_MAX_TRIES} 次登录未成功: {err.group(1) if err else body[:150]!r}，重试...")
        time.sleep(2)

    return False, f"验证码连续 {LOGIN_MAX_TRIES} 次识别失败（本机 ddddocr 对该站识别率不足时请改用 Cookie 模式）"


# ---------------- 签到层 ----------------
def _get_sign_page(session: requests.Session, debug: bool = False) -> str:
    """GET 签到页，返回 HTML；请求异常返回空串。"""
    try:
        r = session.get(SIGN_URL, timeout=session._timeout,
                        headers={"Referer": FORUM_URL})
        if debug:
            print(f"    [debug] 签到页 HTTP {r.status_code}, {len(r.text)}B")
        return r.text
    except Exception as e:
        print(f"⚠️ 签到页请求失败: {e}")
        return ""


def do_signin(session: requests.Session, debug: bool = False) -> dict:
    """
    k_misign 签到。流程（均为实测）：
      1. GET 签到页：有 id="JD_sign" -> 今天未签；无 -> 已签/未登录
      2. GET ...&operation=qiandao&formhash=<fh>&format=text -> 算术题 XML
      3. 计算答案后 GET ...&mathverify_answer=<ans> -> 结果 XML（CDATA 文本）
      4. 回查签到页确认 JD_sign 消失
    返回 {"state": signed_today|success|failed, "message": str, "stats": str}
    """
    html = _get_sign_page(session, debug)
    if not html:
        return {"state": "failed", "message": "签到页请求失败", "stats": ""}

    if 'id="JD_sign"' not in html:
        # 无签到按钮：区分「已签」与「Cookie 失效被踢回登录页」
        if re.search(r'id="lxdays"\s+value="\d+"', html):
            return {"state": "signed_today", "message": "今日已签到", "stats": fetch_sign_stats(html)}
        if "member.php?mod=logging" in html or re.search(r"discuz_uid\s*=\s*'0'", html):
            return {"state": "failed", "message": "登录态失效（签到页被跳转登录）", "stats": ""}
        return {"state": "failed", "message": "签到页无签到按钮且无统计信息（页面结构可能变化）", "stats": ""}

    m = re.search(
        r'id="JD_sign"\s+href="plugin\.php\?id=k_misign:sign&operation=qiandao'
        r'&formhash=([a-f0-9]+)&format=text"', html)
    if not m:
        return {"state": "failed", "message": "未解析到 JD_sign 签到链接", "stats": ""}
    formhash = m.group(1)

    sign_headers = {"Referer": SIGN_URL, "X-Requested-With": "XMLHttpRequest"}
    try:
        r = session.get(
            f"{SIGN_URL}&operation=qiandao&formhash={formhash}&format=text",
            timeout=session._timeout, headers=sign_headers)
    except Exception as e:
        return {"state": "failed", "message": f"签到接口请求失败: {e}", "stats": ""}

    # 第一次返回：算术题（站点开启 mathverify 时）；部分站点关闭验证时直接返回结果
    q = re.search(r'var q="([^"]*)"', r.text)
    if not q:
        cdata = re.search(r"<!\[CDATA\[(.*?)\]\]>", r.text, re.S)
        text = cdata.group(1).strip() if cdata else r.text[:150]
        return {"state": "failed",
                "message": f"签到接口未返回算术验证题，原始返回: {text[:100]}",
                "stats": fetch_sign_stats(_get_sign_page(session, debug))}

    answer = solve_math(q.group(1))
    if answer is None:
        return {"state": "failed", "message": f"算术验证题解析失败: {q.group(1)[:50]}", "stats": ""}
    if debug:
        print(f"    [debug] 算术题: {q.group(1)} -> {answer}")

    try:
        r2 = session.get(
            f"{SIGN_URL}&operation=qiandao&formhash={formhash}&format=text"
            f"&mathverify_answer={quote(str(answer))}",
            timeout=session._timeout, headers=sign_headers)
    except Exception as e:
        return {"state": "failed", "message": f"签到确认请求失败: {e}", "stats": ""}

    cdata = re.search(r"<!\[CDATA\[(.*?)\]\]>", r2.text, re.S)
    result_text = cdata.group(1).strip() if cdata else r2.text[:150]

    # 以页面回查为准做最终状态判定（返回文本因插件版本而异，不可靠）
    html2 = _get_sign_page(session, debug)
    stats = fetch_sign_stats(html2)
    if 'id="JD_sign"' not in html2:
        return {"state": "success", "message": f"签到成功（{result_text[:40]}）", "stats": stats}
    # JD_sign 仍在：答案错误（插件会重新出题）或其他失败
    return {"state": "failed", "message": f"签到未生效，接口返回: {result_text[:100]}", "stats": stats}


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
            if check_cookie_valid(session, debug):
                print("    💾 缓存 Cookie 有效，跳过登录")
            else:
                print("    ⚠️ 缓存 Cookie 失效，重新登录")
                session.cookies.clear()

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
    proxy, proxy_src = "", ""
    for name in ("SXSY_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        v = (os.getenv(name) or "").strip()
        if v:
            proxy, proxy_src = v, name
            break

    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    if proxy:
        print(f"🌐 代理: {proxy} （来源: {proxy_src}）")
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
