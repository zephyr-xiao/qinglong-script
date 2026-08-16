# -*- coding: utf-8 -*-
"""
new Env('7x.hk / 8s.hk 签到');
cron: 20 8 * * *

适配青龙面板 - 仅依赖 requests + 标准库
双站点：https://7x.hk/  https://8s.hk/        架构：NewAPI

环境变量：
  S7XHK_SESSION      7x.hk 的 session 值（不含 session= 前缀）；多账号用 & 分隔（可选，不配则跳过该站）
  S7XHK_USER_ID      7x.hk 的 new-api-user 请求头值；可选，留空时自动从 session 解码
                     仅当自动解码失败时才需手动填写；多账号用 & 分隔，与 SESSION 一一对应，缺省位置留空
  S8SHK_SESSION      8s.hk 的 session 值；多账号用 & 分隔（可选，不配则跳过该站）
  S8SHK_USER_ID      8s.hk 的 new-api-user 请求头值；可选，留空时自动从 session 解码
  S7X8S_PROXY        HTTP/SOCKS 代理；若站点网络受限可填写
                     例：http://172.17.0.1:7890   或   socks5://172.17.0.1:7891
  S7X8S_NOTIFY       true/false，默认 true，是否调用青龙 notify.py 推送
  S7X8S_NOTIFY_ONLY_FAIL  true/false，默认 false，仅当存在失败时才推送
                     （需 S7X8S_NOTIFY=true 时生效，全部成功则静默）
  S7X8S_TIMEOUT      HTTP 超时秒数，默认 30
  S7X8S_DEBUG        true 时输出请求 URL、状态码、响应片段

作者: 箫遥风
"""

import os
import re
import sys
import time
import traceback
from datetime import datetime

# 强制 stdout/stderr 使用 UTF-8，避免 Windows 控制台 GBK 报错
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
    print("❌ 缺少 requests 库，请在青龙「依赖管理」-「Python」中安装 requests")
    sys.exit(1)


# ====================== 兼容青龙通知 ======================

_qinglong_send = None
try:
    # 青龙运行时会把 notify.py 加入 sys.path
    from notify import send as _qinglong_send  # type: ignore
except Exception:
    # 青龙容器内 notify.py 在 /ql/data/scripts/，脚本运行时 cwd 可能不在该目录
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


# ====================== 常量 ======================

SITES = [
    {"name": "7x.hk", "env_prefix": "S7XHK", "home": "https://7x.hk"},
    {"name": "8s.hk", "env_prefix": "S8SHK", "home": "https://8s.hk"},
]

SELF_URL = "/api/user/self"
CHECKIN_URL = "/api/user/checkin"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": None,  # 由站点动态注入
    "Referer": None,  # 由站点动态注入
}


# ====================== 工具函数 ======================

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


def parse_accounts(raw: str) -> list:
    """
    把多账号字符串按 & 切成列表。

    保留空位（如 "&8411&" → ["", "8411", ""]）：README 推荐的缺省位置留空
    写法依赖空位与账号索引对齐，过滤空项会导致 user_id 错发给前面的账号。
    """
    return [c.strip() for c in raw.split("&")]


def get_env_for_site(prefix: str, key: str) -> str:
    """取站点专属环境变量值，如 S7XHK_SESSION / S8SHK_USER_ID。"""
    # 统一前缀 + 下划线 + 键
    env_name = f"{prefix}_{key}"
    return (os.getenv(env_name) or "").strip()


def fmt_quota(v) -> str:
    """额度数字缩写展示：≥1M 显示 x.xM、≥1K 显示 x.xK。"""
    if v is None:
        return "0"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{(n / 1_000_000):.1f}M"
    if n >= 1_000:
        return f"{(n / 1_000):.1f}K"
    return str(int(n)) if n.is_integer() else f"{n:g}"


def extract_user_id_from_session(session_val: str):
    """
    从 NewAPI 的 session cookie 中解码出 user_id。
    session 是 gorilla/securecookie 格式：base64( "<时间戳>|<base64 gob payload>|<hmac签名>" )
    payload 是 gob 编码的 map，含 username/role/status/group/id 等字段。
    """
    try:
        import base64
        raw = base64.urlsafe_b64decode(session_val + "==").decode("latin-1", errors="replace")
        parts = raw.split("|")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        gob = base64.urlsafe_b64decode(payload_b64 + "==")

        def dec_uint(buf, pos):
            n = buf[pos]
            if n < 128:
                return n, pos + 1
            length = 256 - n
            val = int.from_bytes(buf[pos + 1:pos + 1 + length], "big")
            return val, pos + 1 + length

        marker = b"id\x03int"
        pos = gob.find(marker)
        if pos < 0:
            return None
        scan_from = pos + len(marker)
        best = None
        for start in range(scan_from, min(scan_from + 8, len(gob))):
            try:
                v, _ = dec_uint(gob, start)
            except Exception:
                continue
            if v <= 0:
                continue
            real = v // 2 if v % 2 == 0 else (v + 1) // 2
            if real > 0 and (best is None or real > best):
                best = real
        return best
    except Exception:
        return None


