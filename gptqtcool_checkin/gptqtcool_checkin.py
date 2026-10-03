#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new Env('gpt.qt.cool 签到');
cron: 35 8 * * *

gpt.qt.cool 自动签到 - 青龙面板版
基于 Playwright + OpenCV 的滑动验证码识别

环境变量:
  GPTQTCOOL_KEY              必填,签到网站 API Key
  GPTQTCOOL_FORCE_RUN        可选,强制执行(忽略今日已签到),默认 false
  GPTQTCOOL_LOG_LEVEL        可选,日志级别(error/warn/info/debug),默认 info
  GPTQTCOOL_SCREENSHOT_DIR   可选,截图输出目录,默认 artifacts
  GPTQTCOOL_TIMEOUT          可选,页面加载超时(毫秒),默认 30000
  GPTQTCOOL_NOTIFY           可选,是否调用青龙 notify.py 推送,默认 true
  GPTQTCOOL_NOTIFY_ONLY_FAIL 可选,仅在失败时推送,默认 false
  GPTQTCOOL_SERVERPUSHKEY    可选,Server酱 Turbo KEY,默认复用青龙 SERVERPUSHKEY
  HEADLESS                   可选,浏览器无头模式,默认 true;本地调试可设 false

依赖:
  pip install playwright opencv-python numpy
  playwright install chromium
