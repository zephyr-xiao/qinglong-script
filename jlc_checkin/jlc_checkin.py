# -*- coding: utf-8 -*-
"""
new Env('嘉立创签到');
cron: 40 8 * * *

嘉立创(立创商城 m.jlc.com)每日签到脚本,移植自 GitHub Foticing/LC-AutoSign。
签到领金豆,第七天自动领取 8 金豆券,查询金豆余额。

认证方式:AccessToken(嘉立创 App 抓包获取),多账号换行分隔。
抓 Token 教程见同目录 README.md(必须用 Chrome 设备模拟,Edge 不行)。

环境变量:
  JLC_TOKEN                 必填,AccessToken,多账号用换行分隔
  JLC_PROXY                 可选,HTTP 代理,如 http://172.17.0.1:7890
  JLC_NOTIFY                true/false 默认 true,是否调用青龙 notify.py 推送
  JLC_NOTIFY_ONLY_FAIL      true/false 默认 false,全部成功时静默
  JLC_TIMEOUT               请求超时秒数,默认 30
  JLC_DEBUG                 true/false 默认 false,输出接口响应细节用于排错

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

# 三个接口均只靠 X-JLC-AccessToken 请求头鉴权
ASSETS_URL = "https://m.jlc.com/api/appPlatform/center/assets/selectPersonalAssetsInfo"
SIGN_URL = "https://m.jlc.com/api/activity/sign/signIn?source=3"
SEVENTH_DAY_URL = "https://m.jlc.com/api/activity/sign/receiveVoucher"

# 上游验证过的移动 App WebView UA,接口侧按此识别客户端
DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Html5Plus/1.0 "
    "(Immersed/20) JlcMobileApp"
)

# 已签到幂等关键词(命中即视为成功跳过;不含"签到成功"等首次成功标志)
ALREADY_SIGNED_IN_KEYWORDS = [
    "已签到", "已经签到", "今日已签", "您已签到", "您今日已",
    "签到过", "重复签到",
]

# 网络重试
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5

DEBUG = False
HTTP_TIMEOUT = 30


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


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except Exception:
        return default


def mask(token_or_code: str) -> str:
    """账号脱敏:保留前 2 后 2,过短全打码。"""
    s = str(token_or_code or "")
    if not s:
        return "****"
    if len(s) <= 4:
        return s[:1] + "*" * (len(s) - 1)
    return s[:2] + "*" * max(1, len(s) - 4) + s[-2:]


def _is_already_signed_in(msg: str) -> bool:
    if not msg:
        return False
    return any(kw in msg for kw in ALREADY_SIGNED_IN_KEYWORDS)


def rand_sleep(min_s: float, max_s: float):
    """随机休眠,用于请求间隔降低风控特征。"""
    time.sleep(random.uniform(min_s, max_s))


def http_get(session: requests.Session, url: str) -> dict:
    """
    带重试的 GET,响应按 JSON 解析。
    仅网络异常重试(线性退避);业务失败(success=false)不打重试,由调用方分类。
    返回 {"ok": True, "data": <json dict>} 或 {"ok": False, "error": str}。
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=HTTP_TIMEOUT)
            if DEBUG:
                print(f"   🔍 GET {url[:70]} → {resp.status_code} {resp.text[:200]}")
            try:
                body = resp.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return {"ok": False, "error": f"HTTP {resp.status_code} 非 JSON 响应: {resp.text[:100]}"}
            return {"ok": True, "data": body}
        except requests.RequestException as e:
            last_err = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * attempt
                print(f"   ⚠️ 请求失败({type(e).__name__}),{delay}s 后重试 {attempt}/{MAX_RETRIES - 1}")
                time.sleep(delay)
    return {"ok": False, "error": f"网络异常: {type(last_err).__name__}: {last_err}"[:100]}


def build_session(access_token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "X-JLC-AccessToken": access_token,
    })
    proxy = os.getenv("JLC_PROXY", "").strip()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        print(f"🌐 使用代理: {proxy}")
    return session


# ============================ 响应解析(纯函数,可单测) ============================

def classify_sign_response(body: dict) -> dict:
    """
    分类签到接口响应,三态:signed(签到成功带收益) / seventh(第七天待领券) /
    duplicate(已签到幂等) / failed(业务失败)。
    返回 {status, message, gain}(gain 仅 signed 态有值)。
    """
    if not body.get("success"):
        msg = str(body.get("message") or "未知错误")
        if _is_already_signed_in(msg):
            return {"status": "duplicate", "message": "今日已签到", "gain": None}
        return {"status": "failed", "message": f"签到失败 - {msg[:80]}", "gain": None}

    data = body.get("data") or {}
    gain_num = data.get("gainNum")
    status = data.get("status")

    # 上游逻辑:status>0 且无 gainNum 是第七天,需另调 receiveVoucher 领 8 金豆
    # status 容忍 float(JSON 可能给 7.0),但排除 bool(True/False 是 int 子类)
    if isinstance(status, (int, float)) and not isinstance(status, bool) and status > 0 and not gain_num:
        return {"status": "seventh", "message": "第七天签到,待领券", "gain": None}

    if gain_num:
        return {"status": "signed", "message": "签到成功", "gain": gain_num}

    # success=true 但既非第七天也无收益:按已签幂等兜底,不误报失败
    return {"status": "duplicate", "message": "已签到(无收益数据)", "gain": None}


def parse_assets(body: dict) -> dict:
    """
    解析资产查询响应。返回 {valid, customer_code, beans}。
    valid=False 表示 token 失效或接口异常。
    """
    if not body.get("success"):
        return {"valid": False, "customer_code": None, "beans": None}
    data = body.get("data") or {}
    customer_code = data.get("customerCode")
    if not customer_code:
        return {"valid": False, "customer_code": None, "beans": None}
    return {
        "valid": True,
        "customer_code": str(customer_code),
        "beans": data.get("integralVoucher"),
    }


