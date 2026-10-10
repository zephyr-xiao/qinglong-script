# -*- coding: utf-8 -*-
"""签到主流程、结果判定与自检。

依赖 config / notify / detect / slider / auth。
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime

from playwright.async_api import async_playwright

from .auth import (
    _load_storage_state,
    _save_storage_state,
    is_logged_in,
    relogin_after_auth_failure,
    switch_to_chinese_if_needed,
)
from .config import (
    CHECKIN_URL,
    GPTQTCOOL_FORCE_RUN,
    GPTQTCOOL_KEY,
    GPTQTCOOL_SCREENSHOT_DIR,
    GPTQTCOOL_TIMEOUT,
    HEADLESS,
    MAX_RUNTIME_MIN,
    RETRY_ROUNDS,
    logger,
)
from .notify import send_notify
from .slider import wait_and_handle_captcha_with_retry

BROWSER_ARGS = [
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
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 幂等判定：中英文界面下的"今日已签到"按钮文案
ALREADY_SIGNED_SELECTOR = (
    'button:has-text("今日已签到"), button:has-text("Checked in today"), '
    'button:has-text("Signed")'
)


async def _new_context(browser, storage_state):
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=USER_AGENT,
        ignore_https_errors=True,
        storage_state=storage_state,
    )
    # add_init_script 在 context 级注入，对所有页面加载生效
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.navigator.chrome = { runtime: {} };
    """)
    return context


def _latest_fail_screenshot() -> str | None:
    """返回最近一张失败截图文件名（用于失败通知里给出可回查线索）。"""
    try:
        files = sorted(GPTQTCOOL_SCREENSHOT_DIR.glob("captcha_fail_*.png"),
                       key=lambda p: p.stat().st_mtime)
        return files[-1].name if files else None
    except Exception:
        return None


# ====================== 结果判定 ======================

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
        # 收紧：只认提示条容器（去掉宽泛的 [class*="success"]，它会命中页面上任何
        # class 含 success 的静态元素），并限定文本长度，避免把整块面板当提示
        try:
            success_msgs = await page.locator(
                '.message, .toast, .snackbar, .notification, '
                '[class*="toast"], [class*="message"], [class*="snackbar"]'
            ).all()
            for msg in success_msgs:
                text = await msg.text_content()
                if not text:
                    continue
                t = text.strip()
                if len(t) > 120:
                    continue
                if any(s in t for s in ("签到成功", "续期成功", "已签到", "checked in")):
                    logger.info(f"✅ 检测到成功提示: {t}")
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


# ====================== 主流程 ======================

async def _ensure_logged_in(page, context, auth: dict) -> bool:
    """确保处于有效登录态：缓存命中且无 401 直通；否则完整登录/自愈。"""
    if await is_logged_in(page):
        # 等待页面发出首批带鉴权的 API 请求,用 401 计数验证登录态真伪
        await asyncio.sleep(3.0)
        if auth["count"] > 0:
            logger.warning(
                f"🚨 页面 UI 显示已登录,但 API 已返回 {auth['count']} 次 401,登录态已失效"
            )
            return await relogin_after_auth_failure(page, context, auth)
        logger.info("✅ 检测到已登录状态(缓存命中且无 401),跳过登录与滑块")
        await _save_storage_state(context)
        return True
    logger.info("🔐 未登录,走完整登录流程")
    return await relogin_after_auth_failure(page, context, auth)


