# -*- coding: utf-8 -*-
"""
new Env('New API 站点自动签到');
cron: 8 8 * * *

脚本来源: 重写自 https://github.com/zhangguoguo1314/quan-zidong-zhushou
适配青龙面板 - 仅依赖 requests + 标准库

支持站点：
  - Liminality 贝之中转站 (beizhi.sylu.cc, New API 新版)   Bearer Token
  - 可萌中转站      (api456.me, New API)               New-Api-User Header + Session Cookie
  - 哈基米 API 站   (api.gemai.cc, New API)            New-Api-User Header
  - DZZI.AI 大肘子API (api.dzzi.ai, New API)            Bearer Token
  - 自定义任意 API 站                                    通过 QZD_CUSTOM JSON 配置

环境变量：
  QZD_BEIZHI    用户名#密码 (多账号 & 分隔)
  QZD_API456    用户名#密码 (多账号 & 分隔)
  QZD_GEMAI     用户名#密码 (多账号 & 分隔)
  QZD_DZZI      用户名#密码 (多账号 & 分隔)
  QZD_CUSTOM    JSON 数组   高级用户自定义站点（详见 README）
  QZD_NOTIFY    true/false  默认 true，是否调用青龙 notify.py 推送
  QZD_NOTIFY_ONLY_FAIL  true/false  默认 false，true 时仅在有失败时推送
  QZD_TIMEOUT   秒          默认 30，HTTP 请求超时
  QZD_PROXY     代理地址    默认空，如 http://172.17.0.1:7890

作者: zephyr_xiao（基于源项目改写）
"""

import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# 强制 stdout 使用 UTF-8，避免 Windows GBK 控制台无法输出 emoji
# （青龙容器为 Linux + UTF-8，无副作用）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
    # 关闭证书警告
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
except ImportError:
    print("❌ 缺少 requests 库，请在青龙面板「依赖管理」-「Python」中安装 requests")
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
    print("⚠️ notify.py 推送最终失败")
    return False


# ====================== 站点预设（来自源项目 SITE_PRESETS） ======================

SITE_PRESETS = {
    "beizhi": {
        "name": "Liminality贝之中转站",
        "type": "custom-api",
        "api_config": {
            "login_url": "https://beizhi.sylu.cc/api/user/login",
            "login_method": "POST",
            "login_body_template": '{"username": "{{username}}", "password": "{{password}}"}',
            "login_content_type": "application/json",
            "token_path": "data.access_token",
            "token_expires_path": "data.access_expires_at",
            "signin_url": "https://beizhi.sylu.cc/api/user/checkin",
            "signin_method": "POST",
            "signin_body": "{}",
            "signin_content_type": "application/json",
            "auth_header_template": "Bearer {{token}}",
            "auth_header_name": "Authorization",
            "success_field": "success",
            "message_field": "message",
            "quota_info_url": "https://beizhi.sylu.cc/api/user/self",
            "quota_field": "data.quota",
            "quota_per_unit": 500000,
            "quota_currency": "$",
        },
    },
    "api456": {
        "name": "可萌中转站",
        "type": "custom-api",
        "api_config": {
            "login_url": "https://api456.me/api/user/login",
            "login_method": "POST",
            "login_body_template": '{"username": "{{username}}", "password": "{{password}}"}',
            "login_content_type": "application/json",
            "token_path": "data.id",
            "signin_url": "https://api456.me/api/user/checkin",
            "signin_method": "POST",
            "signin_body": "{}",
            "signin_content_type": "application/json",
            "auth_header_name": "New-Api-User",
            "auth_header_template": "{{token}}",
            "success_field": "success",
            "message_field": "message",
            "quota_info_url": "https://api456.me/api/user/self",
            "quota_field": "data.quota",
            "quota_per_unit": 500000,
            "quota_currency": "$",
        },
    },
    "gemai": {
        "name": "哈基米API站",
        "type": "custom-api",
        "api_config": {
            "login_url": "https://api.gemai.cc/api/user/login",
            "login_method": "POST",
            "login_body_template": '{"username": "{{username}}", "password": "{{password}}"}',
            "login_content_type": "application/json",
            "token_path": "data.id",
            "signin_url": "https://api.gemai.cc/api/user/checkin",
            "signin_method": "POST",
            "signin_body": "{}",
            "signin_content_type": "application/json",
            "auth_header_name": "New-Api-User",
            "auth_header_template": "{{token}}",
            "success_field": "success",
            "message_field": "message",
            "quota_info_url": "https://api.gemai.cc/api/user/self",
            "quota_field": "data.total_quota",
            "quota_field_fallback": ["data.quota", "data.gift_quota"],
            "quota_per_unit": 500000,
            "quota_currency": "¥",
        },
    },
    "dzzi": {
        "name": "DZZI.AI（大肘子API）",
        "type": "custom-api",
        "api_config": {
            "login_url": "https://api.dzzi.ai/api/user/login",
            "login_method": "POST",
            "login_body_template": '{"username": "{{username}}", "password": "{{password}}"}',
            "login_content_type": "application/json",
            "token_path": "data.access_token",
            "signin_url": "https://api.dzzi.ai/api/user/checkin",
            "signin_method": "POST",
            "signin_body": "{}",
            "signin_content_type": "application/json",
            "auth_header_template": "Bearer {{token}}",
            "auth_header_name": "Authorization",
            "success_field": "success",
            "message_field": "message",
            "quota_info_url": "https://api.dzzi.ai/api/user/self",
            "quota_field": "data.quota",
            "quota_per_unit": 500000,
            "quota_currency": "$",
        },
    },
}


