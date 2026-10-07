# -*- coding: utf-8 -*-
"""
new Env('维咔VikACG签到');
cron: 23 8 * * *

维咔VikACG（www.vikacg.cc，V站）每日钱包签到领积分，纯 API 免验证码。
流程：账密登录换 JWT 双 token（30 天）→ 每日签到（userMission）→ 青龙 notify.py 推送。
登录态（token + 设备指纹）按账号本地缓存复用：缓存有效直接签到；401 先用 refreshToken
静默续期、失败才重新账密登录（缓存只存凭证不存密码）。设备指纹（X-Device-Code）与 token
绑定且随缓存持久化，避免每次运行换设备触发风控。

接口探测结论（2026-10-07，源自前端 Nuxt bundle 逆向 + 真机验证）：
  POST /api/vikacg/v1/login          {"account","password","platform":"web"} → data.token/refreshToken
  POST /api/vikacg/v1/userMission    {} + Bearer → 200 签到成功 / 409「用户今天已签到」（幂等）
  POST /api/vikacg/v1/refreshToken   {"refreshToken":...} → 换发新双 token
  服务端校验浏览器特征头（Sec-Fetch-* 等），缺失报「非法的客户端，请下载官方版本」——
  因此请求头必须完整模拟浏览器（含 Sec-Fetch-Site/Mode/Dest、Sec-Ch-Ua*、X-Client-Name 等）。

环境变量：
  VIKACG_ACCOUNTS        账号：邮箱#密码（多账号用 & 或换行分隔）
  VIKACG_NOTIFY          true/false 是否推送，默认 true
  VIKACG_NOTIFY_ONLY_FAIL true/false 仅失败时推送，默认 false
  VIKACG_TIMEOUT         HTTP 超时秒数，默认 30
  VIKACG_DEBUG           true/false 输出调试细节，默认 false
  VIKACG_PROXY           HTTP/SOCKS 代理，如 http://172.17.0.1:7890（站点在 Cloudflare 后，
                         大陆直连 TLS 常被掐断，一般需要代理；也读全局 HTTPS_PROXY 等变量）
  VIKACG_INSECURE        true/false 跳过 TLS 证书校验（自签代理场景），默认 false
  VIKACG_BASE_URL        站点地址，默认 https://www.vikacg.cc（备用域名时改）
作者: zephyr_xiao
"""
import base64
import json
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

# 1. UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 问题）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 2. requests 缺失友好提示
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


# ============ 常量与环境变量 ============

BASE_URL = os.getenv("VIKACG_BASE_URL", "https://www.vikacg.cc").rstrip("/")
API_BASE = BASE_URL + "/api/vikacg/v1"
TOKEN_CACHE_FILE = Path(__file__).resolve().parent / "vikacg_token.json"

# 站点在 Cloudflare 后，大陆直连 TLS 常被掐断；代理一律非必须，三级回退见 resolve_proxy
GLOBAL_PROXY_KEYS = (
    "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)

ALREADY_SIGNED_IN_KEYWORDS = [
    "已签到", "已经签到", "今日已签", "您已签到", "您今日已",
    "签到过", "重复签到",
    "already signed", "already checked",
]
# ⚠️ 不要把"签到成功""签到完成"放入此列表（首次成功标志，不是幂等提示）；
#    英文关键词用组合词，避免裸 "signed in" 误伤英文成功文案


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
    """解析 'a#b&c#d' / 多行 -> [(user, pwd), ...]，密码含 # 按首个 # 切分"""
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


def resolve_proxy(*specific_keys: str) -> tuple:
    """解析代理地址，返回 (代理地址, 来源变量名)。优先级：专属变量 > 全局代理 > 直连。"""
    for name in (*specific_keys, *GLOBAL_PROXY_KEYS):
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


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
    if len(s) <= 4:
        # 短串头尾切片会重叠导致字符重复出现，直接中间打码
        return s[0] + "*" * (len(s) - 1)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _is_already_signed_in(msg: str) -> bool:
    if not msg:
        return False
    return any(kw.lower() in msg.lower() for kw in ALREADY_SIGNED_IN_KEYWORDS)


def jwt_exp(token: str) -> int:
    """解析 JWT payload 的 exp（Unix 秒）。非标准 JWT / 解析失败返回 0，由调用方回退 TTL。"""
    try:
        parts = (token or "").split(".")
        if len(parts) != 3:
            return 0
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return int(data.get("exp") or 0)
    except Exception:
        return 0


# ============ 请求层 ============

# 服务端校验浏览器特征头（Sec-Fetch-* 等），缺失报「非法的客户端」，
# 因此必须完整模拟 Chrome 浏览器请求；X-Client-Name/Architecture 来自前端 bundle 硬编码。
BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not?A_Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Client-Name": "VikACG Moonlight",
    "Architecture": "AixPot",
    "Referer": BASE_URL + "/",
    "Origin": BASE_URL,
}