# ====================== 签到核心 ======================

def _debug(debug: bool, method: str, url: str, status: int, body: str) -> None:
    if not debug:
        return
    snippet = (body or "")[:120].replace("\n", " ")
    print(f"   [debug] {method} {url} -> {status} | {snippet}")


def signin_one_account(idx: int, site_name: str, base_url: str,
                       session_val: str, user_id: str,
                       timeout: int, debug: bool, proxy: str = "") -> tuple:
    """
    对单个账号执行签到流程：验证登录 → 签到 → 本月统计。
    返回 (success: bool, summary: str)。
    """
    label = f"{site_name} 账号 {idx}"
    session = requests.Session()
    headers = dict(DEFAULT_HEADERS)
    headers["Origin"] = base_url
    headers["Referer"] = base_url + "/profile"
    session.headers.update(headers)
    # ★ 关键：固定 cookie 名 session 注入认证
    session.headers["Cookie"] = f"session={session_val}"

    # ★ user_id 处理：环境变量优先，否则从 session 自动解码
    uid = user_id.strip() if user_id else ""
    if not uid:
        decoded = extract_user_id_from_session(session_val)
        if decoded:
            uid = str(decoded)
            if debug:
                print(f"   [debug] 已从 session 自动解码 user_id={uid}")
        else:
            print(f"   ⚠️ {label}: 未能从 session 解码 user_id，尝试不带头请求")
    if uid:
        session.headers["new-api-user"] = uid

    # 代理
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        if debug:
            print(f"   [debug] 使用代理: {proxy}")

    # ① 验证登录 GET /api/user/self
    self_url = base_url + SELF_URL
    try:
        r = session.get(self_url, timeout=timeout, verify=True)
    except Exception as e:
        return False, f"{label}: ❌ 验证请求异常: {e}"

    _debug(debug, "GET", self_url, r.status_code, r.text)

    data = {}
    try:
        data = r.json()
    except Exception:
        data = {}

    if r.status_code != 200:
        # 非 200：按状态码分类提示，避免 429/5xx 被误报成 Session 过期误导用户重抓
        if r.status_code == 429:
            return False, f"{label}: ❌ 请求过于频繁（HTTP 429 限流），请稍后重试"
        if r.status_code >= 500:
            return False, f"{label}: ❌ 服务端故障（HTTP {r.status_code}），请稍后重试"
        if r.status_code in (401, 403):
            return False, f"{label}: ❌ 认证失效（HTTP {r.status_code}），Session 可能已过期，请重新抓取"
        return False, f"{label}: ❌ 请求失败（HTTP {r.status_code}）"
    if data.get("success") is False or not data.get("data"):
        msg_text = str(data.get("message") or "")
        if "New-Api-User" in msg_text or "new-api-user" in msg_text:
            return False, f"{label}: ❌ 缺少 user_id（new-api-user 头），请配置 USER_ID 或检查 session 解码"
        return False, f"{label}: ❌ Session 已过期，请重新抓取/填写"

    user_data = data["data"]
    username = user_data.get("username") or user_data.get("email") or "未知用户"

    # ★ 兜底：用 self 返回的真实 id 补齐 user_id
    real_id = user_data.get("id")
    if real_id and session.headers.get("new-api-user") != str(real_id):
        session.headers["new-api-user"] = str(real_id)
        if debug:
            print(f"   [debug] 以 self 返回的真实 id 校正 new-api-user={real_id}")

    # ② 签到 POST /api/user/checkin
    checkin_url = base_url + CHECKIN_URL
    try:
        r2 = session.post(checkin_url, timeout=timeout, verify=True)
    except Exception as e:
        return False, f"{label}: ❌ 签到请求异常: {e}"

    _debug(debug, "POST", checkin_url, r2.status_code, r2.text)

    result = {}
    try:
        result = r2.json()
    except Exception:
        result = {}

    if result.get("success"):
        awarded = fmt_quota((result.get("data") or {}).get("quota_awarded"))
        ok = True
        msg = f"{label}: ✅ 签到成功 | {username} | +{awarded}"
    else:
        fail_msg = result.get("message") or "未知错误"
        # 幂等识别（repeat 单独出现可能是 "do not repeat" 之类的失败文案，需组合匹配）
        if re.search(r"(已签到|已经签到|重复签到|今日已签|already.{0,20}check|repeat.{0,20}(check|sign))",
                     fail_msg, re.IGNORECASE):
            ok = True
            msg = f"{label}: ✅ 今日已签到 | {username} | {fail_msg}"
        else:
            ok = False
            msg = f"{label}: ❌ {fail_msg} | {username}"

    # ③ 本月统计 GET /api/user/checkin?month=YYYY-MM
    now = datetime.now()
    ym = f"{now.year}-{now.month:02d}"
    try:
        r3 = session.get(checkin_url, params={"month": ym}, timeout=timeout, verify=True)
        _debug(debug, "GET", f"{checkin_url}?month={ym}", r3.status_code, r3.text)
        hist = r3.json()
        stats = (hist.get("data") or {}).get("stats") or {}
        cnt = stats.get("checkin_count") or stats.get("total_checkins") or 0
        total = fmt_quota(stats.get("total_quota"))
        if stats:
            msg += f"\n      📊 本月已签 {cnt} 天 | 累计 {total}"
    except Exception:
        pass

    return ok, msg


