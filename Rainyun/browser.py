"""Playwright 浏览器层：Xvfb 虚拟显示 + 有头 Chromium + 反检测。

雨云签到提交强制走 Cap（cap.rainyun.cn）人机验证，服务端会校验 instrumentation
采集到的浏览器行为数据。实测结论：
- headless 浏览器会被 Cap 判为自动化（headless_token / SwiftShader / webdriver）
- Selenium 的合成点击无指针轨迹，行为分不足
- Playwright + Xvfb 有头 Chromium 可以正常通过

因此本模块统一负责浏览器生命周期，main.py 只依赖 page/context 对象。
"""
import atexit
import os
import random
import re
import shutil
import subprocess
import time

from playwright.sync_api import sync_playwright

# Xvfb 虚拟显示器编号：避开常见的 :0（真实桌面）
XVFB_DISPLAY = ":99"
# 分辨率够用即可：Cap 的 instrumentation 会在 20 秒内跑完 WASM 编译探测与
# 数十项环境检查，分辨率越高渲染开销越大，越容易在弱 CPU 容器里超时
XVFB_SCREEN = "1280x720x24"

# 候选浏览器二进制：Playwright 自带 Chromium 优先，其次系统 Chrome
CHROMIUM_BINARY_CANDIDATES = [
    "/ql/data/.cache/ms-playwright",   # 青龙数据卷持久化（重建容器不丢）
    "/home/qinglong/.cache/ms-playwright",
    os.path.expanduser("~/.cache/ms-playwright"),
]
CHROME_BINARY_PATHS = [
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/opt/google/chrome/chrome",
]

_xvfb_proc = None


def _detect_playwright_chromium() -> str:
    """在 Playwright 浏览器缓存目录里找最新版本的 chromium 可执行文件。

    目录形如 chromium-1248/chrome-linux64/chrome。必须按版本号数字排序而不是
    字符串排序：字典序下 "chromium-998" > "chromium-1248"，Playwright 升级后
    旧目录不删，会静默选到旧版浏览器。
    """
    candidates = []
    for base in CHROMIUM_BINARY_CANDIDATES:
        if not os.path.isdir(base):
            continue
        for entry in os.listdir(base):
            match = re.match(r"^chromium-(\d+)$", entry)
            if not match:
                continue
            for rel in ("chrome-linux64/chrome", "chrome-linux/chrome"):
                candidate = os.path.join(base, entry, rel)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    candidates.append((int(match.group(1)), candidate))
                    break
    if not candidates:
        return ""
    # 取版本号最大的
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _detect_chrome_binary() -> str:
    """探测可用的浏览器二进制：Playwright 自带优先，其次系统 Chrome。"""
    path = _detect_playwright_chromium()
    if path:
        return path
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    for path in CHROME_BINARY_PATHS:
        if os.path.isfile(path):
            return path
    return ""


def _browser_major_version(binary: str) -> str:
    """取浏览器主版本号，用于构造一致的 UA。"""
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        match = re.search(r"(\d+)\.", out)
        return match.group(1) if match else ""
    except Exception:
        return ""


