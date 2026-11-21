# -*- coding: utf-8 -*-
"""
new Env('老王FIP签到');
cron: 30 8 * * *

老王论坛（laowang.vip）Discuz 论坛 k_misign 插件每日签到。
站点被墙，必须挂代理；登录/签到均需通过 tncode 滑块校验。

为什么可以纯 requests（2026-10-10 实测）：
  站点的 tncode 是未混淆的经典实现（/captcha/tn_code.js 带中文注释，HMAC 密钥明文
  硬编码）。所谓 sign/track 只是：轨迹统计 -> JSON -> 与密钥逐字节 XOR -> base64，
  再用 FNV-1a 对 trackStr+ts+offset+密钥 求签名。全部可在 Python 复现，因此无需
  浏览器：直接取 /captcha/tncode.php 的三联图（带缺口背景 / 拼图块 / 完整背景），
  用 OpenCV 差异法解出缺口位置，再按 JS 算法构造 payload 提交 /captcha/check.php
  即可拿到 `<token>_ok`。服务端行为风控对"正常点数的人类轨迹"放行。

环境变量：
  LWFIP_ACCOUNTS          必填 用户名#密码，多账号用 & 或换行分隔
  LWFIP_PROXY             可选 HTTP 代理（站点被墙，建议配置），如 http://172.17.0.1:7890；
                          留空时自动回退青龙全局代理（HTTPS_PROXY/HTTP_PROXY/ALL_PROXY）
  LWFIP_PROXY_REQUIRED    可选 默认 false，true 时缺代理直接报错退出
  LWFIP_BASE_URLS         可选 逗号分隔的候选域名，按顺序探测，首个可用即采用；
                          默认 https://laowang.vip,https://laowangfip372.vip
  LWFIP_BASE_URL          可选 兼容旧配置；设置后只用该域名、不做降级
  LWFIP_NOTIFY            可选 默认 true
  LWFIP_NOTIFY_ONLY_FAIL  可选 默认 false，true 时仅在有失败时推送
  LWFIP_TIMEOUT           可选 默认 30（秒）
  LWFIP_DEBUG             可选 默认 false，输出调试日志
  LWFIP_MAX_CAPTCHA_RETRY 可选 默认 5，单次滑块识别失败重试次数
  LWFIP_MAX_RETRY         可选 默认 5，账号级任务重试次数（仅可重试错误触发）
  LWFIP_RETRY_INTERVAL    可选 默认 60（秒），账号级重试间隔
  LWFIP_COOKIE_CACHE      可选 默认 true，登录成功后会话 Cookie 落盘复用，
                          后续任务/重试直通签到跳过滑块；false 关闭

依赖：requests、opencv-python-headless、numpy
作者: zephyr_xiao
"""

import base64
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except ImportError:
    print("❌ 缺少 requests 库，请在青龙面板「依赖管理」-「Python」中安装 requests")
    sys.exit(1)

try:
    import cv2
    import numpy as np
except ImportError:
    print("❌ 缺少 opencv-python-headless 或 numpy，请在青龙面板「依赖管理」-「Python」中安装")
    sys.exit(1)

# 青龙 notify.py 兼容（青龙容器内 notify.py 在 /ql/data/scripts/）
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


# ---------------- 站点常量（2026-11-21 复测） ----------------
DEFAULT_BASE_URL = "https://laowang.vip"
# 主域名对部分代理出口被 Cloudflare 整站拦截（403 Just a moment），备用域名同后端不拦
DEFAULT_BASE_URLS = ("https://laowang.vip", "https://laowangfip372.vip")
LOGIN_PAGE_PATH = "/member.php?mod=logging&action=login"
SIGN_PAGE_PATH = "/plugin.php?id=k_misign:sign"
CAPTCHA_IMG_PATH = "/captcha/tncode.php"
CAPTCHA_CHECK_PATH = "/captcha/check.php"

# 年龄确认网关：站点在根路径投放「性材料警告」页，需置 is_agree=1 才放行。
# 实测该 cookie 只解网关，对 Cloudflare 403 无效（两者独立）。
AGE_GATE_COOKIE_NAME = "is_agree"
AGE_GATE_COOKIE_VALUE = "1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# tncode 前端加密密钥（站点 /captcha/tn_code.js 明文硬编码）
TNCODE_SECRET = "GWDiugh398huiw0ioOYGd0934hew"

# 会话 Cookie 缓存（命名须匹配仓库 .gitignore 的 cookie_*.json 规则，含真实凭证严禁入库）
COOKIE_CACHE_DIR = Path(__file__).resolve().parent

# 青龙面板「配置文件 / 环境变量」里配置的全局代理变量
GLOBAL_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

# 账号级重试默认值
MAX_RETRY = 5
RETRY_INTERVAL_SEC = 60


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


def resolve_proxy(*specific_keys: str) -> tuple:
    """
    解析代理地址，返回 (代理地址, 来源变量名)，两者均可能为空字符串。
    优先级：脚本专属变量 > 青龙全局代理变量 > 空（直连）。
    requests 的 Session.proxies 需显式赋值，不读环境变量，故此处统一取值。
    """
    for name in (*specific_keys, *GLOBAL_PROXY_KEYS):
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


# ---------------- 候选域名 ----------------
def _normalize_base_url(raw: str) -> str:
    """归一化单个域名：去空白与尾斜杠；非 http(s) scheme 视为非法返回空串。"""
    value = (raw or "").strip().rstrip("/")
    if not value:
        return ""
    if not value.lower().startswith(("http://", "https://")):
        return ""
    return value