# 环境变量名 -> 预设 key 的映射（按需扩展）
ENV_PRESET_MAP = {
    "QZD_BEIZHI": "beizhi",
    "QZD_API456": "api456",
    "QZD_GEMAI": "gemai",
    "QZD_DZZI": "dzzi",
}


# ====================== 全局配置 ======================

def env_bool(name: str, default: bool) -> bool:
    """安全读取布尔型环境变量。"""
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    """安全读取整型环境变量，非法值回退到 default。"""
    try:
        return int(os.getenv(name) or default)
    except Exception:
        return default


DEFAULT_TIMEOUT = env_int("QZD_TIMEOUT", 30)
NOTIFY_ENABLED = env_bool("QZD_NOTIFY", True)
NOTIFY_ONLY_FAIL = env_bool("QZD_NOTIFY_ONLY_FAIL", False)
PROXY_URL = (os.environ.get("QZD_PROXY") or "").strip()

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ====================== 凭证缓存（token / cookie 落盘复用） ======================

# 缓存目录：脚本同目录 .token/，青龙数据卷持久化；文件名用站点 + 账号 hash 防敏感信息泄漏
TOKEN_DIR = Path(__file__).parent / ".token"


def token_cache_path(site_key: str, username: str) -> Path:
    """返回某站某账号的凭证缓存文件路径。"""
    digest = hashlib.md5(username.encode("utf-8")).hexdigest()[:10]
    return TOKEN_DIR / f"{site_key}_{digest}.json"


def load_token_cache(path: Path) -> dict:
    """读取凭证缓存；文件缺失或损坏返回 {}。"""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"  ⚠️ 读取凭证缓存异常: {e}")
    return {}


