# -*- coding: utf-8 -*-
"""登录、登录态判定与持久化。

该站前端会用 localStorage 残影渲染"已登录"UI，所以登录态判定与自愈逻辑
必须结合 API 401（由调用方统计）而非只看 DOM。
"""
from __future__ import annotations

import asyncio
import json
import random

from .config import (
    CHECKIN_URL,
    GPTQTCOOL_KEY,
    GPTQTCOOL_TIMEOUT,
    STATE_FILE,
    logger,
)
from .detect import click_if_visible, dismiss_overlays


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
    """判断页面是否已进入有效登录态(多重信号)

    只保留语义明确的信号：宽匹配（[class*='user']、[class*='avatar']、.profile）与
    裸文本"剩余"在未登录落地页也会命中，会误判成已登录而跳过登录，故不再采信。
    """
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

    # 6. 用户元素（收紧：只保留语义明确的 user-* / data-user，
    #    去掉 [class*='avatar']、.profile —— 未登录页也常命中）
    for selector in ["[class*='user-avatar']", "[class*='user-name']",
                     "[class*='user-info']", "[data-user]"]:
        if await page.locator(selector).count() > 0:
            logger.info(f"✅ 检测到用户元素 {selector},判定已登录")
            return True

    # 7. 余额/使用次数（弱信号）：必须同时出现签到页特征文案才采信，
    #    否则未登录落地页的"余额/使用次数"字样会把状态误判为已登录
    if ("余额" in page_text or "使用次数" in page_text) and ("签到" in page_text):
        logger.info("✅ 检测到余额/使用次数且含签到文案,判定已登录")
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
            # 只比较长度，不打印任何 key 片段（避免密钥前缀泄漏进日志）
            if GPTQTCOOL_KEY.startswith(typed_value):
                logger.info(f"⚠️ API Key 输入校验:输入值({len(typed_value)}字符)是完整 key({len(GPTQTCOOL_KEY)}字符)的前缀,判断为前端截断,继续执行")
            else:
                logger.warning(f"❌ API Key 输入校验不一致:输入值 {len(typed_value)} 字符 ≠ 期望值 {len(GPTQTCOOL_KEY)} 字符")
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
            if await click_if_visible(page, selector, timeout=5000):
                logger.info(f"🖱️ 已点击登录按钮: {selector}")
                clicked = True
                break

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


# ====================== 登录态持久化 ======================

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
