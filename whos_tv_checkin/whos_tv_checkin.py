# -*- coding: utf-8 -*-
"""
new Env('whos.tv 签到');
cron: 15 8 * * *

适配青龙面板 - 浏览器方案(Patchright 驱动真实浏览器内核)

背景:whos.tv 已启用 Cloudflare Managed Challenge 人机验证。
纯 HTTP 库(requests / curl_cffi)即使伪造 UA、Cookie、TLS 指纹,
也一律被 403 挑战页拦截(已实测)。唯一可行路径:真实浏览器内核
自动通过挑战后,再在浏览器会话内 fetch 调用接口。
选用 Patchright(未检测版 Playwright):无 Runtime.enable/Console.enable
等 CDP 自动化痕迹,可通过 Managed Challenge(原版 Playwright 会被识别)。

运行要求:
  - Python 依赖:patchright
  - 系统依赖:chromium + xvfb(容器内无显示,需 xvfb 提供虚拟显示)
  - 青龙任务命令建议:
      xvfb-run -a task whos_tv_checkin/whos_tv_checkin.py
    (headless 无头模式实测被 Cloudflare 拦截,必须走有头 + xvfb)

站点：https://whos.tv/        签到页：https://whos.tv/points-center/tasks

环境变量：
  WHOSTV_COOKIE   完整 Cookie 字符串；多账号用 & 分隔
  WHOSTV_ACCOUNT  账号密码，格式 用户名#密码；多账号用 & 分隔
                  例：user1@mail.com#pass1&user2#pass2
  WHOSTV_PROXY    HTTP/SOCKS 代理；whos.tv 在大陆网络被屏蔽，建议走代理
                   例：http://172.17.0.1:7890   或   socks5://172.17.0.1:7891
                   未配置时自动回退青龙全局代理（HTTPS_PROXY / HTTP_PROXY / ALL_PROXY）
  WHOSTV_NOTIFY   true/false，默认 true，是否调用青龙 notify.py 推送
  WHOSTV_NOTIFY_ONLY_FAIL  true/false，默认 false，仅当存在失败时才推送
                   （需 WHOSTV_NOTIFY=true 时生效，全部成功则静默）
  WHOSTV_TIMEOUT  HTTP 超时秒数，默认 30（fetch 请求）
  WHOSTV_BROWSER_PATH  可选，指定 chromium 可执行文件路径（默认自动探测）
  WHOSTV_DEBUG    true 时输出探测细节（试过哪些路径、状态码、响应片段）

两种认证方式可同时使用，也可单独使用。Cookie 优先执行，账号登录随后执行。
若两者都不配置则脚本退出。

作者: 箫遥风
"""

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from datetime import datetime

# 强制 stdout/stderr 使用 UTF-8，避免 Windows 控制台 GBK 报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from patchright.sync_api import sync_playwright
except ImportError:
    print("❌ 缺少 patchright，请在青龙「依赖管理」-「Python」中安装 patchright")
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

HOME = "https://whos.tv"
TASKS_URL = f"{HOME}/points-center/tasks"
LOGIN_URL = f"{HOME}/api/login"

# Cloudflare 挑战单轮最长等待秒数（实测通过时间约 5~150 秒，波动较大）
CF_CHALLENGE_TIMEOUT = 180
# 挑战未通过时的额外轮数。出口 IP 信誉波动时 CF 放行是概率性的（同环境
# 实测通过率约 1/3 且随机），多轮 + 轮间隔抽签比单轮死等通过率高得多
CF_CHALLENGE_RETRY = 3
# 轮间隔秒数：刷新页面重新触发挑战，给 CF 风控窗口滑动的时间
CF_CHALLENGE_ROUND_DELAY = 20

# 网络级失败（超时/断网，拿不到任何 HTTP 响应）的自动重试次数与间隔。
# 仅网络层失败才重试；服务器已有响应（含 4xx/5xx）属业务结果，重试无意义
NETWORK_RETRY = 2
NETWORK_RETRY_DELAY = 3

# 首跳导航（goto）的网络错误重试次数与间隔。浏览器首跳不走页内 fetch 的重试
# 链路，代理/链路瞬时抖动（ERR_CONNECTION_CLOSED 等）单独重发兜底；
# goto 自身超时说明链路极慢，重发大概率还是超时，不在此列
GOTO_RETRY = 2
GOTO_RETRY_DELAY = 5

# 浏览器调试端口与启动等待上限。
# Patchright launch 自带启动超时（受限容器里 chromium 冷启动可能远超默认值，
# 通过 WHOSTV_BROWSER_WAIT 调大），端口同时用于孤儿实例的 CDP 复用检测。
BROWSER_PORT_DEFAULT = 9222
BROWSER_WAIT_DEFAULT = 180

# C 层：常见路径白名单（按经验顺序，v2 真实接口优先）
FALLBACK_PATHS = [
    "/api/user/tasks/signin",       # ★ v2 真实签到接口
    "/api/user/tasks/checkin",
    "/api/user/signin",
    "/api/user/checkin",
    "/api/checkin",
    "/api/check-in",
    "/api/sign-in",
    "/api/signin",
    "/api/user/sign-in",
    "/api/points/checkin",
    "/api/points-center/checkin",
    "/api/points-center/sign-in",
    "/api/tasks/checkin",
    "/api/tasks/sign-in",
    "/api/v1/checkin",
    "/api/v1/sign-in",
]

# 提取候选 API 路径的正则
API_PATTERN = re.compile(
    r"""['"`](/api/[A-Za-z0-9\-_/]*"""
    r"""(?:check[-_]?in|sign[-_]?in|claim|daily)"""
    r"""[A-Za-z0-9\-_/]*)['"`]""",
    re.IGNORECASE,
)


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


