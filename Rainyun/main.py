import logging
import os
import random
import re
import shutil
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
from playwright.sync_api import TimeoutError as PWTimeout

from config import CONFIG
from account_parser import parse_accounts, Account
from api_client import RainyunAPI
from server_manager import ServerManager
import browser as browser_layer

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
    session: object            # browser.BrowserSession，持有 page/context/browser
    det: ddddocr.DdddOcr
    temp_dir: str
    config: dict
    console_logs: list = None  # 浏览器 console 日志收集器（失败诊断用）
    net_logs: list = None      # 4xx/5xx 与失败请求收集器（失败诊断用）
    frame: object = None       # 验证码 iframe 作用域（frame_locator），非验证码场景为 None

    def __post_init__(self):
        if self.console_logs is None:
            self.console_logs = []
        if self.net_logs is None:
            self.net_logs = []

    @property
    def page(self):
        """当前页面对象（Playwright Page）。"""
        return self.session.page

    @property
    def context(self):
        """浏览器上下文（Playwright BrowserContext），Cookie 操作走这里。"""
        return self.session.context

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

# 赚取积分页顶部「积分」卡片的数值
POINTS_XPATH = '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3'

# 每日签到卡片头：点它打开签到模态框（右侧「积分或抽奖券」只是固定说明文案，不是状态）
SIGN_CARD_XPATH = '//button[contains(@class,"daily-check-in-task")]'

# 签到模态框内的结构（class 选择器比绝对 XPath 稳定）
SIGN_MODAL_CSS = ".daily-check-in"
SIGN_MODAL_TITLE_CSS = ".daily-check-in__title"
SIGN_MODAL_STATUS_CSS = ".daily-check-in__status"
SIGN_REWARDS_CSS = ".daily-check-in__rewards"
SIGN_REWARD_BTN_CSS = ".daily-check-in__reward"
SIGN_REWARD_AMOUNT_CSS = ".daily-check-in__amount"

# 卡片 badge 与模态框标题/状态里出现这些文案即视为「今天已领过」
SIGN_DONE_TEXTS = ("已完成", "今日已签到", "已领取")
# 默认领取的奖励名，必须与页面 .daily-check-in__amount 的文案一致
SIGN_REWARD_DEFAULT = "500 积分"



# 签到提交接口路径片段：命中即记录请求与响应，用于定位「点击了但没生效」的真因
SIGN_API_HINT = "/user/reward/tasks"


def inject_api_recorder(page):
    """在当前页面注入 fetch/XHR 钩子，把签到提交接口的状态码与响应体记到 window.__signApiLog。

    签到失败时日志只能看到「状态没变」，无法区分是按钮 disabled（压根没发请求）、
    还是请求发了被服务端拒绝（如 Cap 人机验证失败）。抓一次真实响应即可定性。

    刻意用 page.evaluate 而不是 context.add_init_script：
    add_init_script 会传播进 Cap 后续创建的 sandbox srcdoc iframe，把 fetch 和
    XMLHttpRequest.prototype.open/send 包成非原生实现，而 Cap 的 instrumentation
    会校验原生函数标记——这正是 stealth.min.js 导致 instr_timeout 的同一原因。
    页面级 evaluate 只作用于当前文档，不污染 Cap 的采集帧。
    """
    script = """
    (function () {
      if (window.__signApiRecorderInstalled) return;
      window.__signApiRecorderInstalled = true;
      window.__signApiLog = [];
      const HINT = "%s";

      function record(entry) {
        try {
          window.__signApiLog.push(entry);
          if (window.__signApiLog.length > 50) window.__signApiLog.shift();
        } catch (e) {}
      }

      // 拦截 fetch
      const origFetch = window.fetch;
      if (origFetch) {
        window.fetch = function (input, init) {
          const url = typeof input === "string" ? input : (input && input.url) || "";
          const p = origFetch.apply(this, arguments);
          if (url.indexOf(HINT) !== -1) {
            p.then(function (resp) {
              resp.clone().text().then(function (body) {
                record({via: "fetch", url: url, status: resp.status, body: body.slice(0, 2000)});
              }).catch(function () {
                record({via: "fetch", url: url, status: resp.status, body: "<读取失败>"});
              });
            }).catch(function (err) {
              record({via: "fetch", url: url, status: 0, body: "请求异常: " + err});
            });
          }
          return p;
        };
      }

      // 拦截 XMLHttpRequest（axios 在老浏览器走这条）
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function (method, url) {
        this.__signUrl = url || "";
        return origOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function () {
        const xhr = this;
        if ((xhr.__signUrl || "").indexOf(HINT) !== -1) {
          xhr.addEventListener("loadend", function () {
            let body = "";
            try { body = (xhr.responseText || "").slice(0, 2000); } catch (e) { body = "<读取失败>"; }
            record({via: "xhr", url: xhr.__signUrl, status: xhr.status, body: body});
          });
        }
        return origSend.apply(this, arguments);
      };
    })();
    """ % SIGN_API_HINT
    try:
        page.evaluate(script)
    except Exception as e:
        logger.warning(f"⚠️  注入签到接口记录器失败（不影响签到）: {e}")


