#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new Env('DZZI.AI 自动签到');
cron: 8 8 * * *

DZZI.AI (New API) 自动签到脚本

支持功能：
- 多账号批量签到（账号格式：user|pwd 换行 或 JSON 数组）
- 签到前先查询状态，已签到则跳过（幂等）
- 推送：优先青龙 notify.py 全渠道，无青龙环境时回退内置飞书/Server酱/Telegram/Bark
- GitHub Actions / 青龙面板 / 本地 / 任意 Linux cron 通用

环境变量：
  DZZI_ACCOUNTS    账号列表。格式1：user1|pwd1\nuser2|pwd2；格式2：JSON 数组 [{"username","password"}]
  DZZI_NOTIFY      是否推送结果，true/false，默认 true
  NOTIFIER         回退推送方式（feishu/serverchan/telegram/bark），可选
  NOTIFIER_TOKEN   回退推送 token，可选
  本地可用 .env 文件注入（青龙环境忽略）

API 路径（参考 QuantumNous/new-api controller/checkin.go）：
- 登录:    POST /api/user/login
- 状态:    GET  /api/user/checkin
- 执行:    POST /api/user/checkin
作者: 箫遥风
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 编码问题）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# requests 缺失友好提示，不要让脚本裸抛 ImportError
try:
    import requests
except ImportError:
    print("❌ 缺少 requests 库，请在青龙面板「依赖管理」-「Python」中安装 requests")
    sys.exit(1)

DEFAULT_BASE_URL = "https://api.dzzi.ai"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
QUOTA_PER_YUAN = 500000  # new-api 内 1 元 = 500000 quota

# token 缓存目录（脚本同目录，青龙数据卷持久化；文件名用账号 hash，防敏感信息泄漏）
TOKEN_DIR = Path(__file__).parent / ".token"


def token_cache_path(username: str, base_url: str) -> Path:
    """返回某账号的 token 缓存文件路径。"""
    digest = hashlib.md5(f"{username}@{base_url}".encode("utf-8")).hexdigest()[:12]
    return TOKEN_DIR / f"{digest}.json"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def load_env_file(path: str = ".env") -> None:
    """加载 .env 文件注入环境变量（青龙环境无 .env 直接跳过，仅本地便捷用）。"""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and not os.environ.get(key):
                    os.environ[key] = val
    except OSError:
        pass


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def mask(s: str) -> str:
    """账号脱敏：保留前 2 后 2，邮箱保留 @ 之后，避免完整账号泄入日志/推送。"""
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


def log(level: str, msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}", flush=True)


# 服务端幂等提示关键词：命中表示"今日已签到"，应视为成功跳过而非失败。
# ⚠️ 不可放入"签到成功""签到完成"——那是首次签到成功的标志，放入会把首次成功覆盖成"已签到"。
ALREADY_SIGNED_IN_KEYWORDS = [
    "已签到", "已经签到", "今日已签", "您已签到", "您今日已",
    "签到过", "重复签到", "连续签到",
    "already", "already signed", "signed in", "signed-in", "signed",
]


def _is_already_signed_in(msg: str) -> bool:
    """判定服务端返回是否为"已签到"幂等提示（GET 失败兜底 / POST 幂等返回）。"""
    if not msg:
        return False
    return any(kw.lower() in msg.lower() for kw in ALREADY_SIGNED_IN_KEYWORDS)


class AuthExpiredError(Exception):
    """token 失效，需清缓存重新登录。"""


def _is_auth_failure(data: dict, code: int) -> bool:
    """判定响应是否表示 token 失效（缓存复用时应清缓存重登）。"""
    if code == 401:
        return True
    msg = " ".join(str(data.get(k, "")) for k in ("message", "msg", "error", "detail"))
    low = msg.lower()
    return any(kw in low for kw in (
        "未登录", "登录已过期", "会话已过期", "token 失效", "token已失效",
        "not logged", "unauthorized", "invalid token", "access token 失效",
    ))


def quota_to_yuan(quota: int) -> float:
    """new-api 内 1 元 = 500000 quota，换算成元便于阅读。"""
    if quota is None:
        return 0.0
    return round(quota / QUOTA_PER_YUAN, 5)