async def auto_checkin() -> dict:
    """主签到函数。

    返回运行信息字典：
        ok          是否签到成功（含"今日已签到"幂等命中）
        stage       结束阶段：done/already/login/captcha/button/result/timeout
        rounds_used 实际用掉的整轮次数
        retried     是否发生过整轮重试
        screenshot  失败截图文件名（若有）
    """
    # 401 感知:页面渲染出的"已登录"UI 可能只是 localStorage 残影,
    # 服务端 token 过期时 API 会返回 401,以此判定登录态的真实有效性
    auth = {"count": 0}
    deadline = time.time() + MAX_RUNTIME_MIN * 60
    result = {"ok": False, "stage": "", "rounds_used": 0, "retried": False, "screenshot": None}

    def _on_auth_response(response):
        try:
            if response.status == 401:
                auth["count"] += 1
                logger.warning(f"🚨 API 响应 401(累计 {auth['count']} 次): {response.url}")
        except Exception:
            pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=BROWSER_ARGS)
        try:
            storage_state = _load_storage_state()
            context = await _new_context(browser, storage_state)
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
            # 实测：语言切换按钮是 toggle，点击后页面会把签到状态重置成"未签到"文案
            # 且不自行恢复（幂等检查因此看不到"今日已签到"，已签到日会重复点签到）。
            # 重载一次拿回真实状态，再进入登录/签到流程。
            await page.reload(wait_until="domcontentloaded", timeout=GPTQTCOOL_TIMEOUT)
            await asyncio.sleep(2.0 + random.random())

            if not await _ensure_logged_in(page, context, auth):
                result["stage"] = "login"
                return result

            for round_no in range(1, RETRY_ROUNDS + 1):
                result["rounds_used"] = round_no
                if time.time() > deadline:
                    logger.error(f"❌ 已达总时长上限 {MAX_RUNTIME_MIN} 分钟,停止重试")
                    result["stage"] = "timeout"
                    return result

                if round_no > 1:
                    result["retried"] = True
                    logger.info(f"🔁 第 {round_no}/{RETRY_ROUNDS} 轮：重载页面后重新签到")
                    try:
                        await page.goto(CHECKIN_URL, wait_until="domcontentloaded",
                                        timeout=GPTQTCOOL_TIMEOUT)
                    except Exception as e:
                        logger.warning(f"⚠️ 重载页面失败(忽略,继续): {e}")
                    await asyncio.sleep(2.0 + random.random())
                    await switch_to_chinese_if_needed(page)
                    # 重载后可能又暴露 401
                    if auth["count"] > 0 and not await relogin_after_auth_failure(page, context, auth):
                        result["stage"] = "login"
                        return result

                # GPTQTCOOL_FORCE_RUN: 检查今日是否已签到（幂等复查，每轮都做）
                if not GPTQTCOOL_FORCE_RUN and await page.locator(ALREADY_SIGNED_SELECTOR).count() > 0:
                    logger.info("ℹ️ 今日已签到,GPTQTCOOL_FORCE_RUN=false,跳过执行")
                    result["ok"] = True
                    result["stage"] = "already"
                    return result
                if GPTQTCOOL_FORCE_RUN:
                    logger.info("⚠️ GPTQTCOOL_FORCE_RUN=true,忽略今日已签到状态,强制执行")

                count_before = auth["count"]

                if not await click_checkin_button(page):
                    logger.warning("⚠️ 未找到签到按钮")
                    result["stage"] = "button"
                    if round_no >= RETRY_ROUNDS:
                        return result
                    continue

                captcha_result = await wait_and_handle_captcha_with_retry(page)
                if captcha_result is False:
                    logger.warning("⚠️ 验证码未通过")
                    result["stage"] = "captcha"
                    result["screenshot"] = _latest_fail_screenshot()
                elif captcha_result is None:
                    logger.info("ℹ️ 本轮未出现验证码")

                if captcha_result is True:
                    # 验证码通过=签到门槛已过。实测页面内按钮不会即时更新为"今日已签到"
                    # （只有重载才变），继续轮询只会空转满 18s，直接刷新判定
                    logger.info("ℹ️ 验证码已通过,直接刷新页面判定结果")
                    if await reload_and_verify(page):
                        result["ok"] = True
                        result["stage"] = "done"
                        return result
                else:
                    if await wait_for_checkin_result(page, timeout=18.0):
                        result["ok"] = True
                        result["stage"] = "done"
                        return result
                    # 验证码未通过时结果不可信，刷新兜底只在未出现验证码/疑似通过时做
                    if captcha_result is not False:
                        if await reload_and_verify(page):
                            result["ok"] = True
                            result["stage"] = "done"
                            return result

                # 本轮出现新 401 → 登录态失效,清凭证重登后进入下一轮重试
                if auth["count"] > count_before:
                    logger.warning(
                        f"🚨 本次签到周期出现 {auth['count'] - count_before} 次 401,判定登录态失效,自愈后重试"
                    )
                    if not await relogin_after_auth_failure(page, context, auth):
                        result["stage"] = "login"
                        return result

                logger.warning(f"⚠️ 第 {round_no}/{RETRY_ROUNDS} 轮未确认签到成功")
                if round_no < RETRY_ROUNDS:
                    await asyncio.sleep(2.0 + random.random())

            result["stage"] = result["stage"] or "result"
            return result

        except Exception as e:
            logger.error(f"❌ 签到过程中发生错误: {e}")
            raise
        finally:
            await browser.close()
            logger.info("🔒 浏览器已关闭")