def read_api_log(page):
    """取出注入钩子收集到的签到接口记录；没有则返回空列表。"""
    try:
        return page.evaluate("() => window.__signApiLog || []") or []
    except Exception:
        return []


def _is_element_disabled(locator) -> bool:
    """判断元素是否处于 disabled 状态。"""
    try:
        return bool(locator.is_disabled())
    except Exception:
        return False


def human_like_click(page, locator) -> None:
    """带指针轨迹的点击（委托给 browser 层实现）。"""
    browser_layer.human_like_click(page, locator)


def _log_browser_console(ctx: RuntimeContext) -> None:
    """打印已收集的浏览器控制台日志，用于暴露 Cap 人机验证被拒的真实原因。"""
    logs = getattr(ctx, "console_logs", None) or []
def _log_browser_console(ctx: RuntimeContext) -> None:
    """打印已收集的浏览器控制台日志，用于暴露 Cap 人机验证被拒的真实原因。"""
    logs = getattr(ctx, "console_logs", None) or []
    if not logs:
        logger.info("🔍 浏览器控制台无输出")
    else:
        logger.info(f"🔍 浏览器控制台最近 {min(len(logs), 20)} 条日志:")
        for line in logs[-20:]:
            logger.info(f"   {line}")

    net_logs = getattr(ctx, "net_logs", None) or []
    if net_logs:
        logger.info(f"🔍 网络失败请求最近 {min(len(net_logs), 15)} 条:")
        for line in net_logs[-15:]:
            logger.info(f"   {line}")


def _log_sign_api_result(ctx: RuntimeContext) -> None:
    """把抓到的签到接口记录打到日志，用于区分失败真因。

    没有任何记录 = 点击没触发提交（多半按钮 disabled）；有记录则打出状态码与响应体。
    """
    entries = read_api_log(ctx.page)
    if not entries:
        logger.error("🔍 诊断：未捕获到签到提交请求 —— 点击未触发提交")
        logger.error("   可能原因：奖励按钮处于 disabled（任务 Status≠1，条件未满足）")
        return
    logger.error(f"🔍 诊断：捕获到 {len(entries)} 条签到提交记录")
    for entry in entries:
        logger.error(
            f"   [{entry.get('via')}] {entry.get('url')} "
            f"-> HTTP {entry.get('status')}"
        )
        body = str(entry.get("body", ""))
        if body:
            logger.error(f"   响应: {body[:800]}")


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
        for c in ctx.context.cookies():
            # expires 为 -1 的是 session cookie，浏览器一关即失效，缓存无意义
            if not c.get("expires") or c["expires"] < 0:
                continue
            item = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "expires": c["expires"],   # 秒级时间戳（Playwright 与之一致）
            }
            if c.get("secure"):
                item["secure"] = True
            if c.get("httpOnly"):
                item["httpOnly"] = True
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
    """把缓存的 Cookie 注入当前浏览器上下文。返回是否成功。

    Playwright 的 add_cookies 不需要先导航到目标域，也不需要 expiry/expires
    键名转换（统一用 expires，秒级浮点），比 Selenium 简单。
    """
    path = cookie_cache_path(username)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        cookies = []
        for c in data.get("cookies", []):
            # 已过期的 cookie 跳过（不删文件，可能只是部分过期）
            if c.get("expires") and c["expires"] < now:
                continue
            cookies.append(c)
        if not cookies:
            return False

        # 逐条注入：Playwright 的 add_cookies 是整批校验，任一条不合法（缺 domain、
        # sameSite 取值异常、expires 类型不对）会整体抛 ValidationError，
        # 导致一批有效 Cookie 全废。逐条注入可跳过坏条目保留其余。
        injected = 0
        for cookie in cookies:
            try:
                ctx.context.add_cookies([cookie])
                injected += 1
            except Exception as e:
                logger.warning(f"⚠️ 跳过一条无效 Cookie({cookie.get('name')}): {e}")
        if injected == 0:
            return False
        logger.info(f"🍪 已注入 {injected}/{len(cookies)} 条缓存 Cookie")
        return True
    except Exception as e:
        logger.warning(f"⚠️ 读取/注入 Cookie 缓存失败: {e}")
        return False