def parse_accounts(raw: str) -> List[Dict[str, str]]:
    """
    支持两种账号配置方式：
    1. JSON 数组：   '[{"username":"a","password":"b"}]'（可带 base_url）
    2. 换行/分号分隔： user1|pwd1\nuser2|pwd2  或  user1:pwd1;user2:pwd2
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        data = json.loads(raw)
        accounts: List[Dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("username") and item.get("password"):
                accounts.append({
                    "username": str(item["username"]).strip(),
                    "password": str(item["password"]).strip(),
                    "base_url": str(item.get("base_url") or DEFAULT_BASE_URL).strip(),
                })
        return accounts

    accounts = []
    for line in raw.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 同时支持 : 与 | 分隔
        sep = "|" if "|" in line else (":" if ":" in line else None)
        if not sep:
            continue
        user, pwd = line.split(sep, 1)
        accounts.append({
            "username": user.strip(),
            "password": pwd.strip(),
            "base_url": DEFAULT_BASE_URL,
        })
    return accounts


# ---------------------------------------------------------------------------
# 签到核心
# ---------------------------------------------------------------------------
class DzziClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        self.user_id: Optional[int] = None
        self.display_name: Optional[str] = None
        # ★ token 缓存：命中则直接注入认证头，跳过重复登录
        self._cache_path = token_cache_path(self.username, self.base_url)
        self._cached = self._load_token_cache()

    # -------------------- token 缓存 --------------------

    def _load_token_cache(self) -> bool:
        """尝试从本地缓存恢复 access_token，命中则注入认证头并返回 True。"""
        try:
            if not self._cache_path.exists():
                return False
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if data.get("base_url") != self.base_url:
                return False
            token = str(data.get("access_token") or "")
            if not token:
                return False
            self.session.headers["Authorization"] = f"Bearer {token}"
            uid = data.get("user_id")
            if uid:
                self.user_id = int(uid)
                self.session.headers["New-Api-User"] = str(uid)
            self.display_name = data.get("display_name") or self.username
            log("INFO", f"[{self.label}] 🍪 命中 token 缓存，跳过登录")
            return True
        except Exception as exc:
            log("WARN", f"[{self.label}] 读取 token 缓存异常: {exc}")
            return False

    def _save_token_cache(self) -> None:
        """登录成功后把 access_token / user_id 落盘，供下次直通签到。"""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "base_url": self.base_url,
                "access_token": self.session.headers.get("Authorization", "").replace("Bearer ", ""),
                "user_id": self.user_id,
                "display_name": self.display_name,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log("INFO", f"[{self.label}] 💾 已缓存 token -> {self._cache_path.name}")
        except Exception as exc:
            log("WARN", f"[{self.label}] 保存 token 缓存失败: {exc}")

    def _drop_token_cache(self) -> None:
        """token 失效时删除缓存文件，避免反复复用坏 token。"""
        try:
            if self._cache_path.exists():
                self._cache_path.unlink()
                log("WARN", f"[{self.label}] 🗑️ 已删除失效 token 缓存")
        except Exception as exc:
            log("WARN", f"[{self.label}] 删除 token 缓存失败: {exc}")

    def ensure_auth(self) -> bool:
        """确保已认证：优先复用缓存 token，未命中/已失效才真正登录。登录成功后落盘。"""
        if self._cached:
            return True
        ok = self.login()
        if ok:
            self._save_token_cache()
            self._cached = True
        return ok

    @property
    def label(self) -> str:
        """脱敏后的账号标识，用于日志与推送，避免泄露完整账号。"""
        return mask(self.username)

    def _has_auth(self) -> bool:
        """是否已携带认证凭据（Bearer token 或 New-Api-User 头，兼容缓存命中时缺 user_id 的场景）。"""
        return bool(self.session.headers.get("Authorization") or self.user_id)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], int]:
        url = self._url(path)
        try:
            resp = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            return {"success": False, "message": f"网络异常: {exc}"}, -1
        try:
            data = resp.json()
        except ValueError:
            data = {"success": False, "message": f"非 JSON 响应 (HTTP {resp.status_code})"}
        return data, resp.status_code

    def login(self) -> bool:
        """登录拿 access_token 与 user id。turnstile 留空即可（dzzi 未启用）。"""
        data, _ = self._request(
            "POST",
            "/api/user/login",
            params={"turnstile": ""},
            json_body={"username": self.username, "password": self.password},
        )
        if not data.get("success"):
            log("ERROR", f"[{self.label}] 登录失败: {data.get('message')}")
            return False
        payload = data.get("data") or {}
        user = payload.get("user") or {}
        self.user_id = user.get("id") or payload.get("id")
        self.display_name = user.get("display_name") or payload.get("display_name") or self.username
        access_token = payload.get("access_token") or ""
        # dzzi 实测签到/状态接口走 Bearer token 认证（旧版 New-Api-User 头返回 401）
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"
        if self.user_id:
            # 顺带兼容老版本 new-api 的 New-Api-User 头认证
            self.session.headers["New-Api-User"] = str(self.user_id)
        log("INFO", f"[{self.label}] 登录成功 (id={self.user_id})")
        return True

    def get_status(self) -> Optional[Dict[str, Any]]:
        if not self._has_auth():
            return None
        data, code = self._request("GET", "/api/user/checkin")
        # 网络波动/读超时（code==-1）时重试一次，避免误判"未签到"而重复 POST
        if not data.get("success") and code == -1:
            log("WARN", f"[{self.label}] 查询签到状态网络异常，1 秒后重试…")
            time.sleep(1)
            data, code = self._request("GET", "/api/user/checkin")
        if _is_auth_failure(data, code):
            raise AuthExpiredError(str(data.get("message") or code))
        if not data.get("success"):
            log("ERROR", f"[{self.label}] 查询签到状态失败: {data.get('message')}")
            return None
        return data.get("data") or {}

    def do_checkin(self) -> Dict[str, Any]:
        if not self._has_auth():
            return {"success": False, "message": "未登录"}
        data, code = self._request("POST", "/api/user/checkin", params={"turnstile": ""})
        if _is_auth_failure(data, code):
            raise AuthExpiredError(str(data.get("message") or code))
        return data


# ---------------------------------------------------------------------------
# 通知：优先青龙 notify.py 全渠道，失败/不可用时回退内置推送
# ---------------------------------------------------------------------------
_qinglong_send = None
try:
    # 青龙面板新版已内置 notify.py（注入脚本运行环境），脚本目录无需自带；
    # 本地/无青龙环境时导入失败则回退内置推送
    from notify import send as _qinglong_send  # type: ignore
except Exception:
    # 面板集成的 notify.py 在 /ql/data/scripts/，脚本运行时 cwd 可能不在该目录
    _scripts_dir = "/ql/data/scripts"
    if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
        try:
            from notify import send as _qinglong_send  # type: ignore
        except Exception:
            _qinglong_send = None


def send_feishu(token: str, title: str, content: str) -> None:
    """飞书自定义机器人。token 形如 https://open.feishu.cn/... 或纯 webhook url。"""
    url = token if token.startswith("http") else f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"
    body = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
            "elements": [{"tag": "markdown", "content": content}],
        },
    }
    r = requests.post(url, json=body, timeout=15)
    log("INFO", f"飞书推送: HTTP {r.status_code}")