def parse_base_urls() -> list:
    """
    解析候选域名列表（按优先级排序，首个可用即采用）。

    优先级：LWFIP_BASE_URLS（多域名）> LWFIP_BASE_URL（单域名，禁用降级）
            > DEFAULT_BASE_URLS（内置主域名 + 备用域名）。
    LWFIP_BASE_URL 单独保留是为了兼容旧配置：用户显式指定了就尊重，不擅自换成别的域名。
    """
    multi = (os.getenv("LWFIP_BASE_URLS") or "").strip()
    if multi:
        raw_items = re.split(r"[,\s]+", multi)
        source = "LWFIP_BASE_URLS"
    else:
        single = (os.getenv("LWFIP_BASE_URL") or "").strip()
        if single:
            raw_items = [single]
            source = "LWFIP_BASE_URL"
        else:
            raw_items = list(DEFAULT_BASE_URLS)
            source = "默认"

    urls = []
    for item in raw_items:
        if not item.strip():
            continue
        normalized = _normalize_base_url(item)
        if not normalized:
            print(f"  ⚠️ 忽略非法域名（需以 http:// 或 https:// 开头）: {item.strip()[:60]}")
            continue
        if normalized not in urls:   # 去重，保留首次出现的顺序
            urls.append(normalized)

    if not urls:
        print("  ⚠️ 候选域名全部非法，回退到默认域名列表")
        urls = [_normalize_base_url(u) for u in DEFAULT_BASE_URLS]
        source = "默认（回退）"

    if len(urls) == 1:
        print(f"  ℹ️ 候选域名（来源: {source}）仅 1 个，不会做域名降级")
    return urls


def next_base_url(current: str, candidates: list) -> str:
    """环形取下一个候选域名；单域名时原样返回（无可降级目标）。"""
    if len(candidates) <= 1:
        return current
    try:
        idx = candidates.index(current)
    except ValueError:
        return candidates[0]
    return candidates[(idx + 1) % len(candidates)]


# ---------------- 页面类型判定 ----------------
def is_cloudflare_challenge(html: str) -> bool:
    """Cloudflare 拦截页（Just a moment / 托管质询）。"""
    return "Just a moment..." in html or "window._cf_chl_opt" in html


def is_age_gate_page(html: str) -> bool:
    """
    年龄确认网关页（「性材料警告」+ is_agree 按钮脚本）。

    两个特征串同时命中才判定，避免把普通页面里偶然出现的 is_agree 误判。
    实测该页含 is_agree/agree() 各 2 次、formhash 0 次；登录页两者均为 0 次。
    """
    return "is_agree" in html and "agree()" in html


def is_domain_block_error(err_text: str) -> bool:
    """错误是否属于「换域名可恢复」的拦截类。"""
    text = err_text or ""
    return "cloudflare" in text.lower() or "年龄确认网关" in text


def is_retryable_error(err_text: str) -> bool:
    """
    判断错误是否可重试。
    可重试：网络/超时/代理类、滑块验证码类、签到流程类（页面加载不完整/跳转结果未知）。
    不可重试：密码错误、用户名不存在等业务错误（重试无意义，且会加剧风控）。
    """
    retry_keywords = [
        "timeout", "timed out", "超时",
        "proxy", "代理",
        # 收窄到具体网络异常名，避免页面正文里的 "connect"/"network" 等普通词误触发重试
        "connectionerror", "connection refused", "connection reset", "connection aborted",
        "newconnectionerror", "max retries", "sslerror", "read timed out",
        "connectionpool", "remotedisconnected", "incompleteread",
        # 滑块验证码类
        "验证码", "滑块", "captcha", "tncode",
        # 签到流程类
        "未到达验证页", "提交后仍在验证页", "签到结果未知", "找不到签到",
        # 域名拦截类（换域名即可恢复，值得重试）
        "cloudflare", "年龄确认网关",
    ]
    lower = err_text.lower()
    return any(kw in lower for kw in retry_keywords)


def compact_error(err: BaseException, limit: int = 160) -> str:
    """把异常压成单行摘要（折叠空白后再截断，保留关键信息）。"""
    return re.sub(r"\s+", " ", str(err)).strip()[:limit]


def parse_credentials(env_value: str):
    """解析 'user#pwd&user2#pwd2' / 多行 -> [(user, pwd), ...]（密码含 # 时按首个 # 切分）。"""
    if not env_value:
        return []
    accounts = []
    for raw in re.split(r"[&\n]+", env_value.strip()):
        raw = raw.strip()
        if not raw or "#" not in raw:
            if raw:
                # 不回显原文：避免用户误把 Cookie 等凭据填进来时泄漏到日志
                print("  ⚠️ 跳过格式错误的账号项（缺少 # 分隔符）")
            continue
        u, p = raw.split("#", 1)
        u, p = u.strip(), p.strip()
        if u and p:
            accounts.append((u, p))
    return accounts


def mask_account(s: str) -> str:
    """账号脱敏，避免日志/推送泄漏明文。"""
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


def mask_proxy(proxy: str) -> str:
    """代理 URL 脱敏：隐藏 user:pass@ 中的凭据（青龙全局代理常带账号密码）。"""
    if not proxy:
        return ""
    return re.sub(r"://[^@/]+@", "://***@", proxy)