"""

import os
import sys
import re
import time
import json
import random
import base64
import logging
import asyncio
import traceback
import urllib.request
from pathlib import Path
from urllib.parse import urlencode
from datetime import datetime

# UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 问题，Linux UTF-8 容器无副作用）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ========== 第三方依赖检查 ==========
try:
    from playwright.async_api import async_playwright
except ImportError:
    print("❌ 缺少 playwright,请执行: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    import cv2
    import numpy as np
except ImportError:
    print("❌ 缺少 opencv-python/numpy,请执行: pip install opencv-python numpy")
    sys.exit(1)


# ====================== 兼容青龙通知 ======================

# Server酱 Turbo KEY:优先独立变量 GPTQTCOOL_SERVERPUSHKEY,否则复用青龙 SERVERPUSHKEY
_SERVERJ_KEY = os.environ.get("GPTQTCOOL_SERVERPUSHKEY") or os.environ.get("SERVERPUSHKEY")
# 进程内屏蔽青龙 notify.py 的 serverJ 渠道(青龙 send() 每次调用时才读环境变量,pop 有效;
# 仅影响本进程,青龙其它任务的 SERVERPUSHKEY 不受影响),避免重复推送与异常刷屏
os.environ.pop("SERVERPUSHKEY", None)

_qinglong_send = None
try:
    # 青龙运行时会把 notify.py 加入 sys.path
    from notify import send as _qinglong_send  # type: ignore
except Exception:
    # 青龙容器内 notify.py 在 /ql/data/scripts/,脚本运行时 cwd 可能不在该目录
    _scripts_dir = "/ql/data/scripts"
    if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
        try:
            from notify import send as _qinglong_send  # type: ignore
        except Exception:
            _qinglong_send = None


def env_bool(name: str, default: bool) -> bool:
    """解析布尔型环境变量(1/true/yes/y/on 视为真)"""
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    """解析整型环境变量,非法值回退默认"""
    try:
        return int(os.getenv(name) or default)
    except Exception:
        return default


_SERVERJ_UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]


def send_serverj(title: str, content: str) -> bool:
    """自实现 Server酱 Turbo 推送(GET+浏览器UA+超时+3次重试)。

    青龙内置 notify.py 的 serverJ 渠道无 timeout/UA/重试,且为异步子线程,网络一抖即丢通知;
    此处绕过它直连 sctapi.ftqq.com,保证可靠送达。
    """
    if not _SERVERJ_KEY:
        logger.warning("⚠️ 未配置 Server酱 KEY(GPTQTCOOL_SERVERPUSHKEY / SERVERPUSHKEY),跳过")
        return False
    api_url = f"https://sctapi.ftqq.com/{_SERVERJ_KEY}.send?" + urlencode({
        "title": title,
        "desp": content,
    })
    max_attempts, retry_delay = 3, 2
    for attempt in range(1, max_attempts + 1):
        try:
            # 每次轮换 UA,降低被服务端风控概率
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": _SERVERJ_UA_LIST[attempt % len(_SERVERJ_UA_LIST)]},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0:
                logger.info(f"✅ Server酱 推送成功(第 {attempt} 次)")
                return True
            # 服务端明确返回错误,重试无意义
            logger.warning(f"⚠️ Server酱 返回异常: {result}")
            return False
        except Exception as e:
            logger.warning(f"⚠️ Server酱 第 {attempt}/{max_attempts} 次推送失败: {e}")
            if attempt < max_attempts:
                logger.info(f"   等待 {retry_delay} 秒后重试...")
                time.sleep(retry_delay)
    logger.warning("⚠️ Server酱 推送最终失败")
    return False


def send_notify(title: str, content: str) -> bool:
    """推送通知。优先自实现 Server酱(GET+UA+超时+重试),否则走青龙 notify.py。返回是否成功。"""
    if _SERVERJ_KEY:
        # 自实现 Server酱:绕过青龙内置 serverJ 渠道(无 timeout/UA/重试且异步丢通知)
        return send_serverj(title, content)
    if not _qinglong_send:
        logger.info("ℹ️ 未找到 notify.py(仅在青龙环境内可用),跳过推送")
        logger.info(f"📢 [通知内容] {title}: {content}")
        return False
    max_attempts, retry_delay = 3, 3
    for attempt in range(1, max_attempts + 1):
        try:
            _qinglong_send(title, content)
            logger.info("📨 已通过 notify.py 推送")
            return True
        except Exception as e:
            logger.warning(f"⚠️ 推送第 {attempt}/{max_attempts} 次失败: {e}")
            if attempt < max_attempts:
                logger.info(f"   等待 {retry_delay} 秒后重试推送...")
                time.sleep(retry_delay)
            else:
                logger.warning("⚠️ notify.py 推送最终失败")
    return False


# ====================== 弹窗/覆盖层处理 ======================

async def dismiss_overlays(page) -> None:
    """关闭页面上的干扰弹窗和覆盖层（Cookie 同意、弹窗广告、通知权限等）"""
    overlay_closed = False

    # 策略 1: 常见关闭按钮
    close_selectors = [
        '[aria-label="Close"]',
        '[aria-label="close"]',
        '[aria-label="关闭"]',
        "button.close",
        ".close",
        ".btn-close",
        '[class*="modal"] button[class*="close"]',
        '[class*="modal"] [class*="close"]',
        '[class*="modal-header"] button',
        '[class*="modal-footer"] button:has-text("确定")',
        '[class*="modal-footer"] button:has-text("确认")',
        'button:has-text("我知道了")',
        'button:has-text("好的")',
        'button:has-text("同意")',
        'button:has-text("Accept")',
        'button:has-text("I understand")',
        'button:has-text("继续")',
        'button:has-text("关闭")',
    ]
    for selector in close_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.count() > 0 and await btn.is_visible(timeout=1000):
                await btn.click(timeout=2000)
                logger.info(f"✅ 已关闭弹窗: {selector}")
                overlay_closed = True
                await asyncio.sleep(0.5)
        except Exception:
            pass

    # 策略 2: 查找覆盖层元素并移除
    overlay_selectors = [
        '[class*="overlay"]',
        '[class*="modal-backdrop"]',
        '[class*="mask"]',
        '[class*="dialog-mask"]',
    ]
    for selector in overlay_selectors:
        try:
            overlays = page.locator(selector)
            count = await overlays.count()
            for i in range(count):
                try:
                    await page.evaluate(
                        f'document.querySelectorAll(\'{selector}\')[{i}].remove()'
                    )
                except Exception:
                    pass
        except Exception:
            pass

    if overlay_closed:
        await asyncio.sleep(0.8)

    return


# ====================== 配置 ======================
GPTQTCOOL_KEY = os.getenv("GPTQTCOOL_KEY", "").strip()
# 必填校验移到 main() 开头（模块 import 检视时不应直接退出）

CHECKIN_URL = "https://gpt.qt.cool/checkin"
GPTQTCOOL_FORCE_RUN = env_bool("GPTQTCOOL_FORCE_RUN", False)
GPTQTCOOL_LOG_LEVEL = os.getenv("GPTQTCOOL_LOG_LEVEL", "info").strip().upper()
GPTQTCOOL_SCREENSHOT_DIR = Path(os.getenv("GPTQTCOOL_SCREENSHOT_DIR", "artifacts"))
GPTQTCOOL_TIMEOUT = env_int("GPTQTCOOL_TIMEOUT", 30000)  # 页面加载超时（毫秒）
NOTIFY = env_bool("GPTQTCOOL_NOTIFY", True)
NOTIFY_ONLY_FAIL = env_bool("GPTQTCOOL_NOTIFY_ONLY_FAIL", False)
HEADLESS = env_bool("HEADLESS", True)

# 登录态持久化文件（Playwright storage_state：含 cookie + localStorage）
# 脚本同目录，青龙数据卷持久化；命中则跳过"填 Key 登录 + 滑块验证"
STATE_FILE = Path(__file__).parent / "gptqtcool_state.json"


# ========== 日志 ==========
logging.basicConfig(
    level=getattr(logging, GPTQTCOOL_LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("checkin")

GPTQTCOOL_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ========== 验证码检测 ==========
async def detect_slider_captcha(page) -> bool:
    """检测当前页面是否出现滑动验证码(文本关键词 + 元素选择器)"""
    try:
        page_text = await page.inner_text("body")
    except Exception:
        page_text = ""

    captcha_texts = [
        "请完成滑块验证", "请先完成滑块验证", "请完成人机验证",
        "按住滑块向右拖动", "向右拖动完成验证", "拖动滑块",
        "滑动验证", "滑块验证", "安全验证", "验证失败", "验证超时",
    ]
    for text in captcha_texts:
        if text in page_text:
            logger.info(f'✅ 通过页面文本检测到验证码: "{text}"')
            return True

    # 滑块选择器只取语义明确的：div[style*="position: absolute"][style*="left"] 这类过宽选择器误判率高，不用
    slider_selectors = [
        'div[class*="slider"]',
        ".captcha-slider",
        ".geetest",
        ".verify-slider",
    ]
    for selector in slider_selectors:
        if await page.locator(selector).count() > 0:
            logger.info(f"✅ 通过元素选择器检测到验证码: {selector}")
            return True

    return False


async def wait_and_handle_captcha_with_retry(page) -> bool:
    """等待并处理验证码(多轮检测 + 重试)"""
    max_attempts = 4
    wait_per_round = 4.0

    for attempt in range(1, max_attempts + 1):
        logger.info(f"⏳ 第{attempt}/{max_attempts}轮:检测验证码或结果...")
        # 先检测再等待：多数签到无验证码，先睡 4 秒会让无验证码场景白白空转 ~17 秒
        has_captcha = await detect_slider_captcha(page)
        if not has_captcha:
            logger.info(f"ℹ️ 第{attempt}轮未检测到滑动验证码,等待 {wait_per_round}s 后继续观察...")
            await asyncio.sleep(wait_per_round + random.random())
            continue

        logger.info(f"🔐 第{attempt}轮检测到滑动验证码,开始处理...")
        if await handle_slider_captcha(page):
            logger.info("✅ 验证码验证成功")
            return True

        await asyncio.sleep(2.0 + random.random())

        if not await detect_slider_captcha(page):
            logger.info("✅ 验证码已消失,疑似验证通过")
            return True

        logger.info("🔄 验证失败,尝试刷新验证码并重新触发...")
        await asyncio.sleep(0.8 + random.random() * 0.4)

        try:
            await page.evaluate("""() => {
                const refreshBtn = document.querySelector('[class*="refresh"], [class*="reload"], [title*="刷新"]');
                if (refreshBtn) refreshBtn.click();
            }""")
            await asyncio.sleep(1.0 + random.random() * 0.5)
        except Exception:
            logger.info("ℹ️ 未找到刷新按钮,直接重新触发")

        if await page.locator("button#checkinBtn.ci-btn.renew").count() > 0:
            await page.evaluate("""() => {
                const button = document.querySelector('button#checkinBtn.ci-btn.renew');
                if (button) button.click();
            }""")
            logger.info("🖱️ 重新点击签到按钮(JavaScript 执行)")
            await asyncio.sleep(1.0 + random.random() * 0.5)

        logger.warning(f"⚠️ 第{attempt}轮处理后验证码仍存在,准备下一轮重试")

    logger.error("❌ 多轮检测后仍未通过验证码")
    return False


async def switch_to_chinese_if_needed(page) -> None:
    """尝试将页面切换为中文(点击右上角语言切换)"""
    lang_selectors = [
        "button.n5-chip.i18n-switch",
        ".i18n-switch",
        'button:has-text("中文")',
        'a:has-text("中文")',
        '[role="button"]:has-text("中文")',
        'button:has-text("ZH")',
        'a:has-text("ZH")',
        '[role="button"]:has-text("ZH")',
    ]
    for selector in lang_selectors:
        el = page.locator(selector).first
        if await el.count() > 0:
            try:
                await el.click(timeout=3000)
                await asyncio.sleep(1.5)
                logger.info(f"🌐 已尝试切换页面语言为中文: {selector}")
                return
            except Exception as e:
                logger.info(f"ℹ️ 语言切换点击失败({selector}): {e}")
    logger.info("ℹ️ 未找到语言切换按钮(中文/ZH),继续当前语言执行")


async def has_session_expired_hint(page) -> bool:
    """判断页面是否出现登录失效提示"""
    hints = ["未登录", "会话已过期", "登录已过期", "请先登录", "token 过期", "Token 过期"]
    try:
        page_text = await page.inner_text("body")
    except Exception:
        page_text = ""
    return any(h in page_text for h in hints)


async def is_logged_in(page) -> bool:
    """判断页面是否已进入有效登录态(多重信号)"""
    try:
        page_text = await page.inner_text("body")
    except Exception:
        page_text = ""

    # 1. 已绑定邮箱 / 退出登录(最可靠)
    if "已绑定:" in page_text or "退出登录" in page_text:
        logger.info('✅ 检测到"已绑定"或"退出登录",确认已登录')
        return True

    # 2. 可见的密码输入框 → 未登录（emoji 用 ℹ️，避免与密码框的 🔑 混淆）
    password_input = page.locator('input#renewKey[type="password"]')
    if await password_input.count() > 0:
        try:
            is_visible = await password_input.first.is_visible()
        except Exception:
            is_visible = False
        if is_visible:
            logger.info("ℹ️ 检测到可见的密码输入框,说明未登录")
            return False
        logger.info("ℹ️ 密码输入框存在但不可见,可能正在登录中")

    # 3. 可见的登录按钮 → 未登录
    login_button = page.locator('button:has-text("登录"), button:has-text("Login")')
    if await login_button.count() > 0:
        try:
            is_visible = await login_button.first.is_visible()
        except Exception:
            is_visible = False
        if is_visible:
            logger.info("ℹ️ 检测到可见的登录按钮,说明未登录")
            return False

    # 4. 签到按钮
    if await page.locator(
        'button:has-text("签到续期"), button:has-text("签到"), button#checkinBtn.ci-btn.renew'
    ).count() > 0:
        logger.info("✅ 检测到签到按钮,确认已登录")
        return True

    # 5. 今日已签到
    if await page.locator('button:has-text("今日已签到")').count() > 0:
        logger.info('✅ 检测到"今日已签到"按钮,确认已登录')
        return True

    # 6. 用户元素
    # 注意：不用宽匹配 [class*='user']——会命中 user-select 等无关样式导致误判已登录
    for selector in ["[class*='user-avatar']", "[class*='user-name']", "[class*='user-info']",
                     "[class*='avatar']", ".profile", "[data-user]"]:
        if await page.locator(selector).count() > 0:
            logger.info(f"✅ 检测到用户元素 {selector},可能已登录")
            return True

    # 7. 余额 / 使用次数
    if "余额" in page_text or "使用次数" in page_text or "剩余" in page_text:
        logger.info("✅ 检测到余额/使用次数信息,可能已登录")
        return True

    logger.info("ℹ️ 无法确定登录状态")
    return False


async def locate_renew_key_input(page):
    """分两级查找 API Key 输入框,返回 locator 或 None"""
    # 一级:精确选择器
    exact_selector = 'input#renewKey.ci-input[type="password"]'
    el = page.locator(exact_selector)
    if await el.count() > 0:
        try:
            is_visible = await el.first.is_visible()
        except Exception:
            is_visible = False
        tag_info = await el.first.evaluate("el => el.tagName + (el.className ? '.' + el.className.split(' ').join('.') : '')")
        logger.info(f"✅ 精确选择器找到输入框: {tag_info}")
        if is_visible:
            return el.first
        logger.info("⚠️ 精确选择器找到但不可见,尝试降级")

    # 二级:仅 id 选择器
    fallback_selector = "#renewKey"
    el2 = page.locator(fallback_selector)
    if await el2.count() > 0:
        try:
            is_visible2 = await el2.first.is_visible()
        except Exception:
            is_visible2 = False
        tag_info2 = await el2.first.evaluate("el => el.tagName + (el.className ? '.' + el.className.split(' ').join('.') : '')")
        logger.info(f"✅ 降级选择器 #renewKey 找到输入框: {tag_info2}, visible={is_visible2}")
        if is_visible2:
            return el2.first
        logger.info("⚠️ 降级选择器找到但不可见")

    return None


async def try_login_with_retry(page) -> bool:
    """填充 KEY 并点击登录(可重试,每轮统一等待)"""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        logger.info(f"🔄 第{attempt}/{max_attempts}次尝试登录...")

        # 每轮先尝试关闭干扰弹窗
        await dismiss_overlays(page)

        if await is_logged_in(page):
            logger.info("✅ 检测到已处于登录状态,跳过本次尝试")
            return True

        key_input = await locate_renew_key_input(page)
        if key_input is None:
            logger.info("❌ 未找到 API Key 输入框(两级选择器均未命中)")
            await asyncio.sleep(1.5)
            continue

        try:
            is_visible = await key_input.is_visible()
        except Exception:
            is_visible = False
        if not is_visible:
            logger.info("⚠️ 输入框不可见,尝试等待或刷新后重试")
            await asyncio.sleep(2.0)
            continue

        await key_input.click(timeout=5000)
        await key_input.fill("")
        await key_input.fill(GPTQTCOOL_KEY)

        typed_value = await key_input.input_value()
        if not typed_value:
            logger.info("❌ API Key 输入后输入框为空")
            await asyncio.sleep(1.5)
            continue
        if typed_value != GPTQTCOOL_KEY:
            if GPTQTCOOL_KEY.startswith(typed_value):
                logger.info(f"⚠️ API Key 输入校验:输入值({len(typed_value)}字符)是完整 key({len(GPTQTCOOL_KEY)}字符)的前缀,判断为前端截断,继续执行")
            else:
                logger.warning(f"❌ API Key 输入校验不一致:输入值({len(typed_value)}字符)='{typed_value[:8]}...' ≠ 期望值({len(GPTQTCOOL_KEY)}字符)='{GPTQTCOOL_KEY[:8]}...'")
                await asyncio.sleep(1.5)
                continue

        logger.info("✅ 已输入 API Key 到 #renewKey(并通过输入校验)")

        login_button_selectors = [
            'button:has-text("登录")',
            'button:has-text("Login")',
            'button[type="submit"]',
            'button:has-text("授权")',
            'button:has-text("确认")',
            'button:has-text("提交")',
        ]
        clicked = False
        for selector in login_button_selectors:
            button = page.locator(selector)
            if await button.count() > 0:
                try:
                    is_btn_visible = await button.first.is_visible()
                except Exception:
                    is_btn_visible = False
                if not is_btn_visible:
                    logger.info(f"ℹ️ 登录按钮({selector})存在但不可见,跳过")
                    continue
                try:
                    await button.first.click(timeout=5000)
                    logger.info(f"🖱️ 已点击登录按钮: {selector}")
                    clicked = True
                    break
                except Exception as e:
                    logger.error(f"点击登录按钮失败({selector}): {e}")

        if not clicked:
            logger.info("⚠️ 本轮未成功点击登录按钮")
            await asyncio.sleep(1.5)
            continue

        await asyncio.sleep(2.5)

        if await is_logged_in(page):
            logger.info("✅ 登录状态有效(检测到邮箱/退出登录/签到按钮)")
            return True

        if await has_session_expired_hint(page):
            logger.info('⚠️ 检测到"未登录/会话已过期"提示,准备重试登录')
            await asyncio.sleep(1.5)
            continue

        logger.info("⚠️ 登录后仍未进入有效状态,准备重试")
        await asyncio.sleep(1.5)

    return False


# ========== 缺口检测 ==========
def detect_gap_by_edge(screenshot: bytes, slider_width: int, save_debug_path: Path | None = None) -> tuple[int, float]:
    """基于 OpenCV 的缺口检测

    核心思路: 拼图缺口是一个矩形凹槽,其左右两侧各有一条明显的竖直边缘,
    且凹槽内部与背景在亮度/颜色上有明显差异。本算法:
    1. 用 Canny/Sobel 检测竖直边缘;
    2. 寻找左右成对、间距约等于拼图块宽度的边缘;
    3. 对每对边缘内部的区域进行亮度/颜色一致性评分;
    4. 返回最佳缺口左边缘。

    返回:
        gap_x: 缺口左边缘在截图坐标系中的 x 坐标
        confidence: 综合评分置信度
    """
    img = cv2.imdecode(np.frombuffer(screenshot, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        logger.error("❌ 无法解码截图")
        return 0, 0.0

    h, w = img.shape[:2]
    logger.info(f"📊 图像尺寸: {w}x{h},开始缺口检测...")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Step 1: Canny 边缘检测(对竖直边缘敏感)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)

    # Step 2: 计算每列的竖直边缘强度(只统计竖直方向的边缘)
    col_edges = edges.sum(axis=0) / max(1, h)

    # Step 3: 用 Sobel 水平梯度补充边缘信息
    grad = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    grad_abs = np.abs(grad)
    col_grad = grad_abs.sum(axis=0) / max(1, h)

    # Step 4: 搜索范围
    min_x = max(slider_width, int(w * 0.05))
    max_x = min(w - slider_width - 10, int(w * 0.95))

    # Step 5: 寻找缺口候选位置
    # 缺口左边缘应该是一个明显的竖直边缘,右边缘在左边缘 + slider_width 附近
    candidates = []
    for left in range(min_x, max_x - slider_width):
        right = left + slider_width
        if right >= w:
            continue

        # 左边缘强度
        left_edge = col_edges[left] + col_grad[left] * 0.5
        # 右边缘强度
        right_edge = col_edges[right] + col_grad[right] * 0.5

        # 缺口内部区域
        inner = gray[:, left:right]
        if inner.size == 0:
            continue

        # 缺口内部通常与背景不同:计算内部与左右邻域的亮度差异
        left_bg = gray[:, max(0, left - slider_width):left]
        right_bg = gray[:, right:min(w, right + slider_width)]

        inner_mean = float(inner.mean())
        left_bg_mean = float(left_bg.mean()) if left_bg.size > 0 else inner_mean
        right_bg_mean = float(right_bg.mean()) if right_bg.size > 0 else inner_mean

        # 亮度差异(缺口内部通常较亮或较暗)
        brightness_diff = abs(inner_mean - (left_bg_mean + right_bg_mean) / 2)

        # 内部一致性(缺口内部颜色应相对均匀)
        inner_std = float(inner.std())
        uniformity = 1.0 / (1.0 + inner_std / 50.0)

        # 综合评分
        score = (
            left_edge * 0.35
            + right_edge * 0.35
            + brightness_diff * 0.2
            + uniformity * 50.0 * 0.1
        )

        candidates.append({
            "x": left,
            "score": score,
            "left_edge": left_edge,
            "right_edge": right_edge,
            "brightness_diff": brightness_diff,
            "uniformity": uniformity,
        })

    if not candidates:
        logger.warning("⚠️ 未找到缺口候选")
        return min_x, 0.0

    # 按评分排序
    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]

    # 计算置信度: best score 与次优的差距
    scores = [c["score"] for c in candidates]
    mean_score = float(np.mean(scores))
    std_score = float(np.std(scores)) or 1.0
    confidence = (best["score"] - mean_score) / std_score

    gap_x = best["x"]

    logger.info(
        f"🎯 缺口左边缘: {gap_x}px (置信度: {confidence:.2f}, "
        f"左边缘={best['left_edge']:.1f}, 右边缘={best['right_edge']:.1f}, "
        f"亮度差={best['brightness_diff']:.1f})"
    )
    top_str = " | ".join(
        f"x={c['x']} score={c['score']:.1f}" for c in candidates[:3]
    )
    logger.info(f"🔍 Top 候选: {top_str}")

    # 保存带标记的调试图
    if save_debug_path:
        try:
            debug_img = img.copy()
            cv2.line(debug_img, (gap_x, 0), (gap_x, h), (0, 0, 255), 2)
            cv2.line(debug_img, (gap_x + slider_width, 0), (gap_x + slider_width, h), (0, 255, 0), 1)
            cv2.imwrite(str(save_debug_path), debug_img)
            logger.info(f"📸 已保存缺口检测调试图: {save_debug_path}")
        except Exception as e:
            logger.info(f"ℹ️ 保存调试图失败: {e}")

    return gap_x, max(0.0, confidence)


def detect_gap_by_template(
    master: bytes,
    tile: bytes,
    master_rendered_size: tuple[float, float] | None = None,
    tile_rendered_size: tuple[float, float] | None = None,
) -> tuple[int, float, str] | None:
    """OpenCV 模板匹配(原生,无子进程)

    用 TM_CCOEFF_NORMED 定位拼图块图案在背景图中的位置。
    若提供了渲染尺寸,会先将 tile 缩放到与 master 同一比例,避免原始图与截图尺寸不一致导致误匹配。
    返回 (gap_x, confidence, method_name)。
    """
    master_img = cv2.imdecode(np.frombuffer(master, dtype=np.uint8), cv2.IMREAD_COLOR)
    tile_img = cv2.imdecode(np.frombuffer(tile, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if master_img is None or tile_img is None:
        logger.info("❌ 模板匹配:无法解码图像")
        return None

    # alpha 通道转 3 通道
    if len(tile_img.shape) == 3 and tile_img.shape[2] == 4:
        tile_img = cv2.cvtColor(tile_img, cv2.COLOR_BGRA2BGR)

    mh, mw = master_img.shape[:2]
    th, tw = tile_img.shape[:2]

    # 按渲染尺寸比例缩放 tile,使模板与 master 处于同一坐标系
    if master_rendered_size and tile_rendered_size:
        mr_w, mr_h = master_rendered_size
        tr_w, tr_h = tile_rendered_size
        if mr_w > 0 and tr_w > 0:
            scale_x = mr_w / mw
            scale_y = mr_h / mh
            new_tw = int(tw * scale_x)
            new_th = int(th * scale_y)
            if 5 < new_tw < mw and 5 < new_th < mh:
                tile_img = cv2.resize(tile_img, (new_tw, new_th), interpolation=cv2.INTER_AREA)
                th, tw = tile_img.shape[:2]
                logger.info(f"🔍 模板匹配: tile 缩放至 {tw}x{th} 以匹配 master 渲染比例")

    if th > mh or tw > mw:
        logger.info(f"❌ 模板匹配:tile ({tw}x{th}) 大于 master ({mw}x{mh})")
        return None

    best_x, best_conf, best_method = 0, -1.0, ""
    # 只用 TM_CCOEFF_NORMED:该站点背景是海水纹理,CCORR 在亮水面处普遍 0.91+,
    # 置信度失去区分度(实测三轮假阳性全落在同一片亮水面);CCOEFF 扣除均值,
    # 无真匹配时分数会诚实回落到 0.3 附近
    result = cv2.matchTemplate(master_img, tile_img, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    best_conf = round(float(max_val), 4)
    # 左边缘语义：匹配窗口左上角 = 图案左边缘（与边缘检测分支的 gap_x 语义一致）
    best_x = int(max_loc[0])
    best_method = "TM_CCOEFF_NORMED"

    logger.info(f"🔍 模板匹配: gapX={best_x}, confidence={best_conf}, method={best_method}")
    return best_x, best_conf, best_method


# 高亮方块采信阈值:窗口与周边亮度差超过该值才认为存在白色半透明方块
# (实测该站点方块得分 38~45,海水纹理/阳光亮斑本底 < 20,取中间偏下值)
HIGHLIGHT_MIN_CONF = 25.0


def detect_gap_by_highlight(
    master: bytes,
    tile_size: int,
    prior_y: float | None = None,
    bg_render_h: float | None = None,
    tile_rect: dict | None = None,
    bg_rect: dict | None = None,
) -> tuple[int, int, float]:
    """高亮方块型缺口检测(该站点验证码形态)

    站点的缺口不是"拼图内容挖洞"而是白色半透明方块直接标注在背景图上,
    拼图块图案与背景图内容无对应关系(线性回归 R²≈0.1),模板匹配必然失效。
    检测原理:方块区域亮度显著高于周边海面,用拼图块尺寸滑窗计算
    "窗口均值 - 环形周边均值",峰值位置即方块左上角(背景图自然坐标系)。

    参数:
        prior_y: 拼图块 DOM 中心 y(渲染坐标系)。实测方块与拼图块同行,
                 提供时按 bg_render_h/natural_h 比例换算后把扫描带限定在
                 该 y 附近,抑制远处阳光亮斑干扰。
        bg_render_h: 背景图渲染高度,用于 prior_y 的坐标换算。
        tile_rect / bg_rect: 拼图块与背景图的 DOM 渲染位置。拼图块预览
                 叠加在背景图左侧(非背景内容),映射到自然坐标系后排除,
                 否则拼图块预览自身/其涂黑副本会被误判为高亮区。

    返回 (gap_x, gap_y, score);score < HIGHLIGHT_MIN_CONF 时上层应回退其他方法。
    """
    img = cv2.imdecode(np.frombuffer(master, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        logger.info("❌ 高亮检测:无法解码背景图")
        return 0, 0, 0.0

    h, w = img.shape[:2]
    size = min(tile_size, w - 1, h - 1)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)

    # 拼图块预览区映射到自然坐标系(渲染坐标 - 背景图原点)×缩放
    exclude_x1 = 0.0
    if tile_rect and bg_rect:
        scale = w / max(1.0, bg_rect["width"])
        exclude_x1 = (tile_rect["x"] - bg_rect["x"] + tile_rect["width"]) * scale

    # 积分图加速:任意矩形区域均值 O(1),滑窗+环形周边全图扫描无需逐像素循环
    integral = cv2.integral(gray)  # (h+1, w+1)

    def rect_mean(x0: int, y0: int, x1: int, y1: int) -> float:
        """积分图求 [y0,y1) x [x0,x1) 区域均值,坐标越界自动裁剪"""
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        s = integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
        return float(s) / ((y1 - y0) * (x1 - x0))

    # y 扫描带:有先验时限定换算后的拼图块中心 y ±size,无先验时全图扫描
    if prior_y is not None and bg_render_h:
        py = prior_y * h / bg_render_h
        y_min = max(0, int(py - size))
        y_max = min(h - size, int(py + size))
    else:
        y_min, y_max = 0, h - size
    if y_max < y_min:
        y_min, y_max = 0, max(0, h - size)

    ring = max(6, size // 4)  # 环形周边厚度
    # x 从 size 起扫:方块不会贴到背景图左缘,且左侧边缘的环形数据不完整;
    # 有拼图块预览时再右移一个"窗口+环"宽度——紧贴预览区的窗口其环形左带
    # 会覆盖预览区(亮度异常源),把 ring_mean 拉低产生虚高分
    x_min = max(size, int(exclude_x1) + size + ring)
    best_score, best_x, best_y = -1e9, 0, 0
    for y in range(y_min, y_max + 1):
        for x in range(x_min, w - size + 1):
            win = rect_mean(x, y, x + size, y + size)
            # 环形均值 = 大矩形均值按面积扣掉窗口,而非四条带直接平均——
            # 边角处部分条带越界为空,直接平均会被 0 稀释造成边角假高分
            bx0, by0 = x - ring, y - ring
            bx1, by1 = x + size + ring, y + size + ring
            big = rect_mean(bx0, by0, bx1, by1)
            big_area = (min(h, by1) - max(0, by0)) * (min(w, bx1) - max(0, bx0))
            ring_area = big_area - size * size
            ring_mean = (big * big_area - win * size * size) / max(1, ring_area)
            score = win - ring_mean
            if score > best_score:
                best_score, best_x, best_y = score, x, y

    logger.info(f"🔍 高亮方块检测: x={best_x}, y={best_y}, score={best_score:.1f}")
    return best_x, best_y, best_score


async def detect_gap(
    screenshot: bytes,
    slider_width: int,
    tile_screenshot: bytes | None = None,
    master_rendered_size: tuple[float, float] | None = None,
    tile_rendered_size: tuple[float, float] | None = None,
    save_debug_path: Path | None = None,
    tile_rect: dict | None = None,
    bg_rect: dict | None = None,
) -> tuple[int, float, str]:
    """四层递进缺口检测: 高亮方块 → 模板匹配 → 边缘检测 → 降级

    优先使用高亮方块检测:该站点验证码形态是白色半透明方块标注缺口,
    拼图块图案与背景图无内容对应,模板匹配在此形态下必然漂移。
    拼图内容型验证码(高亮检测低分)回退到模板匹配,边缘检测作为最后备选。
    """
    # 策略 0: 高亮方块检测(需要背景图与拼图块渲染位置)
    if tile_rect and bg_rect:
        hl_x, hl_y, hl_score = detect_gap_by_highlight(
            screenshot,
            int(tile_rect["width"]) or 52,
            prior_y=(tile_rect["y"] + tile_rect["height"] / 2) if tile_rect else None,
            bg_render_h=bg_rect["height"],
            tile_rect=tile_rect,
            bg_rect=bg_rect,
        )
        if hl_score >= HIGHLIGHT_MIN_CONF:
            logger.info(f"✅ 高亮方块命中: gap_x={hl_x}, y={hl_y}, score={hl_score:.1f}")
            if save_debug_path:
                try:
                    img = cv2.imdecode(np.frombuffer(screenshot, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        h = img.shape[0]
                        cv2.line(img, (hl_x, 0), (hl_x, h), (0, 0, 255), 2)
                        cv2.imwrite(str(save_debug_path), img)
                        logger.info(f"📸 已保存缺口检测调试图: {save_debug_path}")
                except Exception as e:
                    logger.info(f"ℹ️ 保存调试图失败: {e}")
            return hl_x, hl_score, "highlight"
        logger.info(
            f"⚠️ 高亮方块检测低分 ({hl_score:.1f} < {HIGHLIGHT_MIN_CONF}),"
            f"判定为拼图内容型验证码,回退模板匹配"
        )

    # 策略 1: 模板匹配(有拼图块截图时)
    if tile_screenshot:
        logger.info("🔍 尝试模板匹配...")
        # ★ 防自匹配：拼图块图案与缺口图案相同，若不涂黑拼图块自身区域，
        # 匹配分数最高点必然是拼图块自身位置（gap_x 返回拼图块位置导致拖动错位）。
        # 按 tile_rect/bg_rect 把拼图块在背景图中的区域涂黑后再匹配。
        # masked_region 记录涂黑区（master 坐标系），既用于涂黑也用于"漂移拒绝"约束
        masked_region: tuple[int, int, float] | None = None
        master_for_match = screenshot
        if tile_rect and bg_rect:
            try:
                img = cv2.imdecode(np.frombuffer(screenshot, dtype=np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    mh, mw = img.shape[:2]
                    tile_x = (tile_rect["x"] - bg_rect["x"]) * mw / max(1.0, bg_rect["width"])
                    tile_y = (tile_rect["y"] - bg_rect["y"]) * mh / max(1.0, bg_rect["height"])
                    tile_w = tile_rect["width"] * mw / max(1.0, bg_rect["width"])
                    tile_h = tile_rect["height"] * mh / max(1.0, bg_rect["height"])
                    pad = max(4, int(tile_w * 0.15))
                    x0 = max(0, int(tile_x) - pad)
                    y0 = max(0, int(tile_y) - pad)
                    x1 = min(mw, int(tile_x + tile_w) + pad)
                    y1 = min(mh, int(tile_y + tile_h) + pad)
                    masked_region = (x0, x1, tile_w)
                    cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), -1)
                    ok, buf = cv2.imencode(".png", img)
                    if ok:
                        master_for_match = buf.tobytes()
                        logger.info(f"🔍 已涂黑拼图块区域 (x:{x0}-{x1}, y:{y0}-{y1}) 防止自匹配")
            except Exception as e:
                logger.info(f"ℹ️ 涂黑拼图块区域失败: {e}（跳过，仍用原图匹配）")

        tm = detect_gap_by_template(
            master_for_match, tile_screenshot, master_rendered_size, tile_rendered_size
        )
        if tm:
            tm_x, tm_conf, tm_method = tm
            min_conf = 0.85
            # 漂移拒绝：匹配窗口若与拼图块区域（涂黑区）重叠，说明命中的是拼图块
            # 自身附近而非真缺口（涂黑区边缘的亮度突变会产生虚假高响应），拒绝采信回退边缘检测
            drifted = False
            if masked_region:
                mx0, mx1, mtw = masked_region
                if tm_x + mtw > mx0 and tm_x < mx1 + mtw:
                    drifted = True
                    logger.info(
                        f"⚠️ 模板匹配窗口({tm_x}-{tm_x + mtw:.0f})与拼图块区域"
                        f"({mx0}-{mx1})重叠,判定漂移,拒绝采信"
                    )
            if tm_conf >= min_conf and not drifted:
                logger.info(f"✅ 模板匹配成功: gapX={tm_x}, 置信度={tm_conf:.2f}, method={tm_method}")
                # 用模板匹配结果更新调试图
                if save_debug_path:
                    try:
                        img = cv2.imdecode(np.frombuffer(screenshot, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            h = img.shape[0]
                            cv2.line(img, (tm_x, 0), (tm_x, h), (0, 0, 255), 2)
                            cv2.imwrite(str(save_debug_path), img)
                            logger.info(f"📸 已保存缺口检测调试图: {save_debug_path}")
                    except Exception as e:
                        logger.info(f"ℹ️ 保存调试图失败: {e}")
                return tm_x, tm_conf, "template"
            logger.info(
                f"⚠️ 模板匹配未采信 (conf={tm_conf:.2f} < {min_conf} 或位置漂移),回退到边缘检测"
            )

    # 策略 2: 边缘检测
    gap_x, conf = detect_gap_by_edge(screenshot, slider_width, save_debug_path)
    if conf >= 1.5:
        logger.info(f"✅ 边缘检测置信度足够 ({conf:.1f}),直接使用")
        return gap_x, conf, "edge"

    # 策略 3: 降级返回边缘检测结果 + 低置信度
    logger.info("⚠️ 所有检测方法置信度均低,使用边缘检测结果 + 随机扰动")
    return gap_x, conf, "edge_lowconf"


async def extract_captcha_images(page, container_box: dict | None = None) -> dict:
    """从 DOM 提取拼图块和背景图(base64 data URL → bytes)

    同时返回渲染位置/尺寸(rendered_rect)和原始尺寸(natural_width/height),
    便于后续将检测坐标从原始图坐标系换算到屏幕坐标系。
    """
    result: dict = {
        "tile": None,
        "background": None,
        "tile_rect": None,
        "tile_natural": None,
        "bg_rect": None,
        "bg_natural": None,
    }

    # 策略 1: DOM 拼图块 base64
    # 拼图块通常比背景图小,且可能是 data:image/png(带 alpha)或 data:image/jpeg
    try:
        tile_data = await page.evaluate("""() => {
            // 先找所有 data:image 图片,排除尺寸最大的(通常是背景图)
            const allImgs = Array.from(document.querySelectorAll('img[src*="data:image"]'));
            const candidates = allImgs
                .filter(el => el.src && el.src.startsWith('data:image'))
                .map(el => ({
                    el,
                    area: el.naturalWidth * el.naturalHeight,
                    rect: el.getBoundingClientRect(),
                }))
                .filter(item => item.area > 100);  // 过滤极小图标
            if (candidates.length === 0) return null;
            // 按面积从小到大排序,优先取最小的(最可能是拼图块)
            candidates.sort((a, b) => a.area - b.area);
            // 若最小面积仍大于 30000,可能是单图验证码,放弃
            if (candidates[0].area > 30000) return null;
            const el = candidates[0].el;
            const rect = candidates[0].rect;
            return {
                src: el.src,
                naturalWidth: el.naturalWidth,
                naturalHeight: el.naturalHeight,
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
            };
        }""")
        if tile_data and tile_data.get("src"):
            b64 = tile_data["src"].split(",", 1)[-1]
            result["tile"] = base64.b64decode(b64)
            result["tile_natural"] = (tile_data["naturalWidth"], tile_data["naturalHeight"])
            result["tile_rect"] = {
                "x": tile_data["x"],
                "y": tile_data["y"],
                "width": tile_data["width"],
                "height": tile_data["height"],
            }
            logger.info(
                f"✅ DOM 提取拼图块: 原始 {tile_data['naturalWidth']}x{tile_data['naturalHeight']}, "
                f"渲染 {tile_data['width']:.0f}x{tile_data['height']:.0f} @({tile_data['x']:.0f},{tile_data['y']:.0f})"
            )
    except Exception:
        pass

    # 策略 2: DOM 背景图(排除已用作拼图块的元素)
    try:
        bg_data = await page.evaluate("""() => {
            const allImgs = Array.from(document.querySelectorAll('img[src*="data:image"]'));
            const candidates = allImgs
                .filter(el => el.src && el.src.startsWith('data:image'))
                .map(el => ({
                    el,
                    area: el.naturalWidth * el.naturalHeight,
                    rect: el.getBoundingClientRect(),
                }))
                .filter(item => item.area > 100)
                .sort((a, b) => b.area - a.area);  // 背景图通常最大
            if (candidates.length === 0) return null;
            const el = candidates[0].el;
            const rect = candidates[0].rect;
            return {
                src: el.src,
                naturalWidth: el.naturalWidth,
                naturalHeight: el.naturalHeight,
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
            };
        }""")
        if bg_data and bg_data.get("src"):
            if bg_data["src"].startswith("data:"):
                b64 = bg_data["src"].split(",", 1)[-1]
                result["background"] = base64.b64decode(b64)
                result["bg_natural"] = (bg_data["naturalWidth"], bg_data["naturalHeight"])
                result["bg_rect"] = {
                    "x": bg_data["x"],
                    "y": bg_data["y"],
                    "width": bg_data["width"],
                    "height": bg_data["height"],
                }
                logger.info(
                    f"✅ DOM 提取背景图: 原始 {bg_data['naturalWidth']}x{bg_data['naturalHeight']}, "
                    f"渲染 {bg_data['width']:.0f}x{bg_data['height']:.0f} @({bg_data['x']:.0f},{bg_data['y']:.0f})"
                )
            else:
                logger.info("ℹ️ 背景图是 http URL,走截图方式")
    except Exception:
        pass

    # 策略 3: 截图兜底
    if not result["tile"]:
        logger.info("🔍 截图方式提取拼图块...")
        for selector in [
            'div[class*="block"] img',
            'div[class*="tile"] img',
            'div[class*="puzzle"] img',
            'div[class*="slice"] img',
            'div[class*="graph"] img',
        ]:
            el = page.locator(selector).first
            if await el.count() > 0:
                try:
                    box = await el.bounding_box()
                except Exception:
                    box = None
                if box and box["width"] > 10 and box["height"] > 10:
                    result["tile"] = await page.screenshot(clip=box)
                    result["tile_rect"] = box
                    logger.info(
                        f"✅ 截图提取拼图块: {selector} ({box['width']:.0f}x{box['height']:.0f})"
                    )
                    break

    # 策略 4: 容器截图作为背景兜底
    if not result["background"] and container_box:
        try:
            result["background"] = await page.screenshot(clip=container_box)
            result["bg_rect"] = container_box
            logger.info(
                f"✅ 容器截图作为背景图: {container_box['width']:.0f}x{container_box['height']:.0f}"
            )
        except Exception:
            pass

    return result


# ========== 滑块操作 ==========
def generate_human_track(distance: float) -> list[dict]:
    """生成拟人化滑动轨迹

    特点:
    - 总时长 0.4~1.2 秒,避免过长被识别为机器人
    - 先加速后减速,符合人类拖动习惯
    - 轨迹点数量适中(10~25 个),每个点间隔不均匀
    - 带小幅 Y 轴抖动和随机回退
    - 末段有轻微过冲/回调
    """
    tracks: list[dict] = []
    current = 0.0

    # 总时长随距离变化: 近距离快,远距离稍慢
    total_time = 400 + random.random() * 400 + distance * 1.5
    total_time = max(300, min(1200, total_time))

    # 轨迹点数量
    num_points = max(10, min(25, int(distance / 8)))

    # 生成每个点的进度(0~1),使用幂函数模拟加速-减速
    for i in range(1, num_points + 1):
        # 幂函数: 先快后慢
        t = i / num_points
        progress = t ** (0.7 + random.random() * 0.4)
        target = progress * distance

        step = target - current
        if abs(step) < 0.5:
            continue

        # 随机回退(10% 概率,末段前)
        if i < num_points - 2 and random.random() < 0.1:
            step -= random.random() * min(5, step * 0.3)

        # 小幅 Y 轴抖动
        y_offset = (random.random() - 0.5) * (2 + random.random() * 2)

        # 该步耗时,基于进度: 起始快,末段慢
        if progress < 0.3:
            delay = (total_time / num_points) * (0.5 + random.random() * 0.4)
        elif progress < 0.8:
            delay = (total_time / num_points) * (0.8 + random.random() * 0.6)
        else:
            delay = (total_time / num_points) * (1.2 + random.random() * 1.0)

        delay = max(8, delay + (random.random() - 0.5) * 10)

        tracks.append({
            "x": step,
            "y": y_offset,
            "delay": delay,
        })
        current += step

    # 末段补齐
    if abs(distance - current) > 0.5:
        tracks.append({
            "x": distance - current,
            "y": (random.random() - 0.5) * 1.5,
            "delay": 30 + random.random() * 50,
        })
        current = distance

    # 最终微调(过冲/回调)
    if random.random() < 0.7:
        final_adjust = (random.random() - 0.5) * 3
        if abs(final_adjust) > 0.3:
            tracks.append({
                "x": final_adjust,
                "y": (random.random() - 0.5) * 1,
                "delay": 50 + random.random() * 80,
            })

    return tracks


async def find_slider_handle(page):
    """查找滑块元素 - 4 策略(CSS选择器 / 箭头文本 / DOM遍历 / iframe)"""
    # 策略 1: 精确 CSS 选择器
    for selector in [
        'div[class*="slider"] div[class*="block"]',
        'div[class*="slider"] .handler',
        'div[class*="handler"]',
        ".slider-btn",
        ".slider-block",
        ".captcha-slider div",
        'div[class*="slider"] div[class*="btn"]',
        'div[class*="slider"] div[class*="handle"]',
        'div[class*="slide"] div[class*="block"]',
        'div[class*="slide"] div[class*="btn"]',
        'div[class*="verify"] div[class*="block"]',
        'div[class*="verify"] div[class*="btn"]',
    ]:
        el = page.locator(selector)
        if await el.count() > 0:
            logger.info(f"✅ 使用选择器找到滑块: {selector}")
            return el.first

    # 策略 2: 箭头字符匹配
    arrow_re = re.compile(r"^[>›▶→]$")
    arrow_selector = page.locator("div").filter(has_text=arrow_re)
    if await arrow_selector.count() > 0:
        logger.info("✅ 使用箭头文本找到滑块")
        return arrow_selector.first

    # 策略 3: 遍历 DOM 找箭头字符
    logger.info("🔍 遍历 DOM 查找箭头字符...")
    all_divs = await page.locator("div").element_handles()
    for div in all_divs:
        try:
            text = await div.text_content()
        except Exception:
            text = ""
        if text and text.strip() in (">", "›", "▶", "→"):
            try:
                is_visible = await div.is_visible()
            except Exception:
                is_visible = False
            if is_visible:
                logger.info("✅ 遍历找到滑块")
                return div

    # 策略 4: iframe 中查找
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            frame_slider = frame.locator(
                'div[class*="slider"], div[class*="handler"], div[class*="block"], .geetest, .captcha-slider'
            )
            if await frame_slider.count() > 0:
                logger.info("✅ 在 iframe 中找到滑块")
                return frame_slider.first
        except Exception:
            continue

    return None


async def _resolve_slider_box(page, slider_handle):
    """取滑块当前 bounding_box；locator 失效时重新查找。

    滑动失败后验证码常会刷新 DOM（箭头 div 被移除/文本变化），惰性 locator
    的 bounding_box() 默认会阻塞等 30s 超时，把整个重试循环卡死（日志中
    "Timeout 30000ms exceeded" 即此问题）。这里用 2s 短超时，失败立即重新
    定位滑块，返回 (滑块 locator, bounding_box)；彻底失败返回 (None, None)。
    """
    if slider_handle is not None:
        try:
            box = await slider_handle.bounding_box(timeout=2000)
            if box:
                return slider_handle, box
        except Exception:
            pass
    # 原 locator 失效，重新查找
    try:
        fresh = await find_slider_handle(page)
        if fresh is not None:
            box = await fresh.bounding_box(timeout=2000)
            if box:
                return fresh, box
    except Exception:
        pass
    return None, None


async def perform_slide(page, handle_box: dict, slide_distance: float) -> bool:
    """执行拟人化滑动操作"""
    if not handle_box:
        logger.info("⚠️ 滑块 bounding_box 为空,跳过本次滑动")
        return False
    start_x = handle_box["x"] + handle_box["width"] / 2
    start_y = handle_box["y"] + handle_box["height"] / 2 + (random.random() - 0.5) * 3
    end_x = start_x + slide_distance

    logger.info(
        f"🚀 开始滑动:从 ({start_x:.1f}, {start_y:.1f}) 到 ({end_x:.1f}, {start_y:.1f})"
    )

    # 初始停顿
    await asyncio.sleep(0.1 + random.random() * 0.2)

    # 移动鼠标到滑块(带小幅随机)
    await page.mouse.move(
        start_x + (random.random() - 0.5) * 2,
        start_y + (random.random() - 0.5) * 2,
        steps=3 + random.randint(0, 3),
    )
    await asyncio.sleep(0.05 + random.random() * 0.1)

    # 按下
    await page.mouse.down()
    await asyncio.sleep(0.05 + random.random() * 0.08)

    tracks = generate_human_track(slide_distance)
    current_x, current_y = start_x, start_y
    logger.info(f"👤 生成了 {len(tracks)} 个轨迹点,开始滑动...")

    for track in tracks:
        current_x += track["x"]
        current_y += track["y"]
        await page.mouse.move(
            current_x, current_y,
            steps=1 + random.randint(0, 2)
        )
        await asyncio.sleep(track["delay"] / 1000.0)

    # 释放前微调
    final_jitter_x = (random.random() - 0.5) * 2
    final_jitter_y = (random.random() - 0.5) * 2
    await page.mouse.move(
        end_x + final_jitter_x,
        start_y + final_jitter_y,
        steps=2 + random.randint(0, 2),
    )
    await asyncio.sleep(0.1 + random.random() * 0.2)

    # 释放
    await page.mouse.up()

    logger.info("✅ 滑块拖动完成,等待验证结果...")
    await asyncio.sleep(1.5 + random.random() * 1.0)

    if not await detect_slider_captcha(page):
        logger.info("✅ 验证码已消失,验证成功")
        return True

    logger.info("⏳ 验证码仍存在,等待动画完成...")
    await asyncio.sleep(2.0 + random.random() * 1.0)
    if not await detect_slider_captcha(page):
        logger.info("✅ 验证码动画完成后消失,验证成功")
        return True

    logger.info("⏳ 等待滑块归位...")
    await asyncio.sleep(1.5 + random.random() * 0.5)
    if not await detect_slider_captcha(page):
        logger.info("✅ 滑块归位后验证码消失,验证成功")
        return True

    logger.info("❌ 验证码仍然存在,验证失败")
    return False


async def slide_with_retry(page, handle_box: dict, container_box) -> bool:
    """带重试的宽范围滑动(找不到容器或置信度极低时降级)"""
    max_width = (container_box["width"] - handle_box["width"] - 15) if container_box else 300
    distances: list[float] = []
    d = 40.0
    while d < max_width:
        distances.append(d)
        d += 20 + random.randint(0, 14)

    for attempt in range(3):
        if not distances:
            break
        idx = random.randint(0, len(distances) - 1)
        dist = distances.pop(idx)
        logger.info(f"🔄 [阶段 B] 尝试 #{attempt + 1}: 滑动距离={dist:.0f}px")
        if await perform_slide(page, handle_box, dist):
            return True
        await asyncio.sleep(1.0 + random.random())

    logger.info("❌ [阶段 B] 所有尝试均失败")
    return False


async def handle_slider_captcha(page) -> bool:
    """处理滑动验证码 - 以屏幕截图坐标系为基准,高密度微调试

    关键处理:
    1. 始终优先使用 page.screenshot(clip=container_box) 作为缺口检测输入,
       保证检测坐标与页面渲染 1:1。
    2. 若只能拿到 DOM 原始背景图,则按 natural/rendered 比例换算 gap_x。
    3. 检测完成后保存背景图、拼图块、带标记的检测图,便于定位问题。
    4. 主尝试失败后,在 gap_x ±10px 范围内以 2px 步长高密度微调。
    """
    try:
        await dismiss_overlays(page)

        # 连续滑块重定位失败计数: 该站点首次滑动失败后会隐藏滑块(切到"人机验证"),
        # 此时多枪连发无意义。连续 2 次失败即判定滑块已消失,提前放弃让外层刷新
        # 验证码重试——实测"刷新重来"远比空转跑完所有偏移更快更稳
        miss_streak = 0

        logger.info("🔍 查找滑块验证码...")
        await asyncio.sleep(1.5 + random.random())

        # 1. 找滑块
        slider_handle = await find_slider_handle(page)
        if not slider_handle:
            logger.info("❌ 未找到滑块元素")
            return False
        try:
            is_visible = await slider_handle.is_visible()
        except Exception:
            is_visible = False
        if not is_visible:
            logger.info("❌ 滑块不可见")
            return False

        handle_box = await slider_handle.bounding_box()
        if not handle_box:
            logger.info("❌ 无法获取滑块位置")
            return False
        logger.info(
            f"📍 滑块: x={handle_box['x']:.0f} y={handle_box['y']:.0f} "
            f"w={handle_box['width']:.0f} h={handle_box['height']:.0f}"
        )

        # 2. 找验证码容器(向上 3 层)
        container_box = None
        for level in range(1, 4):
            el = slider_handle
            for _ in range(level):
                el = el.locator("..")
            try:
                container_box = await el.bounding_box()
            except Exception:
                container_box = None
            if container_box:
                break

        if not container_box:
            logger.info("⚠️ 无法定位验证码容器,使用宽范围滑动")
            return await slide_with_retry(page, handle_box, None)
        logger.info(
            f"🖼️ 容器: x={container_box['x']:.0f} y={container_box['y']:.0f} "
            f"w={container_box['width']:.0f} h={container_box['height']:.0f}"
        )

        # 3. 提取图片,同时拿到渲染位置/尺寸和原始尺寸
        captcha_images = await extract_captcha_images(page, container_box)
        ts = int(time.time())

        # 保存拼图块调试图
        if captcha_images.get("tile"):
            tile_debug_path = GPTQTCOOL_SCREENSHOT_DIR / f"captcha_tile_{ts}.png"
            try:
                with open(tile_debug_path, "wb") as f:
                    f.write(captcha_images["tile"])
                logger.info(f"📸 已保存拼图块调试图: {tile_debug_path}")
            except Exception as e:
                logger.info(f"ℹ️ 保存拼图块调试图失败: {e}")

        # 优先使用 DOM 提取的完整背景图作为检测源和坐标基准
        if captcha_images.get("background") and captcha_images.get("bg_rect"):
            bg_rect = captcha_images["bg_rect"]
            bg_for_detection = captcha_images["background"]
            bg_rendered_size = (bg_rect["width"], bg_rect["height"])
            logger.info(
                f"🖼️ 使用 DOM 背景图作为检测源: {bg_rect['width']:.0f}x{bg_rect['height']:.0f} "
                f"@({bg_rect['x']:.0f},{bg_rect['y']:.0f})"
            )
        else:
            # 回退: 使用容器截图
            bg_rect = container_box
            bg_for_detection = await page.screenshot(clip=container_box)
            bg_rendered_size = (container_box["width"], container_box["height"])
            logger.info("⚠️ 未提取到 DOM 背景图,回退使用容器截图")
        # 若 DOM 背景图为原始图且渲染尺寸与原始尺寸不一致,记录缩放比例
        # scale_x 提升为局部变量（默认 1.0）：缺口检测返回自然坐标系坐标，
        # 渲染存在缩放时必须换算成渲染坐标再与屏幕坐标混算
        scale_x = 1.0
        if captcha_images.get("background") and captcha_images.get("bg_rect"):
            rendered_w, rendered_h = bg_rect["width"], bg_rect["height"]
            natural_w, natural_h = captcha_images["bg_natural"] or (rendered_w, rendered_h)
            if natural_w and rendered_w and abs(rendered_w - natural_w) > 1:
                scale_x = rendered_w / natural_w
                logger.info(
                    f"🔍 背景图原始/渲染缩放: natural={natural_w}x{natural_h}, "
                    f"rendered={rendered_w:.0f}x{rendered_h:.0f}, scale_x={scale_x:.3f}"
                )

        # 检测缺口(背景图坐标系)
        detect_debug_path = GPTQTCOOL_SCREENSHOT_DIR / f"captcha_detect_{ts}.png"
        # 缺口宽 = 拼图块宽（实验证明传滑块宽会偏 53px：缺口实际与拼图块等宽）
        slider_width = (
            int(captcha_images["tile_rect"]["width"])
            if captcha_images.get("tile_rect") else int(handle_box["width"])
        )
        gap_x, conf, method = await detect_gap(
            bg_for_detection,
            slider_width,
            captcha_images["tile"],
            master_rendered_size=bg_rendered_size,
            tile_rendered_size=(
                (captcha_images["tile_rect"]["width"], captcha_images["tile_rect"]["height"])
                if captcha_images.get("tile_rect") else None
            ),
            save_debug_path=detect_debug_path,
            tile_rect=captcha_images.get("tile_rect"),
            bg_rect=bg_rect,
        )

        # 4. 基准滑动距离
        # gap_x 是缺口左边缘在背景图自然坐标系中的 x 坐标，
        # DOM 渲染存在缩放时换算为渲染坐标，再与屏幕坐标混算
        # 滑动距离 = 背景图左 + 缺口左(渲染坐标) - 滑块左
        gap_x_rendered = gap_x * scale_x
        base_dist = bg_rect["x"] + gap_x_rendered - handle_box["x"]
        max_dist = bg_rect["width"] - handle_box["width"] - 10

        def clamp(d: float) -> float:
            return max(5, min(max_dist, d))

        logger.info(
            f"📏 gap_x={gap_x}px, 背景图左={bg_rect['x']:.0f}px, "
            f"滑块左={handle_box['x']:.0f}px, 基准距离={base_dist:.0f}px, "
            f"method={method}, confidence={conf:.2f}"
        )

        # 5. 多枪连发: 固定偏移（覆盖检测残余误差）
        # highlight 实测误差 ±1px,小偏移足矣;template 保留中等扫描;
        # edge 系误差大,维持宽扫描
        offsets = [0, 4, -4, 8, -8, 12, -12, 16, -16, 20, -20, 24, -24]
        max_offset = 8 if method == "highlight" else (12 if method == "template" else (20 if method == "edge" else 24))
        valid_offsets = [o for o in offsets if abs(o) <= max_offset]
        logger.info(
            f"🎯 第一阶段: 最大偏移={max_offset}px, 尝试 {len(valid_offsets)} 次"
        )

        for offset in valid_offsets:
            # 每次重新解析滑块坐标：滑动失败后 DOM 可能刷新，旧 locator 会卡 30s 超时
            fresh_loc, fresh_box = await _resolve_slider_box(page, slider_handle)
            if not fresh_box:
                miss_streak += 1
                if miss_streak >= 2:
                    logger.info("⚠️ 滑块已消失(连续重定位失败),放弃当前验证码,等待刷新重试")
                    return False
                logger.info(f"ℹ️ offset={offset:+d}: 滑块重新定位失败,跳过")
                continue
            miss_streak = 0
            slider_handle = fresh_loc or slider_handle
            dist = clamp(bg_rect["x"] + gap_x_rendered - fresh_box["x"] + offset)

            logger.info(f"🔄 尝试 offset={offset:+d}px → 滑动距离={dist:.0f}px")
            if await perform_slide(page, fresh_box, dist):
                logger.info(f"✅ offset={offset}px 命中!")
                return True
            await asyncio.sleep(0.6 + random.random() * 0.4)

        logger.info("❌ 第一阶段缺口偏移尝试均失败")

        # 6. 高密度微调试: gap_x ±10px,步长 2px
        logger.info("🔄 第二阶段: 高密度微调(±10px,步长2px)...")
        fine_offsets = list(range(-10, 11, 2))
        for offset in fine_offsets:
            fresh_loc, fresh_box = await _resolve_slider_box(page, slider_handle)
            if not fresh_box:
                miss_streak += 1
                if miss_streak >= 2:
                    logger.info("⚠️ 滑块已消失(连续重定位失败),放弃当前验证码,等待刷新重试")
                    return False
                logger.info(f"ℹ️ 微调 offset={offset:+d}: 滑块重新定位失败,跳过")
                continue
            miss_streak = 0
            slider_handle = fresh_loc or slider_handle
            dist = clamp(bg_rect["x"] + gap_x_rendered - fresh_box["x"] + offset)

            logger.info(f"🔄 微调 offset={offset:+d}px → 滑动距离={dist:.0f}px")
            if await perform_slide(page, fresh_box, dist):
                logger.info(f"✅ 微调 offset={offset}px 命中!")
                return True
            await asyncio.sleep(0.5 + random.random() * 0.3)

        logger.info("❌ 第二阶段高密度微调均失败")

        # 7. 终极降级:纯滑块模式(拖到右端)
        right_distances = [0.75, 0.80, 0.85, 0.90, 0.93, 0.96]
        logger.info("🔄 第三阶段: 纯滑块模式(拖到右端)...")
        for ratio in right_distances:
            fresh_loc, fresh_box = await _resolve_slider_box(page, slider_handle)
            if not fresh_box:
                miss_streak += 1
                if miss_streak >= 2:
                    logger.info("⚠️ 滑块已消失(连续重定位失败),放弃当前验证码,等待刷新重试")
                    return False
                logger.info(f"ℹ️ 纯滑块 {ratio * 100:.0f}%: 滑块重新定位失败,跳过")
                continue
            miss_streak = 0
            slider_handle = fresh_loc or slider_handle
            dist = clamp(int(bg_rect["width"] * ratio))
            logger.info(f"🔄 纯滑块 {ratio * 100:.0f}%: 距离={dist}px")
            if await perform_slide(page, fresh_box, dist):
                logger.info(f"✅ 纯滑块 {ratio * 100:.0f}% 命中!")
                return True
            await asyncio.sleep(0.6 + random.random() * 0.4)

        logger.info("❌ 所有尝试均失败")

        # 调试截图
        try:
            debug_path = GPTQTCOOL_SCREENSHOT_DIR / f"captcha_fail_{ts}.png"
            await page.screenshot(path=str(debug_path), full_page=False)
            logger.info(f"📸 已保存失败调试截图: {debug_path}")
        except Exception:
            pass

        return False

    except Exception as e:
        logger.error(f"❌ handle_slider_captcha 错误: {e}")
        return False


async def wait_for_checkin_result(page, timeout: float = 18.0) -> bool:
    """轮询检查签到结果,每 1.5 秒检测一次,最多等待 timeout 秒"""
    logger.info(f"⏳ 等待签到结果(超时 {timeout:.0f}s)...")
    deadline = time.time() + timeout
    check_interval = 1.5

    while time.time() < deadline:
        # 条件 1: 签到按钮文本变为已签到状态
        checkin_button = page.locator("button#checkinBtn.ci-btn.renew")
        if await checkin_button.count() > 0:
            button_text = await checkin_button.text_content()
            if button_text:
                logger.info(f"📝 签到按钮文本: {button_text.strip()}")
                # 注意：不含 "Check-in"——它是"去签到"的动作动词（未签到状态），
                # 误列为成功标志会在签到实际失败时把按钮判成成功
                success_texts = ["今日已签到", "已签到", "Signed", "Renewed", "Checked in today"]
                if any(s in button_text for s in success_texts):
                    logger.info(f"🎉 检测到按钮状态变为: {button_text.strip()}")
                    return True

        # 条件 2: 今日已签到按钮
        if await page.locator('button:has-text("今日已签到")').count() > 0:
            logger.info('🎉 检测到"今日已签到"按钮,签到成功!')
            return True

        # 条件 3: 成功 Toast/Notification
        try:
            success_msgs = await page.locator(
                '.message, .toast, .snackbar, .notification, '
                '[class*="message"], [class*="toast"], [class*="success"]'
            ).all()
            for msg in success_msgs:
                text = await msg.text_content()
                if text and ("成功" in text or "success" in text.lower() or "已签到" in text):
                    logger.info(f"✅ 检测到成功提示: {text.strip()}")
                    return True
        except Exception:
            pass

        # 条件 4: 日历签到标记
        try:
            today = datetime.now().day
            cal_days = await page.locator(".ci-cal-day").all()
            for cd in cal_days:
                cd_text = await cd.text_content()
                if cd_text and cd_text.strip() == str(today):
                    has_mark = await cd.locator(
                        '.dot, .checked, [class*="sign"]'
                    ).count() > 0
                    if has_mark:
                        logger.info(f"🎉 签到成功!{today}号已有签到标记")
                        return True
                    break
        except Exception:
            pass

        logger.info(f"⏳ 签到结果尚未确认,继续等待({max(0, int(deadline - time.time()))}s剩余)...")
        await asyncio.sleep(check_interval)

    logger.warning("⚠️ 轮询超时,未检测到明确的签到成功标志")
    return False


async def click_checkin_button(page) -> bool:
    """点击签到按钮(三级选择器回退),返回是否成功点击"""
    logger.info("🖱️ 寻找并点击签到按钮...")
    if await page.locator("button#checkinBtn.ci-btn.renew").count() > 0:
        logger.info("✅ 找到精确的签到按钮")
        await page.evaluate("""() => {
            const button = document.querySelector('button#checkinBtn.ci-btn.renew');
            if (button) button.click();
        }""")
        logger.info("🖱️ 点击签到按钮(JavaScript 执行)")
        return True
    if await page.locator('button:has-text("签到续期")').count() > 0:
        logger.info("✅ 找到签到续期按钮")
        await page.evaluate("""() => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                if (btn.textContent && btn.textContent.includes('签到续期')) {
                    btn.click();
                    break;
                }
            }
        }""")
        logger.info("🖱️ 点击签到续期按钮(JavaScript 执行)")
        return True
    if await page.locator('button:has-text("签到")').count() > 0:
        logger.info("✅ 找到签到按钮")
        await page.evaluate("""() => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const t = (btn.textContent || '').trim();
                // has-text("签到") 会同时命中"今日已签到"按钮（FORCE_RUN 放行时），
                // 点击前排除它，避免点了无效按钮后走完整验证码流程
                if (t.includes('签到') && !t.includes('今日已签到')) {
                    btn.click();
                    break;
                }
            }
        }""")
        logger.info("🖱️ 点击签到按钮(JavaScript 执行)")
        return True
    return False


async def reload_and_verify(page) -> bool:
    """刷新页面后的兜底成功检测(按钮文本 / 今日已签到按钮 / 日历标记)"""
    logger.info("🔄 刷新页面以更新签到状态...")
    await page.reload(wait_until="domcontentloaded")
    await asyncio.sleep(3.0)

    logger.info("📅 等待日历加载...")
    try:
        await page.wait_for_selector(".ci-cal-day", state="visible", timeout=5000)
        logger.info("✅ 日历已加载")
    except Exception:
        logger.info("⚠️ 日历加载超时,继续检查")

    logger.info("🔍 刷新后兜底检查签到状态...")

    # 1. 按钮文本变为"今日已签到"/"已签到"
    checkin_button = page.locator("button#checkinBtn.ci-btn.renew")
    if await checkin_button.count() > 0:
        button_text = await checkin_button.text_content()
        logger.info(f"📝 签到按钮文本: {button_text}")
        # 注意：不含 "Check-in"（未签到状态的动作动词），避免签到失败时误判成功
        success_texts = ["今日已签到", "已签到", "Signed", "Renewed", "Checked in today"]
        if button_text and any(s in button_text for s in success_texts):
            logger.info(f"🎉 检测到按钮状态变为: {button_text}")
            return True

    # 2. 今日已签到按钮
    if await page.locator('button:has-text("今日已签到")').count() > 0:
        logger.info('🎉 检测到"今日已签到"按钮,签到成功!')
        return True

    # 3. 日历今日是否有签到标记(精确匹配日期数字)
    today = datetime.now().day
    logger.info(f"📅 检查今天({today}号)是否有签到标记...")
    cal_days = await page.locator(".ci-cal-day").all()
    found_today = False
    for cd in cal_days:
        cd_text = await cd.text_content()
        if cd_text and cd_text.strip() == str(today):
            found_today = True
            has_sign_mark = await cd.locator(
                '.dot, .checked, [class*="sign"]'
            ).count() > 0
            if has_sign_mark:
                logger.info(f"🎉 签到成功!{today}号已有签到标记")
                return True
            logger.info(f"ℹ️ {today}号未发现签到标记")
            break
    if not found_today:
        logger.info(f"ℹ️ 日历中未找到日期{today}")

    logger.warning("⚠️ 未检测到明确的签到成功标志")
    return False


# ========== 主流程 ==========

def _load_storage_state() -> str | None:
    """读取持久化登录态文件路径；文件缺失或损坏返回 None（忽略缓存回退完整登录）。"""
    if not STATE_FILE.exists():
        return None
    try:
        # 预检 JSON 合法性，避免损坏文件导致 new_context 抛异常
        json.loads(STATE_FILE.read_text(encoding="utf-8"))
        logger.info(f"💾 发现登录态缓存 {STATE_FILE.name}，尝试复用")
        return str(STATE_FILE)
    except Exception as e:
        logger.warning(f"⚠️ storage_state 损坏({e})，忽略缓存")
        return None


async def _save_storage_state(context) -> None:
    """把当前登录态落盘，供下次直通签到跳过登录与滑块。"""
    try:
        await context.storage_state(path=str(STATE_FILE))
        logger.info(f"💾 已保存登录态 -> {STATE_FILE.name}")
    except Exception as e:
        logger.warning(f"⚠️ 保存登录态失败: {e}")


def _drop_storage_state() -> None:
    """删除失效的登录态文件，避免反复复用坏登录态。"""
    try:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            logger.info("🗑️ 已删除失效登录态")
    except Exception as e:
        logger.warning(f"⚠️ 删除登录态失败: {e}")


async def relogin_after_auth_failure(page, context, auth: dict) -> bool:
    """登录态被服务端拒绝(401)后的自愈:清凭证 → 完整登录 → 落盘

    背景:该站前端会用 localStorage 里残留的用户信息渲染"已登录"UI,
    即使服务端 token 已过期(API 全部 401),纯 DOM 检测仍会误判已登录。
    因此自愈时必须把客户端凭证清干净,让页面回到真实未登录视图。
    """
    logger.info("🛠️ 开始登录态自愈:清除本地凭证并重新登录")
    _drop_storage_state()
    try:
        await context.clear_cookies()
    except Exception as e:
        logger.warning(f"⚠️ 清除 cookie 失败(忽略): {e}")
    try:
        await page.evaluate(
            "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
        )
    except Exception as e:
        logger.warning(f"⚠️ 清除 web storage 失败(忽略): {e}")

    await page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=GPTQTCOOL_TIMEOUT)
    await asyncio.sleep(2.0 + random.random())
    await switch_to_chinese_if_needed(page)

    if not await try_login_with_retry(page):
        logger.error("❌ 自愈重新登录失败")
        return False
    logger.info("✅ 自愈重新登录成功")
    await _save_storage_state(context)
    # 重置计数,避免历史 401 干扰后续签到周期的失效判定
    auth["count"] = 0
    return True


async def auto_checkin() -> bool:
    """主签到函数"""
    # 401 感知:页面渲染出的"已登录"UI 可能只是 localStorage 残影,
    # 服务端 token 过期时 API 会返回 401,以此判定登录态的真实有效性
    auth = {"count": 0}

    def _on_auth_response(response):
        try:
            if response.status == 401:
                auth["count"] += 1
                logger.warning(f"🚨 API 响应 401(累计 {auth['count']} 次): {response.url}")
        except Exception:
            pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--start-maximized",
            ],
        )
        try:
            # ★ 复用持久化登录态：命中则直接进签到，跳过填 Key 登录与滑块验证
            storage_state = _load_storage_state()
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=True,
                storage_state=storage_state,
            )

            # add_init_script 在 context 级注入，对所有页面加载生效
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.navigator.chrome = { runtime: {} };
            """)

            page = await context.new_page()

            # 只打印 warn/error，过滤站点自身的 info/log 噪音（按级别输出）
            def _on_page_console(msg):
                if msg.type == "error":
                    logger.error(f"📄 页面日志[error]: {msg.text}")
                elif msg.type == "warning":
                    logger.warning(f"📄 页面日志[warning]: {msg.text}")

            page.on("console", _on_page_console)
            page.on("pageerror", lambda err: logger.error(f"❌ 页面错误: {err}"))
            page.on("response", _on_auth_response)

            logger.info(f"🌐 正在访问签到页面: {CHECKIN_URL}")
            await page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=GPTQTCOOL_TIMEOUT)
            logger.info("✅ 页面加载完成")

            await asyncio.sleep(2.0 + random.random())
            await switch_to_chinese_if_needed(page)

            # 登录：缓存命中且加载期无 401 → 直通签到；否则完整登录后落盘
            if await is_logged_in(page):
                # 等待页面发出首批带鉴权的 API 请求,用 401 计数验证登录态真伪
                await asyncio.sleep(3.0)
                if auth["count"] > 0:
                    logger.warning(
                        f"🚨 页面 UI 显示已登录,但 API 已返回 {auth['count']} 次 401,登录态已失效"
                    )
                    if not await relogin_after_auth_failure(page, context, auth):
                        raise Exception("登录态已失效且重新登录失败")
                else:
                    logger.info("✅ 检测到已登录状态(缓存命中且无 401),跳过登录与滑块")
                    await _save_storage_state(context)
            else:
                logger.info("🔐 未登录,走完整登录流程")
                if not await relogin_after_auth_failure(page, context, auth):
                    raise Exception("多次重试后仍未登录成功(可能未登录或会话已过期)")

            # GPTQTCOOL_FORCE_RUN: 检查今日是否已签到
            if not GPTQTCOOL_FORCE_RUN:
                # 幂等检查覆盖中英文按钮文案（英文界面为 "Checked in today"/"Signed"）
                if await page.locator(
                    'button:has-text("今日已签到"), button:has-text("Checked in today"), '
                    'button:has-text("Signed")'
                ).count() > 0:
                    logger.info("ℹ️ 今日已签到,GPTQTCOOL_FORCE_RUN=false,跳过执行")
                    return True
            else:
                logger.info("⚠️ GPTQTCOOL_FORCE_RUN=true,忽略今日已签到状态,强制执行")

            # 签到主流程:失败且伴随新 401 时判定登录态失效,自愈重登后重试一次
            for attempt in (1, 2):
                if attempt == 2:
                    logger.info("🔁 自愈完成,重试签到(第 2 次尝试)...")
                count_before = auth["count"]

                if not await click_checkin_button(page):
                    raise Exception("未找到签到按钮")

                # 处理验证码
                captcha_ok = await wait_and_handle_captcha_with_retry(page)
                if not captcha_ok:
                    logger.warning("⚠️ 未检测到/未通过验证码,尝试直接检查签到结果...")

                # 轮询检查签到结果
                if await wait_for_checkin_result(page, timeout=18.0):
                    return True

                # 若验证码未通过且结果也未确认,再抛异常
                if not captcha_ok:
                    raise Exception("验证码验证失败,无法继续签到")

                # 刷新页面确保状态更新（兜底检测）
                if await reload_and_verify(page):
                    return True

                # 本轮出现新 401 → 登录态失效,清凭证重登后重试;无 401 则维持原判定
                if attempt == 1 and auth["count"] > count_before:
                    logger.warning(
                        f"🚨 本次签到周期出现 {auth['count'] - count_before} 次 401,"
                        "判定登录态失效,自愈后重试"
                    )
                    if not await relogin_after_auth_failure(page, context, auth):
                        break
                    continue
                break

            return False

        except Exception as e:
            logger.error(f"❌ 签到过程中发生错误: {e}")
            raise
        finally:
            await browser.close()
            logger.info("🔒 浏览器已关闭")


async def main() -> None:
    if not GPTQTCOOL_KEY:
        print("❌ 未设置 GPTQTCOOL_KEY 环境变量")
        sys.exit(1)
    try:
        logger.info("========== 🚀 开始签到(青龙 Python 版) ==========")
        success = await auto_checkin()
        if success:
            logger.info("✅ 签到成功")
            if NOTIFY and not (NOTIFY_ONLY_FAIL):
                send_notify("✅ gpt.qt.cool 签到成功", "签到续期已完成 ✅")
            elif NOTIFY and NOTIFY_ONLY_FAIL:
                logger.info("ℹ️ NOTIFY_ONLY_FAIL=true,本次成功不推送")
            sys.exit(0)
        else:
            logger.info("❌ 签到失败")
            if NOTIFY:
                send_notify("❌ gpt.qt.cool 签到失败", "未检测到签到成功标志")
            sys.exit(1)
    except Exception as e:
        logger.error(f"❌ 签到失败: {e}")
        logger.error(traceback.format_exc())
        if NOTIFY:
            send_notify("❌ gpt.qt.cool 签到异常", f"签到过程异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
