# -*- coding: utf-8 -*-
"""
new Env('ZodGame签到');
cron: 30 9 * * *

ZodGame 论坛(zodgame.xyz,Discuz X3.4 + Cloudflare)自动收益脚本。
每日两项收益一次跑完:
  1. 每日签到(dsu_paulsign 插件):随机心情签到,得酱油奖励
  2. BUX 广告任务(jnbux 插件):逐个完成广告任务,每个 +2 点币
     (点币仅累积,不做兑换)

认证方式:仅 Cookie 模式(浏览器 F12 抓取完整 Cookie)。
Cookie 失效 / 被 Cloudflare 拦截 / 网络异常 / 页面改版 四类情况分开判定与告警。

环境变量:
  ZODGAME_COOKIE            必填,完整 Cookie 字符串,多账号用换行分隔
  ZODGAME_PROXY             可选,HTTP 代理;留空时自动回退青龙全局代理
                            (HTTPS_PROXY / HTTP_PROXY / ALL_PROXY),仍为空则直连
  ZODGAME_NOTIFY            true/false 默认 true,是否调用青龙 notify.py 推送
  ZODGAME_NOTIFY_ONLY_FAIL  true/false 默认 false,全部成功时静默
  ZODGAME_DEBUG             true/false 默认 false,输出接口响应细节并落盘
  ZODGAME_DEBUG_DIR         可选,调试产物目录,默认脚本同目录 _zodgame_debug/
  ZODGAME_AD_SECONDS        可选,强制广告观看秒数;留空则从广告页真实倒计时解析

命令行:
  python3 zodgame_checkin.py           正常签到
  python3 zodgame_checkin.py --check   自检:不签到、不执行广告任务,只验环境与登录态

依赖:requests

作者: zephyr_xiao
"""

import os
import random
import re
import sys
import time
import traceback
import urllib.parse
from datetime import datetime
from html import unescape as html_unescape

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

# ============================ 常量 ============================

BASE_URL = "https://zodgame.xyz"
SIGN_PAGE_URL = f"{BASE_URL}/plugin.php?id=dsu_paulsign:sign"
CHECKIN_URL = SIGN_PAGE_URL + "&operation=qiandao&infloat=1&inajax=1"
JNBUX_URL = f"{BASE_URL}/plugin.php?id=jnbux"
JNBUX_ACTION_URL = f"{BASE_URL}/plugin.php?id=jnbux:jnbux"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 签到页 title 特征:有效/失效(失效时整站 302 到登录页)
TITLE_VALID = "每日签到 -"
TITLE_INVALID = "登录 -"

# 已签到幂等特征(签到页正文;站点把"已签到"与"签到时间未开始"合并成同一句话)
ALREADY_SIGNED_MARK = "您今天已经签到过了"

# 签到接口返回(CDATA 包 HTML)里的成败特征
SIGN_SUCCESS_MARK = "恭喜你签到成功"
SIGN_DUPLICATE_MARKS = ("您今日已经签到", "请明天再来")
SIGN_FAILED_MARKS = ("签到失败", "权限不足", "参数错误", "非法")

# BUX:任务列表区块无任务时站点写死的文案(无任务与已全部完成共用同一句)
BUX_NO_TASK_MARK = "暂时没有广告任务可执行"

# BUX:未加入系统时,模板会渲染出带 do=join 的加入链接
BUX_NOT_JOINED_RE = re.compile(r"showWindow\('join',\s*'[^']*do=join", re.I)

# BUX:广告任务校验接口的成败特征(do=check 的响应)
BUX_CHECK_SUCCESS_MARK = "检查成功"
BUX_CHECK_FAIL_MARK = "检查失败"

# Cloudflare 挑战页特征
CF_MARKERS = (
    "Just a moment", "cf-mitigated", "cf_chl_opt", "__cf_chl",
    "Checking your browser", "Attention Required",
)

# dsu_paulsign 随机心情(discuz 签到插件的 9 种心情代号)
MOODS = ["kx", "ng", "ym", "wl", "nu", "ch", "fd", "yl", "shuai"]

# 网络重试
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5
REQUEST_TIMEOUT = 25