def build_headers(device_code: str, client_code: str, token: str = "") -> dict:
    headers = dict(BASE_HEADERS)
    headers["X-Device-Code"] = device_code
    headers["X-Client-Code"] = client_code
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def api_post(session: "requests.Session", path: str, payload: dict, device_code: str,
             client_code: str, token: str = "", timeout: int = 30) -> dict:
    """POST JSON 并返回 {http_code, json}。网络异常上抛由调用方处理。"""
    resp = session.post(
        API_BASE + path,
        json=payload,
        headers=build_headers(device_code, client_code, token),
        timeout=timeout,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"status": "fail", "message": resp.text[:200]}
    return {"http_code": resp.status_code, "json": data if isinstance(data, dict) else {}}


# ============ 凭证缓存（只存凭证与设备指纹，绝不存密码） ============

def read_token_cache() -> dict:
    if not TOKEN_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        return data.get("accounts", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_token_cache(account: str, entry: dict):
    """写入/更新指定账号的凭证缓存。entry 需含 token/refreshToken/deviceCode/clientCode。"""
    data = {"accounts": {}}
    if TOKEN_CACHE_FILE.exists():
        try:
            data = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {"accounts": {}}
    accounts = data.setdefault("accounts", {})
    entry["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    accounts[account] = entry
    TOKEN_CACHE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cached_token_usable(entry: dict) -> bool:
    """缓存 token 是否可直接复用：优先 JWT exp（留 12h 余量），解不出按 14 天 TTL 兜底。"""
    token = entry.get("token") or ""
    if not token:
        return False
    exp = jwt_exp(token)
    if exp > 0:
        return time.time() < exp - 12 * 3600
    saved = entry.get("update_time") or ""
    try:
        saved_ts = datetime.strptime(saved, "%Y-%m-%d %H:%M:%S").timestamp()
        return time.time() - saved_ts < 14 * 24 * 3600
    except Exception:
        return False


# ============ 认证链 ============

def new_device_identity() -> tuple:
    """生成新设备指纹（站点前端即为随机 UUIDv4，持久化后复用以稳定风控画像）。"""
    return str(uuid.uuid4()), str(uuid.uuid4())


def do_login(session, account: str, password: str, timeout: int) -> dict:
    """账密登录。成功返回缓存 entry，失败返回 {"error": ...}。"""
    device_code, client_code = new_device_identity()
    r = api_post(session, "/login",
                 {"account": account, "password": password, "platform": "web"},
                 device_code, client_code, timeout=timeout)
    data = r["json"]
    if r["http_code"] == 200 and data.get("status") == "success" and (data.get("data") or {}).get("token"):
        d = data["data"]
        return {
            "token": d["token"],
            "refreshToken": d.get("refreshToken") or "",
            "deviceCode": device_code,
            "clientCode": client_code,
            "nickname": ((d.get("user") or {}).get("name") or ""),
        }
    msg = data.get("message") or data.get("statusMessage") or ""
    return {"error": f"登录失败 (HTTP {r['http_code']}): {msg or str(data)[:200]}"}


def do_refresh(session, entry: dict, timeout: int) -> dict:
    """用 refreshToken 静默续期。成功返回更新后的 entry，失败返回 None。"""
    refresh_token = entry.get("refreshToken") or ""
    device_code = entry.get("deviceCode") or ""
    client_code = entry.get("clientCode") or ""
    if not refresh_token or not device_code:
        return None
    try:
        r = api_post(session, "/refreshToken", {"refreshToken": refresh_token},
                     device_code, client_code, timeout=timeout)
    except Exception:
        return None
    data = r["json"]
    if r["http_code"] == 200 and data.get("status") == "success" and (data.get("data") or {}).get("token"):
        d = data["data"]
        entry["token"] = d["token"]
        entry["refreshToken"] = d.get("refreshToken") or refresh_token
        return entry
    return None


def ensure_token(session, account: str, password: str, timeout: int) -> dict:
    """三级降级链：缓存 token → refreshToken 续期 → 重新账密登录。返回 {"token",...} 或 {"error"}。"""
    cache = read_token_cache()
    entry = cache.get(account) or {}
    if cached_token_usable(entry):
        return {**entry, "from": "cache"}

    # 缓存过期 → 先尝试 refreshToken 静默续期（不触碰登录流程，对风控友好）
    if entry.get("refreshToken"):
        refreshed = do_refresh(session, entry, timeout)
        if refreshed:
            refreshed.pop("from", None)
            write_token_cache(account, refreshed)
            return {**refreshed, "from": "refresh"}

    # 续期失败 / 无缓存 → 完整账密登录
    result = do_login(session, account, password, timeout)
    if result.get("error"):
        return result
    write_token_cache(account, result)
    return {**result, "from": "login"}


# ============ 签到执行 ============

def run_one_task(session, account: str, password: str, timeout: int, debug: bool) -> dict:
    """单账号签到：取 token → userMission（401 续期/重登后仅重试一次）→ 解析结果。"""
    auth = ensure_token(session, account, password, timeout)
    if auth.get("error"):
        return {"success": False, "error": auth["error"]}
    if debug:
        print(f"           🔍 token 来源: {auth.get('from')}")

    def do_mission(token: str):
        return api_post(session, "/userMission", {},
                        auth.get("deviceCode") or "", auth.get("clientCode") or "",
                        token=token, timeout=timeout)

    result = do_mission(auth["token"])
    data = result["json"]

    # token 失效 → 走续期/重登链路后重试一次（防 401 死循环，只重试一次）
    # 注意：403 不进降级链——它通常是「非法的客户端」这类头校验拒绝，重登必然同样失败
    if result["http_code"] == 401:
        print("           🔄 登录态失效，尝试续期/重新登录...")
        refreshed = do_refresh(session, auth, timeout)
        if refreshed:
            refreshed.pop("from", None)
            write_token_cache(account, refreshed)
            auth = refreshed
        else:
            auth = do_login(session, account, password, timeout)
            if auth.get("error"):
                return {"success": False, "error": "续期失败且重登失败: " + auth["error"]}
            write_token_cache(account, auth)
        result = do_mission(auth["token"])
        data = result["json"]

    http_code = result["http_code"]
    message = data.get("message") or data.get("statusMessage") or ""

    # 幂等判定优先于成败解析（服务端语义：HTTP 409 + 「用户今天已签到」）
    if _is_already_signed_in(message):
        return {"success": True, "message": f"今日已签到（{message}）"}

    if http_code == 200 and data.get("status") == "success":
        d = data.get("data") or {}
        balance = d.get("count")
        sign_days = d.get("sign_days")
        sign_count = d.get("sign_count")
        parts = []
        if sign_days is not None:
            parts.append(f"连续签到 {sign_days} 天")
        if sign_count is not None:
            parts.append(f"累计 {sign_count} 次")
        if balance is not None:
            parts.append(f"积分余额 {balance}")
        nickname = auth.get("nickname") or ""
        summary = "，".join(parts) if parts else ""
        who = f"（{nickname}）" if nickname else ""
        return {"success": True, "message": "签到成功" + who + ("：" + summary if summary else "")}

    if http_code == 401:
        return {"success": False, "error": "未登录或登录已过期 (HTTP 401)"}
    if http_code >= 500:
        return {"success": False, "error": f"服务器内部错误 (HTTP {http_code})"}
    return {"success": False, "error": f"签到失败 (HTTP {http_code}): {message or str(data)[:200]}"}


# ============ 主流程 ============

def main():
    title = "维咔VikACG签到"
    notify_enabled = env_bool("VIKACG_NOTIFY", True)
    notify_only_fail = env_bool("VIKACG_NOTIFY_ONLY_FAIL", False)
    timeout = env_int("VIKACG_TIMEOUT", 30)
    debug = env_bool("VIKACG_DEBUG", False)

    proxy, proxy_source = resolve_proxy("VIKACG_PROXY")

    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)
    if proxy:
        print(f"🌐 代理: {proxy} （来源: {proxy_source}）")
    else:
        print("🌐 代理: 未配置，直连（站点在 Cloudflare 后，直连失败请配 VIKACG_PROXY）")

    accounts = parse_credentials(os.getenv("VIKACG_ACCOUNTS", ""))
    if not accounts:
        print("⚠️ 未配置任何账号，请设置 VIKACG_ACCOUNTS（格式：邮箱#密码，多账号 & 或换行分隔）")
        # 配置缺失按失败处理：退出码 1 + 推送提醒，避免青龙任务显示绿色"成功"掩盖问题
        msg = "⚠️ 维咔VikACG签到：未配置 VIKACG_ACCOUNTS，任务未执行"
        if notify_enabled:
            send_notify("❌ 维咔VikACG签到 未配置账号", msg)
        sys.exit(1)

    print(f"📋 共发现 {len(accounts)} 个账号\n")
    session = requests.Session()
    session.headers.update({"User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")})
    # 默认校验 TLS 证书（登录请求含明文密码，禁校验有 MITM 风险）；
    # 自签代理等场景用 VIKACG_INSECURE=true 显式放开
    session.verify = not env_bool("VIKACG_INSECURE", False)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    results, success_cnt = [], 0
    for idx, (account, password) in enumerate(accounts, 1):
        print(f"[{idx}/{len(accounts)}] 🔄 维咔VikACG [{mask(account)}] ...")
        t0 = time.time()
        try:
            result = run_one_task(session, account, password, timeout, debug)
        except Exception:
            traceback.print_exc()
            result = {"success": False, "error": "脚本异常（网络或依赖问题）"}
        cost = time.time() - t0

        if result.get("success"):
            success_cnt += 1
            print(f"           ✅ {result.get('message', '签到成功')} ({cost:.1f}s)")
        else:
            print(f"           ❌ {result.get('error', '未知错误')} ({cost:.1f}s)")
        results.append({"label": mask(account), **result})

        # 风控间隔：多账号别一把梭
        if idx < len(accounts):
            time.sleep(2)

    total = len(accounts)
    if success_cnt == total:
        notify_title = f"✅ {title} 全部成功（{success_cnt}/{total}）"
    elif success_cnt == 0:
        notify_title = f"❌ {title} 全部失败（0/{total}）"
    else:
        notify_title = f"⚠️ {title} 部分失败（{success_cnt}/{total}）"

    ok_lines = [f"- **[{r.get('label', '')}]** {r.get('message') or r.get('error', '')}"
                for r in results if r.get("success")]
    fail_lines = [f"- **[{r.get('label', '')}]** {r.get('message') or r.get('error', '')}"
                  for r in results if not r.get("success")]

    lines = [f"# {notify_title} - 执行报告", ""]
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
            print("ℹ️ VIKACG_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(notify_title, report)
    else:
        print("ℹ️ VIKACG_NOTIFY=false，已禁用推送")

    # 全失败 exit(1)（青龙红色标记）；部分失败仍 exit(0)，详情看推送
    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