def save_token_cache(path: Path, obj: dict) -> None:
    """写入/更新凭证缓存（token 或 cookies）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  💾 已缓存凭证 -> {path.name}")
    except Exception as e:
        print(f"  ⚠️ 保存凭证缓存失败: {e}")


def drop_token_cache(path: Path) -> None:
    """删除失效的凭证缓存，避免反复复用坏凭证。"""
    try:
        if path.exists():
            path.unlink()
            print(f"  🗑️ 已删除失效凭证缓存 {path.name}")
    except Exception as e:
        print(f"  ⚠️ 删除凭证缓存失败: {e}")


def _snapshot_cookies(session) -> list:
    """把 requests.Session 的 Cookie 快照成可 JSON 序列化的 [{name,value}]。"""
    return [{"name": c.name, "value": c.value} for c in session.cookies]


def _restore_cookies(session, cookies: list) -> None:
    """恢复缓存中的 Cookie 到 Session，供 session 型站点（如 New API 传统版）免登录签到。"""
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") and c.get("value"):
            try:
                session.cookies.set(c["name"], c["value"])
            except Exception:
                pass


def _cache_is_valid(cached: dict) -> bool:
    """缓存有效性判定：有 token 且未过期（expires_at 缺省视为不过期）。"""
    if not cached.get("token"):
        return False
    expires_at = cached.get("expires_at")
    if isinstance(expires_at, (int, float)) and time.time() >= expires_at:
        return False
    return True


def _build_cache_payload(session, config: dict, token: str, login_data) -> dict:
    """
    构造三合一缓存内容：token + 会话 Cookie 快照 + 可选过期时间。

    session Cookie 对 New API 传统版（api456/gemai）是必需凭证，
    只存 token 的旧格式会导致缓存命中后仍要重新登录。
    """
    payload = {"token": token, "cookies": _snapshot_cookies(session)}
    expires_path = (config.get("token_expires_path") or "").strip()
    if expires_path and isinstance(login_data, dict):
        expires_at = _extract_field(login_data, expires_path)
        if isinstance(expires_at, (int, float)):
            payload["expires_at"] = int(expires_at)
    return payload


def _build_auth_headers(config: dict, token: str, username: str) -> dict:
    """按站点配置构造带鉴权的请求头（签到与余额查询共用）。"""
    headers = {"User-Agent": DEFAULT_UA}
    template_vars = {"token": token, "username": username, "cookie": ""}
    sh_template = config.get("signin_headers", {})
    if isinstance(sh_template, dict):
        for k, v in sh_template.items():
            headers[k] = _apply_template(str(v), template_vars)

    auth_template = config.get("auth_header_template", "")
    auth_name = config.get("auth_header_name", "Authorization")
    if auth_template and auth_name not in headers:
        headers[auth_name] = _apply_template(auth_template, template_vars)
    return headers


def _fetch_quota(session, config: dict, token: str, username: str):
    """查询账户余额（quota 原始值）。接口未配置、请求失败或字段缺失时返回 None，不影响签到主流程。"""
    quota_url = (config.get("quota_info_url") or "").strip()
    if not quota_url:
        return None
    headers = _build_auth_headers(config, token, username)
    try:
        resp = session.get(quota_url, headers=headers, timeout=DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # 主路径取不到时依次尝试候选路径（部分 New API fork 把总余额放在 total_quota）
    paths = [config.get("quota_field") or "data.quota"]
    fallback = config.get("quota_field_fallback") or []
    if isinstance(fallback, list):
        paths.extend(p for p in fallback if p and p not in paths)
    for path in paths:
        value = _extract_field(data, path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return int(value)
    return None


def _format_quota(raw: int, config: dict) -> str:
    """把 quota 原始值按站点单位换算为带货币符的展示文本。"""
    try:
        per_unit = float(config.get("quota_per_unit") or 500000)
    except (TypeError, ValueError):
        per_unit = 500000.0
    currency = config.get("quota_currency") or "$"
    return f"{currency}{raw / per_unit:.2f}"


# ====================== 工具函数（萃取自 signin_executor.py） ======================

ALREADY_SIGNED_IN_KEYWORDS = [
    "已签到", "已经签到", "今日已签", "您已签到", "您今日已",
    "签到过", "重复签到", "连续签到",
    "already", "already signed", "already signed in",
]


def _is_already_signed_in(msg: str) -> bool:
    """判断响应文本是否包含『已签到』类关键词。"""
    if not msg:
        return False
    msg_lower = msg.lower()
    return any(kw.lower() in msg_lower for kw in ALREADY_SIGNED_IN_KEYWORDS)


def _apply_template(template: str, variables):
    """模板变量替换：{key} 与 {{key}} 都支持。"""
    result = str(template)
    for key, value in variables.items():
        v = str(value) if value is not None else ""
        result = result.replace("{{" + key + "}}", v)
        result = result.replace("{" + key + "}", v)
    return result


def _extract_field(data, path: str):
    """通过点号路径在 dict 中取值，找不到返回 None。"""
    if not path or not isinstance(data, dict):
        return None
    current = data
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _classify_login_error(status_code: int, resp_text: str, default_msg: str) -> str:
    """根据登录响应分类错误，返回友好的中文错误信息。"""
    text = (resp_text or "")[:200].lower()
    if status_code == 401:
        if "password" in text or "密码" in text:
            return f"密码错误 (HTTP 401): {(resp_text or '')[:200]}"
        if "user" in text or "账号" in text or "account" in text:
            return f"账号不存在或认证失败 (HTTP 401): {(resp_text or '')[:200]}"
        return f"认证失败 (HTTP 401): {(resp_text or '')[:200]}"
    if status_code == 403:
        return f"账号被禁止访问 (HTTP 403): {(resp_text or '')[:200]}"
    if status_code == 404:
        return "登录接口不存在 (HTTP 404): 请检查站点配置中的登录URL是否正确"
    if status_code >= 500:
        return f"服务器内部错误 (HTTP {status_code}): 服务端可能暂时不可用"
    return f"{default_msg}: {(resp_text or '')[:200]}"


def _classify_signin_error(status_code: int, resp_text: str) -> str:
    """根据签到响应分类错误。"""
    text = (resp_text or "")[:200].lower()
    if status_code == 401:
        return f"未登录或登录已过期 (HTTP 401): {(resp_text or '')[:200]}"
    if status_code == 403:
        if "permission" in text or "权限" in text:
            return f"权限不足 (HTTP 403): {(resp_text or '')[:200]}"
        return f"禁止访问 (HTTP 403): {(resp_text or '')[:200]}"
    if status_code == 404:
        return "签到接口不存在 (HTTP 404): 请检查站点配置中的签到URL是否正确"
    if status_code == 429:
        return "请求过于频繁 (HTTP 429): 请稍后重试"
    if status_code >= 500:
        return f"服务器内部错误 (HTTP {status_code})"
    return f"HTTP {status_code}: {(resp_text or '')[:200]}"


# ====================== 通用 API 签到执行器 ======================

def execute_signin(site_key: str, api_config: dict, username: str, password: str) -> dict:
    """
    通用 API 签到执行器（同步 requests 版本，对应源项目 _do_execute_signin）。

    流程：
      1. 如果配置了 login_url，先登录拿到 token / cookies
      2. 构造签到请求头与请求体（支持 {{token}} / {{username}} / {{cookie}} 变量）
      3. 发起签到请求
      4. 根据 success_field / message_field 判定结果
      5. 401 时清缓存重新登录后重试一次
    """
    config = api_config or {}
    session = requests.Session()
    session.verify = False
    session.headers.update({"User-Agent": DEFAULT_UA})
    if PROXY_URL:
        session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})

    cache_path = token_cache_path(site_key, username)

    # =============== Step 1: 登录（优先复用缓存凭证） ===============
    token = ""
    login_url = (config.get("login_url") or "").strip()
    if login_url:
        cached = load_token_cache(cache_path)
        if _cache_is_valid(cached):
            token = cached["token"]
            _restore_cookies(session, cached.get("cookies"))
            print(f"  🍪 命中缓存凭证，跳过登录")
        else:
            ok, login_token, err, login_data = _do_login(session, config, username, password)
            if not ok:
                return {"success": False, "error": err}
            token = login_token
            save_token_cache(cache_path, _build_cache_payload(session, config, token, login_data))

    # =============== Step 1.5: 签到前余额（用于计算今日签到奖励） ===============
    quota_before = _fetch_quota(session, config, token, username)

    # =============== Step 2: 签到 ===============
    result = _do_signin(session, config, username, password, token,
                        allow_retry=bool(login_url), cache_path=cache_path)

    # =============== Step 3: 组装余额 / 奖励信息 ===============
    # 401 重试路径会用刷新后的 token 重签，_final_token 保证此处拿到的是最终有效凭证
    if result.get("success") and config.get("quota_info_url"):
        quota_after = _fetch_quota(session, config, result.get("_final_token") or token, username)
        if quota_after is not None:
            quota_str = _format_quota(quota_after, config)
            if result.get("already"):
                result["message"] = f"{result.get('message', '今日已签到')} | 余额 {quota_str}"
            elif quota_before is not None:
                reward = quota_after - quota_before
                if reward > 0:
                    result["message"] = (
                        f"{result.get('message', '签到成功')}，"
                        f"获得 {_format_quota(reward, config)} | 余额 {quota_str}"
                    )
                else:
                    result["message"] = f"{result.get('message', '签到成功')} | 余额 {quota_str}"
            else:
                result["message"] = f"{result.get('message', '签到成功')} | 余额 {quota_str}"

    result.pop("_final_token", None)
    return result


def _do_login(session, config, username, password):
    """登录并提取 token，返回 (ok, token, error, login_data)。login_data 供调用方提取过期时间等字段。"""
    login_url = config.get("login_url", "").strip()
    login_method = (config.get("login_method") or "POST").upper()
    login_body_template = config.get("login_body") or config.get("login_body_template", "{}")
    content_type = config.get("login_content_type", "application/json")

    headers = {"Content-Type": content_type, "User-Agent": DEFAULT_UA}
    body_str = _apply_template(str(login_body_template), {"username": username, "password": password})

    try:
        if login_method == "POST":
            if content_type == "application/x-www-form-urlencoded":
                resp = session.post(login_url, data=body_str, headers=headers, timeout=DEFAULT_TIMEOUT)
            else:
                try:
                    body = json.loads(body_str)
                except (json.JSONDecodeError, TypeError):
                    body = {}
                resp = session.post(login_url, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
        else:
            resp = session.get(login_url, headers=headers, timeout=DEFAULT_TIMEOUT)
    except requests.Timeout:
        return False, "", "登录请求超时，请检查网络或登录接口", {}
    except requests.ConnectionError:
        return False, "", "登录连接失败，请检查网络或站点地址", {}
    except Exception as e:
        return False, "", f"登录过程异常: {str(e)[:300]}", {}

    if resp.status_code == 401:
        return False, "", _classify_login_error(401, resp.text, "密码错误或认证失败"), {}
    if resp.status_code == 403:
        return False, "", _classify_login_error(403, resp.text, "账号被禁止访问"), {}
    if resp.status_code == 404:
        return False, "", _classify_login_error(404, resp.text, "登录接口不存在"), {}
    if resp.status_code >= 500:
        return False, "", _classify_login_error(resp.status_code, resp.text, f"服务器错误 (HTTP {resp.status_code})"), {}

    # 解析登录响应 JSON
    token = ""
    try:
        login_data = resp.json()
    except Exception:
        login_data = {}

    # New API 系站点惯用 HTTP 200 + success:false 表达业务错误（如密码错误），
    # 状态码分支捕获不到，需在提取 token 前先识别
    if isinstance(login_data, dict) and login_data.get("success") is False:
        biz_msg = str(login_data.get("message") or "")[:200]
        if biz_msg:
            if "password" in biz_msg.lower() or "密码" in biz_msg or "incorrect" in biz_msg.lower():
                return False, "", f"密码错误或账号被禁: {biz_msg}", login_data
            return False, "", f"登录失败: {biz_msg}", login_data

    # 提取 token：优先 token_path，再尝试 token_path_fallback 列表
    token_paths = []
    primary = config.get("token_field") or config.get("token_path") or ""
    if primary:
        token_paths.append(primary)
    fallback = config.get("token_path_fallback") or []
    if isinstance(fallback, list):
        token_paths.extend(p for p in fallback if p and p not in token_paths)
    elif isinstance(fallback, str) and fallback and fallback not in token_paths:
        token_paths.append(fallback)

    if token_paths and isinstance(login_data, dict):
        for path in token_paths:
            extracted = _extract_field(login_data, path)
            if extracted not in (None, ""):
                token = str(extracted)
                break

    # 配置了提取路径却全部落空：说明响应结构与预期不符，明确报错而非静默放行
    if token_paths and not token:
        snippet = (resp.text or "")[:200]
        return False, "", (
            f"登录成功但未能从响应提取 token（请检查 token_path 配置）| 响应片段: {snippet}"
        ), login_data

    return True, token, "", login_data


def _do_signin(session, config, username, password, token, allow_retry=True, cache_path=None):
    """构造签到请求并解析结果。cache_path 存在时，401 会清缓存重登并更新缓存。"""
    signin_url = (config.get("signin_url") or "").strip()
    if not signin_url:
        return {"success": False, "error": "未配置签到 URL"}

    method = (config.get("signin_method") or "POST").upper()
    content_type = config.get("signin_content_type", "application/json")

    headers = _build_auth_headers(config, token, username)
    if content_type and "Content-Type" not in headers:
        headers["Content-Type"] = content_type

    # 构造请求体
    template_vars = {"token": token, "username": username, "cookie": ""}
    body_str = _apply_template(str(config.get("signin_body", "{}")), template_vars)

    try:
        if method == "POST":
            if content_type == "application/x-www-form-urlencoded":
                resp = session.post(signin_url, data=body_str, headers=headers, timeout=DEFAULT_TIMEOUT)
            else:
                try:
                    body = json.loads(body_str)
                except (json.JSONDecodeError, TypeError):
                    body = {}
                resp = session.post(signin_url, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
        elif method == "GET":
            resp = session.get(signin_url, headers=headers, timeout=DEFAULT_TIMEOUT)
        else:
            resp = session.request(method, signin_url, data=body_str, headers=headers, timeout=DEFAULT_TIMEOUT)
    except requests.Timeout:
        return {"success": False, "error": "签到请求超时"}
    except requests.ConnectionError:
        return {"success": False, "error": "签到连接失败"}
    except Exception as e:
        return {"success": False, "error": f"签到请求异常: {str(e)[:300]}"}

    # 401 重试一次（先清缓存，避免反复复用坏凭证）
    if resp.status_code == 401 and allow_retry and config.get("login_url"):
        print(f"  ↪ 签到返回 401，清缓存并重新登录后重试...")
        if cache_path:
            drop_token_cache(cache_path)
        ok, new_token, err, login_data = _do_login(session, config, username, password)
        if ok:
            if cache_path:
                save_token_cache(cache_path, _build_cache_payload(session, config, new_token, login_data))
            retry_result = _do_signin(session, config, username, password, new_token,
                                      allow_retry=False, cache_path=cache_path)
            # 回传刷新后的 token，供调用方查余额使用
            retry_result["_final_token"] = new_token
            return retry_result

    result = _parse_signin_response(resp, config)
    result["_final_token"] = token
    return result


def _parse_signin_response(resp, config):
    """解析签到响应。"""
    resp_text = resp.text or ""
    try:
        resp_data = resp.json()
    except Exception:
        resp_data = {"raw": resp_text}

    success_check = config.get("success_check") or config.get("success_field") or ""
    message_field = config.get("message_field", "")

    result = {"success": False, "error": "", "status_code": resp.status_code}
    raw_text = resp_text or str(resp_data)

    # 已签到
    if _is_already_signed_in(raw_text):
        msg = _extract_field(resp_data, message_field) if message_field else "今日已签到"
        result["success"] = True
        result["already"] = True
        result["message"] = str(msg) if msg else "今日已签到"
        result["raw_response"] = resp_text[:500]
        return result

    if isinstance(resp_data, dict):
        if success_check:
            val = _extract_field(resp_data, success_check)
            # 字符串 "0"/"false" 与数字 0 同样表示失败（不少站点以字符串返回状态码）
            if val is not None and val is not False and str(val).strip().lower() not in ("", "0", "false"):
                msg = _extract_field(resp_data, message_field) if message_field else "签到成功"
                result["success"] = True
                result["message"] = str(msg) if msg else "签到成功"
            else:
                msg = _extract_field(resp_data, message_field) if message_field else ""
                result["error"] = str(msg) if msg else "签到失败"
        elif message_field:
            msg = _extract_field(resp_data, message_field)
            # 非空 message 不等于成功：叠加 HTTP 200 门槛 + 失败词黑名单
            # （"积分不足/今日次数已用完"等失败提示不得误报成功）
            fail_hints = ("失败", "不足", "已用完", "用完", "错误", "无权限", "禁止", "不存在")
            if msg:
                if resp.status_code == 200 and not any(h in str(msg) for h in fail_hints):
                    result["success"] = True
                    result["message"] = str(msg)
                else:
                    result["error"] = str(msg)
            elif resp.status_code == 200:
                result["success"] = True
                result["message"] = f"签到请求已发送 (HTTP {resp.status_code})"
            else:
                result["error"] = _classify_signin_error(resp.status_code, resp_text)
        else:
            if resp.status_code == 200:
                result["success"] = True
                result["message"] = "签到请求已发送"
            else:
                result["error"] = _classify_signin_error(resp.status_code, resp_text)
    else:
        if resp.status_code == 200:
            result["success"] = True
            result["message"] = "签到请求已发送"
        else:
            result["error"] = _classify_signin_error(resp.status_code, resp_text)

    result["raw_response"] = resp_text[:500]
    return result


# ====================== Discuz 签到（同步版） ======================

DISCUZ_KEYWORDS = [
    "已签到", "已经签到", "今日已签", "您已签到", "您今日已",
    "连续签到",
    "already", "already signed", "signed in",
]


def _discuz_extract_formhash(html: str) -> str:
    """从 Discuz HTML 中提取 formhash。"""
    if not html:
        return ""
    m = re.search(r'name="formhash"\s+value="([a-zA-Z0-9]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'value="([a-zA-Z0-9]+)"\s+name="formhash"', html)
    if m:
        return m.group(1)
    m = re.search(r'formhash["\s=]+([a-zA-Z0-9]{6,})', html)
    if m:
        return m.group(1)
    return ""


def _discuz_extract_message(text: str) -> str:
    """从 Discuz AJAX 响应中提取消息文本。"""
    if not text:
        return ""
    m = re.search(r'<!\[CDATA\[(.*?)\]\]>', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    clean = re.sub(r"<[^>]+>", "", text).strip()
    return clean[:200] if clean else text[:200]


def discuz_sign_in(site_key: str, api_config: dict, username: str, password: str) -> dict:
    """Discuz! X3.x 论坛签到（k_misign 百变每日签到插件）。登录会话 Cookie 落盘复用。"""
    config = api_config or {}
    base_url = (config.get("base_url") or "").rstrip("/")
    if not base_url:
        return {"success": False, "error": "未配置论坛地址 (base_url)"}
    if not username or not password:
        return {"success": False, "error": "用户名或密码未设置"}

    cookiepre = config.get("cookiepre", "")
    questionid = str(config.get("login_questionid", "0"))
    answer = config.get("login_answer", "")

    session = requests.Session()
    session.verify = False
    session.headers.update({"User-Agent": DEFAULT_UA})
    if PROXY_URL:
        session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})

    cache_path = token_cache_path(site_key, username)

    try:
        # Step 0: 复用缓存 Cookie（命中且登录态有效则跳过登录与 formhash 流程）
        logged_in = False
        sign_page_html = ""
        sign_formhash = ""
        cached = load_token_cache(cache_path)
        if cached and isinstance(cached.get("cookies"), list):
            for c in cached["cookies"]:
                if c.get("name") and c.get("value"):
                    try:
                        session.cookies.set(c["name"], c["value"])
                    except Exception:
                        pass
            try:
                cached_resp = session.get(
                    f"{base_url}/plugin.php?id=k_misign:sign", timeout=DEFAULT_TIMEOUT
                )
                cached_sign_html = cached_resp.text or ""
                if "请先登录" in cached_sign_html:
                    print("  🍪 缓存 Cookie 已失效，删除并重新登录")
                    drop_token_cache(cache_path)
                else:
                    cached_formhash = _discuz_extract_formhash(cached_sign_html)
                    if cached_formhash or "btnvisted" in cached_sign_html:
                        logged_in = True
                        sign_page_html = cached_sign_html
                        sign_formhash = cached_formhash
                        print("  🍪 命中缓存 Cookie，跳过登录")
            except Exception as e:
                print(f"  ⚠️ 缓存 Cookie 校验异常({e})，走完整登录")
                drop_token_cache(cache_path)

        # Step 1-2: 登录（缓存未命中/失效时执行）
        if not logged_in:
            # Step 1: 获取 loginhash 与 formhash
            login_page_url = (
                f"{base_url}/member.php?mod=logging&action=login"
                f"&infloat=yes&handlekey=login&inajax=1"
            )
            page_resp = session.get(login_page_url, timeout=DEFAULT_TIMEOUT)
            page_html = page_resp.text or ""

            if not cookiepre:
                m = re.search(r"var\s+cookiepre\s*=\s*['\"]([^'\"]+)['\"]", page_html)
                if m:
                    cookiepre = m.group(1)

            loginhash = ""
            m = re.search(r"loginhash=([a-zA-Z0-9]+)", page_html)
            if m:
                loginhash = m.group(1)

            formhash = _discuz_extract_formhash(page_html)

            # Step 2: 登录
            login_url = (
                f"{base_url}/member.php?mod=logging&action=login"
                f"&loginsubmit=yes&handlekey=login&loginhash={loginhash}&inajax=1"
            )
            login_data = {
                "formhash": formhash,
                "referer": f"{base_url}/forum.php",
                "loginfield": "username",
                "username": username,
                "password": password,
                "questionid": questionid,
                "answer": answer,
                "cookietime": "2592000",
            }
            login_resp = session.post(
                login_url,
                data=login_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=DEFAULT_TIMEOUT,
            )
            login_text = login_resp.text or ""

            if "欢迎您回来" not in login_text and "登录成功" not in login_text:
                err_match = re.search(r'errorhandle_login\([^)]*"([^"]+)"', login_text)
                if err_match:
                    return {"success": False, "error": f"登录失败: {err_match.group(1)}"}
                if "密码错误" in login_text or "password" in login_text.lower():
                    return {"success": False, "error": "登录失败: 密码错误"}
                if "登录失败" in login_text:
                    return {"success": False, "error": f"登录失败: {login_text[:200]}"}
                # 兜底：检查是否有 auth cookie
                auth_name = f"{cookiepre}auth" if cookiepre else "auth"
                has_auth = any(auth_name in k for k in session.cookies.keys())
                if not has_auth:
                    return {"success": False, "error": f"登录失败: {login_text[:200]}"}

            # 登录成功 → 会话 Cookie 落盘，供下次直通签到
            saved_cookies = [{"name": c.name, "value": c.value} for c in session.cookies]
            save_token_cache(cache_path, {"cookies": saved_cookies})

            # Step 3: 获取签到页面 formhash（k_misign 的入口是 plugin.php，伪静态路径不返回该字段）
            sign_page_resp = session.get(f"{base_url}/plugin.php?id=k_misign:sign", timeout=DEFAULT_TIMEOUT)
            sign_page_html = sign_page_resp.text or ""
            sign_formhash = _discuz_extract_formhash(sign_page_html)
            if not sign_formhash:
                home_resp = session.get(f"{base_url}/forum.php", timeout=DEFAULT_TIMEOUT)
                sign_formhash = _discuz_extract_formhash(home_resp.text or "")
            if not sign_formhash:
                return {"success": False, "error": "无法获取签到 formhash，请检查是否已登录"}

        # Step 3.5: 检测新版 k_misign 已签到标志
        if "btnvisted" in sign_page_html:
            return {"success": True, "message": "今日已签到"}

        # Step 3.6: 检测 tncode 滑块验证码（纯 requests 无法通过）
        tncode_markers = ["tncode", "clicaptcha-submit-info", "v2_captcha_form"]
        if any(marker in sign_page_html for marker in tncode_markers):
            return {
                "success": False,
                "error": "该站点已启用滑块验证码，纯 API 模式无法签到，请使用浏览器驱动模式（模式 C）",
            }

        # Step 4: 执行签到（优先解析新版 J_chkitot 按钮，fallback 到老版逻辑）
        sign_url = None
        # 新版 k_misign：解析 <a class="btn J_chkitot" href="plugin.php?...operation=qiandao...">
        btn_match = re.search(
            r'<a[^>]+class="[^"]*J_chkitot[^"]*"[^>]+href="([^"]*operation=qiandao[^"]*)"',
            sign_page_html,
        )
        if btn_match:
            sign_url = btn_match.group(1)
            if not sign_url.startswith("http"):
                sign_url = base_url + "/" + sign_url.lstrip("/")

        # 老版 fallback
        if not sign_url:
            sign_url = (
                f"{base_url}/plugin.php?id=k_misign:sign"
                f"&operation=qiandao&formhash={sign_formhash}&format=text&inajax=1"
            )

        sign_resp = session.get(sign_url, timeout=DEFAULT_TIMEOUT)
        sign_text = sign_resp.text or ""

        # Step 5: 解析结果
        msg = _discuz_extract_message(sign_text)
        if any(kw in sign_text for kw in DISCUZ_KEYWORDS):
            return {"success": True, "message": msg or "签到成功"}
        # 新版签到成功关键词
        if "签到成功" in sign_text or "签到完成" in sign_text:
            return {"success": True, "message": msg or "签到成功"}
        # 失败词优先于宽松兜底：被踢回登录页/插件报错但 HTTP 200 时不得误判成功
        fail_kw = ("请先登录", "未登录", "操作失败", "操作错误", "无权", "不能", "失败", "错误")
        if any(k in sign_text for k in fail_kw):
            return {"success": False, "error": f"签到失败: {msg or sign_text[:200]}"}
        if "积分" in sign_text or sign_resp.status_code == 200:
            return {"success": True, "message": msg or "签到请求已发送"}
        return {"success": False, "error": f"签到失败: {sign_text[:200]}"}

    except requests.Timeout:
        return {"success": False, "error": "请求超时，请检查网络连接"}
    except requests.ConnectionError:
        return {"success": False, "error": "连接失败，请检查论坛地址是否正确"}
    except Exception as e:
        return {"success": False, "error": f"签到异常: {str(e)[:300]}"}


# ====================== 配置解析 ======================

def parse_credentials(env_value: str):
    """
    解析单个环境变量内的多账号配置。

    支持分隔符：& 或 换行
    每条格式：用户名#密码
    返回：[(username, password), ...]
    """
    if not env_value:
        return []

    env_value = env_value.strip()
    # 同时支持 & 与 换行作为账号分隔符
    parts = re.split(r"[&\n]+", env_value)
    accounts = []
    for raw in parts:
        raw = raw.strip()
        if not raw:
            continue
        if "#" not in raw:
            print(f"  ⚠️ 跳过格式错误的账号（缺少 # 分隔符）: {raw[:30]}")
            continue
        u, p = raw.split("#", 1)
        u, p = u.strip(), p.strip()
        if u and p:
            accounts.append((u, p))
        else:
            print(f"  ⚠️ 跳过空用户名或密码的条目: {raw[:30]}")
    return accounts


def collect_tasks():
    """
    扫描所有 QZD_* 环境变量，构造任务列表：
      [{site_key, site_name, type, api_config, username, password}, ...]
    """
    tasks = []

    # 内置预设
    for env_name, preset_key in ENV_PRESET_MAP.items():
        env_value = os.environ.get(env_name, "")
        if not env_value:
            continue
        preset = SITE_PRESETS.get(preset_key)
        if not preset:
            continue
        accounts = parse_credentials(env_value)
        for u, p in accounts:
            tasks.append({
                "site_key": preset_key,
                "site_name": preset["name"],
                "type": preset["type"],
                "api_config": preset["api_config"],
                "username": u,
                "password": p,
            })

    # 自定义站点 QZD_CUSTOM
    custom_raw = os.environ.get("QZD_CUSTOM", "").strip()
    if custom_raw:
        try:
            custom_list = json.loads(custom_raw)
            if not isinstance(custom_list, list):
                print("⚠️ QZD_CUSTOM 必须是 JSON 数组，已忽略")
                custom_list = []
            for idx, item in enumerate(custom_list):
                if not isinstance(item, dict):
                    print(f"⚠️ QZD_CUSTOM[{idx}] 不是对象，已跳过")
                    continue
                name = item.get("name") or f"自定义站点{idx + 1}"
                stype = (item.get("type") or "custom-api").lower()
                api_config = item.get("api_config") or {}
                username = item.get("username", "")
                password = item.get("password", "")
                if not username or not password:
                    print(f"⚠️ QZD_CUSTOM[{idx}] {name} 缺少 username/password，已跳过")
                    continue
                tasks.append({
                    "site_key": f"custom_{idx}",
                    "site_name": name,
                    "type": stype,
                    "api_config": api_config,
                    "username": username,
                    "password": password,
                })
        except json.JSONDecodeError as e:
            print(f"⚠️ QZD_CUSTOM 不是合法 JSON: {e}")

    return tasks


# ====================== 主流程 ======================

def run_one_task(task) -> dict:
    """执行单个签到任务，返回标准化结果。"""
    site_type = (task.get("type") or "").lower()
    site_key = task.get("site_key", "custom")
    api_config = task.get("api_config") or {}
    username = task.get("username", "")
    password = task.get("password", "")

    try:
        if site_type == "discuz":
            return discuz_sign_in(site_key, api_config, username, password)
        else:
            return execute_signin(site_key, api_config, username, password)
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": f"任务执行异常: {str(e)[:300]}"}


def mask(s: str) -> str:
    """对账号做脱敏展示。"""
    if not s:
        return ""
    if "@" in s:
        head, tail = s.split("@", 1)
        if len(head) <= 2:
            return head[0] + "*@" + tail
        return head[:2] + "*" * (len(head) - 2) + "@" + tail
    # 过短字符串首尾重叠，仅保留首字符避免完整泄露
    if len(s) <= 4:
        return s[0] + "*" * max(1, len(s) - 1)
    return s[:2] + "*" * max(1, len(s) - 4) + s[-2:]


def main():
    title = "New API 站点自动签到"
    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    tasks = collect_tasks()
    if not tasks:
        msg = (
            "⚠️ 未配置任何站点账号。\n"
            "请在青龙面板「环境变量」中配置以下任意一项：\n"
            "  QZD_BEIZHI  = 用户名#密码（贝之中转站）\n"
            "  QZD_API456  = 用户名#密码（可萌中转站）\n"
            "  QZD_GEMAI   = 用户名#密码（哈基米API）\n"
            "  QZD_DZZI    = 用户名#密码（DZZI.AI）\n"
            "  QZD_CUSTOM  = JSON 数组   （自定义站点）\n"
            "多账号请用 & 或换行分隔。"
        )
        print(msg)
        if NOTIFY_ENABLED:
            send_notify(title, msg)
        sys.exit(1)

    print(f"📋 共发现 {len(tasks)} 个签到任务\n")

    results = []
    success_count = 0
    fail_count = 0

    for idx, task in enumerate(tasks, 1):
        site_name = task["site_name"]
        user_show = mask(task["username"])
        print(f"[{idx}/{len(tasks)}] 🔄 {site_name} [{user_show}] 开始签到...")
        t0 = time.time()
        result = run_one_task(task)
        cost = time.time() - t0

        ok = bool(result.get("success"))
        if ok:
            success_count += 1
            msg = result.get("message", "签到成功")
            print(f"           ✅ {msg}  (耗时 {cost:.1f}s)")
        else:
            fail_count += 1
            err = result.get("error", "未知错误")
            print(f"           ❌ {err}  (耗时 {cost:.1f}s)")

        results.append({
            "site_name": site_name,
            "username": user_show,
            "success": ok,
            "message": result.get("message", "") if ok else result.get("error", ""),
        })

        # 多账号之间稍作间隔，避免被风控
        if idx < len(tasks):
            time.sleep(1)

    # ============ 汇总（标题按成败分档 + Markdown 成败分组）============
    if fail_count == 0:
        notify_title = f"✅ {title} 全部成功（{success_count}/{len(tasks)}）"
    elif success_count == 0:
        notify_title = f"❌ {title} 全部失败（0/{len(tasks)}）"
    else:
        notify_title = f"⚠️ {title} 部分失败（{success_count}/{len(tasks)}）"

    ok_lines = [
        f"- ✅ **[{r['site_name']}]** [{r['username']}] {r['message']}"
        for r in results if r["success"]
    ]
    fail_lines = [
        f"- ❌ **[{r['site_name']}]** [{r['username']}] {r['message']}"
        for r in results if not r["success"]
    ]

    lines = [f"# {notify_title} - 执行报告", ""]
    lines.append(f"⏰ 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"📊 总计 {len(tasks)} 个任务，✅ 成功 {success_count}，❌ 失败 {fail_count}")
    if ok_lines:
        lines += ["", "## ✅ 成功", ""] + ok_lines
    if fail_lines:
        lines += ["", "## ❌ 失败", ""] + fail_lines
    report = "\n".join(lines)

    print()
    print("=" * 60)
    print(report)
    print("=" * 60)

    # ============ 推送（失败才推送门控 + 3 次重试）============
    if NOTIFY_ENABLED and (not NOTIFY_ONLY_FAIL or fail_count > 0):
        if NOTIFY_ONLY_FAIL and fail_count == 0:
            print("ℹ️ 全部成功且已开启 QZD_NOTIFY_ONLY_FAIL，跳过推送")
        else:
            send_notify(notify_title, report)
    else:
        print("ℹ️ QZD_NOTIFY=false 或已跳过推送")

    # 有失败时以非零码退出，让青龙任务状态如实反映签到结果
    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
