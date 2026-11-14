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
  SOPHNET_INTERACT        true/false  默认 false，是否把「使用bot交互N分钟」任务做到发奖
  SOPHNET_TIMEOUT         秒          默认 30，HTTP 超时
  SOPHNET_DEBUG           true/false  默认 false，输出接口响应细节用于排错
  SOPHNET_PROXY           代理地址    默认空，如 http://172.17.0.1:7890

凭证获取：在已登录 SophNet 的浏览器标签页打开 DevTools Console 执行
  localStorage.getItem('sophnet-auth-tab-sync-v1')
取返回 JSON 中的 p.refreshToken 字段值即可。

作者: zephyr_xiao
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
URL_INTERACT_REPORT = "/api/sys/checkin/interact/report"   # 上报 bot 交互时长
URL_TASK_CLAIM = "/api/sys/checkin/task/claim"  # 领取任务奖励（兜底）

# 交互任务（福利中心「使用bot交互N分钟」）的服务端限制，均为实测得出：
# 单次上报 ≤1800 秒、两次上报间隔 ≥10 秒、每日累计上限 7200 秒。
INTERACT_CHUNK_MAX = 1800
INTERACT_MIN_INTERVAL = 11      # 服务端要求 10 秒，留 1 秒余量
TASK_TYPE_INTERACT = "INTERACT"

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


# 响应体里可能携带凭证的字段名，DEBUG 输出前统一脱敏
_SECRET_FIELDS = ("token", "refreshToken", "accessToken", "idToken")