# ---------------- 会话 Cookie 缓存 ----------------
def cookie_cache_path(username: str, base_url: str = "") -> Path:
    """
    缓存文件路径：脚本同目录，文件名用 (用户名 + 域名) hash（不含明文）。

    带上域名是因为 Cookie 与域名绑定：两个候选域名切换时若共用一份缓存，会互相
    覆盖且加载后对当前域名无效，导致每次都要重登。base_url 留空时退化为只哈希
    用户名，保持与旧版本缓存文件兼容。
    """
    key = f"{username}|{base_url}" if base_url else username
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return COOKIE_CACHE_DIR / f"cookie_{digest}.json"


def save_cookies_to_cache(session, username: str, base_url: str = "") -> bool:
    """把会话 Cookie 落盘，供后续任务/重试直通签到（跳过滑块）。"""
    try:
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in session.cookies
        ]
        cookie_cache_path(username, base_url).write_text(
            json.dumps(cookies, ensure_ascii=False), encoding="utf-8"
        )
        print(f"🍪 已保存会话 Cookie 缓存（{len(cookies)} 条）")
        return True
    except Exception as e:
        print(f"⚠️ 保存 Cookie 缓存失败: {e}")
        return False


def load_cookies_from_cache(username: str, base_url: str = ""):
    """读取 Cookie 缓存；缺失或损坏返回 None。"""
    try:
        data = json.loads(cookie_cache_path(username, base_url).read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return None


def remove_cookie_cache(username: str, base_url: str = "") -> None:
    """删除失效缓存，避免反复复用坏 Cookie。"""
    try:
        cookie_cache_path(username, base_url).unlink(missing_ok=True)
    except Exception:
        pass


# ---------------- 浏览器指纹（复刻 /captcha/dz_fp.js） ----------------
def _fnv1a32(text: str) -> str:
    """FNV-1a 32 位哈希，返回 8 位十六进制（与 dz_fp.js 的 h32 一致）。"""
    h = 0x811C9DC5
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return format(h, "08x")


def compute_fingerprint() -> str:
    """复刻 dz_fp.js：对 collect() 拼接串做四段 FNV-1a 组合指纹。"""
    raw = "||".join([
        UA, "zh-CN,zh", "1280x800x24", "-480", "8", "8", "Win32", "nc", "nc",
    ])
    a = _fnv1a32(raw)
    b = _fnv1a32(a + raw[:len(raw) >> 1])
    c = _fnv1a32(b + raw[len(raw) >> 1:])
    d = _fnv1a32(c + str(len(raw)))
    return a + b + c + d


# ---------------- tncode 滑块求解 ----------------
def solve_slider_candidates(img_bytes: bytes, debug: bool = False):
    """
    从 /captcha/tncode.php 的三联图求缺口左沿 x 的候选列表（按可能性降序）。

    三联图布局（各 240x150）：块0=带缺口背景，块1=拼图碎片，块2=完整背景。

    站点用"双缺口"反爬：块0 里有时会画两个同尺寸拼图形缺口，只有一个是真的。
    判别依据——拼图只做水平拖动，真缺口必然与拼图碎片同一行（y 对齐），故把
    y 与拼图最接近的候选排在最前。注意不能用形态学闭运算，否则相邻缺口会被
    合并成一个轮廓而无法区分。

    返回 x 候选列表（去重）；无候选返回 []。
    """
    img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        if debug:
            print("   [captcha] 验证码图解码失败")
        return []

    h = img.shape[0] // 3
    if h <= 0:
        return []
    panel_gap = cv2.cvtColor(img[:h], cv2.COLOR_BGR2GRAY)             # 带缺口背景
    panel_piece = cv2.cvtColor(img[h:2 * h], cv2.COLOR_BGR2GRAY)      # 拼图碎片
    panel_full = cv2.cvtColor(img[2 * h:3 * h], cv2.COLOR_BGR2GRAY)   # 完整背景

    ys, _ = np.where(panel_piece > 20)
    if len(ys) == 0:
        if debug:
            print("   [captcha] 未找到拼图碎片")
        return []
    piece_y0 = int(ys.min())
    piece_h = int(ys.max()) - piece_y0 + 1

    # 缺口 = 完整背景 与 带缺口背景 的差异区（不做形态学，避免相邻缺口合并）
    diff = cv2.absdiff(panel_full, panel_gap)
    _, binary = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for c in contours:
        x, y, w, hh = cv2.boundingRect(c)
        if not (25 <= w <= 90 and 20 <= hh <= 90):
            continue
        if not (0.5 <= w / max(1, hh) <= 2.0):
            continue
        candidates.append((x, y, w, hh))
    if not candidates:
        if debug:
            print("   [captcha] 差异图未找到合格缺口轮廓")
        return []

    # y 对齐的排前，其次尺寸接近拼图块
    candidates.sort(key=lambda c: (abs(c[1] - piece_y0), abs(c[3] - piece_h)))
    xs = []
    for x, _y, _w, _hh in candidates:
        if x not in xs:
            xs.append(x)
    if debug:
        print(f"   [captcha] 缺口候选 x={xs}（拼图 y={piece_y0}）")
    return xs


def solve_slider_from_sprite(img_bytes: bytes, debug: bool = False):
    """取最可能的缺口左沿 x（候选列表首个）；失败返回 None。"""
    xs = solve_slider_candidates(img_bytes, debug)
    return xs[0] if xs else None


def make_human_track(distance: int, start_x: int = 200, start_y: int = 300):
    """
    生成一条人类观感轨迹（余弦缓动 + 微抖动 + 时间递增）。

    服务端行为风控（error_track）会拒绝"点数过少/瞬移"的轨迹，故用约 40 个点、
    每点 12~20ms 的节奏。距离为负时退化为最小轨迹。
    """
    distance = max(1, int(distance))
    points = [{"x": start_x, "y": start_y, "t": 0}]
    t = 0
    steps = 40
    for i in range(1, steps + 1):
        ratio = i / steps
        eased = 0.5 * (1 - math.cos(math.pi * ratio))
        t += int(12 + 8 * math.sin(ratio * math.pi))
        points.append({
            "x": int(start_x + distance * eased),
            "y": start_y + (i % 3 - 1),
            "t": t,
        })
    return points


def _track_info(track_data: list) -> dict:
    """复刻 JS generateSecurePayload 的 trackInfo 统计（键顺序必须一致）。"""
    valid = len(track_data) >= 2
    total_time = track_data[-1]["t"]
    total_dist = 0.0
    speeds, directions = [], []
    for i in range(1, len(track_data)):
        dx = track_data[i]["x"] - track_data[i - 1]["x"]
        dy = track_data[i]["y"] - track_data[i - 1]["y"]
        dt = track_data[i]["t"] - track_data[i - 1]["t"]
        if dt > 0:
            dist = math.hypot(dx, dy)
            total_dist += dist
            speeds.append(dist / dt)
            directions.append(math.atan2(dy, dx))
    avg_speed = sum(speeds) / len(speeds) if speeds else 0
    speed_var = (sum((s - avg_speed) ** 2 for s in speeds) / len(speeds)) if speeds else 0
    dir_changes = sum(1 for i in range(1, len(directions))
                      if abs(directions[i] - directions[i - 1]) > math.pi / 4)
    return {
        "valid": valid,
        "points": len(track_data),
        "totalTime": total_time,
        "totalDist": total_dist,
        "avgSpeed": avg_speed,
        "maxSpeed": max(speeds) if speeds else 0,
        "minSpeed": min(speeds) if speeds else 0,
        "speedVar": speed_var,
        "dirChanges": dir_changes,
        "finalX": track_data[-1]["x"] - track_data[0]["x"],
    }


def build_tncode_payload(track_data: list, mark_offset: float, ts: int) -> str:
    """
    复刻 JS generateSecurePayload，返回 check.php 的 POST body。
    XOR(密钥) -> base64 -> FNV-1a 签名。
    """
    track_str = json.dumps(_track_info(track_data), separators=(",", ":"), ensure_ascii=False)
    normalized = f"{mark_offset:.2f}"
    xored = "".join(
        chr(ord(c) ^ ord(TNCODE_SECRET[i % len(TNCODE_SECRET)]))
        for i, c in enumerate(track_str)
    )
    encrypted = base64.b64encode(xored.encode("utf-8")).decode("ascii")
    combined = track_str + str(ts) + normalized + TNCODE_SECRET
    h = 0x811C9DC5
    for ch in combined:
        h ^= ord(ch)
        h = (h + (h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24)) & 0xFFFFFFFF
    sign = format(h, "x")
    return (f"tn_r={normalized}&track={quote(encrypted, safe='')}"
            f"&ts={ts}&sign={sign}")


def pass_tncode(session, base_url: str, debug: bool, max_retry: int = 5):
    """
    取三联图 -> 解缺口候选 -> 逐个候选构造 payload 提交 check.php。
    每张图可提交多次（实测同一张图连续提交不会作废），因此把双缺口的候选都试一遍；
    全部失败才换新图。成功返回服务端 `<token>_ok` 字符串；失败返回 None。
    """
    for attempt in range(1, max_retry + 1):
        try:
            resp = session.get(
                base_url + CAPTCHA_IMG_PATH,
                params={"t": f"{random.random()}"},
                timeout=session._timeout,
            )
            candidates = solve_slider_candidates(resp.content, debug)
            if not candidates:
                if debug:
                    print(f"   [captcha] 第 {attempt}/{max_retry} 次未解出缺口，换图重试")
                continue

            for gap_x in candidates:
                payload = build_tncode_payload(
                    make_human_track(gap_x), gap_x, int(time.time() * 1000))
                r = session.post(
                    base_url + CAPTCHA_CHECK_PATH,
                    data=payload,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=session._timeout,
                )
                text = r.text.strip()
                if "_ok" in text:
                    if debug:
                        print(f"   [captcha] 第 {attempt} 张图、候选 x={gap_x} 通过")
                    return text
                if debug:
                    print(f"   [captcha] 候选 x={gap_x} 服务端返回 {text[:40]!r}")
            if debug:
                print(f"   [captcha] 第 {attempt}/{max_retry} 张图全部候选未过，换图重试")
        except Exception as e:
            if debug:
                print(f"   [captcha] 第 {attempt} 次异常: {compact_error(e)}")
    return None


# ---------------- HTML 表单解析 ----------------
class _FormParser(HTMLParser):
    """提取指定 id 的 <form>：action 与全部 input 的 name/value。"""

    def __init__(self, form_id: str):
        super().__init__()
        self.form_id = form_id
        self.depth = 0          # 进入目标 form 后 >0
        self.found = False
        self.action = ""
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            if not self.found and a.get("id") == self.form_id:
                self.found = True
                self.action = a.get("action", "")
                self.depth = 1
            elif self.found:
                self.depth += 1
        elif tag == "input" and self.depth > 0:
            name = a.get("name")
            if name:
                self.fields[name] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.depth > 0:
            self.depth -= 1


def parse_form(html: str, form_id: str):
    """解析页面中指定 id 的 form，返回 (action, {name: value})；未找到返回 (None, {})。"""
    p = _FormParser(form_id)
    try:
        p.feed(html)
    except Exception:
        return None, {}
    if not p.found:
        return None, {}
    return p.action, p.fields


# ---------------- 页面获取（含年龄网关自动放行） ----------------
class AgeGateBlocked(Exception):
    """置了 is_agree 仍返回年龄网关页：站点改了放行机制，需人工介入。"""


def _base_domain(base_url: str) -> str:
    """从 base_url 取主机名，供 cookie 域精确匹配（避免把 is_agree 发给别的域）。"""
    return re.sub(r"^https?://", "", (base_url or "").strip().rstrip("/")).split("/")[0]


def set_age_gate_cookie(session, base_url: str) -> None:
    """给当前站点置年龄确认 cookie（等价于网关页「同意 / I Agree」按钮）。"""
    domain = _base_domain(base_url)
    if not domain:
        return
    session.cookies.set(AGE_GATE_COOKIE_NAME, AGE_GATE_COOKIE_VALUE, domain=domain, path="/")


def fetch_page(session, url: str, base_url: str, debug: bool = False, **kwargs):
    """
    GET 页面，自动放行年龄确认网关。

    为什么需要包装：站点在部分路径投放「性材料警告」网关页，页面里没有业务内容。
    命中网关时置 is_agree=1 后重试一次；仍命中则抛 AgeGateBlocked，交由上层换域名
    或报错，避免把网关页当业务页继续解析出「formhash 缺失」这类误导性错误。

    注意：Cloudflare 拦截页不在这里处理（is_agree 对它无效），由调用方按错误文本识别。
    """
    r = session.get(url, timeout=session._timeout, **kwargs)
    if not is_age_gate_page(r.text):
        return r

    if debug:
        print("   [gate] 命中年龄确认网关，置 is_agree=1 后重试")
    set_age_gate_cookie(session, base_url)
    r = session.get(url, timeout=session._timeout, **kwargs)
    if is_age_gate_page(r.text):
        raise AgeGateBlocked("年龄确认网关拦截（置 is_agree 后仍被拦）")
    return r


# ---------------- 登录 ----------------
def check_logged_in(session, base_url: str, debug: bool = False) -> bool:
    """
    GET forum.php，按 discuz_uid>0 / 退出链接判定登录态。

    被 Cloudflare 拦截时这里只会返回 False（等同「Cookie 失效」），随后走正常登录
    流程即可由 login_with_requests 报出明确的 CF 拦截错误并触发换域名，故不在此单独区分。
    """
    try:
        r = fetch_page(session, base_url + "/forum.php", base_url, debug)
        m = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
        if m and m.group(1) != "0":
            return True
        if "action=logout" in r.text and "退出" in r.text:
            return True
    except Exception as e:
        if debug:
            print(f"   [login] 登录态检查异常: {compact_error(e)}")
    return False


def login_with_requests(session, base_url: str, username: str, password: str,
                        debug: bool, max_captcha_retry: int):
    """浏览器登录页 -> 过 tncode -> 提交登录表单。返回 (success: bool, msg: str)。"""
    try:
        r = fetch_page(session, base_url + LOGIN_PAGE_PATH, base_url, debug)
        # 先判拦截：否则会落到下面的「formhash 缺失」，把 IP 被封误报成页面结构变化
        if is_cloudflare_challenge(r.text):
            return False, "Cloudflare 拦截（当前出口 IP 被质询，可尝试切换域名）"
        fh = re.search(r'name="formhash" value="([0-9a-f]+)"', r.text)
        lh = re.search(r"loginhash=([A-Za-z0-9]+)", r.text)
        if not fh or not lh:
            return False, "登录页解析失败（未取到 formhash/loginhash）"
        formhash, loginhash = fh.group(1), lh.group(1)

        token = pass_tncode(session, base_url, debug, max_captcha_retry)
        if not token:
            return False, "验证码识别失败（滑块多次未通过）"

        action = (f"{base_url}/member.php?mod=logging&action=login"
                  f"&loginsubmit=yes&loginhash={loginhash}&inajax=1")
        data = {
            "formhash": formhash,
            "referer": base_url + "/./",
            "username": username,
            "password": password,
            "cookietime": "2592000",
            "clicaptcha-submit-info": token,
            "fingerprint": compute_fingerprint(),
            "loginfield": "username",
            "answer": "",
        }
        r = session.post(
            action, data=data,
            headers={"Referer": base_url + LOGIN_PAGE_PATH,
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=session._timeout,
        )
        if "succeedhandle" in r.text:
            return True, "登录成功"
        # 失败信息提取（Discuz 登录错误提示）
        for kw in ("密码错误", "密码不正确", "用户名不存在", "用户不存在", "验证码"):
            if kw in r.text:
                return False, f"登录失败: {kw}"
        if check_logged_in(session, base_url, debug):
            return True, "登录成功"
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text)).strip()[:120]
        return False, f"登录失败（未检测到成功标志）: {snippet}"
    except requests.exceptions.RequestException as e:
        return False, f"登录网络异常: {compact_error(e)}"
    except Exception as e:
        if debug:
            traceback.print_exc()
        return False, f"登录异常: {compact_error(e)}"