def send_serverchan(token: str, title: str, content: str) -> None:
    """Server 酱 (sct.ftqq.com / sct.dev)。token 支持 'SCTxxxxxx' 或完整 url。"""
    if token.startswith("http"):
        url = token
    else:
        url = f"https://sctapi.ftqq.com/{token}.send"
    r = requests.post(url, data={"title": title, "desp": content}, timeout=15)
    log("INFO", f"Server 酱推送: HTTP {r.status_code}")


def send_telegram(token: str, title: str, content: str) -> None:
    """token 形如 'BOT_TOKEN|CHAT_ID'。"""
    if "|" not in token:
        log("ERROR", "Telegram 推送需 BOT_TOKEN|CHAT_ID 格式")
        return
    bot_token, chat_id = token.split("|", 1)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": chat_id, "text": f"*{title}*\n\n{content}", "parse_mode": "Markdown"},
        timeout=15,
    )
    log("INFO", f"Telegram 推送: HTTP {r.status_code}")


def send_bark(token: str, title: str, content: str) -> None:
    """Bark：token 形如 'https://api.day.app/yourkey/' 或 'https://api.day.app/yourkey'。"""
    base = token.rstrip("/")
    url = f"{base}/{title}"
    r = requests.get(url, params={"body": content}, timeout=15)
    log("INFO", f"Bark 推送: HTTP {r.status_code}")


def send_fallback_notifier(kind: str, token: str, title: str, content: str) -> None:
    """内置回退推送（无青龙 notify.py 时用）。"""
    if not token:
        return
    try:
        if kind in ("feishu", "lark", "飞书"):
            send_feishu(token, title, content)
        elif kind in ("serverchan", "server", "sct"):
            send_serverchan(token, title, content)
        elif kind == "telegram":
            send_telegram(token, title, content)
        elif kind == "bark":
            send_bark(token, title, content)
        else:
            log("WARN", f"未知的推送方式: {kind}")
    except Exception as exc:  # noqa: BLE001
        log("ERROR", f"推送失败 ({kind}): {exc}")


