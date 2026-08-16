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
from selenium.common import TimeoutException
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
    ocr: ddddocr.DdddOcr
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
COOKIE_DIR_NAME = "cookies"   # Cookie 缓存目录（脚本同目录下，青龙数据卷持久化）


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


def _build_chrome_options(binary: str, headless_flag: str) -> Options:
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

    # User-Agent
    ops.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

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
                service=service, options=_build_chrome_options(binary, flag)
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
            ctx.driver.add_cookie(c)
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
    if not load_cookies(ctx, account.username):
        return False
    try:
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


def do_sign_in(ctx: RuntimeContext) -> bool:
    """执行签到"""
    try:
        logger.info("=" * 60)
        logger.info("🌐 访问赚取积分页: https://app.rainyun.com/account/reward/earn")
        ctx.driver.get(EARN_URL)
        ctx.driver.implicitly_wait(5)
        
        logger.info(f"   当前页面URL: {ctx.driver.current_url}")
        logger.info(f"   当前页面标题: {ctx.driver.title}")
        
        # 查找签到按钮
        logger.info("🔍 查找每日签到按钮...")
        try:
            earn_btn_qddiv = ctx.driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[2]/div/div/div/div[1]/div')
            earn_btn_qd = earn_btn_qddiv.find_element(By.XPATH, './/span[contains(text(),"每日签到")]')
            status_elem = earn_btn_qd.find_element(By.XPATH, './following-sibling::span[1]')
            status_text = status_elem.text.strip()
            
            logger.info(f"📌 签到状态: {status_text}")
            
            if status_text == "领取奖励":
                earn_btn = status_elem.find_element(By.XPATH, './a')
                logger.info("🎯 开始领取签到奖励")
                earn_btn.click()
                
                # 处理签到验证码
                time.sleep(2)
                logger.info("⚠️  触发签到验证码")
                ctx.driver.switch_to.frame("tcaptcha_iframe_dy")
                
                from captcha import process_captcha
                if not process_captcha(ctx, ctx.config):
                    logger.error("❌ 签到验证码处理失败")
                    return False
                
                ctx.driver.switch_to.default_content()
                logger.info("⏳ 等待签到结果...")
                time.sleep(5)
                
                logger.info("✅ 签到奖励领取成功")
            else:
                logger.info(f"📌 {status_text}，无需重复签到")
            
            # 获取当前积分
            try:
                points_elem = ctx.driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3')
                import re
                current_points = int(''.join(re.findall(r'\d+', points_elem.text)))
                logger.info(f"💰 当前积分: {current_points} （约 {current_points/2000:.2f} 元）")
            except Exception as e:
                logger.warning(f"⚠️  积分获取失败: {e}")
            
            return True
            
        except TimeoutException:
            logger.error("❌ 未找到签到按钮")
            return False
        
    except Exception as e:
        logger.error(f"❌ 签到异常: {e}", exc_info=True)
        return False


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
        delay_sec = random.randint(0, 60)
        logger.info(f"⏳ 随机延时 {delay_min} 分钟 {delay_sec} 秒")
        time.sleep(delay_min * 60 + delay_sec)
        
        # 初始化组件
        logger.info("🔧 初始化 ddddocr 验证码识别库")
        ocr = ddddocr.DdddOcr(ocr=True, show_ad=False)
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
            ocr=ocr,
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
        result.sign_in_success = do_sign_in(ctx)
        if not result.sign_in_success:
            result.error_msg = "签到失败"
            logger.error("❌ 签到失败")
            return result
        
        # 记录签到后积分
        if account.api_key:
            try:
                logger.info("🔍 正在获取签到后积分...")
                api = RainyunAPI(account.api_key, config)
                result.points_after = api.get_user_points()
                result.points_earned = result.points_after - result.points_before
                logger.info(f"💰 当前积分: {result.points_after} (本次获得 {result.points_earned} 分)")
                logger.info(f"💵 约合人民币: {result.points_after / config['points_to_cny_rate']:.2f} 元")
            except Exception as e:
                logger.warning(f"⚠️  获取最终积分失败: {e}")
        
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
    
    # 积分统计
    total_points_before = sum(r.points_before for r in results)
    total_points_after = sum(r.points_after for r in results)
    total_earned = sum(r.points_earned for r in results)
    
    if total_points_after > 0:
        lines.append(f"\n💰 积分统计:")
        lines.append(f"  签到前总积分: {total_points_before}")
        lines.append(f"  签到后总积分: {total_points_after}")
        lines.append(f"  本次获得: {total_earned} 分")
        lines.append(f"  约合人民币: {total_points_after / config['points_to_cny_rate']:.2f} 元")
    
    # 各账号详情
    lines.append(f"\n📋 各账号详情:")
    lines.append("-" * 60)
    
    for idx, result in enumerate(results, 1):
        lines.append(f"\n【账号 {idx}】 {result.username}")
        
        if result.is_success():
            lines.append(f"  状态: ✅ 成功")
            if result.points_after > 0:
                lines.append(f"  积分: {result.points_before} → {result.points_after} (+{result.points_earned})")
            if result.auto_renew_enabled:
                lines.append(f"  自动续费: ✅ 已启用")
                if result.renew_summary:
                    lines.append(f"    {result.renew_summary}")
            else:
                lines.append(f"  自动续费: ⏭️  未启用")
        else:
            lines.append(f"  状态: ❌ 失败")
            lines.append(f"  原因: {result.error_msg}")
    
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