# 广告观看:优先用广告页真实倒计时,解析不到时用兜底秒数;统一加余量
AD_FALLBACK_SECONDS = 15
AD_MARGIN_SECONDS = 2

# 失败分类(用于汇总与推送分档)
CAT_COOKIE = "cookie_invalid"
CAT_CF = "cf_blocked"
CAT_NETWORK = "network"
CAT_STRUCTURE = "structure"
CAT_BIZ = "biz"
CAT_OK = "ok"
CAT_LABELS = {
    CAT_COOKIE: "Cookie 失效",
    CAT_CF: "Cloudflare 拦截",
    CAT_NETWORK: "网络异常",
    CAT_STRUCTURE: "解析失败/疑似改版",
    CAT_BIZ: "业务失败(签到/BUX)",
}
# 一个账号可能同时踩多个坑,汇总时按此优先级取主分类
CAT_PRIORITY = [CAT_COOKIE, CAT_CF, CAT_NETWORK, CAT_STRUCTURE, CAT_BIZ]

DEBUG = False
DEBUG_DIR = os.path.join(SCRIPT_DIR, "_zodgame_debug")

# ============================ notify.py 兼容 ============================

_qinglong_send = None
try:
    from notify import send as _qinglong_send  # type: ignore
except ImportError:
    # 青龙容器内 notify.py 在 /ql/data/scripts/,任务运行时 cwd 不一定在该目录
    _scripts_dir = "/ql/data/scripts"
    if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    try:
        from notify import send as _qinglong_send  # type: ignore
    except ImportError:
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


# ============================ 工具函数 ============================

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


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
    """Cookie 脱敏展示:仅保留前几个键名。"""
    keys = [item.split("=", 1)[0] for item in cookie_str.split(";") if "=" in item]
    return f"<{len(keys)} 项: {', '.join(keys[:4])}...>"


def strip_tags(raw: str, keep_script: bool = False) -> str:
    """
    去 script/style/标签/XML 包装,压成单行正文,用于文案匹配。
    keep_script=True 时保留 script 内部文本——Discuz 的浮窗提示会把成败文案
    塞在 errorhandle_xxx('...') / showDialog('...') 里,删掉 script 就再也匹配不到。
    """
    s = raw
    if not keep_script:
        s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<!\[CDATA\[|\]\]>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return " ".join(html_unescape(s).split())


def rand_sleep(min_s: float, max_s: float):
    """随机休眠,用于请求间隔降低风控特征。"""
    time.sleep(random.uniform(min_s, max_s))


def debug_dump(name: str, text: str):
    """DEBUG 模式下把关键响应落盘,便于下次抓样本与回归对拍。"""
    if not DEBUG:
        return
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print(f"   💾 调试落盘: {path}")
    except Exception as e:
        print(f"   ⚠️ 调试落盘失败: {e}")


GLOBAL_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                     "ALL_PROXY", "all_proxy")


def resolve_proxy() -> tuple:
    """代理优先级:ZODGAME_PROXY > 青龙全局代理 > 直连。返回 (proxy, 来源)。"""
    own = os.getenv("ZODGAME_PROXY", "").strip()
    if own:
        return own, "ZODGAME_PROXY"
    for key in GLOBAL_PROXY_KEYS:
        val = os.getenv(key, "").strip()
        if val:
            return val, key
    return None, None


# ============================ HTTP(带重试) ============================

def request_with_retry(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """
    带重试的请求。仅对网络层错误重试(超时/连接/SSL EOF 等),
    业务层响应(哪怕 4xx/5xx)原样返回,交给上层判定。
    """
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if DEBUG:
                print(f"   🔍 {method} {url[:80]} → {resp.status_code} ({len(resp.text)}B)")
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * attempt
                print(f"   ⚠️ 请求失败({type(e).__name__}),{delay}s 后重试 "
                      f"{attempt}/{MAX_RETRIES - 1}")
                time.sleep(delay)
    raise last_exc


def http_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    return request_with_retry(session, "GET", url, **kwargs)


def http_post(session: requests.Session, url: str, **kwargs) -> requests.Response:
    return request_with_retry(session, "POST", url, **kwargs)


# ============================ 会话构建 ============================

def build_session(cookie_str: str) -> requests.Session:
    session = requests.Session()
    session.cookies.update(parse_cookies(cookie_str))
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": BASE_URL + "/",
    })
    proxy, source = resolve_proxy()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        print(f"🌐 使用代理: {proxy} (来源 {source})")
    return session


