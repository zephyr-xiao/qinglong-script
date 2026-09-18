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
Cookie 失效时推送告警,提醒重新抓取。

环境变量:
  ZODGAME_COOKIE            必填,完整 Cookie 字符串,多账号用换行分隔
  ZODGAME_PROXY             可选,HTTP 代理,如 http://172.17.0.1:7890
  ZODGAME_NOTIFY            true/false 默认 true,是否调用青龙 notify.py 推送
  ZODGAME_NOTIFY_ONLY_FAIL  true/false 默认 false,全部成功时静默
  ZODGAME_DEBUG             true/false 默认 false,输出接口响应细节用于排错

依赖:requests

作者: 箫遥风
"""

import os
import random
import re
import sys
import time
import traceback
from datetime import datetime

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

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 签到页 title 特征:有效/失效(失效时整站 302 到登录页)
TITLE_VALID = "每日签到 -"
TITLE_INVALID = "登录 -"

# 已签到幂等特征(签到页正文)
ALREADY_SIGNED_MARK = "您今天已经签到过了"

# dsu_paulsign 随机心情(discuz 签到插件的 9 种心情代号)
MOODS = ["kx", "ng", "ym", "wl", "nu", "ch", "fd", "yl", "shuai"]

# 网络重试
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5

# 广告观看时长:页面倒计时 3 秒,加余量
AD_WATCH_SECONDS = 4

DEBUG = False

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


def http_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """带重试的 GET。站点有偶发 SSL EOF 抖动,必须重试兜底。"""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=25, **kwargs)
            if DEBUG:
                print(f"   🔍 GET {url[:70]} → {resp.status_code} ({len(resp.text)}B)")
            return resp
        except Exception as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * attempt
                print(f"   ⚠️ 请求失败({type(e).__name__}),{delay}s 后重试 {attempt}/{MAX_RETRIES - 1}")
                time.sleep(delay)
    raise last_exc


def rand_sleep(min_s: float, max_s: float):
    """随机休眠,用于请求间隔降低风控特征。"""
    time.sleep(random.uniform(min_s, max_s))


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
    proxy = os.getenv("ZODGAME_PROXY", "").strip()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        print(f"🌐 使用代理: {proxy}")
    return session


# ============================ Cookie 有效性 ============================

def check_cookie_valid(session: requests.Session) -> tuple[bool, str]:
    """访问签到页检测 Cookie。返回 (是否有效, 页面文本备用)。"""
    resp = http_get(session, SIGN_PAGE_URL)
    if TITLE_INVALID in resp.text and TITLE_VALID not in resp.text:
        return False, resp.text
    return True, resp.text


# ============================ 每日签到 ============================

def extract_sign_result(xml_text: str) -> dict:
    """
    解析签到接口返回的 XML(CDATA 包 HTML)。
    返回 {status: success/duplicate/failed/unknown, message: str}
    """
    result = {"status": "unknown", "message": "未匹配到签到相关关键信息"}

    pattern_success = re.compile(
        r"恭喜你签到成功!获得随机奖励\s+(\S+)\s+(\d+)\s+(\S+)\.", re.UNICODE)
    pattern_duplicate = re.compile(r"您今日已经签到，请明天再来！", re.UNICODE)

    success_match = pattern_success.search(xml_text)
    if success_match:
        item, count, unit = success_match.groups()
        result["status"] = "success"
        result["message"] = f"签到成功,获得 {item} {count} {unit}"
        return result
    if pattern_duplicate.search(xml_text):
        result["status"] = "duplicate"
        result["message"] = "今日已签到过"
        return result
    if "签到失败" in xml_text or "权限不足" in xml_text or "参数错误" in xml_text:
        result["status"] = "failed"
        # 提取提示文本
        tips = re.search(r'<div class="c">\s*(.+?)\s*</div>', xml_text, re.S)
        result["message"] = tips.group(1).strip()[:60] if tips else "签到失败"
        return result
    return result


def do_sign(session: requests.Session, page_html: str) -> dict:
    """执行每日签到。page_html 为签到页文本(避免重复请求)。"""
    # 幂等:页面已含已签到特征则跳过
    if ALREADY_SIGNED_MARK in page_html:
        return {"status": "duplicate", "message": "今日已签到过(页面特征)"}

    formhash_match = re.search(r'name="formhash" value="([a-z0-9]+)"', page_html)
    if not formhash_match:
        return {"status": "failed", "message": "无法提取 formhash"}
    formhash = formhash_match.group(1)

    mood = random.choice(MOODS)
    return _post_sign(session, formhash, mood)


def _post_sign(session: requests.Session, formhash: str, mood: str) -> dict:
    try:
        resp = session.post(
            CHECKIN_URL,
            headers={"Referer": SIGN_PAGE_URL},
            data={"formhash": formhash, "qdxq": mood},
            timeout=25,
        )
        return extract_sign_result(resp.text)
    except Exception as e:
        if _is_network_error(e):
            raise
        return {"status": "failed", "message": f"签到异常: {e}"}


def _is_network_error(e: Exception) -> bool:
    """网络层错误需要向上抛由外层重试,业务异常就地消化。"""
    msg = str(e).lower()
    keywords = ("timeout", "connection", "ssl", "proxy", "max retries", "eof")
    return any(k in msg for k in keywords) or isinstance(e, requests.RequestException)


# ============================ BUX 广告任务 ============================

def run_bux_tasks(session: requests.Session) -> dict:
    """
    完成 jnbux 广告任务。
    流程:主页提取(clickid,timeo,onlyhash) → do=click → 等待 → do=update
    返回 {done: 本次完成数, earned: 本次获得点币, total_earn: 累计点币, error: str|None}
    """
    result = {"done": 0, "earned": 0.0, "total_earn": None, "error": None,
              "done_note": None}

    resp = http_get(session, JNBUX_URL)
    tasks = re.findall(
        r'window\.open\("plugin\.php\?id=jnbux:jnbux&do=click&clickid=(\d+)'
        r'&timeo=(\d+)&onlyhash=([a-f0-9]+)&formhash=([a-z0-9]+)&userid=(\d+)"',
        resp.text)

    if not tasks:
        # 无任务链接:区分「站方今日无任务」「已全部完成」与「页面结构变化解析失败」
        m = re.search(r'任务列表(.*?)Powered by', resp.text, re.S)
        if m:
            if "暂时没有广告任务" in m.group(1):
                result["done_note"] = "今日站方未投放广告任务"
            elif re.search(r'<td>\d+</td>\s*<td>', m.group(1)):
                # 任务列表区块内还有任务行(纯数字 ID 单元格)却没解析到链接 → 结构变化
                result["error"] = "发现任务行但未解析到链接(页面结构变化?)"
            # 否则区块内无任务行 = 已全部完成,error 保持 None
        else:
            result["error"] = "页面无任务列表区块"
        result["total_earn"] = _extract_points(resp.text)
        return result

    print(f"   📋 发现 {len(tasks)} 个广告任务")

    for clickid, timeo, onlyhash, formhash, userid in tasks:
        print(f"   ▶️ 任务 {clickid}: 点击广告...")
        base = (f"{BASE_URL}/plugin.php?id=jnbux:jnbux&do={{do}}&clickid={clickid}"
                f"&timeo={timeo}&onlyhash={onlyhash}&formhash={formhash}&userid={userid}")

        try:
            # 1. 记录点击
            http_get(session, base.format(do="click"), headers={"Referer": JNBUX_URL})
            # 2. 等待广告倒计时(3 秒)+ 余量
            time.sleep(AD_WATCH_SECONDS)
            # 3. 领奖
            resp_update = http_get(session, base.format(do="update"),
                                   headers={"Referer": JNBUX_URL})
        except Exception as e:
            if _is_network_error(e):
                raise
            print(f"   ⚠️ 任务 {clickid} 异常: {e}")
            continue

        body = re.sub(r"<script.*?</script>|<style.*?</style>", "",
                      resp_update.text, flags=re.S)
        text = " ".join(re.sub(r"<[^>]+>", " ", body).split())
        if "成功" in text:
            result["done"] += 1
            result["earned"] += 2.0
            print(f"   ✅ 任务 {clickid} 完成 (+2 点币)")
        else:
            print(f"   ❌ 任务 {clickid} 领奖响应: {text[:60]}")
        rand_sleep(1, 3)

    # 刷新主页拿最新点币余额
    final = http_get(session, JNBUX_URL)
    result["total_earn"] = _extract_points(final.text)
    return result


def _extract_points(page_html: str) -> float | None:
    """从 jnbux 主页提取点币余额。"""
    m = re.search(r'点币:\s*([\d.]+)', page_html)
    return float(m.group(1)) if m else None


# ============================ 单账号流程 ============================

def run_one_account(cookie_str: str, index: int) -> dict:
    """完整跑一个账号:Cookie 检测 → 签到 → BUX 任务。"""
    label = f"账号{index + 1}"
    result = {
        "label": label,
        "success": False,
        "cookie_invalid": False,
        "sign": None,       # 签到结果文本
        "bux": None,        # BUX 任务摘要文本
        "points": None,     # 点币余额
        "message": "",
    }

    print(f"\n======== {label} ({mask_cookie(cookie_str)}) ========")
    session = build_session(cookie_str)

    # 1. Cookie 检测 + 签到页一次拿全(formhash / 幂等特征)
    try:
        valid, page_html = check_cookie_valid(session)
    except Exception as e:
        result["message"] = f"网络异常: {type(e).__name__}: {e}"[:80]
        print(f"   ❌ {result['message']}")
        return result

    if not valid:
        result["cookie_invalid"] = True
        result["message"] = "Cookie 已失效,请重新抓取!"
        print(f"   ❌ {result['message']}")
        return result

    # 2. 每日签到
    sign_ok = True
    try:
        sign = do_sign(session, page_html)
        result["sign"] = sign["message"]
        print(f"   📝 签到: {sign['message']}")
        sign_ok = sign["status"] in ("success", "duplicate")
    except Exception as e:
        result["sign"] = f"签到异常: {e}"[:60]
        print(f"   ❌ {result['sign']}")
        sign_ok = False

    rand_sleep(1, 3)

    # 3. BUX 广告任务
    bux_ok = True
    try:
        bux = run_bux_tasks(session)
        result["points"] = bux.get("total_earn")
        if bux.get("error"):
            result["bux"] = bux["error"]
            bux_ok = False
            print(f"   ⚠️ BUX: {bux['error']}")
        elif bux.get("done_note"):
            result["bux"] = bux["done_note"]
            print(f"   📺 BUX: {bux['done_note']}")
        else:
            result["bux"] = f"完成 {bux['done']} 个任务 (+{bux['earned']:.0f} 点币)"
            print(f"   📺 BUX: {result['bux']},余额 {result['points']} 点币")
    except Exception as e:
        result["bux"] = f"BUX 异常: {type(e).__name__}"[:60]
        bux_ok = False
        print(f"   ❌ {result['bux']}")

    # 成功判定:签到与 BUX 两项均无失败才算成功(Cookie 有效是前提)
    result["success"] = sign_ok and bux_ok
    return result


# ============================ 主流程 ============================

def main():
    global DEBUG
    DEBUG = env_bool("ZODGAME_DEBUG", False)

    notify_enabled = env_bool("ZODGAME_NOTIFY", True)
    notify_only_fail = env_bool("ZODGAME_NOTIFY_ONLY_FAIL", False)

    raw = os.getenv("ZODGAME_COOKIE", "").strip()
    if not raw:
        print("❌ 未设置 ZODGAME_COOKIE 环境变量")
        sys.exit(1)

    # 多账号按行分隔
    cookie_list = [line.strip() for line in raw.split("\n") if line.strip()]
    print(f"🚀 ZodGame 签到启动,共 {len(cookie_list)} 个账号")

    results = []
    for i, cookie_str in enumerate(cookie_list):
        try:
            r = run_one_account(cookie_str, i)
        except Exception:
            r = {"label": f"账号{i + 1}", "success": False, "cookie_invalid": False,
                 "sign": None, "bux": None, "points": None,
                 "message": traceback.format_exc().splitlines()[-1][:80]}
            print(f"   ❌ {r['message']}")
        results.append(r)
        # 多账号间隔
        if i < len(cookie_list) - 1:
            rand_sleep(3, 6)

    # 汇总报告
    total = len(results)
    success_cnt = sum(1 for r in results if r["success"])
    fail_cnt = total - success_cnt
    invalid_cnt = sum(1 for r in results if r["cookie_invalid"])

    if success_cnt == total and invalid_cnt == 0:
        report_title = f"✅ ZodGame 全部成功({success_cnt}/{total})"
    elif invalid_cnt > 0 and invalid_cnt == total:
        report_title = f"❌ ZodGame Cookie 全部失效({invalid_cnt}/{total})"
    elif success_cnt == 0:
        report_title = f"❌ ZodGame 全部失败(0/{total})"
    else:
        report_title = f"⚠️ ZodGame 部分成功({success_cnt}/{total})"

    lines = [f"# {report_title}", ""]
    lines.append(f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"📊 账号 {total} 个,✅ {success_cnt},❌ {fail_cnt}")
    lines.append("")
    for r in results:
        lines.append(f"## {r['label']}")
        if r["success"]:
            lines.append(f"- 📝 签到: {r['sign'] or '未执行'}")
            lines.append(f"- 📺 BUX: {r['bux'] or '未执行'}")
            if r["points"] is not None:
                lines.append(f"- 💰 点币余额: {r['points']}")
        else:
            lines.append(f"- ❌ {r['message']}")
            if r["cookie_invalid"]:
                lines.append("- 💡 请浏览器登录后 F12 重新抓取 Cookie 更新 ZODGAME_COOKIE")
        lines.append("")
    report = "\n".join(lines).strip()

    print("\n" + report)

    if notify_enabled:
        if notify_only_fail and fail_cnt == 0 and invalid_cnt == 0:
            print("ℹ️ ZODGAME_NOTIFY_ONLY_FAIL=true 且全部成功,跳过推送")
        else:
            send_notify(report_title, report)
    else:
        print("ℹ️ ZODGAME_NOTIFY=false,已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
