# -*- coding: utf-8 -*-
"""
new Env('SophNet 签到');
cron: 10 8 * * *

SophNet(www.sophnet.com) 福利中心每日签到，签到领 Token 奖励。
纯 API 模式（模式 A），用 refreshToken 兑换短期 accessToken 后签到，无需浏览器/验证码。

环境变量：
  SOPHNET_REFRESH_TOKEN   必填，refreshToken，多账号用 & 或换行分隔
  SOPHNET_NOTIFY          true/false  默认 true，是否调用 notify.py 推送
  SOPHNET_NOTIFY_ONLY_FAIL true/false 默认 false，全成功时静默（需 NOTIFY=true 生效）
  SOPHNET_TIMEOUT         秒          默认 30，HTTP 超时
  SOPHNET_DEBUG           true/false  默认 false，输出接口响应细节用于排错
  SOPHNET_PROXY           代理地址    默认空，如 http://172.17.0.1:7890

凭证获取：在已登录 SophNet 的浏览器标签页打开 DevTools Console 执行
  localStorage.getItem('sophnet-auth-tab-sync-v1')
取返回 JSON 中的 p.refreshToken 字段值即可。

作者: 箫遥风
"""

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

# 1. UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 问题，Linux UTF-8 容器无副作用）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 2. requests 缺失友好提示（不要让脚本直接 ImportError 崩掉）
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

# 3. notify.py 兼容（青龙运行时把 notify.py 加入 sys.path；本地干跑找不到不报错）
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


# 3b. 推送重试封装
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

BASE_URL = "https://www.sophnet.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 签到相关接口（均为相对 BASE_URL 的路径，前缀 /api）
URL_REFRESH = "/api/sys/login/refresh"          # 用 refreshToken 换 accessToken
URL_CHECKIN_DO = "/api/sys/checkin/do"          # 每日签到
URL_WELFARE = "/api/sys/checkin/welfare"        # 签到状态与统计

# SophNet 统一响应状态码
STATUS_OK = 0                  # 请求成功
STATUS_ALREADY = 1             # 今日已签到（幂等）
STATUS_NOT_LOGIN = 10025       # 未登录 / token 失效


# ============================ 通用工具函数 ============================

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


def mask_token(s: str) -> str:
    """token 脱敏：保留前 6 后 4，中间用 * 填充。"""
    if not s:
        return ""
    if len(s) <= 10:
        return s[:2] + "*" * (len(s) - 2)
    return s[:6] + "*" * 6 + s[-4:]


def parse_tokens(env_value: str):
    """解析 refreshToken 列表，支持 & 或换行分隔，自动去空白与空项。"""
    if not env_value:
        return []
    parts = re.split(r"[&\n]+", env_value.strip())
    tokens = []
    for raw in parts:
        raw = raw.strip()
        if raw:
            tokens.append(raw)
    return tokens


def _is_already_signed(msg: str) -> bool:
    """判定 message 是否表示"今日已签到"的幂等提示。"""
    if not msg:
        return False
    # "signed" 单独匹配会误伤 "unsigned" 等词，改为组合匹配；"already" 语义明确可单独保留
    keywords = ["今日已签到", "已签到", "已经签到", "重复签到",
                "already", "signed today"]
    low = msg.lower()
    return any(kw.lower() in low for kw in keywords)


# ============================ 执行层 ============================