# ============================ 页面判定 ============================

def is_cf_challenge(resp: requests.Response) -> bool:
    """是否被 Cloudflare 挑战页拦截。"""
    if resp.headers.get("cf-mitigated"):
        return True
    body = resp.text or ""
    if any(mark in body for mark in CF_MARKERS):
        return True
    # 无特征文本时,只看 CF 前置的 403
    if resp.status_code == 403 and "cloudflare" in (resp.headers.get("server", "") or "").lower():
        return True
    return False


def classify_sign_page(resp: requests.Response) -> str:
    """
    判定签到页状态。返回:
      ok        正常签到页
      invalid   Cookie 失效(被重定向到登录页)
      cf        被 Cloudflare 拦截
      unknown   既非正常签到页也非登录页(疑似改版)
    """
    if is_cf_challenge(resp):
        return "cf"
    body = resp.text or ""
    if TITLE_VALID in body:
        return "ok"
    if TITLE_INVALID in body:
        return "invalid"
    return "unknown"


def extract_formhash(page_html: str):
    """从页面提取 formhash。"""
    m = re.search(r'name="formhash"\s+value="([a-z0-9]+)"', page_html)
    if m:
        return m.group(1)
    m = re.search(r"action=logout&amp;formhash=([a-z0-9]+)", page_html) \
        or re.search(r"action=logout&formhash=([a-z0-9]+)", page_html)
    return m.group(1) if m else None


# 昵称提取:优先 Discuz 通用用户导航(每页都在),再退到插件自带文案
_USERNAME_RES = (
    re.compile(r"class=\"vwmy\"[^>]*>\s*<a[^>]*>([^<]{1,30})</a>"),
    re.compile(r"<a[^>]*class=\"vwmy\"[^>]*>([^<]{1,30})</a>"),
    re.compile(r"昵称:\s*([^\s(<]{1,30})"),
    re.compile(r"<b>([^<]{1,30})</b></font>\s*,\s*您累计已签到"),
)


def extract_username(page_html: str):
    """尽力提取昵称,仅用于多账号区分;取不到返回 None。"""
    for pattern in _USERNAME_RES:
        m = pattern.search(page_html)
        if m:
            return m.group(1).strip()
    return None


# ============================ 每日签到 ============================

def extract_sign_result(xml_text: str) -> dict:
    """
    解析签到接口返回的 XML(CDATA 包 HTML)。
    返回 {status: success/duplicate/failed/unknown, message: str}
    """
    text = strip_tags(xml_text)

    if SIGN_SUCCESS_MARK in text:
        m = re.search(r"恭喜你签到成功[!！]?\s*获得随机奖励\s*(\S+?)\s*(\d+)\s*(\S+?)[.。]",
                      text)
        if m:
            item, count, unit = m.groups()
            return {"status": "success", "message": f"签到成功,获得 {item} {count} {unit}"}
        return {"status": "success", "message": "签到成功"}

    if any(mark in text for mark in SIGN_DUPLICATE_MARKS):
        return {"status": "duplicate", "message": "今日已签到过"}

    if any(mark in text for mark in SIGN_FAILED_MARKS):
        tips = re.search(r'<div class="c">(.*?)</div>', xml_text, re.S)
        msg = strip_tags(tips.group(1))[:60] if tips else "签到失败"
        return {"status": "failed", "message": msg or "签到失败"}

    return {"status": "unknown", "message": "未匹配到签到相关关键信息"}


def do_sign(session: requests.Session, page_html: str) -> dict:
    """执行每日签到。page_html 为签到页文本(避免重复请求)。"""
    if ALREADY_SIGNED_MARK in page_html:
        return {"status": "duplicate", "message": "今日已签到过(页面特征)"}

    formhash = extract_formhash(page_html)
    if not formhash:
        return {"status": "failed", "message": "无法提取 formhash(页面结构可能已变化)"}

    mood = random.choice(MOODS)
    resp = http_post(session, CHECKIN_URL,
                     headers={"Referer": SIGN_PAGE_URL},
                     data={"formhash": formhash, "qdxq": mood})
    debug_dump("02_sign_response.xml", resp.text)
    return extract_sign_result(resp.text)