# ---------------- 签到 ----------------
def fetch_sign_stats(session, base_url: str, debug: bool = False) -> dict:
    """读取签到页 countqian 统计（连续签到/签到等级/积分奖励/总天数）。失败返回 {}。"""
    wanted = {"连续签到": "天", "签到等级": "级", "积分奖励": "分", "总天数": "天"}
    try:
        r = fetch_page(session, base_url + SIGN_PAGE_PATH, base_url, debug)
        block = re.search(r'<ul class="countqian[^"]*"[^>]*>(.*?)</ul>', r.text, re.S)
        if not block:
            return {}
        stats = {}
        for li in re.findall(r"<li>(.*?)</li>", block.group(1), re.S):
            h4 = re.search(r"<h4>(.*?)</h4>", li, re.S)
            if not h4:
                continue
            label = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", h4.group(1)))
            if label not in wanted:
                continue
            # 先定位含 hidnum 的整个 <input> 标签，再在标签内取 value，不依赖属性顺序/引号
            tag = re.search(r"<input[^>]*hidnum[^>]*>", li)
            if not tag:
                continue
            val = re.search(r"""value=["']([^"']*)["']""", tag.group(0))
            if val and val.group(1).strip():
                stats[label] = f"{val.group(1).strip()} {wanted[label]}"
        return stats
    except Exception as e:
        if debug:
            print(f"   [stats] 统计读取失败: {compact_error(e)}")
        return {}