def send_notify(title: str, content: str, fallback_kind: str = "", fallback_token: str = "") -> bool:
    """统一推送入口：优先青龙 notify.py（全渠道），失败/不可用时回退内置推送。"""
    if _qinglong_send:
        try:
            _qinglong_send(title, content)
            log("INFO", "已通过青龙 notify.py 推送")
            return True
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"青龙 notify.py 推送失败，回退内置推送: {exc}")
    if fallback_kind and fallback_token:
        send_fallback_notifier(fallback_kind, fallback_token, title, content)
    return False


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
def _run_one_attempt(client: DzziClient) -> Dict[str, Any]:
    """单次签到尝试（不含登录），token 失效时抛 AuthExpiredError 由上层重登重试。"""
    status = client.get_status() or {}
    already = bool(status.get("stats", {}).get("checked_in_today"))

    if already:
        last = (status.get("stats", {}).get("records") or [{}])[0]
        msg = (
            f"[{client.label}] 今日已签到，"
            f"上次奖励 {quota_to_yuan(last.get('quota_awarded', 0))} 元"
        )
        log("INFO", msg)
        return {"ok": True, "msg": msg, "quota_awarded": 0, "skipped": True}

    result = client.do_checkin()
    if not result.get("success"):
        resp_msg = result.get("message") or ""
        # GET 状态可能因网络失败而跳过，POST 兜底返回"今日已签到"等幂等提示时视为成功跳过
        if _is_already_signed_in(resp_msg):
            skip_msg = f"[{client.label}] 今日已签到（服务端幂等返回）"
            log("INFO", skip_msg)
            return {"ok": True, "msg": skip_msg, "quota_awarded": 0, "skipped": True}
        fail_msg = f"[{client.label}] 签到失败：{resp_msg}"
        log("ERROR", fail_msg)
        return {"ok": False, "msg": fail_msg}

    quota_awarded = (result.get("data") or {}).get("quota_awarded", 0)
    msg = f"[{client.label}] 签到成功，获得 {quota_to_yuan(quota_awarded)} 元"
    log("INFO", msg)
    return {"ok": True, "msg": msg, "quota_awarded": quota_awarded, "skipped": False}


def run_one(client: DzziClient) -> Dict[str, Any]:
    """签到主流程：优先复用缓存 token 免登录；token 失效时清缓存重登后重试一次。"""
    if not client.ensure_auth():
        return {"ok": False, "msg": "登录失败"}
    try:
        return _run_one_attempt(client)
    except AuthExpiredError as exc:
        log("WARN", f"[{client.label}] token 失效（{exc}），清缓存重新登录后重试")
        client._drop_token_cache()
        client._cached = False
        if not client.ensure_auth():
            return {"ok": False, "msg": f"重新登录失败: {exc}"}
        return _run_one_attempt(client)


def main() -> int:
    load_env_file()  # 本地便捷注入 .env（青龙环境无此文件，自动跳过）

    raw_accounts = (
        os.environ.get("DZZI_ACCOUNTS")
        or os.environ.get("ACCOUNTS")
        or os.environ.get("DZZI")
        or ""
    )
    notifier_kind = os.environ.get("NOTIFIER", "").strip()
    notifier_token = os.environ.get("NOTIFIER_TOKEN", "").strip()
    notify_enabled = env_bool("DZZI_NOTIFY", True)

    accounts = parse_accounts(raw_accounts)
    if not accounts:
        log("ERROR", "未找到账号配置。请设置 DZZI_ACCOUNTS 环境变量。")
        return 1

    log("INFO", f"共加载 {len(accounts)} 个账号，开始签到…")

    results: List[Dict[str, Any]] = []
    for acc in accounts:
        client = DzziClient(
            base_url=acc.get("base_url", DEFAULT_BASE_URL),
            username=acc["username"],
            password=acc["password"],
        )
        try:
            results.append(run_one(client))
        except Exception as exc:  # noqa: BLE001
            log("ERROR", f"账号 {mask(acc.get('username', ''))} 异常: {exc}")
            results.append({"ok": False, "msg": str(exc)})
        time.sleep(2)  # 防止风控

    success_n = sum(1 for r in results if r["ok"])
    fail_n = len(results) - success_n

    # 标题按成败三档分档，一眼看出结果
    if fail_n == 0:
        title = f"✅ DZZI.AI 签到 全部成功（{success_n}/{len(results)}）"
    elif success_n == 0:
        title = f"❌ DZZI.AI 签到 全部失败（0/{len(results)}）"
    else:
        title = f"⚠️ DZZI.AI 签到 部分失败（{success_n}/{len(results)}）"

    summary_lines = [
        f"# {title} - 执行报告",
        f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"📊 总计 {len(results)} 个账号，✅ 成功 {success_n}，❌ 失败 {fail_n}",
        "",
    ]
    for idx, r in enumerate(results, 1):
        summary_lines.append(f"{idx}. {'✅' if r['ok'] else '❌'} {r['msg']}")

    summary = "\n".join(summary_lines)
    print()
    print(summary)

    if notify_enabled:
        send_notify(title, summary, notifier_kind, notifier_token)
    else:
        log("INFO", "DZZI_NOTIFY=false，已跳过推送")

    return 0 if fail_n == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