# ============================ BUX 广告任务 ============================

# 任务行的 window.open 脚本块;后接该任务对应的 <tr>
_TASK_SCRIPT_RE = re.compile(
    r'function\s+openNewWindow(\d+)\s*\(\s*\)\s*\{.*?window\.open\(\s*"([^"]+)"', re.S)
# 「任务列表」区块:从标题到该表格结束
_TASK_BLOCK_RE = re.compile(r"任务列表\s*</h2>(.*?)</table>", re.S)
# 广告页倒计时(click.htm 里 var count=$rdtime)
_AD_COUNT_RE = re.compile(r"var\s+count\s*=\s*(\d+)")


def parse_bux_tasks(page_html: str) -> list:
    """
    解析 BUX 任务列表,返回任务 dict 列表。
    每个任务: clickid / timeo / onlyhash / formhash / userid / seconds / earn / check_url。
    """
    matches = list(_TASK_SCRIPT_RE.finditer(page_html))
    tasks = []
    for idx, m in enumerate(matches):
        click_url = html_unescape(m.group(2))
        row_start = m.end()
        row_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(page_html)
        row = page_html[row_start:row_end]

        query = urllib.parse.parse_qs(urllib.parse.urlparse(click_url).query)

        def q(name, default=""):
            return (query.get(name) or [default])[0]

        task = {
            "clickid": q("clickid", m.group(1)),
            "timeo": q("timeo"),
            "onlyhash": q("onlyhash"),
            "formhash": q("formhash"),
            "userid": q("userid"),
            "seconds": None,
            "earn": None,
            "check_url": None,
            "click_url": click_url,
        }

        ms = re.search(r"<td>\s*(\d+)\s*秒\s*</td>", row)
        if ms:
            task["seconds"] = int(ms.group(1))

        me = re.search(r"<td>\s*([\d.]+)\s*点币\s*</td>", row)
        if me:
            task["earn"] = float(me.group(1))

        mc = re.search(r"showWindow\('check',\s*'([^']+)'\)", row)
        if mc:
            task["check_url"] = html_unescape(mc.group(1))

        tasks.append(task)
    return tasks


def detect_bux_state(page_html: str) -> dict:
    """判定 BUX 页状态:是否已加入 / 有无任务区块 / 是否明确无任务 / 是否分页。"""
    block_match = _TASK_BLOCK_RE.search(page_html)
    block = block_match.group(1) if block_match else ""
    return {
        "joined": not BUX_NOT_JOINED_RE.search(page_html),
        "has_task_block": bool(block_match),
        "no_task_text": BUX_NO_TASK_MARK in block,
        "paginated": bool(re.search(r'class="pg', block)),
    }


def _extract_points(page_html: str):
    """从 jnbux 主页提取点币余额。"""
    m = re.search(r"点币:\s*([\d.]+)", page_html)
    return float(m.group(1)) if m else None


def _build_click_url(task: dict) -> str:
    return (f"{JNBUX_ACTION_URL}&do=click&clickid={task['clickid']}"
            f"&timeo={task['timeo']}&onlyhash={task['onlyhash']}"
            f"&formhash={task['formhash']}&userid={task['userid']}")


def _build_update_url(task: dict) -> str:
    return _build_click_url(task).replace("&do=click&", "&do=update&")


def _build_check_url(task: dict, page: int = 0) -> str:
    """
    构造广告校验 URL。模板里的形状是
      plugin.php?id=jnbux:jnbux&do=check&clickid=X&userid=U&formhash=F&page=P
    这里补上 Discuz 浮窗 AJAX 参数,拿到独立的校验片段(便于精确判定)。
    """
    base = task.get("check_url")
    if base:
        if base.startswith("plugin.php"):
            base = f"{BASE_URL}/{base}"
        sep = "&" if "?" in base else "?"
        return base + f"{sep}infloat=yes&handlekey=check&inajax=1&ajaxtarget=fwin_content_check"
    return (f"{JNBUX_ACTION_URL}&do=check&clickid={task['clickid']}"
            f"&userid={task['userid']}&formhash={task['formhash']}&page={page}"
            f"&infloat=yes&handlekey=check&inajax=1&ajaxtarget=fwin_content_check")