def signin_site(site_info: dict, timeout: int, debug: bool, proxy: str) -> list:
    """
    处理单个站点的所有账号，返回结果列表。
    """
    site_name = site_info["name"]
    prefix = site_info["env_prefix"]

    raw = get_env_for_site(prefix, "SESSION")
    if not raw:
        print(f"\n⏭️ 未配置 {prefix}_SESSION，跳过 {site_name}")
        return []

    user_ids = parse_accounts(get_env_for_site(prefix, "USER_ID"))
    # session 账号列表过滤空项（USER_ID 需保留空位对齐索引，session 不需要）
    accounts = [a for a in parse_accounts(raw) if a]
    if not accounts:
        print(f"\n⚠️ {prefix}_SESSION 账号列表为空（检查是否只填了分隔符）")
        return []

    print("\n" + "=" * 50)
    print(f"🌐 {site_name}  |  共 {len(accounts)} 个账号  |  {datetime.now():%Y-%m-%d %H:%M:%S}")
    if proxy:
        print(f"🌐 代理: {proxy}")
    print("=" * 50)

    results = []
    for i, sv in enumerate(accounts, start=1):
        print(f"\n▶ 处理 {site_name} 账号 {i} ...")
        uid = user_ids[i - 1] if i - 1 < len(user_ids) else ""
        try:
            ok, msg = signin_one_account(i, site_name, site_info["home"],
                                          sv, uid, timeout, debug, proxy)
        except Exception:
            ok, msg = False, f"{site_name} 账号 {i}: ❌ 脚本异常\n{traceback.format_exc(limit=2)}"
        print(msg)
        results.append({"idx": i, "success": ok, "message": msg, "site": site_name})
        if i < len(accounts):
            time.sleep(2)

    return results


# ====================== 入口 ======================

def main():
    title = "7x.hk / 8s.hk 签到"
    NOTIFY_ENABLED = env_bool("S7X8S_NOTIFY", True)
    NOTIFY_ONLY_FAIL = env_bool("S7X8S_NOTIFY_ONLY_FAIL", False)
    TIMEOUT = env_int("S7X8S_TIMEOUT", 30)
    DEBUG = env_bool("S7X8S_DEBUG", False)
    PROXY = (os.getenv("S7X8S_PROXY") or "").strip()

    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    all_results = []

    for site in SITES:
        results = signin_site(site, TIMEOUT, DEBUG, PROXY)
        if results:
            all_results.extend(results)

    if not all_results:
        print("\n⚠️ 未配置任何站点，请设置 S7XHK_SESSION 或 S8SHK_SESSION 环境变量")
        sys.exit(1)

    # 汇总
    total = len(all_results)
    success_cnt = sum(1 for r in all_results if r["success"])

    print("\n" + "=" * 60)
    print(f"📊 完成：成功 {success_cnt} / 失败 {total - success_cnt}")
    print("=" * 60)

    # 标题按成败分档
    if success_cnt == total:
        report_title = f"✅ {title} 全部成功（{success_cnt}/{total}）"
    elif success_cnt == 0:
        report_title = f"❌ {title} 全部失败（0/{total}）"
    else:
        report_title = f"⚠️ {title} 部分失败（{success_cnt}/{total}）"

    # 按站点分组
    _label_prefix = re.compile(r"^(?:7x\.hk|8s\.hk)\s*账号\s*\d+\s*[:：]\s*")
    ok_by_site = {}
    fail_by_site = {}
    for r in all_results:
        s = r["site"]
        msg_clean = _label_prefix.sub("", r["message"], count=1)
        entry = f"- **[{s} 账号 {r['idx']}]** {msg_clean}"
        if r["success"]:
            ok_by_site.setdefault(s, []).append(entry)
        else:
            fail_by_site.setdefault(s, []).append(entry)

    lines = [f"# {title} - 执行报告", ""]
    lines.append(f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"📊 总计 {total} 账号，✅ {success_cnt} / ❌ {total - success_cnt}")
    lines.append("")

    # 成功组：按站点列出
    if ok_by_site:
        lines.append("## ✅ 成功")
        for site_name in [s["name"] for s in SITES if s["name"] in ok_by_site]:
            lines.append(f"")
            for entry in ok_by_site[site_name]:
                lines.append(entry)
        lines.append("")

    # 失败组
    if fail_by_site:
        lines.append("## ❌ 失败")
        for site_name in [s["name"] for s in SITES if s["name"] in fail_by_site]:
            lines.append(f"")
            for entry in fail_by_site[site_name]:
                lines.append(entry)
        lines.append("")

    body = "\n".join(lines).strip()
    print("\n" + body)

    # 推送
    if NOTIFY_ENABLED:
        if NOTIFY_ONLY_FAIL and success_cnt == total:
            print("ℹ️ S7X8S_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(report_title, body)
    else:
        print("ℹ️ S7X8S_NOTIFY=false，已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