def _build_session(timeout: int):
    """构造带默认 UA / 代理 / 超时的 session。"""
    session = requests.Session()
    session.verify = False
    session.headers.update({"User-Agent": DEFAULT_UA})
    proxy = (os.getenv("SOPHNET_PROXY") or "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.request = _wrap_request(session, timeout)
    return session


def _wrap_request(session, timeout):
    """包装 session.request 注入默认 timeout，避免每个请求都写 timeout=。"""
    _orig = session.request

    def _request(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return _orig(method, url, **kwargs)

    return _request


def _refresh_access_token(session, refresh_token: str, debug: bool):
    """用 refreshToken 兑换 accessToken。成功返回 (token, '')，失败返回 ('', err)。"""
    try:
        resp = session.post(
            BASE_URL + URL_REFRESH,
            json={"refreshToken": refresh_token},
            headers={"Content-Type": "application/json"},
        )
    except Exception as e:
        return "", f"刷新 token 网络异常: {e}"
    if debug:
        print(f"   [DEBUG] refresh HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code != 200:
        # 按状态码分类：429 限流 / 5xx 服务端故障与凭证失效区分开，避免误导重抓 token
        if resp.status_code == 429:
            return "", "刷新 token 失败 (HTTP 429 限流)，请稍后重试"
        if resp.status_code >= 500:
            return "", f"刷新 token 失败 (HTTP {resp.status_code} 服务端故障)，请稍后重试"
        return "", f"刷新 token 失败 (HTTP {resp.status_code}): refreshToken 可能已失效"
    try:
        data = resp.json()
    except Exception:
        return "", f"刷新 token 响应非 JSON: {resp.text[:200]}"
    if not isinstance(data, dict):
        return "", f"刷新 token 响应格式异常（非对象）: {str(data)[:200]}"
    if data.get("status") != STATUS_OK:
        return "", f"刷新 token 失败 (status={data.get('status')}): {data.get('message', '')}"
    token = (data.get("result") or {}).get("token")
    if not token:
        return "", "刷新 token 失败: 响应未返回新 accessToken"
    return token, ""


def _get_welfare(session, access_token: str, debug: bool):
    """获取签到状态与统计。成功返回 (dict, '')，失败返回 ({}, err)。"""
    try:
        resp = session.get(
            BASE_URL + URL_WELFARE,
            headers={"Authorization": "Bearer " + access_token},
        )
    except Exception as e:
        return {}, f"获取签到状态网络异常: {e}"
    if debug:
        print(f"   [DEBUG] welfare HTTP {resp.status_code}: {resp.text[:400]}")
    if resp.status_code == 401:
        return {}, "未登录或 accessToken 已失效 (HTTP 401)"
    if resp.status_code != 200:
        return {}, f"获取签到状态失败 (HTTP {resp.status_code})"
    try:
        data = resp.json()
    except Exception:
        return {}, f"签到状态响应非 JSON: {resp.text[:200]}"
    if not isinstance(data, dict):
        return {}, f"签到状态响应格式异常（非对象）: {str(data)[:200]}"
    if data.get("status") != STATUS_OK:
        return {}, f"获取签到状态失败 (status={data.get('status')}): {data.get('message', '')}"
    return data.get("result") or {}, ""


def _do_checkin(session, access_token: str, debug: bool):
    """执行每日签到。返回 (success, message)。
    success=True 表示首次签到成功或今日已签到（幂等）。"""
    try:
        resp = session.post(
            BASE_URL + URL_CHECKIN_DO,
            headers={"Authorization": "Bearer " + access_token,
                     "Content-Type": "application/json"},
        )
    except Exception as e:
        return False, f"签到请求网络异常: {e}"
    if debug:
        print(f"   [DEBUG] checkin/do HTTP {resp.status_code}: {resp.text[:400]}")
    try:
        data = resp.json()
    except Exception:
        # 非 JSON 兜底：200 且文本含成功关键词算成功
        text = resp.text or ""
        if resp.status_code == 200 and ("成功" in text or "success" in text.lower()):
            return True, "签到成功"
        return False, f"签到响应非 JSON (HTTP {resp.status_code}): {text[:200]}"
    # 429 判定提前到业务 status 之前：429 常见响应体是 HTML 或非业务 JSON，
    # 按 status 解析会漏掉限流场景
    if resp.status_code == 429:
        return False, "请求过于频繁 (HTTP 429)，请稍后重试"
    if not isinstance(data, dict):
        return False, f"签到响应格式异常（非对象）: {str(data)[:200]}"
    status = data.get("status")
    message = data.get("message", "") or ""
    if status == STATUS_OK:
        return True, "签到成功"
    if status == STATUS_ALREADY or _is_already_signed(message):
        return True, "今日已签到"
    if status == STATUS_NOT_LOGIN:
        return False, "未登录或 accessToken 已失效"
    return False, f"签到失败 (status={status}): {message}"


def _fmt_stats(welfare: dict) -> str:
    """从 welfare 结果中格式化统计摘要用于推送。"""
    if not welfare:
        return ""
    check_in = welfare.get("checkIn") or {}
    parts = []
    balance = check_in.get("tokenBalance")
    if balance is not None:
        parts.append(f"可提现 {balance // 1000}k Tokens")
    cont = check_in.get("currentContinuousDays")
    if cont is not None:
        parts.append(f"连续 {cont} 天")
    total = check_in.get("totalCheckInDays")
    if total is not None:
        parts.append(f"累计 {total} 天")
    return "（" + "，".join(parts) + "）" if parts else ""


def run_one_account(refresh_token: str, idx: int, total: int, timeout: int, debug: bool) -> dict:
    """单账号签到完整流程，返回结果 dict（含 success/message/label）。"""
    label = f"账号 {idx} [{mask_token(refresh_token)}]"
    print(f"[{idx}/{total}] 🔄 {label} 开始签到 ...")

    session = _build_session(timeout)

    # 1) 用 refreshToken 换 accessToken
    access_token, err = _refresh_access_token(session, refresh_token, debug)
    if not access_token:
        print(f"           ❌ {err}")
        return {"success": False, "error": err, "label": label}

    # 2) 先查签到状态，已签则直接幂等返回（省一次 do 请求，更稳）
    welfare, werr = _get_welfare(session, access_token, debug)
    already_today = False
    if welfare:
        already_today = bool((welfare.get("checkIn") or {}).get("todayCheckedIn"))

    if already_today:
        stats = _fmt_stats(welfare)
        msg = f"今日已签到 {stats}".strip()
        print(f"           ✅ {msg}")
        return {"success": True, "message": msg, "label": label}

    # welfare 拉取失败但不是登录失效时，不阻断签到，继续尝试 do
    # "401" 精确匹配（_get_welfare 的 401 文案固定含 "HTTP 401"），避免代理错误等
    # 消息里出现 "401" 字样时误阻断签到
    if werr and "HTTP 401" in werr:
        print(f"           ❌ {werr}")
        return {"success": False, "error": werr, "label": label}

    # 3) 执行签到
    ok, cmsg = _do_checkin(session, access_token, debug)
    if not ok:
        print(f"           ❌ {cmsg}")
        return {"success": False, "error": cmsg, "label": label}

    # 4) 签到后再查一次统计（失败不影响判定成功）
    stats = ""
    welfare2, _ = _get_welfare(session, access_token, debug)
    if welfare2:
        stats = _fmt_stats(welfare2)
    final_msg = f"{cmsg} {stats}".strip()
    print(f"           ✅ {final_msg}")
    return {"success": True, "message": final_msg, "label": label}


# ============================ 入口 ============================

def main():
    title = "SophNet 签到"
    NOTIFY_ENABLED = env_bool("SOPHNET_NOTIFY", True)
    NOTIFY_ONLY_FAIL = env_bool("SOPHNET_NOTIFY_ONLY_FAIL", False)
    TIMEOUT = env_int("SOPHNET_TIMEOUT", 30)
    DEBUG = env_bool("SOPHNET_DEBUG", False)

    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    tokens = parse_tokens(os.getenv("SOPHNET_REFRESH_TOKEN") or "")
    if not tokens:
        print("⚠️ 未配置任何账号，请设置环境变量 SOPHNET_REFRESH_TOKEN")
        print("   多账号用 & 或换行分隔")
        sys.exit(1)

    print(f"📋 共发现 {len(tokens)} 个账号\n")
    results, success_cnt = [], 0

    for idx, tk in enumerate(tokens, 1):
        t0 = time.time()
        try:
            result = run_one_account(tk, idx, len(tokens), TIMEOUT, DEBUG)
        except Exception:
            traceback.print_exc()
            result = {"success": False, "error": "脚本异常",
                      "label": f"账号 {idx} [{mask_token(tk)}]"}
        cost = time.time() - t0
        if result.get("success"):
            success_cnt += 1
        results.append(result)

        # 风控间隔（必须！多账号别一把梭）
        if idx < len(tokens):
            time.sleep(1)

    # 汇总报告
    total = len(tokens)
    if success_cnt == total:
        report_title = f"✅ {title} 全部成功（{success_cnt}/{total}）"
    elif success_cnt == 0:
        report_title = f"❌ {title} 全部失败（0/{total}）"
    else:
        report_title = f"⚠️ {title} 部分失败（{success_cnt}/{total}）"

    _label_prefix = re.compile(r"^账号\s*\d+\s*[:：]\s*")
    ok_lines = [
        f"- **[{r.get('label', f'账号 {i}')}]** {_label_prefix.sub('', r.get('message') or r.get('error', ''), count=1)}"
        for i, r in enumerate(results, 1) if r.get("success")
    ]
    fail_lines = [
        f"- **[{r.get('label', f'账号 {i}')}]** {_label_prefix.sub('', r.get('message') or r.get('error', ''), count=1)}"
        for i, r in enumerate(results, 1) if not r.get("success")
    ]

    lines = [f"# {report_title} - 执行报告", ""]
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

    # 推送
    if NOTIFY_ENABLED:
        if NOTIFY_ONLY_FAIL and success_cnt == total:
            print("ℹ️ SOPHNET_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(report_title, report)
    else:
        print("ℹ️ SOPHNET_NOTIFY=false，已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