def _try_join_bux(session: requests.Session, page_html: str) -> str:
    """未加入 BUX 时按站点自身流程加入,返回刷新后的页面文本。"""
    formhash = extract_formhash(page_html)
    if not formhash:
        return page_html
    print("   ➕ 未加入 BUX 广告系统,尝试自动加入...")
    http_get(session, f"{JNBUX_ACTION_URL}&do=join&formhash={formhash}",
             headers={"Referer": JNBUX_URL})
    return http_get(session, JNBUX_URL).text


def _watch_seconds(task: dict, click_resp) -> int:
    """广告观看秒数:命令行覆盖 > 广告页真实倒计时 > 任务行秒数 > 兜底。"""
    override = os.getenv("ZODGAME_AD_SECONDS", "").strip()
    if override.isdigit():
        return int(override)
    if click_resp is not None:
        m = _AD_COUNT_RE.search(click_resp.text or "")
        if m:
            return int(m.group(1))
    if task.get("seconds"):
        return int(task["seconds"])
    return AD_FALLBACK_SECONDS


def _check_message(raw: str) -> str:
    """从 do=check 响应里取出可读提示文案(Discuz 浮窗把文案放在 showDialog/xxxhandle 里)。"""
    m = re.search(r"showDialog\('([^']{2,200})'", raw) \
        or re.search(r"(?:error|success)handle_\w+\('([^']{2,200})'", raw)
    if m:
        return m.group(1).strip()
    return strip_tags(raw, keep_script=True)[:120]


def judge_check_response(raw: str) -> tuple:
    """
    判定 do=check 校验结果。返回 (status, message):
      success 广告已完成、积分已入账
      failed  未完成/未加入
      unknown 无法判定(疑似改版)
    """
    text = strip_tags(raw, keep_script=True)
    if BUX_CHECK_SUCCESS_MARK in text:
        return "success", _check_message(raw) or "检查成功"
    if BUX_CHECK_FAIL_MARK in text:
        return "failed", _check_message(raw) or "检查失败"
    return "unknown", _check_message(raw)


def run_bux_tasks(session: requests.Session, page_html: str = None) -> dict:
    """
    完成 jnbux 广告任务。
    单个任务流程: do=click → 等待真实倒计时 → do=update → do=check 判定
    返回 {done, earned, total, error, note, task_count}
    """
    result = {"done": 0, "earned": 0.0, "total": None, "error": None,
              "note": None, "task_count": 0}

    if page_html is None:
        page_html = http_get(session, JNBUX_URL).text
        debug_dump("03_bux_page.html", page_html)

    state = detect_bux_state(page_html)
    if not state["joined"]:
        page_html = _try_join_bux(session, page_html)
        debug_dump("03b_bux_page_joined.html", page_html)
        state = detect_bux_state(page_html)
        if not state["joined"]:
            result["error"] = "未加入 BUX 广告系统,自动加入未生效"
            result["total"] = _extract_points(page_html)
            return result

    tasks = parse_bux_tasks(page_html)
    result["task_count"] = len(tasks)

    if not tasks:
        # 只认站点明确文案;其余一律按「疑似改版」告警,不静默当成功
        if not state["has_task_block"]:
            result["error"] = "页面缺少『任务列表』区块(疑似改版)"
        elif state["no_task_text"]:
            result["note"] = "今日无广告任务或已全部完成"
        else:
            result["error"] = "任务列表无链接且无明确『无任务』文案(疑似改版)"
        result["total"] = _extract_points(page_html)
        return result

    if state["paginated"]:
        print("   ℹ️ 任务列表存在分页,本脚本仅处理第一页")

    print(f"   📋 发现 {len(tasks)} 个广告任务")

    for task in tasks:
        cid = task["clickid"]
        earn = task["earn"] if task["earn"] is not None else 2.0
        print(f"   ▶️ 任务 {cid}: 打开广告...")

        try:
            click_resp = http_get(session, _build_click_url(task),
                                  headers={"Referer": JNBUX_URL})
            debug_dump(f"04_task{cid}_click.html", click_resp.text)

            watch = _watch_seconds(task, click_resp)
            print(f"      观看广告 {watch}s(+{AD_MARGIN_SECONDS}s 余量)...")
            time.sleep(watch + AD_MARGIN_SECONDS)

            update_resp = http_get(session, _build_update_url(task),
                                   headers={"Referer": JNBUX_URL})
            debug_dump(f"05_task{cid}_update.html", update_resp.text)

            check_resp = http_get(session, _build_check_url(task),
                                  headers={"Referer": JNBUX_URL})
            debug_dump(f"06_task{cid}_check.xml", check_resp.text)
        except requests.RequestException:
            # 网络层错误向上抛,由账号级流程归类为「网络异常」
            raise
        except Exception as e:
            print(f"   ⚠️ 任务 {cid} 异常: {e}")
            continue

        check_status, check_msg = judge_check_response(check_resp.text)
        if check_status == "success":
            result["done"] += 1
            result["earned"] += earn
            print(f"   ✅ 任务 {cid} 完成 (+{earn:g} 点币)")
        elif check_status == "failed":
            print(f"   ❌ 任务 {cid} 校验未通过: {check_msg[:80]}")
        else:
            print(f"   ❓ 任务 {cid} 校验响应无法判定: {check_msg[:80]}")

        rand_sleep(1, 3)

    final_html = http_get(session, JNBUX_URL).text
    debug_dump("07_bux_page_after.html", final_html)
    result["total"] = _extract_points(final_html)
    return result