def sign_with_requests(session, base_url: str, debug: bool, max_captcha_retry: int) -> dict:
    """
    签到流程：签到页 -> 已签判定 -> 找 qiandao 链接 -> 验证页过 tncode -> 提交表单。
    返回 {"success": bool, "message"/"error": str}。
    """
    try:
        r = fetch_page(session, base_url + SIGN_PAGE_PATH, base_url, debug)
        html = r.text

        # 已签到：span.btn.btnvisted（k_misign 的"已签到"标记）
        if "btnvisted" in html:
            return {"success": True, "message": "今日已签到（btnvisted）"}

        # 找签到按钮链接（href 含 operation=qiandao，formhash 随机仅做存在性判断）
        m = re.search(r'href="([^"]*operation=qiandao[^"]*)"', html)
        if not m:
            if any(kw in html for kw in ("您今日已签到", "今日已经签到", "今天已经签到")):
                return {"success": True, "message": "今日已签到（页面状态）"}
            return {"success": False, "error": "找不到签到按钮（可能页面结构变化或未登录）"}

        qiandao_url = urljoin(base_url + "/", m.group(1).replace("&amp;", "&"))
        r = fetch_page(session, qiandao_url, base_url, debug)
        page = r.text

        # 签到验证页：解析 #v2_captcha_form 的 action 与全部字段
        action, fields = parse_form(page, "v2_captcha_form")
        if not action:
            # 无验证表单：可能已直接签到成功，或弹了错误页
            if "btnvisted" in page:
                return {"success": True, "message": "今日已签到（btnvisted）"}
            for kw in ("签到成功", "签到完成", "您今日已签到"):
                if kw in page:
                    return {"success": True, "message": kw}
            return {"success": False, "error": f"未到达验证页（当前 {r.url[:80]}）"}

        token = pass_tncode(session, base_url, debug, max_captcha_retry)
        if not token:
            return {"success": False, "error": "签到验证码识别失败（滑块多次未通过）"}

        fields["clicaptcha-submit-info"] = token
        submit_url = urljoin(qiandao_url, action)
        r = session.post(submit_url, data=fields,
                         headers={"Referer": qiandao_url,
                                  "Content-Type": "application/x-www-form-urlencoded"},
                         timeout=session._timeout)
        result = r.text

        # 失败守卫优先：仍在验证页 -> 判定失败
        # （不能靠"连续签到"等静态标签判成功：签到页本身就含该标签，会把失败误报为成功）
        if "v2_captcha_form" in result:
            return {"success": False, "error": "提交后仍在验证页（验证码可能未真正通过）"}

        # 权威成功信号：重新拉签到页，k_misign 在已签到时渲染 span.btnvisted
        try:
            verify_text = fetch_page(session, base_url + SIGN_PAGE_PATH, base_url, debug).text
        except Exception:
            verify_text = result
        if "btnvisted" in verify_text:
            return {"success": True, "message": "签到成功"}

        # 次级成功信号：仅真正的成功提示语（排除"连续签到"这类静态统计标签）
        for kw in ("签到成功", "签到完成", "您今日已签到", "今日已经签到", "今天已经签到"):
            if kw in verify_text:
                return {"success": True, "message": kw}
        if re.search(r"积分[^<]{0,10}\+\d+", verify_text):
            return {"success": True, "message": "签到完成（积分已增加）"}

        err = re.search(r"(失败|错误|请重试)[^<\n]{0,40}", verify_text)
        if err:
            return {"success": False, "error": f"签到失败: {err.group(0)[:100]}"}
        return {"success": False, "error": "签到结果未知（提交后未检测到已签到标记）"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"签到网络异常: {compact_error(e)}"}
    except Exception as e:
        if debug:
            traceback.print_exc()
        return {"success": False, "error": f"签到异常: {compact_error(e)}"}