def is_auth_url(url: str) -> bool:
    """判断 URL 是否落在雨云的鉴权区（/auth/login、/auth/verify 等）。

    只判 login 会漏掉二次验证、错误页等同样属于未登录态的页面；反过来判
    dashboard 又因默认落地页已改成积分页而失效。按 /auth/ 前缀判断最贴合现状。
    """
    return "/auth/" in (url or "")


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
        # Playwright 的 add_cookies 不需要先导航到目标域，直接注入即可
        if not load_cookies(ctx, account.username):
            # 全部过期或注入失败：清掉坏缓存，避免每次运行都白试一遍
            _drop_cookie_cache(account.username)
            return False
        # 雨云为 SPA：登录失效时访问内部页会 302 回 /auth/ 下的登录或校验页
        ctx.page.goto(EARN_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        url = ctx.page.url
        logger.info(f"   缓存 Cookie 探测 URL: {url}")
        if is_auth_url(url):
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
        logger.info(f"🌐 访问雨云登录页: {LOGIN_URL}")
        ctx.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

        logger.info(f"   当前页面标题: {ctx.page.title()}")
        logger.info(f"   当前页面URL: {ctx.page.url}")

        logger.info("⏳ 等待登录表单元素加载...")
        timeout_ms = int(ctx.config.get("timeout", 20)) * 1000
        username_elem = ctx.page.locator('[name="login-field"]')
        password_elem = ctx.page.locator('[name="login-password"]')
        login_btn = ctx.page.locator("button[type='submit']")
        username_elem.wait_for(state="visible", timeout=timeout_ms)
        password_elem.wait_for(state="visible", timeout=timeout_ms)
        login_btn.wait_for(state="visible", timeout=timeout_ms)

        logger.info("✅ 登录表单元素加载完成")
        logger.info("📝 输入账号密码")
        username_elem.fill(username)
        password_elem.fill(password)

        logger.info("🖱️  点击登录按钮")
        login_btn.click()
        logger.info("⏳ 正在登录中，耗时较长请稍等……")
        time.sleep(3)

        # 处理登录验证码（腾讯 tcaptcha 点选；雨云已逐步下线，出现才处理）
        try:
            logger.info("🔍 检查是否触发登录验证码...")
            ctx.page.locator("#tcaptcha_iframe_dy").wait_for(state="visible", timeout=15000)
            logger.warning("⚠️  触发登录验证码！")
            # frame_locator 把后续定位限定在 iframe 内，无需手动切换上下文
            ctx.frame = ctx.page.frame_locator("#tcaptcha_iframe_dy")

            from captcha import process_captcha
            if not process_captcha(ctx, ctx.config):
                logger.error("❌ 登录验证码处理失败")
                return False

            logger.info("✅ 登录验证码处理成功")
        except PWTimeout:
            logger.info("✅ 未触发登录验证码")
        finally:
            ctx.frame = None

        logger.info("⏳ 等待页面跳转...")
        time.sleep(5)

        # 验证登录状态：雨云默认落地页是积分页（/account/reward/earn），不再固定跳 dashboard。
        # 未登录时访问内部页会被 302 回 /auth/ 下的页面，因此只有仍落在鉴权区才算失败。
        current_url = ctx.page.url
        logger.info(f"   跳转后URL: {current_url}")
        logger.info(f"   当前页面标题: {ctx.page.title()}")

        if is_auth_url(current_url):
            logger.error("❌ 登录失败！仍停留在鉴权页")
            logger.error(f"   当前URL: {current_url}")
            return False

        # 获取用户名
        try:
            user_elem = ctx.page.locator(
                '//*[@id="app"]/div[1]/nav/div[1]/ul/div[6]/li/a/div/div/p'
            )
            user_name = user_elem.inner_text(timeout=5000).strip()
            logger.info(f"✅ 账号登录成功: {user_name}")
        except Exception:
            logger.info("✅ 登录成功！")

        return True

    except PWTimeout:
        logger.error("❌ 页面加载超时！")
        logger.error("   可能原因：")
        logger.error("   1. 网络连接问题")
        logger.error("   2. 页面加载时间过长，请尝试增加 timeout 配置")
        logger.error("   3. 雨云服务器响应慢")
        return False
    except Exception as e:
        logger.error(f"❌ 登录异常: {e}", exc_info=True)
        return False


def read_points(ctx: RuntimeContext, wait_seconds: float = 5, quiet: bool = False) -> int:
    """读取赚取积分页顶部的当前积分；读取失败返回 0（0 同时表示“未知”）。

    积分数值是页面异步渲染的，元素出现时文本可能还是空的，因此在
    wait_seconds 内轮询等待，避免把「尚未加载」误判成「读取失败」。

    quiet=True 时不打读取失败告警：签到结果轮询里会高频调用，否则会刷屏。
    """
    deadline = time.time() + wait_seconds
    last_error = None
    while True:
        try:
            digits = re.findall(r"\d+", ctx.page.locator(POINTS_XPATH).inner_text(timeout=2000))
            if digits:
                return int("".join(digits))
        except Exception as e:
            last_error = e
        if time.time() >= deadline:
            if last_error is not None and not quiet:
                logger.warning(f"⚠️  积分读取失败: {last_error}")
            return 0
        time.sleep(0.5)


SIGN_STATE_DONE = "done"
SIGN_STATE_CLAIMABLE = "claimable"
SIGN_STATE_UNKNOWN = "unknown"


def classify_sign_state(badge_text: str = "", modal_title: str = "", status_text: str = "") -> str:
    """把签到页面文案归为 done / claimable / unknown 三态。

    页面文案有三个来源：卡片 badge（已完成 / 去签到）、模态框标题（选择今日奖励 /
    今日已签到）、模态框状态行（已领取 500 积分，明天再来）。任一出现「已完成」类
    文案即视为今天已领过，避免重复提交任务被服务端拒绝。
    """
    combined = " ".join(p for p in (badge_text, modal_title, status_text) if p)
    for done_text in SIGN_DONE_TEXTS:
        if done_text in combined:
            return SIGN_STATE_DONE
    if "去签到" in combined or "选择今日奖励" in combined:
        return SIGN_STATE_CLAIMABLE
    return SIGN_STATE_UNKNOWN


def read_text(ctx: RuntimeContext, selector: str, timeout: float = 5.0) -> str:
    """在 timeout 内轮询读取元素文本；读不到返回空串。

    页面文案由 Vue 异步渲染，元素出现时文本可能还是空的，因此轮询而不是读一次。
    selector 是 Playwright 选择器（支持 CSS 与 // 开头的 XPath）。
    """
    deadline = time.time() + timeout
    while True:
        try:
            text = ctx.page.locator(selector).first.inner_text(timeout=1000).strip()
            if text:
                return text
        except Exception:
            pass
        if time.time() >= deadline:
            return ""
        time.sleep(0.3)


def find_sign_card(ctx: RuntimeContext, timeout: float = 20.0):
    """等待并返回签到卡片头按钮；超时抛 PWTimeout。

    返回 .first 收敛到单个元素：Playwright 默认 strict mode 下，locator 命中多个
    元素时 inner_text/click 会直接抛 Error（不是 PWTimeout），而且 click(force=True)
    也绕不过 strict。当前页面只有一个签到卡片，但收敛后不怕将来多出同名卡片。
    """
    locator = ctx.page.locator(SIGN_CARD_XPATH).first
    locator.wait_for(state="attached", timeout=int(timeout * 1000))
    return locator


def normalize_label(text: str) -> str:
    """归一化页面文案用于比较：去掉所有空白（含全角空格、不换行空格）。

    页面文案里的空格可能是普通空格、全角空格或 &nbsp;，直接全等比较会因一个
    不可见字符导致整体匹配失败，因此比较前统一抹掉空白。
    """
    return re.sub(r"\s+", "", text or "")


def find_reward_button(ctx: RuntimeContext, reward_name: str):
    """按奖励名（如「500 积分」）在模态框内找奖励按钮；找不到返回 None。

    比较用归一化后的文案（忽略空白差异）。用全等而非子串匹配，避免「500 积分」
    误配到「1500 积分」这类前缀相同的选项。
    """
    target = normalize_label(reward_name)
    try:
        buttons = ctx.page.locator(SIGN_REWARD_BTN_CSS)
        count = buttons.count()
    except Exception:
        return None
    for i in range(count):
        btn = buttons.nth(i)
        try:
            amount = btn.locator(SIGN_REWARD_AMOUNT_CSS).inner_text(timeout=1000)
        except Exception:
            continue
        if normalize_label(amount) == target:
            return btn
    return None


def wait_sign_result(ctx: RuntimeContext, result: AccountResult, timeout: float = 90.0,
                     quiet_seconds: float = 25.0) -> bool:
    """轮询签到结果，任一条件命中即判定成功。timeout 是包含静默期在内的总时长。

    签到要过 Cap 人机验证：4 个 HashWX PoW + 一段 instrumentation 环境采集。
    后者的 finalize 超时是前端硬编码的 20 秒，而采集脚本要跑 WASM 编译探测、
    指纹采集等重活——如果此时我们还在高频读 DOM，容器 CPU 会被抢走导致
    `instr_timeout`。因此开头 quiet_seconds 内完全不碰页面，让 Cap 安心跑完。

    判定依据按可靠性排序：模态框状态行出现「已领取」→ 卡片 badge 变「已完成」→
    积分比签到前增加。
    """
    deadline = time.time() + timeout

    # 静默期：只睡不查，把 CPU 全让给 Cap 的 PoW 与采集脚本
    time.sleep(min(quiet_seconds, timeout))

    last_status = ""
    while True:
        status_text = read_text(ctx, SIGN_MODAL_STATUS_CSS, 0.5)
        if status_text:
            last_status = status_text
            if classify_sign_state(status_text=status_text) == SIGN_STATE_DONE:
                logger.info(f"📌 签到状态: {status_text}")
                return True

        # 模态框可能已自动关闭，改看卡片 badge
        try:
            card_text = ctx.page.locator(SIGN_CARD_XPATH).first.inner_text(timeout=1000).strip()
            if classify_sign_state(badge_text=card_text) == SIGN_STATE_DONE:
                logger.info(f"📌 签到卡片: {card_text}")
                return True
        except Exception:
            pass

        # 积分增加兜底：领 500 积分时最直接的成功证据
        if result.points_before > 0:
            current = read_points(ctx, wait_seconds=0.5, quiet=True)
            if current > result.points_before:
                logger.info(f"📌 积分已增加（{result.points_before} → {current}）")
                return True

        if time.time() >= deadline:
            logger.info(f"📌 最后读到的签到状态: {last_status or '未读取到'}")
            return False
        time.sleep(1.5)


def do_sign_in(ctx: RuntimeContext, result: AccountResult) -> bool:
    """执行签到，并把页面积分回填到 result（未配置 API Key 时的积分来源）。

    雨云签到页改版后的流程：点「每日签到」卡片头打开模态框 → 在「500 积分」和
    「抽奖券 × 1」里二选一 → 提交时强制过 Cap 人机验证（PoW，页面自动求解）。

    失败时把具体原因写入 result.error_msg，让通知与退出码汇总能区分失败类型
    （只写「签到失败」无法定位是页面改版还是人机验证被拦）。
    """
    def fail(reason: str) -> bool:
        result.error_msg = reason
        logger.error(f"❌ {reason}")
        return False

    try:
        logger.info("=" * 60)
        logger.info(f"🌐 访问赚取积分页: {EARN_URL}")
        # Playwright 没有隐式等待概念，所有等待都是显式 timeout，轮询节奏可控
        ctx.page.goto(EARN_URL, wait_until="domcontentloaded", timeout=45000)
        # 接口记录器必须在当前文档上注入（不能进 init script，否则污染 Cap 的采集帧）
        inject_api_recorder(ctx.page)

        logger.info(f"   当前页面URL: {ctx.page.url}")
        logger.info(f"   当前页面标题: {ctx.page.title()}")

        # 签到前积分：API 未提供（未配置 Key 或取数失败）时用页面值兜底，
        # 通知里才能算出「本次获得」，同时作为签到成功的兜底证据
        if result.points_before == 0:
            page_points = read_points(ctx)
            if page_points > 0:
                result.points_before = page_points
                logger.info(
                    f"💰 签到前积分: {page_points} "
                    f"（约 {page_points / ctx.config['points_to_cny_rate']:.2f} 元）"
                )

        logger.info("🔍 查找每日签到卡片...")
        try:
            card = find_sign_card(ctx)
        except PWTimeout:
            return fail("未找到每日签到卡片（页面结构可能再次改版）")

        card_text = card.inner_text(timeout=5000).strip()
        logger.info(f"📌 签到卡片文案: {card_text}")
        if classify_sign_state(badge_text=card_text) == SIGN_STATE_DONE:
            logger.info("📌 今日已签到，无需重复签到")
            return True

        logger.info("🖱️  点击签到卡片，打开签到弹窗")
        try:
            card.click(timeout=10000)
        except Exception:
            # 卡片头可能被浮层遮挡，force 穿透遮罩再点
            card.click(force=True, timeout=10000)

        # 先等弹窗容器出现再读标题：今日已签时弹窗不渲染奖励列表，
        # 直接等 rewards 会把「已签」误判成「弹窗没打开」
        try:
            ctx.page.locator(SIGN_MODAL_CSS).first.wait_for(state="attached", timeout=10000)
        except PWTimeout:
            return fail("签到弹窗未打开")

        modal_title = read_text(ctx, SIGN_MODAL_TITLE_CSS, 5)
        logger.info(f"📌 签到弹窗标题: {modal_title or '未读取到'}")
        if classify_sign_state(modal_title=modal_title) == SIGN_STATE_DONE:
            logger.info("📌 今日已签到，无需重复签到")
            return True

        try:
            ctx.page.locator(SIGN_REWARDS_CSS).first.wait_for(state="attached", timeout=10000)
        except PWTimeout:
            return fail("签到弹窗内未出现奖励选项")

        reward_btn = find_reward_button(ctx, SIGN_REWARD_DEFAULT)
        if reward_btn is None:
            return fail(f"签到弹窗内未找到奖励「{SIGN_REWARD_DEFAULT}」")

        # Cap 的 instrumentation 采集有前端硬编码 20 秒 finalize 超时，容器里
        # 偶发跑不完（instr_timeout）。失败后重新点一次相当于重新发起挑战，
        # 第二次通常能过——所以这里给两次机会。
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            # 每轮重读按钮状态：重试前页面已刷新，旧状态不再可信
            btn_disabled = _is_element_disabled(reward_btn)
            try:
                btn_classes = (reward_btn.get_attribute("class") or "").strip()
            except Exception:
                btn_classes = ""
            logger.info(
                f"🎯 选择奖励: {SIGN_REWARD_DEFAULT}"
                f"（disabled={btn_disabled}, class={btn_classes or '无'}）"
            )
            logger.info(
                f"🖱️  点击奖励按钮"
                f"（第 {attempt}/{max_attempts} 次，提交时页面会自动完成人机验证）"
            )
            try:
                human_like_click(ctx.page, reward_btn)
            except Exception as e:
                logger.warning(f"⚠️  拟人点击失败({e})，回退普通点击")
                try:
                    reward_btn.click(timeout=10000)
                except Exception:
                    reward_btn.click(force=True, timeout=10000)

            logger.info("⏳ 等待签到结果（含 25 秒静默期，总计最长 90 秒）...")
            if wait_sign_result(ctx, result, timeout=90):
                logger.info("✅ 签到奖励领取成功")
                return True

            _log_sign_api_result(ctx)
            _log_browser_console(ctx)

            if attempt < max_attempts:
                # 刷新页面重新拉挑战，避免复用已超时的 Cap 会话
                logger.warning("⚠️  本次未成功，刷新页面后重试一次")
                try:
                    ctx.page.goto(EARN_URL, wait_until="domcontentloaded", timeout=45000)
                    inject_api_recorder(ctx.page)
                    ctx.page.wait_for_timeout(3000)
                    card = find_sign_card(ctx)
                    if classify_sign_state(badge_text=card.inner_text(timeout=5000)) == SIGN_STATE_DONE:
                        logger.info("📌 重试前检测到今日已签到")
                        return True
                    try:
                        card.click(timeout=10000)
                    except Exception:
                        card.click(force=True, timeout=10000)
                    # 先等弹窗容器，再确认不是「今日已签到」
                    ctx.page.locator(SIGN_MODAL_CSS).first.wait_for(state="attached", timeout=10000)
                    retry_title = read_text(ctx, SIGN_MODAL_TITLE_CSS, 5)
                    if classify_sign_state(modal_title=retry_title) == SIGN_STATE_DONE:
                        logger.info("📌 重试前检测到今日已签到")
                        return True
                    ctx.page.locator(SIGN_REWARDS_CSS).first.wait_for(state="attached", timeout=10000)
                    reward_btn = find_reward_button(ctx, SIGN_REWARD_DEFAULT)
                    if reward_btn is None:
                        return fail(f"重试时未找到奖励「{SIGN_REWARD_DEFAULT}」")
                except Exception as e:
                    logger.warning(f"⚠️  重试准备失败: {e}")
                    break

        return fail("未能确认签到结果（人机验证超时或提交失败）")

    except Exception as e:
        result.error_msg = f"签到异常: {e}"
        logger.error(f"❌ 签到异常: {e}", exc_info=True)
        return False

    finally:
        # 无论签到成功还是中途失败，只要登录态还在就刷新一次积分，
        # 保证通知里能看到账号当前余额（API Key 有值时后续以 API 值为准）
        page_points = read_points(ctx, quiet=True)
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
    session = None
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

        logger.info("🔧 启动 Playwright 浏览器（有头 + Xvfb）")
        session = browser_layer.init_browser(config)
        logger.info(f"✅ 浏览器已启动（{session.binary}）")

        # 创建临时目录
        temp_dir = tempfile.mkdtemp(prefix="rainyun-")
        logger.info(f"📁 临时目录: {temp_dir}")

        # 构建上下文
        ctx = RuntimeContext(
            session=session,
            det=det,
            temp_dir=temp_dir,
            config=config,
        )
        # console 监听必须在首个 goto 之前挂上，否则丢早期日志
        browser_layer.attach_console_logger(ctx.page, ctx.console_logs)
        # 网络失败监听同样要在导航前挂上，用于定位 4xx/5xx 来源
        browser_layer.attach_network_logger(ctx.page, ctx.net_logs)
        
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
            # do_sign_in 已写入具体失败原因，这里只在缺失时兜底，不覆盖
            result.error_msg = result.error_msg or "签到失败"
            logger.error(f"❌ 签到失败: {result.error_msg}")
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
        if session:
            try:
                session.close()
                logger.info("🔒 浏览器已关闭")
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

    # 退出码决定青龙面板显示成功还是失败：任一账号失败即返回非 0，
    # 避免「全失败仍显示绿色成功」掩盖问题；账号列表为空同样算失败，
    # 否则配置写错（如 RAINYUN_ACCOUNT=[]）也会显示绿色
    failed = [r for r in all_results if not r.is_success()]
    if not all_results:
        logger.error("=" * 80)
        logger.error("❌ 任务失败：未解析到任何账号，请检查 RAINYUN_ACCOUNT 配置")
        logger.error("=" * 80)
        sys.exit(1)
    if failed:
        logger.error("=" * 80)
        logger.error(f"❌ 任务失败：{len(failed)}/{len(all_results)} 个账号未签到成功")
        for r in failed:
            logger.error(f"   - {r.username}: {r.error_msg or '未知原因'}")
        logger.error("=" * 80)
        sys.exit(1)


if __name__ == "__main__":
    main()