# ============================ 单账号流程 ============================

def _pick_category(flags: dict) -> str:
    """按优先级从各失败标记里选主分类。"""
    for cat in CAT_PRIORITY:
        if flags.get(cat):
            return cat
    return CAT_OK


def run_one_account(cookie_str: str, index: int) -> dict:
    """完整跑一个账号:Cookie/CF 判定 → 签到 → BUX 任务。"""
    label = f"账号{index + 1}"
    result = {
        "label": label, "username": None, "ok": False, "state": CAT_OK,
        "sign": None, "sign_ok": False,
        "bux": None, "bux_ok": False,
        "points": None, "message": "", "flags": {},
    }
    flags = result["flags"]

    print(f"\n======== {label} ({mask_cookie(cookie_str)}) ========")
    session = build_session(cookie_str)

    # 1. 取签到页,判定 Cookie / CF
    try:
        resp = http_get(session, SIGN_PAGE_URL)
    except requests.RequestException as e:
        flags[CAT_NETWORK] = True
        result["message"] = f"网络异常: {type(e).__name__}: {e}"[:80]
        print(f"   ❌ {result['message']}")
        result["state"] = _pick_category(flags)
        return result

    debug_dump("01_sign_page.html", resp.text)
    verdict = classify_sign_page(resp)

    if verdict == "cf":
        flags[CAT_CF] = True
        result["message"] = "被 Cloudflare 拦截(cf_clearance 与抓取时的 IP/UA 绑定,请重抓或换代理出口)"
        print(f"   ❌ {result['message']}")
        result["state"] = _pick_category(flags)
        return result
    if verdict == "invalid":
        flags[CAT_COOKIE] = True
        result["message"] = "Cookie 已失效,请重新抓取!"
        print(f"   ❌ {result['message']}")
        result["state"] = _pick_category(flags)
        return result
    if verdict == "unknown":
        flags[CAT_STRUCTURE] = True
        result["message"] = "签到页 title 既非签到页也非登录页(疑似改版或异常页)"
        print(f"   ❌ {result['message']}")
        result["state"] = _pick_category(flags)
        return result

    result["username"] = extract_username(resp.text)
    if result["username"]:
        result["label"] = f"{label}({result['username']})"
        print(f"   👤 昵称: {result['username']}")

    # 2. 每日签到
    try:
        sign = do_sign(session, resp.text)
        result["sign"] = sign["message"]
        result["sign_ok"] = sign["status"] in ("success", "duplicate")
        print(f"   📝 签到: {sign['message']}")
    except requests.RequestException as e:
        flags[CAT_NETWORK] = True
        result["sign"] = f"签到网络异常: {type(e).__name__}"
        print(f"   ❌ {result['sign']}")
    except Exception as e:
        result["sign"] = f"签到异常: {e}"[:60]
        print(f"   ❌ {result['sign']}")

    rand_sleep(1, 3)

    # 3. BUX 广告任务
    try:
        bux = run_bux_tasks(session)
        result["points"] = bux.get("total")
        if bux.get("error"):
            flags[CAT_STRUCTURE] = True
            result["bux"] = bux["error"]
            print(f"   ⚠️ BUX: {bux['error']}")
        elif bux.get("note"):
            result["bux"] = bux["note"]
            result["bux_ok"] = True
            print(f"   📺 BUX: {bux['note']}")
        else:
            result["bux"] = f"完成 {bux['done']}/{bux['task_count']} 个任务 (+{bux['earned']:g} 点币)"
            result["bux_ok"] = bux["done"] > 0
            print(f"   📺 BUX: {result['bux']},余额 {result['points']} 点币")
    except requests.RequestException as e:
        flags[CAT_NETWORK] = True
        result["bux"] = f"BUX 网络异常: {type(e).__name__}"
        print(f"   ❌ {result['bux']}")
    except Exception as e:
        result["bux"] = f"BUX 异常: {type(e).__name__}"
        print(f"   ❌ {result['bux']}")

    # 成功判定:签到与 BUX 均无失败才算成功
    if not result["sign_ok"] or not result["bux_ok"]:
        flags.setdefault(CAT_BIZ, True)

    result["state"] = _pick_category(flags)
    result["ok"] = result["state"] == CAT_OK
    if not result["ok"] and not result["message"]:
        result["message"] = "签到或广告任务未全部完成"
    return result