# ============================ 单账号流程 ============================

def run_one_account(access_token: str, index: int) -> dict:
    """完整跑一个账号:token 探测 → 签到 → (第七天领券) → 余额。"""
    result = {
        "label": f"账号{index + 1}",
        "success": False,
        "token_invalid": False,
        "message": "",
        "gain": None,
        "beans": None,
    }

    session = build_session(access_token)
    print(f"\n======== {result['label']} (token {mask(access_token)}) ========")

    # 1. 资产查询兼 token 有效性探测(拿到 customerCode 与签到前余额)
    assets_resp = http_get(session, ASSETS_URL)
    if not assets_resp["ok"]:
        result["message"] = assets_resp["error"]
        print(f"   ❌ {result['message']}")
        return result
    assets = parse_assets(assets_resp["data"])
    if not assets["valid"]:
        result["token_invalid"] = True
        result["message"] = "AccessToken 已失效,请重新抓包获取!"
        print(f"   ❌ {result['message']}")
        return result

    result["beans"] = assets["beans"]
    print(f"   👤 客户编码 {mask(assets['customer_code'])},当前金豆 {assets['beans']}")

    # 2. 签到
    sign_resp = http_get(session, SIGN_URL)
    if not sign_resp["ok"]:
        result["message"] = sign_resp["error"]
        print(f"   ❌ {result['message']}")
        return result
    sign = classify_sign_response(sign_resp["data"])
    print(f"   📝 签到: {sign['message']}")

    if sign["status"] == "failed":
        result["message"] = sign["message"]
        return result

    # 3. 第七天分支:另调领券接口,固定 8 金豆(上游硬编码值)
    if sign["status"] == "seventh":
        seventh_resp = http_get(session, SEVENTH_DAY_URL)
        if seventh_resp["ok"] and seventh_resp["data"].get("success"):
            sign["message"] = "第七天签到成功,领取 8 金豆券"
            sign["gain"] = 8
            print(f"   🎉 {sign['message']}")
        else:
            sign["message"] = "第七天签到,领券接口无收益"
            print(f"   ℹ️ {sign['message']}")

    # 4. 签到成功后刷新余额(signed/seventh/duplicate 三态统一走这里)
    final_resp = http_get(session, ASSETS_URL)
    if final_resp["ok"]:
        final_assets = parse_assets(final_resp["data"])
        if final_assets["valid"]:
            result["beans"] = final_assets["beans"]

    result["success"] = True
    result["gain"] = sign["gain"]
    result["message"] = sign["message"]
    if result["beans"] is not None:
        result["message"] += f",余额 {result['beans']} 金豆"
    print(f"   ✅ {result['message']}")
    return result


# ============================ 主流程 ============================

def main():
    global DEBUG, HTTP_TIMEOUT
    DEBUG = env_bool("JLC_DEBUG", False)
    HTTP_TIMEOUT = env_int("JLC_TIMEOUT", 30)

    notify_enabled = env_bool("JLC_NOTIFY", True)
    notify_only_fail = env_bool("JLC_NOTIFY_ONLY_FAIL", False)

    raw = os.getenv("JLC_TOKEN", "").strip()
    if not raw:
        print("❌ 未设置 JLC_TOKEN 环境变量")
        sys.exit(1)

    # 多账号按行分隔(兼容逗号,便于从上游 TOKEN_LIST 直接迁移)
    token_list = [t.strip() for t in re.split(r"[\n,]+", raw) if t.strip()]
    print(f"🚀 嘉立创签到启动,共 {len(token_list)} 个账号")

    results = []
    for i, token in enumerate(token_list):
        try:
            r = run_one_account(token, i)
        except Exception:
            r = {"label": f"账号{i + 1}", "success": False, "token_invalid": False,
                 "message": traceback.format_exc().splitlines()[-1][:80],
                 "gain": None, "beans": None}
            print(f"   ❌ {r['message']}")
        results.append(r)
        # 多账号风控间隔
        if i < len(token_list) - 1:
            rand_sleep(3, 6)

    # 汇总报告(标题三档 + 正文按成败分组)
    total = len(results)
    success_cnt = sum(1 for r in results if r["success"])
    fail_cnt = total - success_cnt
    invalid_cnt = sum(1 for r in results if r["token_invalid"])

    if success_cnt == total:
        report_title = f"✅ 嘉立创全部成功({success_cnt}/{total})"
    elif invalid_cnt > 0 and invalid_cnt == total:
        report_title = f"❌ 嘉立创 Token 全部失效({invalid_cnt}/{total})"
    elif success_cnt == 0:
        report_title = f"❌ 嘉立创全部失败(0/{total})"
    else:
        report_title = f"⚠️ 嘉立创部分成功({success_cnt}/{total})"

    lines = [f"# {report_title}", ""]
    lines.append(f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"📊 账号 {total} 个,✅ {success_cnt},❌ {fail_cnt}")
    lines.append("")
    ok_lines = [f"- **[{r['label']}]** {r['message']}"
                for r in results if r["success"]]
    fail_lines = [f"- **[{r['label']}]** {r['message']}"
                  for r in results if not r["success"]]
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
        if notify_only_fail and fail_cnt == 0:
            print("ℹ️ JLC_NOTIFY_ONLY_FAIL=true 且全部成功,跳过推送")
        else:
            send_notify(report_title, report)
    else:
        print("ℹ️ JLC_NOTIFY=false,已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
