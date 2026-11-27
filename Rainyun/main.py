import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import json
import hashlib
from dataclasses import dataclass
from typing import List

# UTF-8 强制重配：解决 Windows GBK / 部分容器 locale 导致的乱码与编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import ddddocr
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from config import CONFIG
from account_parser import parse_accounts, Account
from api_client import RainyunAPI
from server_manager import ServerManager

logger = logging.getLogger(__name__)


# notify.py 兼容加载：青龙面板运行时同目录存在 notify.py（含 /ql/data/scripts 兜底）；
# 本地干跑找不到则不报错，推送优雅跳过
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
    """通过青龙 notify.py 推送通知，带 3 次重试。返回是否成功。"""
    if not _qinglong_send:
        logger.info("ℹ️ 未找到 notify.py（仅在青龙环境内可用），跳过推送")
        logger.info("💡 提示: 如需推送通知，请在青龙面板配置通知渠道")
        return False
    max_attempts, retry_delay = 3, 3
    for attempt in range(1, max_attempts + 1):
        try:
            _qinglong_send(title, content)
            logger.info("📨 通知已通过 notify.py 推送")
            return True
        except Exception as e:
            logger.warning(f"⚠️ 推送第 {attempt}/{max_attempts} 次失败: {e}")
            if attempt < max_attempts:
                logger.info(f"   等待 {retry_delay} 秒后重试推送...")
                time.sleep(retry_delay)
    logger.warning("⚠️ notify.py 推送最终失败")
    return False


@dataclass
class AccountResult:
    """单个账号执行结果"""
    username: str
    login_success: bool = False
    sign_in_success: bool = False
    points_before: int = 0
    points_after: int = 0
    points_earned: int = 0
    auto_renew_enabled: bool = False
    renew_summary: str = ""
    error_msg: str = ""
    
    def is_success(self) -> bool:
        """是否成功"""
        return self.login_success and self.sign_in_success


@dataclass
class RuntimeContext:
    """运行时上下文"""
    driver: webdriver.Chrome
    wait: WebDriverWait
    det: ddddocr.DdddOcr
    temp_dir: str
    config: dict
    
    def temp_path(self, filename: str) -> str:
        """获取临时文件路径"""
        return os.path.join(self.temp_dir, filename)