# ============================ --check 自检 ============================

def run_check(cookie_list: list) -> int:
    """自检:只验环境与登录态,不签到、不执行广告任务。"""
    print("🔎 自检模式(--check):不签到、不执行广告任务\n")
    proxy, source = resolve_proxy()
    print(f"🌐 代理: {proxy or '直连'}{f' (来源 {source})' if proxy else ''}\n")

    ok_cnt = 0
    for i, cookie_str in enumerate(cookie_list):
        label = f"账号{i + 1}"
        print(f"======== {label} ({mask_cookie(cookie_str)}) ========")
        session = build_session(cookie_str)
        try:
            resp = http_get(session, SIGN_PAGE_URL)
        except requests.RequestException as e:
            print(f"   ❌ 签到页请求失败: {type(e).__name__}: {e}")
            print()
            continue

        verdict = classify_sign_page(resp)
        if verdict == "cf":
            print("   ❌ 被 Cloudflare 拦截(cf_clearance 与抓取时的 IP/UA 绑定)")
            print()
            continue
        if verdict == "invalid":
            print("   ❌ Cookie 已失效")
            print()
            continue
        if verdict == "unknown":
            print("   ❌ 签到页异常(title 既非签到页也非登录页)")
            print()
            continue

        account_ok = True
        user = extract_username(resp.text)
        print(f"   ✅ Cookie 有效{f' / 昵称 {user}' if user else ''}")
        if ALREADY_SIGNED_MARK in resp.text:
            print("   ℹ️ 今日已签到过")

        try:
            bux_html = http_get(session, JNBUX_URL).text
            state = detect_bux_state(bux_html)
            tasks = parse_bux_tasks(bux_html)
            if not state["joined"]:
                print("   ⚠️ 未加入 BUX 广告系统(正式运行时会自动加入)")
            else:
                print("   ✅ 已加入 BUX 广告系统")
            if tasks:
                print(f"   ✅ 发现 {len(tasks)} 个广告任务待完成")
            elif state["no_task_text"]:
                print("   ℹ️ 今日无广告任务或已全部完成")
            else:
                print("   ❌ 任务列表解析不到链接且无『无任务』文案(疑似改版)")
                account_ok = False
        except requests.RequestException as e:
            print(f"   ❌ BUX 页请求失败: {type(e).__name__}: {e}")
            account_ok = False

        ok_cnt += 1 if account_ok else 0
        print()

    print(f"📊 自检结果:{ok_cnt}/{len(cookie_list)} 个账号可用")
    return 0 if ok_cnt == len(cookie_list) else 1


# ============================ 主流程 ============================