# ---------------- 候选域名探测 ----------------
def probe_base_urls(candidates: list, proxy: str, timeout: int, debug: bool = False) -> tuple:
    """
    按顺序探测候选域名，返回 (可用域名, [(域名, 结论), ...])。

    判定为可用的标准：登录页能同时解析出 formhash 与 loginhash。
    失败原因区分「Cloudflare 拦截」「年龄确认网关」「页面结构异常」「网络异常」，
    便于日志里一眼看出该换域名还是该查脚本。
    全部候选都不可用时返回 ("", 探测记录)，由调用方决定降级还是报错。
    """
    records = []
    for base_url in candidates:
        session = build_session(proxy, timeout)
        try:
            r = fetch_page(session, base_url + LOGIN_PAGE_PATH, base_url, debug)
        except AgeGateBlocked as e:
            records.append((base_url, str(e)))
            continue
        except requests.exceptions.RequestException as e:
            records.append((base_url, f"网络异常: {compact_error(e)}"))
            continue

        if is_cloudflare_challenge(r.text):
            records.append((base_url, f"Cloudflare 拦截（HTTP {r.status_code}）"))
            continue
        if r.status_code >= 400:
            records.append((base_url, f"HTTP {r.status_code}"))
            continue
        if (re.search(r'name="formhash" value="([0-9a-f]+)"', r.text)
                and re.search(r"loginhash=([A-Za-z0-9]+)", r.text)):
            records.append((base_url, "可用"))
            return base_url, records
        records.append((base_url, "登录页结构异常（无 formhash/loginhash）"))
    return "", records