def start_xvfb() -> None:
    """启动 Xvfb 虚拟显示器；已有 DISPLAY 或已在运行时跳过。

    有头浏览器需要显示器，容器里没有物理显示设备，必须用 Xvfb 提供虚拟屏。
    """
    global _xvfb_proc
    if os.getenv("DISPLAY"):
        return
    # 复用已在跑的实例，避免多账号重复启动
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"Xvfb {XVFB_DISPLAY}"], capture_output=True, timeout=5
        )
        if result.returncode == 0:
            os.environ["DISPLAY"] = XVFB_DISPLAY
            return
    except Exception:
        pass

    _xvfb_proc = subprocess.Popen(
        ["Xvfb", XVFB_DISPLAY, "-screen", "0", XVFB_SCREEN, "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = XVFB_DISPLAY
    atexit.register(stop_xvfb)
    # 等 X socket 就绪，否则浏览器启动会报 "Missing X server"
    for _ in range(30):
        if os.path.exists(f"/tmp/.X11-unix/X{XVFB_DISPLAY.lstrip(':')}"):
            return
        time.sleep(0.2)


def stop_xvfb() -> None:
    """结束本进程启动的 Xvfb。"""
    global _xvfb_proc
    if _xvfb_proc is None:
        return
    try:
        _xvfb_proc.terminate()
        _xvfb_proc.wait(timeout=5)
    except Exception:
        pass
    _xvfb_proc = None


# 反检测补丁：只改 Cap 明确会读的几个属性，尽量少动原型链。
# Cap 的 instrumentation 会遍历 21 个原生函数校验 "[native code]" 标记，
# 改写越多越容易被判 tampered，因此这里保持最小集。
STEALTH_PATCH = """
try {
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
} catch (e) {}
try {
  Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
} catch (e) {}
try {
  if (!window.chrome) window.chrome = {runtime: {}};
} catch (e) {}
"""

# CPU 核数伪装：Cap 按 navigator.hardwareConcurrency 开 worker，容器看到 4 核就
# 开 4 个 HashWX worker 并行抢 CPU，反而让 instrumentation 采集（有 20 秒硬超时）
# 抢不到算力而超时。报小一点核数可减少并行 worker，给采集脚本留出 CPU。
CPU_CORES_PATCH = """
try {
  Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 2});
  Object.defineProperty(navigator, 'deviceMemory', {get: () => 4});
} catch (e) {}
"""


class BrowserSession:
    """一次浏览器会话：持有 playwright/browser/context/page，统一负责清理。"""

    def __init__(self, playwright, browser, context, page, binary):
        self.playwright = playwright
        self.browser = browser
        self.context = context
        self.page = page
        self.binary = binary

    def close(self) -> None:
        """按 page → context → browser → playwright 顺序释放。"""
        for closer in (
            lambda: self.context.close(),
            lambda: self.browser.close(),
            lambda: self.playwright.stop(),
        ):
            try:
                closer()
            except Exception:
                pass


def init_browser(config: dict) -> BrowserSession:
    """启动有头 Chromium 并完成反检测注入。

    有头（headless=False）是刻意选择：Cap 会检测 headless 特征，无头浏览器
    即使 PoW 算对也会被 instrumentation 判为自动化。use_xvfb=False 仅供调试
    （无显示器时用 headless 能跑通页面流程，但过不了 Cap）。
    """
    binary = config.get("chrome_binary_path") or _detect_chrome_binary()
    if not binary:
        raise RuntimeError(
            "未找到可用的 Chromium。请执行 playwright install chromium，"
            "或通过 RAINYUN_CONFIG.chrome_binary_path 指定路径"
        )

    use_xvfb = config.get("use_xvfb", True)
    headless = not use_xvfb
    if use_xvfb:
        start_xvfb()

    major = _browser_major_version(binary)
    # UA 只在与真实浏览器版本一致时才伪造；探测失败就留空让浏览器用自己的，
    # 否则 UA 声称的版本与内核不符，本身就是指纹矛盾信号
    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        if major else None
    )

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(
            headless=headless,
            executable_path=binary,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,720",
                "--lang=zh-CN",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-sync",
                # 降渲染开销：容器里 Cap 的 20 秒采集窗口很紧，省下的算力留给它
                "--disable-software-rasterizer",
                "--disable-features=Translate,BackForwardCache,AcceptCHFrame",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=user_agent,
        )
        # init script 必须在任何页面加载前注册，否则 Cap 会先看到 webdriver=true
        context.add_init_script(script=STEALTH_PATCH)
        # 单独注入核数伪装，便于配置开关（关闭后恢复真实 hardwareConcurrency）
        if config.get("limit_cpu_cores", True):
            context.add_init_script(script=CPU_CORES_PATCH)

        # stealth.min.js 默认不注入：Cap 的 instrumentation 会校验 21 个原生函数
        # 是否为未篡改的原生实现（检查 "[native code]"），stealth 大量改写原型方法
        # 会触发该校验导致采集失败/超时。上面的 STEALTH_PATCH 已覆盖关键特征。
        if config.get("use_stealth_js", False):
            stealth_path = config.get("stealth_js_path", "./stealth.min.js")
            script_dir = os.path.dirname(os.path.abspath(__file__))
            stealth_abs = os.path.abspath(os.path.join(script_dir, stealth_path))
            if os.path.isfile(stealth_abs):
                context.add_init_script(path=stealth_abs)

        page = context.new_page()
        return BrowserSession(playwright, browser, context, page, binary)
    except Exception:
        try:
            playwright.stop()
        except Exception:
            pass
        raise


def human_like_click(page, locator, timeout: int = 15000) -> None:
    """带指针轨迹的点击，尽量贴近真人操作。

    Cap 会分析指针移动路径与点击方式，直接 locator.click() 的合成事件
    没有移动过程，容易被判为脚本。这里先滚动到可见区，再分步移动鼠标，
    最后落点点击。
    """
    locator.scroll_into_view_if_needed(timeout=timeout)
    page.wait_for_timeout(random.randint(120, 300))

    box = locator.bounding_box()
    if not box:
        # 拿不到尺寸说明元素不可见，退回常规点击让 Playwright 自己处理
        locator.click(timeout=timeout)
        return

    target_x = box["x"] + box["width"] * random.uniform(0.4, 0.6)
    target_y = box["y"] + box["height"] * random.uniform(0.4, 0.6)

    # 从附近位置分几段移入，制造可见轨迹
    start_x = max(0, target_x + random.randint(-140, 140))
    start_y = max(0, target_y + random.randint(-110, 110))
    page.mouse.move(start_x, start_y)
    page.wait_for_timeout(random.randint(80, 200))

    steps = random.randint(6, 12)
    page.mouse.move(target_x, target_y, steps=steps)
    page.wait_for_timeout(random.randint(120, 280))
    page.mouse.click(target_x, target_y)


def attach_console_logger(page, sink: list) -> None:
    """把浏览器 console 与 pageerror 收集到 sink，供失败时诊断。

    必须在 page.goto 之前调用，否则会丢掉页面加载早期的日志。
    """
    def on_console(msg):
        try:
            sink.append(f"[{msg.type}] {msg.text[:300]}")
        except Exception:
            pass

    def on_error(err):
        sink.append(f"[pageerror] {str(err)[:300]}")

    page.on("console", on_console)
    page.on("pageerror", on_error)


def attach_network_logger(page, sink: list) -> None:
    """记录 4xx/5xx 与失败请求，用于定位接口级拒绝。"""
    def on_response(resp):
        try:
            if resp.status >= 400:
                sink.append(f"{resp.status} {resp.request.method} {resp.url[:160]}")
        except Exception:
            pass

    def on_failed(req):
        try:
            sink.append(f"FAILED {req.method} {req.url[:160]} {req.failure}")
        except Exception:
            pass

    page.on("response", on_response)
    page.on("requestfailed", on_failed)