def build_report(results: list) -> tuple:
    """生成 (标题, 正文)。"""
    total = len(results)
    ok_cnt = sum(1 for r in results if r["ok"])
    fail_cnt = total - ok_cnt

    cat_counts = {}
    for r in results:
        if r["state"] != CAT_OK:
            cat_counts[r["state"]] = cat_counts.get(r["state"], 0) + 1

    if ok_cnt == total:
        title = f"✅ ZodGame 全部成功({ok_cnt}/{total})"
    elif ok_cnt == 0:
        if cat_counts.get(CAT_COOKIE, 0) == total:
            title = f"❌ ZodGame Cookie 全部失效({total}/{total})"
        else:
            title = f"❌ ZodGame 全部失败(0/{total})"
    else:
        title = f"⚠️ ZodGame 部分成功({ok_cnt}/{total})"

    lines = [f"# {title}", ""]
    lines.append(f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"📊 账号 {total} 个,✅ {ok_cnt},❌ {fail_cnt}")
    if cat_counts:
        detail = "、".join(f"{CAT_LABELS.get(k, k)} {v}" for k, v in cat_counts.items())
        lines.append(f"🔍 失败分类: {detail}")
    lines.append("")

    for r in results:
        lines.append(f"## {r['label']}")
        if r["ok"]:
            lines.append(f"- 📝 签到: {r['sign'] or '未执行'}")
            lines.append(f"- 📺 BUX: {r['bux'] or '未执行'}")
            if r["points"] is not None:
                lines.append(f"- 💰 点币余额: {r['points']}")
        else:
            lines.append(f"- ❌ {r['message']}")
            if r["sign"]:
                lines.append(f"- 📝 签到: {r['sign']}")
            if r["bux"]:
                lines.append(f"- 📺 BUX: {r['bux']}")
            if r["state"] == CAT_COOKIE:
                lines.append("- 💡 请浏览器登录后 F12 重新抓取 Cookie 更新 ZODGAME_COOKIE")
            elif r["state"] == CAT_CF:
                lines.append("- 💡 cf_clearance 与抓取时的出口 IP/UA 绑定,"
                             "换代理或换 UA 后必须重新抓取")
        lines.append("")

    return title, "\n".join(lines).strip()


def main(argv=None) -> int:
    global DEBUG, DEBUG_DIR
    argv = sys.argv[1:] if argv is None else argv
    DEBUG = env_bool("ZODGAME_DEBUG", False)
    DEBUG_DIR = os.getenv("ZODGAME_DEBUG_DIR", "").strip() or DEBUG_DIR

    raw = os.getenv("ZODGAME_COOKIE", "").strip()
    if not raw:
        print("❌ 未设置 ZODGAME_COOKIE 环境变量")
        return 1

    cookie_list = [line.strip() for line in raw.split("\n") if line.strip()]

    if "--check" in argv:
        return run_check(cookie_list)

    notify_enabled = env_bool("ZODGAME_NOTIFY", True)
    notify_only_fail = env_bool("ZODGAME_NOTIFY_ONLY_FAIL", False)

    print(f"🚀 ZodGame 签到启动,共 {len(cookie_list)} 个账号")

    results = []
    for i, cookie_str in enumerate(cookie_list):
        try:
            r = run_one_account(cookie_str, i)
        except Exception:
            r = {"label": f"账号{i + 1}", "username": None, "ok": False,
                 "state": CAT_NETWORK, "sign": None, "sign_ok": False,
                 "bux": None, "bux_ok": False, "points": None,
                 "message": traceback.format_exc().splitlines()[-1][:80], "flags": {}}
            print(f"   ❌ {r['message']}")
        results.append(r)
        if i < len(cookie_list) - 1:
            rand_sleep(3, 6)

    title, report = build_report(results)
    print("\n" + report)

    ok_cnt = sum(1 for r in results if r["ok"])
    total = len(results)

    if notify_enabled:
        if notify_only_fail and ok_cnt == total:
            print("ℹ️ ZODGAME_NOTIFY_ONLY_FAIL=true 且全部成功,跳过推送")
        else:
            send_notify(title, report)
    else:
        print("ℹ️ ZODGAME_NOTIFY=false,已禁用推送")

    # 退出码:全成功 0 / 部分成功 2 / 全失败 1
    if ok_cnt == total:
        return 0
    if ok_cnt == 0:
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