async def auto_checkin_with_timeout() -> dict:
    """带总时长硬封顶的签到入口。

    轮次循环里的 deadline 检查只能保证"不再开启新一轮"，单轮内部（验证码四阶段
    滑动 + 结果轮询 + 刷新兜底）仍可能拖长；这里用 wait_for 对整体硬封顶，
    超时按取消处理——auto_checkin 的 finally 会正常关闭浏览器。
    """
    try:
        return await asyncio.wait_for(auto_checkin(), timeout=MAX_RUNTIME_MIN * 60)
    except asyncio.TimeoutError:
        logger.error(f"❌ 已达总时长上限 {MAX_RUNTIME_MIN} 分钟,强制结束本次运行")
        return {
            "ok": False,
            "stage": "timeout",
            "rounds_used": RETRY_ROUNDS,
            "retried": True,
            "screenshot": _latest_fail_screenshot(),
        }


# ====================== 通知文案 ======================

def notify_result(result: dict) -> None:
    """按运行信息发送通知：成功区分"一次通过/重试后通过"，失败带阶段与截图线索。"""
    from .config import NOTIFY, NOTIFY_ONLY_FAIL

    if not NOTIFY:
        return
    ok = result.get("ok")
    rounds = result.get("rounds_used", 0)
    retried = result.get("retried")
    if ok:
        if NOTIFY_ONLY_FAIL:
            logger.info("ℹ️ NOTIFY_ONLY_FAIL=true,本次成功不推送")
            return
        if result.get("stage") == "already":
            send_notify("✅ gpt.qt.cool 今日已签到", "幂等命中：今日已签到，无需重复操作 ✅")
        elif retried:
            send_notify("✅ gpt.qt.cool 签到成功", f"重试 {rounds} 轮后签到成功 ✅")
        else:
            send_notify("✅ gpt.qt.cool 签到成功", "签到续期已完成 ✅")
        return

    stage_text = {
        "login": "登录失败",
        "captcha": "验证码未通过",
        "button": "未找到签到按钮",
        "result": "未检测到签到成功标志",
        "timeout": f"超时（>{MAX_RUNTIME_MIN} 分钟）",
    }.get(result.get("stage"), "未知阶段")
    lines = [f"失败阶段：{stage_text}"]
    if rounds >= 1:
        lines.append(f"已尝试 {rounds} 轮")
    if result.get("screenshot"):
        lines.append(f"失败截图：{result['screenshot']}")
    send_notify("❌ gpt.qt.cool 签到失败", "\n".join(lines))


# ====================== --check 自检 ======================

async def self_check() -> int:
    """自检模式：不执行签到，只验证环境与登录态。返回退出码(0=通过)。"""
    logger.info("========== 🔎 自检模式(--check)：不执行签到 ==========")
    checks: list[tuple[str, bool, str]] = []

    # 1. API Key
    checks.append(("环境变量 GPTQTCOOL_KEY", bool(GPTQTCOOL_KEY),
                   "已设置" if GPTQTCOOL_KEY else "未设置"))

    # 2. 浏览器启动 + 页面可达 + 登录态
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS, args=BROWSER_ARGS)
            checks.append(("Chromium 启动", True, "成功"))
            try:
                context = await _new_context(browser, _load_storage_state())
                page = await context.new_page()
                auth = {"count": 0}

                def _count_401(response):
                    if getattr(response, "status", None) == 401:
                        auth["count"] += 1

                page.on("response", _count_401)
                await page.goto(CHECKIN_URL, wait_until="domcontentloaded",
                                timeout=GPTQTCOOL_TIMEOUT)
                await asyncio.sleep(2.5)
                checks.append(("签到页可达", True, CHECKIN_URL))
                logged = await is_logged_in(page)
                checks.append(("登录态", logged,
                               "已登录" if logged else "未登录（正式运行会自动登录）"))
                checks.append(("页面 401 计数", auth["count"] == 0,
                               f"{auth['count']} 次" + ("（登录态可能已失效）" if auth["count"] else "")))
            finally:
                await browser.close()
    except Exception as e:
        checks.append(("Chromium 启动/页面访问", False, str(e)))

    # 输出结果表
    # 登录态与 401 计数只作告警：缓存过期时必然未登录/出现 401，正式运行会自愈登录，
    # 不属于环境问题；KEY / Chromium / 页面可达 才是硬门槛
    non_fatal = {"登录态", "页面 401 计数"}
    print("\n========== 自检结果 ==========")
    all_ok = True
    for name, ok, detail in checks:
        if ok:
            mark = "✅"
        elif name in non_fatal:
            mark = "⚠️"
        else:
            mark = "❌"
            all_ok = False
        print(f"  {mark} {name}: {detail}")
    print("==============================\n")
    print("✅ 自检通过" if all_ok else "❌ 自检发现问题，请按上面 ❌ 项排查")
    return 0 if all_ok else 1