def mask_secrets_in_json(text: str) -> str:
    """脱敏接口响应中的凭证字段。

    DEBUG 模式会打印响应原文，而登录/刷新接口的返回里包含 accessToken 与
    refreshToken，直接落进青龙日志等于泄露长期凭证，因此先脱敏再截断。
    """
    try:
        data = json.loads(text)
    except Exception:
        return text

    def _walk(node):
        if isinstance(node, dict):
            return {
                k: (mask_token(v) if k in _SECRET_FIELDS and isinstance(v, str) else _walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    # 紧凑序列化，避免默认空格把 300/400 字符的截断额度吃掉
    return json.dumps(_walk(data), ensure_ascii=False, separators=(",", ":"))


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
    # "signed" 单独匹配会误伤 "unsigned" 等词，故用组合短语匹配；"already" 语义明确可单独保留
    keywords = ["今日已签到", "已签到", "已经签到", "重复签到",
                "already", "signed today"]
    low = msg.lower()
    return any(kw.lower() in low for kw in keywords)


# ============================ 执行层 ============================

def _build_session(timeout: int):
    """构造带默认 UA / 代理 / 超时的 session。
    trust_env 关闭：requests 不再读系统环境/注册表代理，代理只认 SOPHNET_PROXY
    与青龙全局代理变量的显式解析链；TLS 默认校验证书。"""
    session = requests.Session()
    session.trust_env = False
    session.verify = True
    session.headers.update({"User-Agent": DEFAULT_UA})
    proxy = (os.getenv("SOPHNET_PROXY") or "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.request = _wrap_request(session, timeout)
    return session


def _wrap_request(session, timeout):
    """包装 session.request 注入默认 timeout；TLS 证书校验失败时降级重试一次。"""
    _orig = session.request
    state = {"downgraded": False}

    def _request(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        try:
            return _orig(method, url, **kwargs)
        except requests.exceptions.SSLError:
            # 站点边缘节点证书异常的兜底：降级为不校验并告警，仅降一次
            if not state["downgraded"]:
                state["downgraded"] = True
                session.verify = False
                print("⚠️ TLS 证书校验失败，已自动降级为不校验继续（如非预期请核查站点证书/代理）")
                try:
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                except Exception:
                    pass
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
        print(f"   [DEBUG] refresh HTTP {resp.status_code}: {mask_secrets_in_json(resp.text)[:300]}")
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
        print(f"   [DEBUG] welfare HTTP {resp.status_code}: {mask_secrets_in_json(resp.text)[:400]}")
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
        print(f"   [DEBUG] checkin/do HTTP {resp.status_code}: {mask_secrets_in_json(resp.text)[:400]}")
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


def _report_interact(session, access_token: str, seconds: int, debug: bool):
    """上报一次 bot 交互时长。成功返回 (result, '')，失败返回 ({}, err)。

    result 含 accepted / todayTotal / dailyLimit，是服务端给出的权威累计值，
    后续分片以 todayTotal 为准推进，避免自己累加算出偏差。
    """
    try:
        resp = session.post(
            BASE_URL + URL_INTERACT_REPORT,
            params={"durationSeconds": seconds, "clientType": "web"},
            headers={"Authorization": "Bearer " + access_token},
        )
    except Exception as e:
        return {}, f"交互时长上报网络异常: {e}"
    if debug:
        print(f"   [DEBUG] interact/report {seconds}s HTTP {resp.status_code}: " f"{mask_secrets_in_json(resp.text)[:300]}")
    try:
        data = resp.json()
    except Exception:
        return {}, f"交互时长上报响应非 JSON (HTTP {resp.status_code}): {(resp.text or '')[:200]}"
    if not isinstance(data, dict):
        return {}, f"交互时长上报响应格式异常（非对象）: {str(data)[:200]}"
    if data.get("status") != STATUS_OK:
        return {}, f"交互时长上报失败 (status={data.get('status')}): {data.get('message', '')}"
    return data.get("result") or {}, ""


def _claim_task(session, access_token: str, task_key: str, debug: bool):
    """领取单个任务奖励。成功返回 (True, '')。

    正常路径下服务端跨过阈值就自动发奖，这里仅在复查发现
    "已完成但未发放" 时兜底，因此失败只记日志、不当成整体失败。
    """
    try:
        resp = session.post(
            BASE_URL + URL_TASK_CLAIM,
            params={"taskKey": task_key},
            headers={"Authorization": "Bearer " + access_token},
        )
    except Exception as e:
        return False, f"领取奖励网络异常: {e}"
    if debug:
        print(f"   [DEBUG] task/claim {task_key} HTTP {resp.status_code}: " f"{mask_secrets_in_json(resp.text)[:300]}")
    try:
        data = resp.json()
    except Exception:
        return False, f"领取奖励响应非 JSON (HTTP {resp.status_code})"
    if not isinstance(data, dict):
        return False, "领取奖励响应格式异常（非对象）"
    if data.get("status") != STATUS_OK:
        return False, f"领取奖励失败: {data.get('message', '')}"
    return True, ""


def _fill_interact(session, access_token: str, debug: bool) -> str:
    """把当日「使用bot交互N分钟」任务做到发奖，返回日志/推送用的摘要，不抛异常。

    只上报到"最高阈值"而不是服务端允许的每日上限：三档阈值封顶就是最高档，
    多报的秒数不产生额外奖励，只会增加被风控注意的概率。
    """
    welfare, err = _get_welfare(session, access_token, debug)
    if not welfare:
        return f"交互任务跳过（无法获取任务状态：{err}）"

    tasks = [t for t in ((welfare.get("dailyTasks") or {}).get("tasks") or [])
             if t.get("taskType") == TASK_TYPE_INTERACT]
    if not tasks:
        return "交互任务跳过（服务端未下发交互任务）"
    if all(t.get("rewarded") for t in tasks):
        return "交互任务已全部领取"

    # 目标秒数跟随服务端下发的最高阈值，不硬编码档位。
    # 实测 currentProgress == min(当日累计秒数, 阈值)，故未完成任务里的最大值即当日累计。
    target = max(int(t.get("threshold") or 0) for t in tasks)
    today_total = max(int(t.get("currentProgress") or 0) for t in tasks)
    before_earned = int((welfare.get("tokenSummary") or {}).get("totalEarned") or 0)

    reported = 0
    need = target - today_total
    while need > 0:
        chunk = min(INTERACT_CHUNK_MAX, need)
        # 服务端要求两次上报间隔 ≥10 秒；首片同样等待，避免与紧邻请求撞上限流
        time.sleep(INTERACT_MIN_INTERVAL)
        result, rerr = _report_interact(session, access_token, chunk, debug)
        if rerr:
            return f"交互任务上报失败（本次已报 {reported} 秒）：{rerr}"
        reported += int(result.get("accepted") or 0)
        today_total = int(result.get("todayTotal") or (today_total + chunk))
        need = target - today_total

    after, aerr = _get_welfare(session, access_token, debug)
    if not after:
        return f"交互任务复查失败：{aerr}"

    claimed = False
    for t in ((after.get("dailyTasks") or {}).get("tasks") or []):
        if t.get("taskType") != TASK_TYPE_INTERACT:
            continue
        if t.get("completed") and not t.get("rewarded"):
            ok, cerr = _claim_task(session, access_token, t.get("taskKey"), debug)
            if ok:
                claimed = True
            else:
                print(f"   ⚠️ 兜底领取 {t.get('taskKey')} 失败: {cerr}")

    # 兜底领取成功后重新拉一次，否则 pending / 到账金额会停留在领取前的快照
    if claimed:
        refreshed, _ = _get_welfare(session, access_token, debug)
        if refreshed:
            after = refreshed

    interact_tasks = [t for t in ((after.get("dailyTasks") or {}).get("tasks") or [])
                      if t.get("taskType") == TASK_TYPE_INTERACT]
    pending = len([t for t in interact_tasks if not t.get("rewarded")])
    gained = int((after.get("tokenSummary") or {}).get("totalEarned") or 0) - before_earned

    if reported <= 0:
        if pending:
            return f"交互任务累计 {today_total} 秒仍未发奖，稍后重试"
        if gained > 0:
            return f"交互任务补领成功（+{gained // 1000}k Tokens）"
        return f"交互任务当日已累计 {today_total} 秒，无需上报"
    if pending:
        return f"上报 {reported} 秒，仍有 {pending} 项未发奖（+{gained // 1000}k Tokens）"
    return f"上报 {reported} 秒，三项已领取（+{gained // 1000}k Tokens）"


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


def _run_signin(session, access_token: str, debug: bool):
    """执行签到流程，返回 (success, message)。已签到按幂等成功处理。"""
    # 先查签到状态，已签则直接返回（省一次 do 请求，更稳）
    welfare, werr = _get_welfare(session, access_token, debug)
    if welfare and bool((welfare.get("checkIn") or {}).get("todayCheckedIn")):
        return True, f"今日已签到 {_fmt_stats(welfare)}".strip()

    # welfare 拉取失败但不是登录失效时，不阻断签到，继续尝试 do
    # "401" 精确匹配（_get_welfare 的 401 文案固定含 "HTTP 401"），避免代理错误等
    # 消息里出现 "401" 字样时误阻断签到
    if werr and "HTTP 401" in werr:
        return False, werr

    ok, cmsg = _do_checkin(session, access_token, debug)
    if not ok:
        return False, cmsg

    # 签到后再查一次统计（失败不影响判定成功）
    stats = ""
    welfare2, _ = _get_welfare(session, access_token, debug)
    if welfare2:
        stats = _fmt_stats(welfare2)
    return True, f"{cmsg} {stats}".strip()


def run_one_account(refresh_token: str, idx: int, total: int, timeout: int,
                    debug: bool, interact: bool = False) -> dict:
    """单账号完整流程，返回结果 dict（含 success/message/label/interact）。"""
    label = f"账号 {idx} [{mask_token(refresh_token)}]"
    print(f"[{idx}/{total}] 🔄 {label} 开始签到 ...")

    session = _build_session(timeout)

    # 1) 用 refreshToken 换 accessToken
    access_token, err = _refresh_access_token(session, refresh_token, debug)
    if not access_token:
        print(f"           ❌ {err}")
        return {"success": False, "error": err, "label": label}

    # 2) 签到
    ok, msg = _run_signin(session, access_token, debug)
    print(f"           {'✅' if ok else '❌'} {msg}")

    # 3) 交互任务（独立于签到结果，失败不影响签到判定）
    interact_msg = ""
    if interact:
        try:
            interact_msg = _fill_interact(session, access_token, debug)
        except Exception as e:
            traceback.print_exc()
            interact_msg = f"交互任务执行异常（已跳过）: {e}"
        print(f"           🤖 {interact_msg}")

    if not ok:
        return {"success": False, "error": msg, "label": label, "interact": interact_msg}
    return {"success": True, "message": msg, "label": label, "interact": interact_msg}


# ============================ 入口 ============================

def main():
    title = "SophNet 签到"
    NOTIFY_ENABLED = env_bool("SOPHNET_NOTIFY", True)
    NOTIFY_ONLY_FAIL = env_bool("SOPHNET_NOTIFY_ONLY_FAIL", False)
    TIMEOUT = env_int("SOPHNET_TIMEOUT", 30)
    DEBUG = env_bool("SOPHNET_DEBUG", False)
    INTERACT = env_bool("SOPHNET_INTERACT", False)

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
            result = run_one_account(tk, idx, len(tokens), TIMEOUT, DEBUG, INTERACT)
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

    def _report_line(r: dict, idx_fallback: int) -> str:
        """单个账号的推送行：签到结果 + 可选的交互任务摘要。"""
        text = _label_prefix.sub("", r.get("message") or r.get("error", ""), count=1)
        interact_msg = (r.get("interact") or "").strip()
        if interact_msg:
            text = f"{text}（🤖 {interact_msg}）"
        label = r.get("label", f"账号 {idx_fallback}")
        return f"- **[{label}]** {text}"

    ok_lines = [_report_line(r, i) for i, r in enumerate(results, 1) if r.get("success")]
    fail_lines = [_report_line(r, i) for i, r in enumerate(results, 1) if not r.get("success")]

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