# ---------------- 单账号编排 ----------------
def build_session(proxy: str, timeout: int):
    """构造 requests.Session（UA/代理/超时统一挂载）。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session._timeout = timeout  # type: ignore[attr-defined]
    return session


def run_one_account(username: str, password: str, base_url: str, proxy: str,
                    timeout: int, debug: bool, max_captcha_retry: int,
                    use_cookie_cache: bool) -> dict:
    """登录（或复用 Cookie）+ 签到一条龙。"""
    session = build_session(proxy, timeout)
    # 预先置年龄确认 cookie：网关若扩大拦截范围也不会卡在网关页
    set_age_gate_cookie(session, base_url)
    label = mask_account(username)

    # 会话 Cookie 复用：命中且登录态有效则直通签到，跳过登录与滑块
    # 注意：必须用 authenticated 标志判断是否已登录，不能看 session.cookies 是否为空
    # —— 上面预置的 is_agree 会让 cookies 永远非空，导致登录被静默跳过。
    authenticated = False
    if use_cookie_cache:
        cached = load_cookies_from_cache(username, base_url)
        if cached:
            for c in cached:
                session.cookies.set(c["name"], c["value"],
                                    domain=c.get("domain") or "", path=c.get("path") or "/")
            if check_logged_in(session, base_url, debug):
                authenticated = True
                print(f"    🍪 {label} 命中会话 Cookie，直通签到")
            else:
                print(f"    🍪 {label} Cookie 已失效，重新登录")
                session.cookies.clear()
                set_age_gate_cookie(session, base_url)
                remove_cookie_cache(username, base_url)

    if not authenticated:
        ok, msg = login_with_requests(session, base_url, username, password,
                                      debug, max_captcha_retry)
        if not ok:
            return {"success": False, "error": msg}
        if use_cookie_cache:
            save_cookies_to_cache(session, username, base_url)

    sign_result = sign_with_requests(session, base_url, debug, max_captcha_retry)
    if sign_result.get("success"):
        stats = fetch_sign_stats(session, base_url, debug)
        if stats:
            sign_result["stats"] = stats
    return sign_result


# ---------------- 主流程 ----------------
def main():
    title = "老王FIP签到"
    proxy, proxy_source = resolve_proxy("LWFIP_PROXY")
    notify_enabled = env_bool("LWFIP_NOTIFY", True)
    notify_only_fail = env_bool("LWFIP_NOTIFY_ONLY_FAIL", False)
    timeout = env_int("LWFIP_TIMEOUT", 30)
    debug = env_bool("LWFIP_DEBUG", False)
    max_captcha_retry = env_int("LWFIP_MAX_CAPTCHA_RETRY", 5)
    max_retry = max(1, env_int("LWFIP_MAX_RETRY", MAX_RETRY))
    retry_interval = max(0, env_int("LWFIP_RETRY_INTERVAL", RETRY_INTERVAL_SEC))
    use_cookie_cache = env_bool("LWFIP_COOKIE_CACHE", True)

    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    if not proxy:
        msg = (
            "⚠️ 未配置 LWFIP_PROXY，也未检测到青龙全局代理，将直连尝试。\n"
            "   老王FIP 站点被墙，直连大概率失败，请至少二选一：\n"
            "     · 设置 LWFIP_PROXY=http://172.17.0.1:7890\n"
            "     · 在青龙「配置文件 / 环境变量」配置全局代理（HTTPS_PROXY 等）\n"
            "   如需缺代理即报错退出，设置 LWFIP_PROXY_REQUIRED=1。"
        )
        print(msg)
        if env_bool("LWFIP_PROXY_REQUIRED", False):
            if notify_enabled:
                send_notify(title, msg)
            sys.exit(1)

    accounts = parse_credentials(os.getenv("LWFIP_ACCOUNTS", "").strip())
    if not accounts:
        msg = "⚠️ 未配置 LWFIP_ACCOUNTS。\n  LWFIP_ACCOUNTS = 用户名#密码  （多账号 & 或换行分隔）"
        print(msg)
        if notify_enabled:
            send_notify(title, msg)
        sys.exit(1)

    proxy_desc = "未配置（直连）" if not proxy else (
        mask_proxy(proxy) if proxy_source == "LWFIP_PROXY" else f"{mask_proxy(proxy)}（来源: {proxy_source}）")
    print(f"📋 共 {len(accounts)} 个账号 | 代理: {proxy_desc}")
    print(f"   debug={debug}, timeout={timeout}s, captcha_retry={max_captcha_retry}, "
          f"task_retry={max_retry}次/间隔{retry_interval}s, cookie_cache={use_cookie_cache}\n")

    # 启动探测：按候选顺序找第一个没被拦的域名（主域名对部分出口会被 CF 整站拦）
    candidates = parse_base_urls()
    print(f"🔍 探测候选域名（共 {len(candidates)} 个）...")
    base_url, probe_records = probe_base_urls(candidates, proxy, timeout, debug)
    for domain, reason in probe_records:
        icon = "✅" if reason == "可用" else "⛔"
        print(f"   {icon} {domain} -> {reason}")

    if not base_url:
        detail = "\n".join(f"  - {d}: {r}" for d, r in probe_records)
        msg = f"⚠️ 所有候选域名均不可用，无法登录。\n{detail}"
        print(msg)
        if notify_enabled:
            send_notify(f"❌ {title}", msg)
        sys.exit(1)

    if base_url != candidates[0]:
        print(f"⚠️ 主域名 {candidates[0]} 不可用，已降级到 {base_url}")
    print(f"🌐 本次使用域名: {base_url}\n")

    results, success_cnt = [], 0

    for idx, (u, p) in enumerate(accounts, 1):
        masked = mask_account(u)
        print(f"[{idx}/{len(accounts)}] 🔄 {masked} 开始登录+签到...")

        result, final_ok, final_msg = None, False, ""
        for attempt in range(1, max_retry + 1):
            if attempt > 1:
                print(f"           ⏳ 第 {attempt}/{max_retry} 次重试，等待 {retry_interval} 秒...")
                time.sleep(retry_interval)
            t0 = time.time()
            try:
                result = run_one_account(u, p, base_url, proxy, timeout, debug,
                                         max_captcha_retry, use_cookie_cache)
            except Exception as e:
                if debug:
                    traceback.print_exc()
                result = {"success": False, "error": f"任务异常: {compact_error(e)}"}
            cost = time.time() - t0

            if result.get("success"):
                success_cnt += 1
                final_ok = True
                final_msg = result.get("message", "签到成功")
                print(f"           ✅ {final_msg}  (耗时 {cost:.1f}s)")
                break

            err = result.get("error", "未知错误")
            print(f"           ❌ {err}  (耗时 {cost:.1f}s)")

            # 运行中被拦截（如代理节点轮换后 IP 变了）：换域名后立即重试，不空等
            if attempt < max_retry and is_domain_block_error(err):
                switched = next_base_url(base_url, candidates)
                if switched != base_url:
                    base_url = switched
                    print(f"           🔀 域名被拦截，切换到 {base_url} 立即重试...")
                    continue

            if attempt < max_retry and is_retryable_error(err):
                print("           🔄 检测到可重试错误，准备重试...")
                continue
            final_ok, final_msg = False, err
            break

        results.append({
            "username": masked,
            "success": final_ok,
            "message": final_msg,
            "stats": (result or {}).get("stats", {}),
        })
        if idx < len(accounts):
            time.sleep(random.uniform(3, 8))  # 风控间隔

    total = len(accounts)
    fail_cnt = total - success_cnt
    status_icon = "✅" if success_cnt == total else ("❌" if success_cnt == 0 else "⚠️")
    notify_title = f"{status_icon} {title}"

    lines = [f"# {notify_title} - 执行报告", ""]
    lines.append(f"⏰ {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"🌐 使用域名: {base_url}")
    lines.append(f"📊 总计 {total} 账号，✅ {success_cnt} / ❌ {fail_cnt}")
    lines.append("\n## 详细结果")
    for r in results:
        icon = "✅" if r["success"] else "❌"
        lines.append(f"- {icon} **[{r['username']}]** {r['message']}")
        stats = r.get("stats") or {}
        if stats:
            ordered = [f"{k}: {stats[k]}" for k in ("连续签到", "签到等级", "积分奖励", "总天数")
                       if k in stats]
            if ordered:
                lines.append(f"  - 签到统计：{'；'.join(ordered)}")
    report = "\n".join(lines)

    print()
    print("=" * 60)
    print(report)
    print("=" * 60)

    should_notify = notify_enabled and (not notify_only_fail or fail_cnt > 0)
    if should_notify:
        send_notify(notify_title, report)
    elif not notify_enabled:
        print("ℹ️ LWFIP_NOTIFY=false，已禁用推送")
    elif notify_only_fail and fail_cnt == 0:
        print("ℹ️ 全部成功且已开启 ONLY_FAIL，跳过推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⚠️ 用户中断")
        sys.exit(2)