def init_logger():
    """初始化日志"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logger.info("=" * 80)
    logger.info("雨云签到工具 by SerendipityR ~")
    logger.info("Github发布页: https://github.com/SerendipityR-2022/Rainyun-Qiandao")
    logger.info("-" * 80)
    logger.info("雨云签到工具容器版 by fatekey ~")
    logger.info("Github发布页: https://github.com/fatekey/Rainyun-Qiandao")
    logger.info("-" * 80)
    logger.info("                   项目为二次开发青龙脚本化运行")
    logger.info("                     本项目基于上述项目开发")
    logger.info("                本项目仅作为学习参考，请勿用于其他用途")
    logger.info("=" * 80)


# ---------------------------------------------------------------------------
# 页面 URL 与 Cookie 缓存常量
# ---------------------------------------------------------------------------
LOGIN_URL = "https://app.rainyun.com/auth/login"
EARN_URL = "https://app.rainyun.com/account/reward/earn"
HOME_URL = "https://app.rainyun.com/"   # 注入 Cookie 前需先落到该域，否则 add_cookie 报 invalid cookie domain
COOKIE_DIR_NAME = "cookies"   # Cookie 缓存目录（脚本同目录下，青龙数据卷持久化）

# 赚取积分页顶部「积分」卡片的数值
POINTS_XPATH = '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3'

# ---------------------------------------------------------------------------
# 每日签到：2026-11 雨云改版后的新版 UI
# ---------------------------------------------------------------------------
# 改版后「每日签到」是独立卡片 button.daily-check-in-task：
#   <button class="card-header daily-check-in-task">
#     <span class="h4">每日签到 <span class="badge">去签到/已完成</span></span>
#     <span class="mr-1 font-weight-bolder">积分或抽奖券</span>   ← 奖励说明，不是状态
#   </button>
# 旧脚本用 following-sibling::span[1] 取到的正是「积分或抽奖券」，因此误判失败。
# 点击卡片会弹出二选一弹窗（500 积分 / 抽奖券 × 1），提交走
# POST /user/reward/tasks {task_name:"每日签到", reward:"point"|"ticket"}，
# 人机验证已由腾讯 TCaptcha 换成自研 cap.rainyun.cn 的 PoW（浏览器自动完成）。
SIGN_TASK_BUTTON_XPATH = "//button[contains(@class,'daily-check-in-task')]"
SIGN_TASK_BADGE_XPATH = (
    "//button[contains(@class,'daily-check-in-task')]"
    "//span[contains(@class,'badge')]"
)
SIGN_REWARD_LIST_XPATH = "//div[contains(@class,'daily-check-in__rewards')]"
SIGN_REWARD_BUTTON_XPATH = "//button[contains(@class,'daily-check-in__reward')]"
SIGN_REWARD_POINT_XPATH = (
    SIGN_REWARD_BUTTON_XPATH
    + "[.//span[contains(@class,'daily-check-in__icon--point')]]"
)
SIGN_REWARD_TICKET_XPATH = (
    SIGN_REWARD_BUTTON_XPATH
    + "[.//span[contains(@class,'daily-check-in__icon--ticket')]]"
)
SIGN_REWARD_TITLE_XPATH = "//h4[contains(@class,'daily-check-in__title')]"
SIGN_REWARD_STATUS_XPATH = "//p[contains(@class,'daily-check-in__status')]"

# 旧版（改版前）定位方式，保留兼容，站点回滚时仍可签到
SIGN_ROW_XPATH = '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[2]/div/div/div/div[1]/div'

# 状态文案（页面 JS：Status=1 可领取 / 2 已完成 / 0 未达成）
SIGN_STATUS_CLAIM = "领取奖励"        # 旧版
SIGN_STATUS_NEW_CLAIM = "去签到"      # 新版
SIGN_STATUS_DONE = "已完成"           # 新旧版一致
SIGN_REWARD_HINT = "积分或抽奖券"      # 新版奖励说明，读到了也不能当状态

# 新版 CAP 人机验证（PoW）耗时较长，给足等待时间
CAP_SIGN_WAIT_SECONDS = 90
CAP_SIGN_MAX_RETRY = 3


# ---------------------------------------------------------------------------
# Chrome 启动探测与参数选择
# ---------------------------------------------------------------------------
# 背景：--headless=new 是 Chrome/Chromium 109+ 才支持的参数。青龙容器里 apt 装的
# Debian chromium 版本可能过老（如 90），传该参数会导致 chrome 进程启动即退出、
# DevToolsActivePort 文件写不出来（报 "session not created"）。因此这里自动探测
# 浏览器二进制与版本、按版本选 headless 参数，启动失败再回退到另一个参数。

# 候选浏览器二进制：优先 PATH 名，其次常见安装路径
CHROME_BINARY_NAMES = [
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
]
CHROME_BINARY_PATHS = [
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/opt/google/chrome/chrome",                                          # google-chrome .deb 默认位置
    "/opt/chrome-for-testing/chrome-linux64/chrome",                      # 手动安装位置
    "/ql/data/scripts/Rainyun/chrome-for-testing/chrome-linux64/chrome",  # 青龙数据卷持久化安装
]

# chromedriver 手动安装路径（自动探测的补充，覆盖 chrome-for-testing 场景）
CHROMEDRIVER_PATHS = [
    "/ql/data/scripts/Rainyun/chrome-for-testing/chromedriver-linux64/chromedriver",
    "/opt/chrome-for-testing/chromedriver-linux64/chromedriver",
]


def _detect_chrome_binary() -> str:
    """探测 Chrome/Chromium 可执行文件路径，找不到返回空字符串"""
    for name in CHROME_BINARY_NAMES:
        path = shutil.which(name)
        if path:
            return path
    for path in CHROME_BINARY_PATHS:
        if os.path.isfile(path):
            return path
    return ""


def _run_version_cmd(binary: str) -> str:
    """执行 <binary> --version 并提取版本号，失败返回空字符串"""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        # stdout 与 stderr 都取：Debian 老版 chromium 可能把版本打到 stderr
        output = (result.stdout or "") + (result.stderr or "")
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\.(\d+)", output)
        return match.group(0) if match else ""
    except Exception:
        return ""


def _pick_headless_flag(major: int) -> str:
    """按 Chrome 主版本选择 headless 参数（>=109 用新模式，否则用旧模式）"""
    if major >= 109:
        return "--headless=new"
    return "--headless"


def _build_chrome_options(binary: str, headless_flag: str, ua_major: int = 0) -> Options:
    """组装 Chrome 启动参数（容器必需配置 + 稳健性参数 + 反爬配置）"""
    ops = Options()
    # 容器必需配置
    ops.add_argument("--no-sandbox")
    ops.add_argument("--disable-dev-shm-usage")
    ops.add_argument("--disable-gpu")
    ops.add_argument(headless_flag)
    ops.add_argument("--window-size=1920,1080")

    # 稳健性参数：避免首次运行引导、后台联网、扩展等干扰无头启动
    ops.add_argument("--no-first-run")
    ops.add_argument("--no-default-browser-check")
    ops.add_argument("--disable-extensions")
    ops.add_argument("--disable-background-networking")
    ops.add_argument("--disable-sync")
    ops.add_argument("--remote-debugging-port=0")

    # UA 与探测到的浏览器主版本保持一致；版本未知时不覆盖，避免指纹矛盾
    if ua_major:
        ops.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ua_major}.0.0.0 Safari/537.36"
        )

    # 反爬配置
    ops.add_experimental_option("excludeSwitches", ["enable-automation"])
    ops.add_experimental_option('useAutomationExtension', False)
    ops.add_argument("--disable-blink-features=AutomationControlled")

    # 显式指定浏览器二进制，避免 ChromeDriver 找不到或找错浏览器
    if binary:
        ops.binary_location = binary
    return ops


def init_selenium(config: dict):
    """初始化 Selenium 驱动（青龙面板专用，自动探测浏览器并兼容老版本 headless）"""
    logger.info("🔧 开始初始化 Selenium WebDriver")

    # 自动探测 chromedriver 路径：
    # 1. 显式配置优先：RAINYUN_CONFIG 的 chrome_driver_path → 环境变量 CHROMEDRIVER_PATH
    #    （显式配置必须存在，否则报错，避免用户写错路径被静默绕过）
    # 2. 未配置时自动探测：PATH 中查找（本地调试）→ 青龙固定路径（容器内 apt 安装位置）
    #    → 手动安装路径（chrome-for-testing 持久化）
    # 3. 都找不到则交给 Selenium Manager 自动管理（selenium 4.6+ 自动下载匹配版本）
    driver_path = config.get("chrome_driver_path") or os.getenv("CHROMEDRIVER_PATH")
    if driver_path:
        if not os.path.exists(driver_path):
            logger.error(f"❌ 配置的 chromedriver 不存在: {driver_path}")
            logger.error("请检查 RAINYUN_CONFIG 的 chrome_driver_path 或 CHROMEDRIVER_PATH")
            raise FileNotFoundError(f"chromedriver not found at {driver_path}")
        service = Service(executable_path=driver_path)
        logger.info(f"   - ChromeDriver 路径: {driver_path}")
    else:
        auto_path = shutil.which("chromedriver") or "/usr/bin/chromedriver"
        if not os.path.exists(auto_path):
            # 补充手动安装路径，如 chrome-for-testing 持久化安装
            for extra in CHROMEDRIVER_PATHS:
                if os.path.exists(extra):
                    auto_path = extra
                    break
        if os.path.exists(auto_path):
            service = Service(executable_path=auto_path)
            logger.info(f"   - ChromeDriver 路径: {auto_path}")
        else:
            service = Service()  # 交给 Selenium Manager 自动匹配下载
            logger.info("   - ChromeDriver 路径: 自动管理（Selenium Manager）")

    # 探测浏览器二进制与版本，按版本选择 headless 参数
    binary = _detect_chrome_binary()
    version = _run_version_cmd(binary) if binary else ""
    major = int(version.split(".")[0]) if version else 0
    if binary:
        logger.info(f"   - Chrome 二进制: {binary}")
        logger.info(f"   - Chrome 版本: {version or '解析失败（按老版本保守处理）'}")
    else:
        logger.warning("   - 未找到 Chrome/Chromium，交由 ChromeDriver 默认查找")

    primary_flag = _pick_headless_flag(major)
    fallback_flag = "--headless" if primary_flag == "--headless=new" else "--headless=new"

    # 双 headless 参数回退：避免单一参数在特定版本上启动即崩溃
    last_error = None
    for flag in (primary_flag, fallback_flag):
        try:
            logger.info(f"   - 尝试 headless 参数: {flag}")
            driver = webdriver.Chrome(
                service=service, options=_build_chrome_options(binary, flag, major)
            )
            driver.delete_all_cookies()
            logger.info("✅ Selenium WebDriver 初始化成功")
            return driver
        except Exception as e:
            last_error = e
            logger.warning(f"   - {flag} 启动失败: {e}")
            try:
                service.stop()  # 清理可能残留的 chromedriver 进程，避免占端口
            except Exception:
                pass
            time.sleep(1)

    # 全部失败：输出诊断信息，便于定位环境问题
    logger.error("❌ Selenium 初始化失败（已尝试两种 headless 参数）")
    logger.error(f"   最终错误: {last_error}")
    logger.error(f"   Chrome 二进制: {binary or '未找到'}")
    logger.error(f"   Chrome 版本: {version or '未知'}")
    cd_bin = driver_path or shutil.which("chromedriver") or "/usr/bin/chromedriver"
    logger.error(f"   ChromeDriver 路径: {cd_bin}")
    logger.error(f"   ChromeDriver 版本: {_run_version_cmd(cd_bin) or '未知'}")
    logger.error("   排查提示:")
    logger.error("     1) 确认 chromium/google-chrome 版本 >= 109（容器内执行: chromium --version / google-chrome --version）")
    logger.error("     2) 确认 chromedriver 与 chrome 主版本一致（chromedriver --version）")
    logger.error("     3) 检查缺库: ldd $(which chromium) | grep 'not found'")
    logger.error("     4) 检查空间: df -h /dev/shm /tmp")
    logger.error("     5) 清理僵尸进程: pkill -9 -f chromedriver; pkill -9 -f 'chrome.*remote-debugging'")
    raise last_error


def inject_stealth_js(driver, config: dict):
    """注入反检测脚本（支持相对路径）"""
    # 获取主脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 从配置读取相对路径（默认与 config.py 的 DEFAULT_CONFIG 一致：主脚本同目录）
    relative_path = config.get("stealth_js_path", "./stealth.min.js")
    
    # 拼接完整路径
    script_path = os.path.join(script_dir, relative_path)
    script_path = os.path.abspath(script_path)  # 转为绝对路径
    
    logger.info(f"🔧 检查反检测脚本: {script_path}")
    
    if not os.path.exists(script_path):
        logger.error(f"❌ 未找到 stealth.min.js！")
        logger.error(f"预期路径: {script_path}")
        logger.error(f"主脚本目录: {script_dir}")
        logger.error(f"配置的相对路径: {relative_path}")
        logger.error("请检查以下几点：")
        logger.error("  1. 文件是否已上传")
        logger.error("  2. 文件名是否正确（区分大小写）")
        logger.error("  3. 配置的相对路径是否正确")
        logger.error("下载地址: https://raw.githubusercontent.com/berstend/puppeteer-extra/master/packages/puppeteer-extra-plugin-stealth/evasions/stealth.min.js")
        sys.exit(1)
    
    with open(script_path, "r", encoding="utf-8") as f:
        js = f.read()
    
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": js})
    logger.info("✅ 已注入 stealth.min.js 反检测脚本")


# ---------------------------------------------------------------------------
# 会话 Cookie 缓存（命中则跳过登录 + 验证码，失效自动回退完整登录）
# ---------------------------------------------------------------------------

def cookie_cache_path(username: str) -> str:
    """会话 Cookie 缓存文件路径：脚本同目录 cookies/ 下，文件名用账号 hash 防敏感信息泄漏。"""
    digest = hashlib.md5(username.encode("utf-8")).hexdigest()[:12]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), COOKIE_DIR_NAME, f"rainyun_{digest}.json")


def save_cookies(ctx: RuntimeContext, username: str) -> bool:
    """登录成功后把浏览器持久会话 Cookie 落盘，供下次直通签到跳过登录与验证码。"""
    try:
        path = cookie_cache_path(username)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cookies = []
        for c in ctx.driver.get_cookies():
            # Selenium 读取用 expiry、注入用 expires；expiry 为 None 的是内存级 session cookie，
            # 浏览器一关即失效，缓存无意义且 add_cookie 会拒收，直接丢弃
            if c.get("expiry") is None:
                continue
            item = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "expires": c["expiry"],   # 秒级时间戳
            }
            if c.get("secure"):
                item["secure"] = True
            if c.get("sameSite"):
                item["sameSite"] = c["sameSite"]
            cookies.append(item)
        if not cookies:
            logger.warning("⚠️ 未发现可持久化的 Cookie（登录态为 session 级），跳过落盘")
            return False
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cookies": cookies}, f, ensure_ascii=False, indent=2)
        logger.info(f"🍪 已缓存 {len(cookies)} 条持久 Cookie -> {os.path.basename(path)}")
        return True
    except Exception as e:
        logger.warning(f"⚠️ 保存 Cookie 缓存失败: {e}")
        return False


def load_cookies(ctx: RuntimeContext, username: str) -> bool:
    """把缓存的 Cookie 逐条注入当前浏览器会话。返回是否成功。"""
    path = cookie_cache_path(username)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        injected = 0
        for c in data.get("cookies", []):
            # 已过期的 cookie 跳过（不删文件，可能只是部分过期）
            if c.get("expires") and c["expires"] < now:
                continue
            # get_cookies() 读出的键是 expires，add_cookie() 要求 expiry；
            # 不转换的话过期时间会被驱动忽略，注入成会话级 Cookie
            cookie = {k: v for k, v in c.items() if k != "expires"}
            if c.get("expires"):
                cookie["expiry"] = c["expires"]
            ctx.driver.add_cookie(cookie)
            injected += 1
        if injected == 0:
            return False
        logger.info(f"🍪 已注入 {injected} 条缓存 Cookie")
        return True
    except Exception as e:
        logger.warning(f"⚠️ 读取/注入 Cookie 缓存失败: {e}")
        return False


def _drop_cookie_cache(username: str) -> None:
    """删除失效的 Cookie 缓存文件，避免反复复用坏 Cookie。"""
    try:
        path = cookie_cache_path(username)
        if os.path.exists(path):
            os.remove(path)
            logger.info("🗑️ 已删除失效 Cookie 缓存")
    except Exception as e:
        logger.warning(f"⚠️ 删除 Cookie 缓存失败: {e}")


def try_restore_session(ctx: RuntimeContext, account: Account) -> bool:
    """尝试用缓存的 Cookie 恢复登录态。命中且有效返回 True；失效/异常删缓存返回 False。"""
    if not os.path.exists(cookie_cache_path(account.username)):
        return False
    try:
        # add_cookie 要求当前页面域与 Cookie 域匹配，about:blank 上注入会报
        # "invalid cookie domain"，因此先落到雨云域再注入
        ctx.driver.get(HOME_URL)
        if not load_cookies(ctx, account.username):
            # 全部过期或注入失败：清掉坏缓存，避免每次运行都白试一遍
            _drop_cookie_cache(account.username)
            return False
        # 雨云为 SPA：登录失效时访问内部页会 302 回 /auth/login
        ctx.driver.get(EARN_URL)
        time.sleep(5)
        url = ctx.driver.current_url
        logger.info(f"   缓存 Cookie 探测 URL: {url}")
        if "login" in url:
            logger.warning("🍪 缓存 Cookie 已失效，回退完整登录")
            _drop_cookie_cache(account.username)
            return False
        logger.info("🍪 缓存 Cookie 有效，直接复用登录态")
        return True
    except Exception as e:
        logger.warning(f"⚠️ 缓存 Cookie 校验异常({e})，回退完整登录")
        _drop_cookie_cache(account.username)
        return False


def do_login(ctx: RuntimeContext, username: str, password: str) -> bool:
    """执行登录"""
    try:
        logger.info("=" * 60)
        logger.info("⏳ 发起登录请求")
        logger.info("🌐 访问雨云登录页: https://app.rainyun.com/auth/login")
        ctx.driver.get(LOGIN_URL)
        
        logger.info(f"   当前页面标题: {ctx.driver.title}")
        logger.info(f"   当前页面URL: {ctx.driver.current_url}")
        
        logger.info("⏳ 等待登录表单元素加载...")
        username_elem = ctx.wait.until(EC.visibility_of_element_located((By.NAME, "login-field")))
        password_elem = ctx.wait.until(EC.visibility_of_element_located((By.NAME, "login-password")))
        login_btn = ctx.wait.until(EC.element_to_be_clickable((By.XPATH, "//button[@type='submit' and contains(., '登')]")))
        
        logger.info("✅ 登录表单元素加载完成")
        logger.info("📝 输入账号密码")
        username_elem.send_keys(username)
        password_elem.send_keys(password)
        
        logger.info("🖱️  点击登录按钮")
        login_btn.click()
        logger.info("⏳ 正在登录中，耗时较长请稍等……")
        time.sleep(3)
        
        # 处理登录验证码
        try:
            logger.info("🔍 检查是否触发登录验证码...")
            ctx.wait.until(EC.visibility_of_element_located((By.ID, "tcaptcha_iframe_dy")))
            logger.warning("⚠️  触发登录验证码！")
            ctx.driver.switch_to.frame("tcaptcha_iframe_dy")
            
            from captcha import process_captcha
            if not process_captcha(ctx, ctx.config):
                logger.error("❌ 登录验证码处理失败")
                return False
                
            logger.info("✅ 登录验证码处理成功")
        except TimeoutException:
            logger.info("✅ 未触发登录验证码")
        
        ctx.driver.switch_to.default_content()
        logger.info("⏳ 等待页面跳转...")
        time.sleep(5)
        
        # 验证登录状态
        current_url = ctx.driver.current_url
        logger.info(f"   跳转后URL: {current_url}")
        logger.info(f"   当前页面标题: {ctx.driver.title}")
        
        if "dashboard" not in current_url:
            logger.error(f"❌ 登录失败！未跳转到控制台页面")
            logger.error(f"   当前URL: {current_url}")
            return False
        
        # 获取用户名
        try:
            user_elem = ctx.driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/nav/div[1]/ul/div[6]/li/a/div/div/p')
            user_name = user_elem.text.strip()
            logger.info(f"✅ 账号登录成功: {user_name}")
        except Exception:
            logger.info("✅ 登录成功！")
        
        return True
        
    except TimeoutException:
        logger.error("❌ 页面加载超时！")
        logger.error("   可能原因：")
        logger.error("   1. 网络连接问题")
        logger.error("   2. 页面加载时间过长，请尝试增加 timeout 配置")
        logger.error("   3. 雨云服务器响应慢")
        return False
    except Exception as e:
        logger.error(f"❌ 登录异常: {e}", exc_info=True)
        return False


def read_points(ctx: RuntimeContext, wait_seconds: float = 5) -> int:
    """读取赚取积分页顶部的当前积分；读取失败返回 0（0 同时表示“未知”）。

    积分数值是页面异步渲染的，元素出现时文本可能还是空的，因此在
    wait_seconds 内轮询等待，避免把「尚未加载」误判成「读取失败」。
    """
    deadline = time.time() + wait_seconds
    last_error = None
    while True:
        try:
            digits = re.findall(r"\d+", ctx.driver.find_element(By.XPATH, POINTS_XPATH).text)
            if digits:
                return int("".join(digits))
        except Exception as e:
            last_error = e
        if time.time() >= deadline:
            if last_error is not None:
                logger.warning(f"⚠️  积分读取失败: {last_error}")
            return 0
        time.sleep(0.5)


def find_optional(ctx: RuntimeContext, xpath: str):
    """按 XPath 找元素，找不到返回 None（新版 DOM 节点不稳定，不能直接抛异常中断流程）。"""
    try:
        return ctx.driver.find_element(By.XPATH, xpath)
    except Exception:
        return None


def find_sign_status_element(ctx: RuntimeContext):
    """旧版：定位「每日签到」右侧的状态元素（其下含领取奖励链接）。"""
    row = ctx.driver.find_element(By.XPATH, SIGN_ROW_XPATH)
    label = row.find_element(By.XPATH, './/span[contains(text(),"每日签到")]')
    return label.find_element(By.XPATH, './following-sibling::span[1]')


def find_sign_state(ctx: RuntimeContext):
    """识别当前签到状态与页面版本。

    Returns:
        (state, ui, text)  state: "done" | "claim" | "unknown"
                           ui: "new" | "legacy" | ""
    新版状态在卡片内的徽标里；紧跟标签的「积分或抽奖券」只是奖励说明，必须忽略。
    """
    # 新版：徽标文本最精确
    badge = find_optional(ctx, SIGN_TASK_BADGE_XPATH)
    if badge is not None:
        text = (badge.text or "").strip()
        if SIGN_STATUS_DONE in text:
            return "done", "new", text
        if SIGN_STATUS_NEW_CLAIM in text:
            return "claim", "new", text

    # 新版兜底：徽标未单独渲染时读整张卡片
    button = find_optional(ctx, SIGN_TASK_BUTTON_XPATH)
    if button is not None:
        text = (button.text or "").strip()
        if SIGN_STATUS_DONE in text:
            return "done", "new", text
        if SIGN_STATUS_NEW_CLAIM in text:
            return "claim", "new", text

    # 旧版：右侧状态 span
    try:
        text = find_sign_status_element(ctx).text.strip()
    except Exception:
        text = ""
    if text and text != SIGN_REWARD_HINT:
        if SIGN_STATUS_DONE in text:
            return "done", "legacy", text
        if SIGN_STATUS_CLAIM in text:
            return "claim", "legacy", text
    return "unknown", "", text


def wait_sign_state(ctx: RuntimeContext, timeout: float = 10):
    """轮询等待签到状态渲染完成，返回 (state, ui, text)。"""
    deadline = time.time() + timeout
    last = ("unknown", "", "")
    while True:
        last = find_sign_state(ctx)
        if last[0] != "unknown":
            return last
        if time.time() >= deadline:
            return last
        time.sleep(0.5)


def wait_sign_status_until(ctx: RuntimeContext, target: str, timeout: float = 20) -> str:
    """旧版：轮询等待签到状态变成目标文案；超时返回最后读到的值。"""
    deadline = time.time() + timeout
    last_text = ""
    while True:
        try:
            text = find_sign_status_element(ctx).text.strip()
            if text:
                last_text = text
                if text == target:
                    return text
        except Exception:
            pass
        if time.time() >= deadline:
            return last_text
        time.sleep(0.5)


def wait_new_sign_result(ctx: RuntimeContext, timeout: float = CAP_SIGN_WAIT_SECONDS):
    """等待新版二选一弹窗的签到结果。

    CAP 人机验证由页面在浏览器里自动完成（PoW + WASM），无需人工/AI 点选，
    但耗时比腾讯验证码长，所以这里统一轮询弹窗文案和卡片徽标。

    Returns:
        (ok, detail)
    """
    deadline = time.time() + timeout
    last = ""
    tick = 0
    while time.time() < deadline:
        # 成功：弹窗标题「今日已签到」或状态「已领取 500 积分，明天再来」
        for xpath in (SIGN_REWARD_TITLE_XPATH, SIGN_REWARD_STATUS_XPATH):
            el = find_optional(ctx, xpath)
            if el is None:
                continue
            text = (el.text or "").strip()
            if text:
                last = text
            if "今日已签到" in text or "已领取" in text:
                return True, text

        # 成功：后端状态已刷新，卡片徽标变成「已完成」
        badge = find_optional(ctx, SIGN_TASK_BADGE_XPATH)
        if badge is not None and SIGN_STATUS_DONE in (badge.text or ""):
            return True, "任务状态：已完成"

        # 明确的失败提示（CAP 加载/验证失败），交给上层刷新重试；
        # 每 3 秒检测一次即可，避免频繁序列化整页 DOM
        tick += 1
        if tick % 6 == 0:
            try:
                body_text = ctx.driver.find_element(By.TAG_NAME, "body").text or ""
            except Exception:
                body_text = ""
            for err in ("人机验证失败", "验证组件加载失败", "当前浏览器无法完成人机验证"):
                if err in body_text:
                    return False, err

        time.sleep(0.5)
    return False, last or "等待签到结果超时"


def do_sign_in_new_ui(ctx: RuntimeContext, reward_type: str) -> bool:
    """新版签到：打开卡片弹窗 → 选择奖励（积分/抽奖券）→ 等待 CAP 验证与提交结果。"""
    label = "500 积分" if reward_type == "point" else "抽奖券 × 1"
    logger.info(f"🎁 签到奖励选择: {label}（sign_reward_type={reward_type}）")

    button = WebDriverWait(ctx.driver, 10).until(
        EC.element_to_be_clickable((By.XPATH, SIGN_TASK_BUTTON_XPATH))
    )
    logger.info("🎯 打开每日签到弹窗")
    button.click()

    # 已经领取过：弹窗直接显示「今日已签到」，没有奖励按钮
    time.sleep(1)
    title = find_optional(ctx, SIGN_REWARD_TITLE_XPATH)
    if title is not None and "今日已签到" in (title.text or ""):
        logger.info("📌 今日已签到，无需重复签到")
        return True

    try:
        WebDriverWait(ctx.driver, 15).until(
            EC.presence_of_element_located((By.XPATH, SIGN_REWARD_LIST_XPATH))
        )
    except TimeoutException:
        logger.error("❌ 每日签到弹窗未出现（任务可能未达成条件或页面结构再次变化）")
        return False

    option_xpath = SIGN_REWARD_TICKET_XPATH if reward_type == "ticket" else SIGN_REWARD_POINT_XPATH
    for attempt in range(1, CAP_SIGN_MAX_RETRY + 1):
        if attempt > 1:
            # CAP 失败会留下错误 toast，直接重点会被残留提示干扰；
            # 刷新页面还能拿到服务端最新任务状态（可能上一次其实已成功）。
            logger.info("🔄 刷新签到页后重试...")
            ctx.driver.get(EARN_URL)
            time.sleep(2)
            state, _, _ = wait_sign_state(ctx, 10)
            if state == "done":
                logger.info("✅ 任务状态已变为「已完成」，判定签到成功")
                return True
            WebDriverWait(ctx.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, SIGN_TASK_BUTTON_XPATH))
            ).click()
            WebDriverWait(ctx.driver, 15).until(
                EC.presence_of_element_located((By.XPATH, SIGN_REWARD_LIST_XPATH))
            )

        option = find_optional(ctx, option_xpath)
        if option is None:
            # 二选一区域消失通常意味着已经领取成功
            ok, detail = wait_new_sign_result(ctx, timeout=5)
            if ok:
                logger.info(f"✅ 签到奖励领取成功（{detail}）")
                return True
            logger.error("❌ 未找到奖励选项按钮")
            return False
        if not option.is_enabled():
            logger.warning("⚠️ 奖励选项不可点击（任务状态非「可领取」），等待状态刷新...")
            time.sleep(2)
            continue

        logger.info(f"🎯 第 {attempt}/{CAP_SIGN_MAX_RETRY} 次领取: {label}")
        option.click()
        # CAP 挑战需要先加载 WASM，给页面一点启动时间再开始判定
        time.sleep(2)
        ok, detail = wait_new_sign_result(ctx)
        if ok:
            logger.info(f"✅ 签到奖励领取成功（{detail}）")
            return True
        logger.warning(f"⚠️ 第 {attempt} 次未确认签到结果: {detail}")
        if attempt < CAP_SIGN_MAX_RETRY:
            time.sleep(3)

    logger.error("❌ 签到奖励领取失败（已重试 %d 次）", CAP_SIGN_MAX_RETRY)
    return False


def do_sign_in_legacy_ui(ctx: RuntimeContext) -> bool:
    """旧版签到：点击「领取奖励」链接 → 腾讯点选验证码 → 等待状态变为「已完成」。"""
    earn_btn = find_sign_status_element(ctx).find_element(By.XPATH, './a')
    logger.info("🎯 开始领取签到奖励")
    earn_btn.click()

    time.sleep(2)
    from captcha import process_captcha
    try:
        WebDriverWait(ctx.driver, 8).until(
            EC.visibility_of_element_located((By.ID, "tcaptcha_iframe_dy"))
        )
        ctx.driver.switch_to.frame("tcaptcha_iframe_dy")
        logger.info("⚠️  触发签到验证码")
        if not process_captcha(ctx, ctx.config):
            logger.error("❌ 签到验证码处理失败")
            return False
        ctx.driver.switch_to.default_content()
    except TimeoutException:
        logger.info("✅ 未触发签到验证码，继续检查签到结果")

    logger.info("⏳ 等待签到结果...")
    final_status = wait_sign_status_until(ctx, SIGN_STATUS_DONE, 20)
    logger.info(f"📌 签到后状态: {final_status or '未读取到'}")
    if final_status == SIGN_STATUS_DONE:
        logger.info("✅ 签到奖励领取成功")
        return True
    logger.error("❌ 未能确认签到结果，本次按失败处理")
    return False


def do_sign_in(ctx: RuntimeContext, result: AccountResult) -> bool:
    """执行签到，并把页面积分回填到 result（未配置 API Key 时的积分来源）。"""
    try:
        logger.info("=" * 60)
        logger.info(f"🌐 访问赚取积分页: {EARN_URL}")
        ctx.driver.get(EARN_URL)
        ctx.driver.implicitly_wait(5)

        logger.info(f"   当前页面URL: {ctx.driver.current_url}")
        logger.info(f"   当前页面标题: {ctx.driver.title}")

        # 签到前积分：API 未提供（未配置 Key 或取数失败）时用页面值兜底，
        # 通知里才能算出「本次获得」
        if result.points_before == 0:
            page_points = read_points(ctx)
            if page_points > 0:
                result.points_before = page_points
                logger.info(
                    f"💰 签到前积分: {page_points} "
                    f"（约 {page_points / ctx.config['points_to_cny_rate']:.2f} 元）"
                )

        # 查找签到入口（自动识别新版二选一 / 旧版领取奖励）
        logger.info("🔍 查找每日签到入口...")
        try:
            state, ui, status_text = wait_sign_state(ctx, 10)
            ui_tag = {"new": "（新版二选一）", "legacy": "（旧版）"}.get(ui, "")
            logger.info(f"📌 签到状态: {status_text or '未读取到'}{ui_tag}")

            if state == "unknown":
                logger.error(
                    f"❌ 未能识别签到状态（页面可能未加载完成，读到: {status_text or '空'}）"
                )
                return False
            if state == "done":
                logger.info("📌 今日已签到，无需重复签到")
                return True

            if ui == "new":
                reward_type = str(ctx.config.get("sign_reward_type", "point")).strip().lower()
                if reward_type not in ("point", "ticket"):
                    logger.warning(f"⚠️  sign_reward_type={reward_type!r} 无效，已回退为 point")
                    reward_type = "point"
                signed = do_sign_in_new_ui(ctx, reward_type)
            else:
                signed = do_sign_in_legacy_ui(ctx)

            if not signed:
                # 兜底：状态已刷新或积分已增加也算成功，避免「页面提示没抓到」误报失败
                new_state, _, new_text = wait_sign_state(ctx, 5)
                if new_state == "done":
                    logger.info(f"✅ 任务状态已变为「{new_text}」，判定签到成功")
                else:
                    current_points = read_points(ctx, wait_seconds=3)
                    if result.points_before > 0 and current_points > result.points_before:
                        logger.info(
                            f"✅ 积分已增加（{result.points_before} → {current_points}），判定签到成功"
                        )
                    else:
                        return False

            return True

        except (TimeoutException, NoSuchElementException):
            logger.error("❌ 未找到签到按钮/奖励选项")
            return False

    except Exception as e:
        logger.error(f"❌ 签到异常: {e}", exc_info=True)
        return False

    finally:
        # 验证码失败时可能仍停留在 iframe 内，先切回主文档再读积分
        try:
            ctx.driver.switch_to.default_content()
        except Exception:
            pass
        # 无论签到成功还是中途失败，只要登录态还在就刷新一次积分，
        # 保证通知里能看到账号当前余额（API Key 有值时后续以 API 值为准）
        page_points = read_points(ctx)
        if page_points > 0 and result.points_after == 0:
            result.points_after = page_points


def execute_auto_renew(account: Account, config: dict) -> str:
    """
    执行自动续费
    
    Returns:
        续费结果摘要
    """
    logger.info("=" * 60)
    logger.info("🔄 开始执行自动续费检查")
    try:
        api = RainyunAPI(account.api_key, config)
        manager = ServerManager(api, config)
        
        result = manager.check_and_renew()
        report = manager.generate_report(result)
        
        logger.info("\n" + report)
        
        # 生成简短摘要
        summary = f"续费: {result['renewed']}台成功, {result['skipped']}台跳过, {result['failed']}台失败"
        return summary
        
    except Exception as e:
        logger.error(f"❌ 自动续费失败: {e}", exc_info=True)
        return f"续费失败: {str(e)}"


def sign_in_rainyun(account: Account, config: dict) -> AccountResult:
    """
    单账号签到流程
    
    Returns:
        账号执行结果
    """
    result = AccountResult(username=account.username)
    driver = None
    temp_dir = None
    
    try:
        logger.info("\n" + "=" * 80)
        logger.info(f"开始处理账号: {account.username}")
        logger.info("=" * 80)
        
        # 随机延时
        delay_min = random.randint(0, config["max_delay"])
        delay_sec = random.randint(0, 59)
        logger.info(f"⏳ 随机延时 {delay_min} 分钟 {delay_sec} 秒")
        time.sleep(delay_min * 60 + delay_sec)
        
        # 初始化组件（只加载检测模型；碎片有效性已改为图像质量检查，不再需要 OCR 模型）
        logger.info("🔧 初始化 ddddocr 检测库")
        det = ddddocr.DdddOcr(det=True, show_ad=False)
        logger.info("✅ ddddocr 初始化成功")
        
        driver = init_selenium(config)
        inject_stealth_js(driver, config)
        wait = WebDriverWait(driver, config["timeout"])
        
        # 创建临时目录
        temp_dir = tempfile.mkdtemp(prefix="rainyun-")
        logger.info(f"📁 临时目录: {temp_dir}")
        
        # 构建上下文
        ctx = RuntimeContext(
            driver=driver,
            wait=wait,
            det=det,
            temp_dir=temp_dir,
            config=config
        )
        
        # 记录签到前积分
        if account.api_key:
            try:
                logger.info("🔍 正在获取签到前积分...")
                api = RainyunAPI(account.api_key, config)
                result.points_before = api.get_user_points()
                logger.info(f"💰 签到前积分: {result.points_before} （约 {result.points_before / config['points_to_cny_rate']:.2f} 元）")
            except Exception as e:
                logger.warning(f"⚠️  获取初始积分失败: {e}")
        
        # ★ 执行登录：优先复用缓存 Cookie（命中则跳过登录与验证码），否则完整登录
        cookie_ok = False
        if config.get("cookie_cache", True):
            logger.info("🔑 尝试复用 Cookie 缓存...")
            cookie_ok = try_restore_session(ctx, account)

        if cookie_ok:
            result.login_success = True
        else:
            result.login_success = do_login(ctx, account.username, account.password)
            if result.login_success:
                save_cookies(ctx, account.username)
        if not result.login_success:
            result.error_msg = "登录失败"
            logger.error("❌ 登录失败，跳过该账号")
            return result
        
        # 执行签到
        result.sign_in_success = do_sign_in(ctx, result)
        if not result.sign_in_success:
            result.error_msg = "签到失败"
            logger.error("❌ 签到失败")
            if result.points_after > 0:
                logger.info(
                    f"💰 当前积分: {result.points_after} "
                    f"（约 {result.points_after / config['points_to_cny_rate']:.2f} 元）"
                )
            return result

        # 记录签到后积分（API Key 存在时以 API 值为准，覆盖页面兜底值）
        if account.api_key:
            try:
                logger.info("🔍 正在获取签到后积分...")
                api = RainyunAPI(account.api_key, config)
                result.points_after = api.get_user_points()
            except Exception as e:
                logger.warning(f"⚠️  获取最终积分失败: {e}")

        # 统一计算本次获得：积分可能来自 API 或页面，按同一口径汇总
        if result.points_before > 0 and result.points_after > 0:
            result.points_earned = result.points_after - result.points_before
            logger.info(f"💰 当前积分: {result.points_after} (本次获得 {result.points_earned} 分)")
            logger.info(f"💵 约合人民币: {result.points_after / config['points_to_cny_rate']:.2f} 元")
        elif result.points_after > 0:
            logger.info(
                f"💰 当前积分: {result.points_after} "
                f"（约 {result.points_after / config['points_to_cny_rate']:.2f} 元）"
            )
        
        # 执行自动续费（如果启用）
        result.auto_renew_enabled = account.auto_renew
        if account.auto_renew and account.api_key:
            result.renew_summary = execute_auto_renew(account, config)
        elif account.auto_renew and not account.api_key:
            result.renew_summary = "未配置API Key，跳过续费"
            logger.warning("⚠️  该账号已启用自动续费但未配置 API Key，跳过续费")
        
        logger.info(f"✅ 账号 {account.username} 处理完成")
        return result
        
    except Exception as e:
        result.error_msg = f"异常: {str(e)}"
        logger.error(f"❌ 账号处理异常: {e}", exc_info=True)
        return result
        
    finally:
        # 清理资源
        if driver:
            try:
                driver.quit()
                logger.info(f"🔒 浏览器已关闭")
            except Exception as e:
                logger.warning(f"⚠️  关闭浏览器失败: {e}")
        
        if temp_dir:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"🗑️  临时文件已清理")
            except Exception as e:
                logger.warning(f"⚠️  清理临时文件失败: {e}")
        
        logger.info("=" * 80 + "\n")


def generate_summary_report(results: List[AccountResult], config: dict) -> str:
    """
    生成汇总报告
    
    Args:
        results: 所有账号的执行结果
        config: 配置字典
        
    Returns:
        汇总报告文本
    """
    rate = config["points_to_cny_rate"]
    lines = []
    lines.append("=" * 60)
    lines.append("📊 雨云签到任务执行报告")
    lines.append("=" * 60)
    
    # 统计信息
    total = len(results)
    success = sum(1 for r in results if r.is_success())
    failed = total - success
    
    lines.append(f"\n📈 总体统计:")
    lines.append(f"  总账号数: {total}")
    lines.append(f"  ✅ 成功: {success}")
    lines.append(f"  ❌ 失败: {failed}")
    
    # 积分统计：只累加成功读取到的值（未配置 API Key 时来自页面抓取）
    total_points_before = sum(r.points_before for r in results)
    total_points_after = sum(r.points_after for r in results)
    total_earned = sum(r.points_earned for r in results)
    
    if total_points_after > 0:
        lines.append(f"\n💰 积分统计:")
        if total_points_before > 0:
            lines.append(f"  签到前总积分: {total_points_before}")
        lines.append(f"  签到后总积分: {total_points_after}")
        if total_points_before > 0:
            lines.append(f"  本次获得: {total_earned} 分")
        lines.append(f"  约合人民币: {total_points_after / rate:.2f} 元")
    
    # 各账号详情
    lines.append(f"\n📋 各账号详情:")
    lines.append("-" * 60)
    
    for idx, result in enumerate(results, 1):
        lines.append(f"\n【账号 {idx}】 {result.username}")
        
        if result.is_success():
            lines.append(f"  状态: ✅ 成功")
        else:
            lines.append(f"  状态: ❌ 失败")
            lines.append(f"  原因: {result.error_msg}")
        
        # 积分行：前后都有展示变化，只有当前值展示余额，登录成功但读不到则明确提示
        if result.points_after > 0:
            if result.points_before > 0:
                lines.append(
                    f"  积分: {result.points_before} → {result.points_after} "
                    f"({result.points_earned:+d})，约 {result.points_after / rate:.2f} 元"
                )
            else:
                lines.append(f"  当前积分: {result.points_after}，约 {result.points_after / rate:.2f} 元")
        elif result.login_success:
            lines.append("  积分: 获取失败")
        
        if result.is_success():
            if result.auto_renew_enabled:
                lines.append(f"  自动续费: ✅ 已启用")
                if result.renew_summary:
                    lines.append(f"    {result.renew_summary}")
            else:
                lines.append(f"  自动续费: ⏭️  未启用")
    
    lines.append("\n" + "=" * 60)
    lines.append(f"📅 执行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    
    return "\n".join(lines)


def send_notification(title: str, content: str):
    """
    发送通知（对接青龙面板 notify.py）

    Args:
        title: 通知标题
        content: 通知内容
    """
    send_notify(title, content)


def main():
    """主函数"""
    # 记录开始时间
    start_time = time.time()
    
    init_logger()
    
    # 加载配置
    config = CONFIG.config
    
    # 解析账号
    accounts = parse_accounts()
    
    # 存储所有账号的执行结果
    all_results: List[AccountResult] = []
    
    # 依次处理每个账号
    for idx, account in enumerate(accounts, 1):
        logger.info(f"\n{'#'*80}")
        logger.info(f"第 {idx}/{len(accounts)} 个账号")
        logger.info(f"{'#'*80}")
        
        try:
            result = sign_in_rainyun(account, config)
            all_results.append(result)
        except Exception as e:
            logger.error(f"账号 {account.username} 处理失败: {e}")
            # 即使失败也要记录结果
            failed_result = AccountResult(
                username=account.username,
                error_msg=f"未知异常: {str(e)}"
            )
            all_results.append(failed_result)
        
        # 账号间间隔
        if idx < len(accounts):
            interval = random.uniform(3, 6)
            logger.info(f"⏳ 等待 {interval:.1f} 秒后处理下一个账号...")
            time.sleep(interval)
    
    # 计算总耗时
    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)
    
    # 生成汇总报告
    logger.info("\n" + "=" * 80)
    logger.info("🎉 所有账号处理完成！")
    logger.info(f"⏱️  总耗时: {minutes} 分钟 {seconds} 秒")
    logger.info("=" * 80)
    
    # 生成并发送通知
    summary_report = generate_summary_report(all_results, config)
    logger.info("\n" + summary_report)
    
    # 发送通知
    send_notification("雨云签到任务完成", summary_report)


if __name__ == "__main__":
    main()