# 青龙面板「配置文件 / 环境变量」里配置的全局代理变量
GLOBAL_PROXY_KEYS = (
    "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)


def resolve_proxy(*specific_keys: str) -> tuple:
    """
    解析代理地址，返回 (代理地址, 来源变量名)，两者均可能为空字符串。

    优先级：脚本专属变量 > 青龙全局代理变量 > 空（直连）。
    Patchright 的 launch 参数不会读取环境变量，浏览器链路必须在这里显式取值；
    HTTP 链路走 urllib，其 ProxyHandler 本身就会读环境变量，无需干预。
    """
    for name in (*specific_keys, *GLOBAL_PROXY_KEYS):
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def parse_cookie_str(s: str) -> dict:
    """把 'a=1; b=2' 这种 Cookie 头字符串解析成 dict。"""
    jar = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar[k.strip()] = v.strip()
    return jar


def parse_credentials(env_value: str) -> list:
    """解析 'user1#pass1&user2#pass2' 或换行分隔 -> [(user, pwd), ...]"""
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
    return s[:2] + "*" * max(1, len(s) - 4) + s[-2:]


def find_checkin_state(html: str):
    """
    从 HTML 中检测签到按钮状态。
    data-signed-in 可能由 JS 动态生成而不在服务端 HTML 里，因此同时检测
    insufficient-points-checkin-btn 等元素判断页面是否正常。
    返回 'true' / 'false' / None（找不到签到相关元素）。
    """
    # 页面形态一：HTML 里直接带 data-signed-in
    m = re.search(
        r'id=["\']checkin-btn["\'][^>]*?data-signed-in=["\'](true|false)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).lower()
    m = re.search(
        r'data-signed-in=["\'](true|false)["\'][^>]*?id=["\']checkin-btn["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).lower()
    # ★ v2 新版：有 insufficient-points-checkin-btn 说明页面加载正常
    # 按钮状态需通过 API 判断，这里返回 'false' 表示需要签到
    if "insufficient-points-checkin-btn" in html or "checkin-btn" in html:
        return "false"
    return None


def looks_like_login_page(html: str) -> bool:
    """粗略判断 Cookie 失效被踢回登录页。"""
    if not html:
        return False
    # 任务页应当出现 checkin-btn；如完全没有，且页面里出现明显登录元素，判定为失效
    if "checkin-btn" in html:
        return False
    lowered = html.lower()
    hits = 0
    for kw in ("/login", "sign in", "登录", "登 录", "log in", "signin"):
        if kw.lower() in lowered:
            hits += 1
    return hits >= 1


def extract_api_candidates(text: str) -> list:
    """从文本（HTML 或 JS 源码）里抽取所有看起来像签到接口的相对路径。"""
    if not text:
        return []
    seen = []
    for m in API_PATTERN.finditer(text):
        path = m.group(1)
        if path not in seen:
            seen.append(path)
    return seen


def absolutize(url: str) -> str:
    """把相对路径补成绝对地址。"""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return HOME + url
    return HOME + "/" + url


# ====================== 浏览器层 ======================

def _find_chromium(browser_path: str) -> str:
    """返回 chromium 可执行文件路径；环境变量优先，否则 which 探测；找不到返回空串。"""
    if browser_path:
        return browser_path
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    # Windows 的 Chrome/Edge 不在 PATH，探测常见安装路径（本地调试用）
    if platform.system().lower() == "windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    return ""


def _probe_proxy(proxy: str) -> None:
    """TCP 探测代理 host:port 在容器内是否可达（DNS 波动 / 网络隔离一测便知）。"""
    m = re.match(r"^[a-zA-Z]+://([^:/\\]+):(\d+)", proxy.strip())
    if not m:
        print(f"  代理地址解析失败，跳过连通性探测: {proxy}")
        return
    host, port = m.group(1), int(m.group(2))
    try:
        with socket.create_connection((host, port), timeout=3):
            print(f"  代理 {proxy} 可达（TCP 连通）")
    except OSError as e:
        print(f"  ❌ 代理 {proxy} 不可达: {e}")


def diagnose_browser_env(proxy: str = "") -> None:
    """
    浏览器启动失败时的环境诊断：打印 chromium / xvfb / DISPLAY / /dev/shm / 磁盘 /
    内存 / 浏览器进程数 / 代理连通性，并手动试启动一次 chromium 抓取崩溃原因
    （框架自身不回传 stderr）。仅用于 Linux 容器环境，Windows 本地跳过。
    """
    if platform.system().lower() != "linux":
        return
    print("  ── 环境诊断（Linux）──")
    # 1. chromium 路径与版本
    chrome_path = _find_chromium(os.getenv("WHOSTV_BROWSER_PATH") or "")
    if chrome_path:
        print(f"  chromium 路径: {chrome_path}")
        try:
            # stderr 也合并，Debian 的 chromium 可能把版本输出写到 stderr。
            # 注意：capture_output=True 与 stderr=STDOUT 冲突，须显式指定管道。
            ver = subprocess.run(
                [chrome_path, "--version"], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=3,
            ).stdout.strip()
            print(f"  版本: {ver or '(无输出)'}")
        except Exception as e:
            print(f"  手动执行 --version 失败: {e}")
    else:
        print("  ❌ 未找到 chromium/chrome 可执行文件（which 无结果），请安装 chromium 或设置 WHOSTV_BROWSER_PATH")
    # 2. xvfb
    for name in ("xvfb-run", "Xvfb"):
        p = shutil.which(name)
        if p:
            print(f"  {name}: {p}")
        else:
            print(f"  ❌ {name} 未找到")
    # 3. DISPLAY 环境变量
    print(f"  DISPLAY: {os.getenv('DISPLAY') or '(未设置 — 有头模式会启动失败，须用 xvfb-run)'}")
    # 4. /dev/shm 容量（容器默认 64MB，chromium 渲染进程常因此崩溃）
    try:
        out = subprocess.run(
            ["df", "-h", "/dev/shm"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        print(f"  /dev/shm:\n{out}")
    except Exception:
        print("  /dev/shm: 无法读取（跳过）")
    # 5. 磁盘 / 内存 / chromium 进程数（辅助判断资源受限导致的启动慢/卡死）
    for cmd, label in ((["df", "-h", "/"], "磁盘"), (["free", "-m"], "内存")):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
            print(f"  {label}:\n{out}")
        except Exception:
            pass
    try:
        out = subprocess.run(["ps", "-ef"], capture_output=True, text=True, timeout=3).stdout
        count = sum(1 for line in out.splitlines() if "chromium" in line.lower())
        print(f"  chromium 进程存活数: {count}")
    except Exception:
        pass
    # 6. 手动试启动 chromium，抓取真实崩溃原因（进程存活但 60 秒未完成渲染即为卡死）
    if not chrome_path:
        print("  ❌ 无 chromium 可执行文件，跳过手动启动诊断")
    else:
        print("  ── 手动试启动 chromium（最多 60 秒）──")
        try:
            p = subprocess.Popen(
                [chrome_path, "--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-gpu", "--headless=new", "--enable-logging=stderr",
                 "--dump-dom", "about:blank"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                out, err = p.communicate(timeout=60)
                if p.returncode == 0:
                    print(f"  ✅ chromium 手动启动成功（返回码 0），输出: {(out or '').strip()[:100] or '(空)'}")
                else:
                    print(f"  ❌ chromium 手动启动失败（返回码 {p.returncode}）")
                    if err:
                        print(f"  stderr:\n{(err or '').strip()[:600]}")
            except subprocess.TimeoutExpired:
                # 进程存活但 60 秒未完成渲染 —— 与脚本预启动超时一致，读取已刷出的日志
                print("  ⚠️ chromium 进程存活但 60 秒内未完成渲染（启动极慢/卡死）")
                try:
                    p.terminate()
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        p.wait(5)
                    except Exception:
                        pass
                if p.stderr:
                    try:
                        buffered = p.stderr.read()
                    except Exception:
                        buffered = ""
                    if buffered:
                        print(f"  已刷出的 stderr:\n{buffered.strip()[:600]}")
        except Exception as e:
            print(f"  手动启动诊断跳过: {e}")
    # 7. 代理连通性（whos.tv 依赖代理，不可达时浏览器网络层会异常）
    if proxy:
        _probe_proxy(proxy)


def _http_json(port: int, path: str = "/json", timeout: float = 2) -> object:
    """GET http://127.0.0.1:port/path 并解析 JSON；失败返回 None。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def browser_port_ready(port: int, timeout: float = 2) -> bool:
    """调试端口是否就绪：/json 列表里已出现 page/webview 标签页，与 CDP 调试协议判据一致。"""
    tabs = _http_json(port, "/json", timeout)
    if not isinstance(tabs, list):
        return False
    return any(tab.get("type") in ("page", "webview") for tab in tabs)


def port_in_use(port: int, timeout: float = 1) -> bool:
    """TCP 层端口是否已被占用（包括非 chromium 进程）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _clear_profile_locks(profile_dir: str) -> None:
    """启动前清理上次异常退出遗留的 Singleton 锁，否则浏览器会判定已有实例而拒绝接管。"""
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = os.path.join(profile_dir, lock)
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except OSError:
                pass


def _finalize_adopted_browser(pw, browser) -> None:
    """
    任务结束时收尾 CDP 接管的孤儿浏览器：先走 CDP Browser.close 强杀进程
    （adopted 浏览器上 browser.close() 只断开连接不杀进程，会留幽灵实例占端口），
    失败再退回 browser.close() 兜底；全程吞异常，不影响主流程收尾。
    """
    try:
        browser.new_browser_cdp_session().send("Browser.close")
    except Exception:
        pass
    try:
        browser.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass


def _finalize_launched_context(pw, ctx) -> None:
    """任务结束时收尾自启动的持久化上下文：ctx.close() 会连带终止浏览器进程。"""
    try:
        ctx.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass


def create_browser(proxy: str, debug: bool, port: int = BROWSER_PORT_DEFAULT,
                   wait: int = BROWSER_WAIT_DEFAULT) -> tuple:
    """
    创建真实浏览器实例，返回 (page, finalize)。
    ★ 必须使用有头模式：headless 无头模式实测被 Cloudflare 拦截，
    青龙容器内需通过 xvfb-run 提供虚拟显示（见文件头注释）。

    正常路径由 Patchright 的 launch_persistent_context 启动（官方最佳实践，
    自带 180s 级启动超时与失败 stderr 摘要）；若端口已有上次异常退出遗留的
    可用实例，改走 connect_over_cdp 接管复用，省一次冷启动。
    finalize() 供任务结束时强杀浏览器进程，两路径语义一致。
    """
    # ★ profile 目录必须用规范 Windows/Linux 临时路径：不能写死 "/tmp/..."——
    #   Windows 上 Chrome 收到字面量混合斜杠路径时 ProcessSingleton 判定异常，
    #   会把启动请求转交给"现有的浏览器会话"后立即退出（返回码 0、调试端口永不监听）
    profile_dir = os.path.abspath(os.path.join(tempfile.gettempdir(), "whostv_profile", str(port)))
    os.makedirs(profile_dir, exist_ok=True)
    _clear_profile_locks(profile_dir)

    browser_path = (os.getenv("WHOSTV_BROWSER_PATH") or "").strip()
    if proxy and debug:
        print(f"   [debug] 浏览器代理: {proxy}")
    if browser_path and debug:
        print(f"   [debug] 浏览器路径: {browser_path}")

    pw = sync_playwright().start()
    try:
        # ① 端口已有可用浏览器（上次异常退出遗留的孤儿实例）→ CDP 接管复用，省冷启动
        if browser_port_ready(port):
            if debug:
                print(f"   [debug] 端口 {port} 已有可用浏览器，CDP 接管复用")
            browser = pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=wait * 1000
            )
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return page, lambda: _finalize_adopted_browser(pw, browser)

        # ② 端口被占用但不是可用的 chromium 调试服务 → 换端口或清理占用进程
        if port_in_use(port):
            raise RuntimeError(
                f"端口 {port} 已被其他进程占用但不是可用的浏览器调试端口，"
                "请设置 WHOSTV_BROWSER_PORT 换一个端口"
            )

        # ③ 正常路径：Patchright 自己启动（持久化上下文，有头模式）
        #   不传自定义 UA/headers（官方隐身最佳实践红线）；容器必需参数
        #   （--no-sandbox、--disable-dev-shm-usage 等）已在默认参数表中
        if debug:
            print(f"   [debug] 启动 chromium（有头持久化上下文，超时上限 {wait}s）...")
        chrome_path = _find_chromium(browser_path)
        if not chrome_path:
            raise RuntimeError(
                "未找到 chromium/chrome 可执行文件，请 apt 安装 chromium 或设置 WHOSTV_BROWSER_PATH"
            )
        launch_kwargs = {
            "user_data_dir": profile_dir,
            "executable_path": chrome_path,
            "headless": False,
            "no_viewport": True,
            "timeout": wait * 1000,
        }
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return page, lambda: _finalize_launched_context(pw, ctx)
    except Exception:
        # 启动中途失败时释放 playwright 资源（成功路径交给 finalize）
        pw.stop()
        raise


def wait_for_challenge(page, timeout: int = CF_CHALLENGE_TIMEOUT) -> bool:
    """
    等待 Cloudflare 挑战页自动通过。
    挑战页标题为「请稍候…」/「Just a moment...」，通过后跳转为站点真实标题。
    轮询标题，超时返回 False。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        if title and "请稍候" not in title and "Just a moment" not in title:
            return True
        time.sleep(3)
    return False


def browser_fetch(page, url: str, method: str = "POST",
                  data: dict = None, timeout: int = 30) -> tuple:
    """
    在浏览器会话内执行 fetch，自动携带浏览器 Cookie 与 TLS 指纹，
    可绕过 Cloudflare 对纯 HTTP 库的 403 拦截。
    返回 (status_code: int, text: str)。
    status 约定：>0 为真实 HTTP 状态码；0 表示网络级失败（拿不到任何响应），
    text 会带具体原因前缀：
      - "__TIMEOUT__|..."  AbortSignal 超时
      - "__NETWORK__|..."  连接失败（断网/代理抖动/DNS）
      - "__JSERROR__|..."  JS 执行层异常（evaluate 抛错等）
    """
    url_js = json.dumps(url)
    method_js = json.dumps(method)
    # body 传 undefined 表示无请求体（GET）
    body_js = "undefined" if data is None else json.dumps(json.dumps(data))
    # fetch 链路全程 catch：rejected promise 依赖 evaluate 转 Python 异常，
    # 不同版本行为不一（有的抛异常有的返回 None），错误语义会糊掉，
    # 因此在 JS 内部捕获并转成结构化结果，让 Python 层拿到确定性的失败分类
    js = (
        f"(() => fetch({url_js}, {{method: {method_js}, "
        f"headers: {{'Content-Type': 'application/json', "
        f"'Accept': 'application/json, text/plain, */*', "
        f"'X-Requested-With': 'XMLHttpRequest'}}, "
        f"body: {body_js}, "
        f"signal: AbortSignal.timeout({timeout * 1000})"
        f"}})"
        f".then(r => r.text().then(t => ({{ok: true, status: r.status, text: t}})))"
        f".catch(e => ({{ok: false, "
        f"name: e && e.name || '', message: e && e.message || String(e)}})))()"
    )
    try:
        # evaluate 会自动 await 返回的 Promise（CDP 层 awaitPromise:true），
        # 拿到的就是 then 链产生的结构化对象；隔离 world 与主世界同源，
        # fetch / Cookie / AbortSignal 行为一致
        result = page.evaluate(js)
    except Exception as e:
        return 0, f"__JSERROR__|请求异常: {e}"
    if not isinstance(result, dict):
        return 0, f"__JSERROR__|fetch 返回异常: {result}"
    if result.get("ok") is not True:
        err_name = result.get("name") or ""
        err_msg = result.get("message") or ""
        if err_name == "AbortError" or err_name == "TimeoutError":
            return 0, f"__TIMEOUT__|{timeout}s 内未收到响应"
        return 0, f"__NETWORK__|{err_name}: {err_msg}".rstrip(": ")
    return result.get("status", 0), result.get("text", "")


def is_network_failure(status: int, text: str) -> bool:
    """是否网络级失败（status==0 且带哨兵前缀）：服务器未返回任何响应，可安全重试。"""
    return status == 0 and text.startswith(("__TIMEOUT__", "__NETWORK__", "__JSERROR__"))


def network_fail_reason(text: str) -> str:
    """提取网络级失败的简短原因（剥掉哨兵前缀），用于日志与结果消息。"""
    for prefix in ("__TIMEOUT__", "__NETWORK__", "__JSERROR__"):
        if text.startswith(prefix):
            return text[len(prefix):].lstrip("|")
    return text


def fetch_with_retry(page, url: str, method: str = "POST",
                     data: dict = None, timeout: int = 30,
                     retries: int = NETWORK_RETRY, debug: bool = False) -> tuple:
    """
    browser_fetch 的网络级重试包装：仅当拿不到任何 HTTP 响应（超时/断网/JS 异常）
    时重试，已有服务器响应（含 403/500 等业务状态）不重复请求。
    返回 (status_code, text)，语义与 browser_fetch 一致。
    """
    status, text = browser_fetch(page, url, method=method, data=data, timeout=timeout)
    attempt = 0
    while is_network_failure(status, text) and attempt < retries:
        attempt += 1
        reason = network_fail_reason(text)
        print(f"   ⚠️ 请求无响应（{reason}），第 {attempt}/{retries} 次重试，"
              f"{NETWORK_RETRY_DELAY}s 后重发...")
        time.sleep(NETWORK_RETRY_DELAY)
        status, text = browser_fetch(page, url, method=method, data=data, timeout=timeout)
        if debug:
            print(f"   [debug] 重试后 {url} -> HTTP {status}")
    return status, text


def is_goto_network_error(e: Exception) -> bool:
    """goto 抛的是否网络层错误（连接被断/重置/DNS 等，可安全重发）。
    goto 自身超时不在列：链路极慢时重发大概率还是超时，白耗几分钟。"""
    msg = str(e)
    return "net::ERR_" in msg and "ERR_TIMED_OUT" not in msg


def goto_with_retry(page, url: str, timeout: int = None,
                    retries: int = GOTO_RETRY, debug: bool = False) -> None:
    """goto 的网络错误重试包装：代理/链路瞬时抖动时重发，最多 retries 次。"""
    kwargs = {"timeout": timeout} if timeout else {}
    attempt = 0
    while True:
        try:
            page.goto(url, **kwargs)
            return
        except Exception as e:
            if not is_goto_network_error(e) or attempt >= retries:
                raise
            attempt += 1
            print(f"   ⚠️ 导航网络错误（{e}），第 {attempt}/{retries} 次重试，"
                  f"{GOTO_RETRY_DELAY}s 后重发...")
            time.sleep(GOTO_RETRY_DELAY)
            if debug:
                print(f"   [debug] 重试导航 {url}")


def reset_cookies_keep_cf(page) -> None:
    """
    清空浏览器业务 Cookie，仅保留 cf_clearance。
    cf_clearance 与当前浏览器指纹绑定，清掉后新请求会重新触发挑战。
    多账号切换登录态时必须保留它，否则每个账号都要重过挑战。
    """
    context = page.context
    try:
        keep = [c for c in context.cookies(HOME) if c.get("name") == "cf_clearance"]
        context.clear_cookies()
        if keep:
            context.add_cookies(keep)
    except Exception as e:
        print(f"   ⚠️ 清除/回写 Cookie 失败（登录会重设会话 Cookie，不影响签到）: {e}")


# ====================== 登录 ======================

def login_one_account(page, username: str, password: str,
                      timeout: int, debug: bool, label: str) -> tuple:
    """
    在浏览器会话内用账号密码登录 whos.tv。
    返回 (success: bool, message: str)。
    登录成功后浏览器自动携带认证 Cookie（HYPERF_SESSION_ID）。
    """
    status, text = fetch_with_retry(
        page, LOGIN_URL,
        data={"username": username, "password": password},
        timeout=timeout, debug=debug,
    )

    if status == 0:
        # HTTP 0 = 没拿到任何响应，具体原因（超时/断网/JS 异常）无论 debug 与否都要打出来，
        # 否则线上失败时丢失最关键的定位线索
        print(f"   ❌ 登录请求网络级失败: {network_fail_reason(text)}")

    if debug:
        body = (text or "")[:200].replace("\n", " ")
        print(f"   [debug] POST {LOGIN_URL} -> HTTP {status} | {body}")

    # 解析响应
    try:
        data = json.loads(text)
    except Exception:
        # 网络级失败给出具体原因；其余保持原格式（如 Cloudflare 拦截返回 HTML 页）
        if status == 0:
            return False, f"登录网络异常: {network_fail_reason(text)}"
        return False, f"登录失败: 响应非 JSON (HTTP {status})"

    code = data.get("code")
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None

    if code_int == 200000:
        # 提取用户名用于展示
        profile = (data.get("data") or {})
        display_name = profile.get("username") or profile.get("email") or username
        return True, f"登录成功 [{mask(display_name)}]"

    # 登录失败，分类错误信息
    message = data.get("message") or ""
    if "密码" in message or "password" in message:
        return False, f"登录失败: 密码错误 | {message}"
    if "不存在" in message or "not found" in message or "not exist" in message:
        return False, f"登录失败: 账号不存在 | {message}"
    if "封" in message or "ban" in message or "disable" in message:
        return False, f"登录失败: 账号已被封禁 | {message}"
    return False, f"登录失败 (code={code_int}): {message}"


# ====================== 签到核心 ======================

def is_success_response(resp_text: str, status_code: int) -> tuple:
    """
    判定一个签到接口的响应是否表示成功。
    返回 (success: bool, msg: str)。
    适配 v2 新格式：code=200000 成功，code=406008 已签到，code=401 未授权。
    """
    if status_code >= 500:
        return False, f"HTTP {status_code}"

    # ★ HTTP 401 直接判定 Cookie 失效（v2 返回 {"code":401,"message":"Unauthorized"}）
    if status_code == 401:
        return False, "Cookie 失效 (HTTP 401)"

    text = (resp_text or "").strip()
    short = text[:200].replace("\n", " ")

    # 优先按 JSON 解析
    data = None
    try:
        data = json.loads(text)
    except Exception:
        data = None

    if isinstance(data, dict):
        # ★ v2 新格式：code 字段为 6 位数
        # 成功/幂等/失效判定仅在 HTTP 200/201 时进入——403/404 但 JSON 体
        # 恰好带 code=200000（网关自定义错误页）时不得误判成功
        if "code" in data and status_code in (200, 201):
            code = data.get("code")
            try:
                code_int = int(code)
            except (TypeError, ValueError):
                code_int = None
            if code_int is not None:
                if code_int == 200000:  # ★ v2 签到成功
                    d = data.get("data") or {}
                    points_earned = d.get("points_earned", 0)
                    streak_bonus = d.get("streak_bonus", 0)
                    consecutive_days = d.get("consecutive_days", 1)
                    total = points_earned + streak_bonus
                    extra = f"（含连续签到奖励 {streak_bonus}）" if streak_bonus > 0 else ""
                    msg = f"签到成功，获得 {total} 积分{extra}，连续签到 {consecutive_days} 天"
                    return True, msg
                if code_int == 406008:  # ★ v2 今日已签到
                    return True, f"已签到（幂等）| {data.get('message', '')}"
                if code_int == 401:  # ★ v2 Cookie 失效
                    return False, f"Cookie 失效 (code=401)"
                # 旧格式兼容：code=0 或 200
                if code_int in (0, 200):
                    return True, f"code={code_int} | {data.get('message') or data.get('msg') or ''}".strip(" |")

        # 常见成功字段
        for k in ("success", "ok"):
            if data.get(k) is True:
                return True, f"{k}=true | {data.get('message') or data.get('msg') or ''}".strip(" |")
        if str(data.get("status", "")).lower() in ("ok", "success", "1"):
            return True, f"status={data.get('status')} | {data.get('message') or data.get('msg') or ''}".strip(" |")

        # 文案里命中"已签到"等幂等提示也算成功
        msg_text = " ".join(str(data.get(k, "")) for k in ("message", "msg", "error", "detail"))
        if re.search(r"(已签到|已经签到|already.*sign|signed.*today|repeat)", msg_text, re.IGNORECASE):
            return True, f"已签到（幂等）| {msg_text[:80]}"

        # 没有明确成功字段 → 当失败处理
        return False, f"HTTP {status_code} | {short}"

    # 非 JSON：状态码 200 且文本里出现"成功"或"已签到"才算
    if status_code == 200 and re.search(
        r"(签到成功|已签到|已经签到|sign.*success|already.*sign|signed.*today)", text, re.IGNORECASE
    ):
        return True, f"HTTP 200 | {short}"

    return False, f"HTTP {status_code} | {short}"


def try_signin_post(page, url: str, timeout: int, debug: bool) -> tuple:
    """对一个候选 URL 发 POST。返回 (success, msg)。"""
    status, text = fetch_with_retry(page, url, timeout=timeout, debug=debug)

    if status == 0:
        # 网络级失败原因无条件打印（同登录请求：这是定位问题的关键线索）
        print(f"   ❌ 签到请求网络级失败: {network_fail_reason(text)}")

    ok, msg = is_success_response(text, status)
    if debug:
        body = (text or "")[:120].replace("\n", " ")
        print(f"   [debug] POST {url} -> HTTP {status} | {body}")
    return ok, msg


def _do_signin(page, label: str, timeout: int, debug: bool) -> tuple:
    """
    对已认证的浏览器会话执行签到流程（Cookie 模式和账号模式共享）。
    返回 (success: bool, message: str)。
    """
    # ★ 先直接调 v2 签到 API（不依赖 HTML 按钮状态）
    signin_url = f"{HOME}/api/user/tasks/signin"
    ok, msg = try_signin_post(page, signin_url, timeout, debug)

    if ok:
        return True, msg

    # Cookie 失效判定（"401" 单独匹配会误伤积分/ID 等数字，需精确匹配
    # "HTTP 401" / "code=401" 两种已知失效文案）
    if "Cookie 失效" in msg or "HTTP 401" in msg or "code=401" in msg:
        return False, f"Cookie 失效，请重新登录后复制 Cookie"

    # 已知接口失败，走探测兜底
    # 网络级失败短路：已知接口重试后仍拿不到任何响应，网络/代理大概率已断，
    # 继续拉任务页+白名单探测只会逐个超时（候选最多 15 个 × 各 30s），快速失败止损
    m_http0 = re.match(r"^HTTP 0 \| (__(?:TIMEOUT|NETWORK|JSERROR)__\|.*)$", msg)
    if m_http0:
        return False, f"网络异常无法完成签到（{network_fail_reason(m_http0.group(1))}），请检查代理 WHOSTV_PROXY 与容器网络"

    # 浏览器会话内拉取任务页 HTML（登录态检查 + 扫描候选接口）
    status, html = fetch_with_retry(page, TASKS_URL, method="GET", timeout=timeout, debug=debug)
    if status != 200:
        return False, f"任务页 HTTP {status}，已知接口也失败: {msg}"

    # 检查登录态：登录页直接判 Cookie 失效；
    # state is None（HTML 未识别到签到按钮，可能是站点改版或 SPA 壳）不阻断，
    # 继续走 A 层 + C 层白名单探测兜底
    state = find_checkin_state(html)
    if state is None and looks_like_login_page(html):
        return False, f"Cookie 失效，请重新登录后复制 Cookie"

    if state == "true":
        return True, f"今日已签到（页面状态）"

    # A 层：HTML 内联扫描
    candidates = [absolutize(p) for p in extract_api_candidates(html)]

    # C 层兜底：附加白名单（跳过已尝试的已知接口）
    for p in FALLBACK_PATHS:
        url = absolutize(p)
        if url not in candidates and url != signin_url:
            candidates.append(url)

    if debug:
        print(f"   [debug] 探测兜底: 总计候选 URL {len(candidates)} 个"
              f"{'（HTML 未识别到签到按钮，依赖白名单兜底）' if state is None else ''}")

    # 逐个尝试探测接口
    last_msg = msg  # 保留已知接口的错误信息
    for url in candidates:
        ok2, msg2 = try_signin_post(page, url, timeout, debug)
        if ok2:
            return True, f"签到成功 [{url}] {msg2}"
        # Cookie 失效短路：候选接口已确认未授权时，不必白跑剩余候选
        if "Cookie 失效" in msg2 or "HTTP 401" in msg2 or "code=401" in msg2:
            return False, f"Cookie 失效，请重新登录后复制 Cookie（探测 {url} 返回未授权）"
        # 网络级失败短路：探测途中网络断开，剩余候选同样会逐个超时，止损退出
        m_fail = re.match(r"^HTTP 0 \| (__(?:TIMEOUT|NETWORK|JSERROR)__\|.*)$", msg2)
        if m_fail:
            return False, f"网络异常中断签到探测（{network_fail_reason(m_fail.group(1))}），最后请求: {url}"
        last_msg = f"{url} -> {msg2}"

    if state is None:
        return False, f"页面未找到签到按钮（站点可能改版），已知接口失败: {msg[:80]}"
    return False, f"所有候选接口均失败（已知: {msg[:80]} | 最后: {last_msg[:80]}）"


# ====================== 两种模式的入口函数 ======================

def signin_one_account(idx: int, cookie_str: str, timeout: int, debug: bool,
                       page) -> tuple:
    """
    Cookie 模式：对单个账号执行签到流程。
    返回 (success: bool, message: str)。
    """
    label = f"账号 {idx}"

    # 注入 Cookie（清掉上一账号的登录态，但保留 cf_clearance）
    cookies = parse_cookie_str(cookie_str)
    if not cookies:
        return False, f"{label}: ❌ Cookie 解析为空"
    reset_cookies_keep_cf(page)
    # Playwright 的 add_cookies 要求 domain 与 path 成对，否则注入被拒
    page.context.add_cookies([
        {"name": k, "value": v, "domain": ".whos.tv", "path": "/"}
        for k, v in cookies.items()
    ])

    # 执行签到
    ok, msg = _do_signin(page, label, timeout, debug)
    prefix = "✅" if ok else "❌"
    decorated = f"{label}: {prefix} {msg}"
    return ok, decorated


def signin_with_login(idx: int, username: str, password: str, timeout: int,
                      debug: bool, page) -> tuple:
    """
    账号模式：先登录再签到。
    返回 (success: bool, message: str)。
    """
    label = f"账号 {idx} [{mask(username)}]"

    # 清掉上一账号的登录态（保留 cf_clearance），避免会话串号
    reset_cookies_keep_cf(page)

    # ① 登录
    login_ok, login_msg = login_one_account(page, username, password, timeout, debug, label)
    if not login_ok:
        return False, f"{label}: ❌ {login_msg}"

    if debug:
        print(f"   [debug] {login_msg}")

    # ② 签到
    ok, msg = _do_signin(page, label, timeout, debug)
    prefix = "✅" if ok else "❌"
    decorated = f"{label}: {prefix} {msg}"
    return ok, decorated


# ====================== 入口 ======================

def main():
    title = "whos.tv 签到"

    raw_cookie = os.getenv("WHOSTV_COOKIE", "").strip()
    raw_account = os.getenv("WHOSTV_ACCOUNT", "").strip()

    if not raw_cookie and not raw_account:
        print("❌ 未配置任何认证方式，请设置 WHOSTV_COOKIE 或 WHOSTV_ACCOUNT 环境变量")
        sys.exit(1)

    notify = env_bool("WHOSTV_NOTIFY", True)
    notify_only_fail = env_bool("WHOSTV_NOTIFY_ONLY_FAIL", False)
    timeout = env_int("WHOSTV_TIMEOUT", 30)
    debug = env_bool("WHOSTV_DEBUG", False)
    proxy, proxy_source = resolve_proxy("WHOSTV_PROXY")
    browser_wait = env_int("WHOSTV_BROWSER_WAIT", BROWSER_WAIT_DEFAULT)
    browser_port = env_int("WHOSTV_BROWSER_PORT", BROWSER_PORT_DEFAULT)

    # 收集两类账号
    cookie_accounts = [c.strip() for c in raw_cookie.split("&") if c.strip()] if raw_cookie else []
    login_accounts = parse_credentials(raw_account)

    total = len(cookie_accounts) + len(login_accounts)
    if total == 0:
        print("❌ 未解析到有效账号，请检查环境变量格式")
        sys.exit(1)

    print("=" * 60)
    print(f"🐳 whos.tv 签到  |  共 {total} 个账号  |  {datetime.now():%Y-%m-%d %H:%M:%S}")
    if raw_cookie:
        print(f"🍪 Cookie 账号: {len(cookie_accounts)} 个")
    if login_accounts:
        print(f"🔑 账号密码: {len(login_accounts)} 个")
    if not proxy:
        print("🌐 代理: 未配置（直连；whos.tv 在大陆网络大概率无法访问）")
    elif proxy_source == "WHOSTV_PROXY":
        print(f"🌐 代理: {proxy}")
    else:
        print(f"🌐 代理: {proxy}（来源: {proxy_source}）")
    print("=" * 60)

    # 启动浏览器并等待 Cloudflare 挑战通过（带多轮抽签重试）
    # 出口 IP 信誉波动时 CF 放行是概率性的（实测同环境通过率约 1/3 且随机），
    # 单轮长等无意义，多轮 + 轮间隔（让 CF 风控窗口滑动）才能抽中放行
    print("\n🚀 启动浏览器，等待 Cloudflare 挑战通过（每轮最长"
          f" {CF_CHALLENGE_TIMEOUT} 秒 × {CF_CHALLENGE_RETRY + 1} 轮，轮间隔"
          f" {CF_CHALLENGE_ROUND_DELAY}s）...")
    page = None
    finalize = None
    try:
        page, finalize = create_browser(proxy, debug, browser_port, browser_wait)
        challenge_ok = False
        for attempt in range(CF_CHALLENGE_RETRY + 1):
            goto_with_retry(page, HOME, timeout=(CF_CHALLENGE_TIMEOUT + 30) * 1000,
                            debug=debug)
            challenge_ok = wait_for_challenge(page)
            if challenge_ok:
                break
            if attempt < CF_CHALLENGE_RETRY:
                print(f"⚠️ 挑战第 {attempt + 1}/{CF_CHALLENGE_RETRY + 1} 轮未通过，"
                      f"{CF_CHALLENGE_ROUND_DELAY}s 后刷新重试...")
                time.sleep(CF_CHALLENGE_ROUND_DELAY)
        if not challenge_ok:
            raise RuntimeError(
                f"Cloudflare 挑战在 {CF_CHALLENGE_RETRY + 1} 轮内均未通过"
                "（出口 IP 被 CF 高风险判定且放行窗口未抽中，建议换代理节点）"
            )
        if debug:
            print(f"   [debug] 挑战通过 | 标题: {page.title()}")
            try:
                print(f"   [debug] UserAgent: {page.evaluate('navigator.userAgent')}")
            except Exception:
                pass
    except Exception as e:
        print(f"❌ 浏览器启动或挑战失败: {e}")
        print("   💡 请确认容器内已安装 chromium 和 patchright，并用"
              " xvfb-run -a 运行脚本（headless 无头模式会被 Cloudflare 拦截）")
        # 挑战未通过（RuntimeError）时 chromium 已正常启动，环境诊断无意义且会多耗几秒；
        # 仅在浏览器启动/连接失败时输出环境诊断，定位 chromium 崩溃的真实原因
        if not isinstance(e, RuntimeError):
            diagnose_browser_env(proxy)
        # 释放浏览器实例（挑战失败后仍在内存中占用资源）
        if finalize:
            finalize()
        if notify:
            send_notify(f"❌ {title} 全部失败（0/{total}）", f"浏览器启动或挑战失败: {e}")
        sys.exit(1)

    try:
        results = []
        success_cnt = 0

        # 先处理 Cookie 账号
        for i, ck in enumerate(cookie_accounts, start=1):
            print(f"\n▶ 处理 Cookie 账号 {i} ...")
            try:
                ok, msg = signin_one_account(i, ck, timeout, debug, page)
            except Exception:
                ok, msg = False, f"Cookie 账号 {i}: ❌ 脚本异常\n{traceback.format_exc(limit=2)}"
            if ok:
                success_cnt += 1
            print(msg)
            results.append({"idx": i, "success": ok, "message": msg, "mode": "Cookie"})
            if i < len(cookie_accounts):
                time.sleep(2)

        # 再处理账号密码账号
        for j, (user, pwd) in enumerate(login_accounts, start=1):
            seq = len(cookie_accounts) + j
            print(f"\n▶ 处理账号 {seq} [{mask(user)}] ...")
            try:
                ok, msg = signin_with_login(seq, user, pwd, timeout, debug, page)
            except Exception:
                ok, msg = False, f"账号 {seq} [{mask(user)}]: ❌ 脚本异常\n{traceback.format_exc(limit=2)}"
            if ok:
                success_cnt += 1
            print(msg)
            results.append({"idx": seq, "success": ok, "message": msg, "mode": "账号"})
            if j < len(login_accounts):
                time.sleep(2)
    finally:
        finalize()

    # 汇总
    print("\n" + "=" * 60)
    print(f"📊 完成：成功 {success_cnt} / 失败 {total - success_cnt}")
    print("=" * 60)

    # 标题按成败分档：全部成功 / 部分失败 / 全部失败
    if success_cnt == total:
        notify_title = f"✅ {title} 全部成功（{success_cnt}/{total}）"
    elif success_cnt == 0:
        notify_title = f"❌ {title} 全部失败（0/{total}）"
    else:
        notify_title = f"⚠️ {title} 部分失败（{success_cnt}/{total}）"

    # 正文：Markdown 分组。msg 形如 "账号 N: ✅ ...", 渲染时用 [账号 N] 标识并剥掉 msg 里冗余的 "账号 N:" 前缀，避免重复
    _label_prefix = re.compile(r"^账号\s*\d+\s*[:：]\s*")
    ok_lines = [f"- **[账号 {r['idx']}]** {_label_prefix.sub('', r['message'], count=1)}" for r in results if r["success"]]
    fail_lines = [f"- **[账号 {r['idx']}]** {_label_prefix.sub('', r['message'], count=1)}" for r in results if not r["success"]]

    lines = ["# whos.tv 签到 - 执行报告", ""]
    lines.append(f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"📊 总计 {total} 账号，✅ {success_cnt} / ❌ {total - success_cnt}")
    lines.append("")
    if ok_lines:
        lines.append("## 成功")
        lines.extend(ok_lines)
        lines.append("")
    if fail_lines:
        lines.append("## 失败")
        lines.extend(fail_lines)
        lines.append("")
    body = "\n".join(lines).strip()

    # 推送
    if notify:
        if notify_only_fail and success_cnt == total:
            print("ℹ️ WHOSTV_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(notify_title, body)
    else:
        print("ℹ️ WHOSTV_NOTIFY=false，已禁用推送")

    sys.exit(0 if success_cnt > 0 else 1)


if __name__ == "__main__":
    main()
