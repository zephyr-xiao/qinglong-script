# -*- coding: utf-8 -*-
"""滑块定位、拟人化轨迹与验证码整体处理。

依赖 detect 提供缺口定位与验证码存在性检测。
"""
from __future__ import annotations

import asyncio
import random
import re
import time

from .config import GPTQTCOOL_SCREENSHOT_DIR, logger
from .detect import (
    detect_gap,
    detect_slider_captcha,
    dismiss_overlays,
    extract_captcha_images,
    has_captcha_failure_hint,
)


# ====================== 拟人化轨迹（纯函数，可单测） ======================

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


# ====================== 滑块定位 ======================

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
    """执行拟人化滑动操作。

    成功判据：滑动后验证码消失，且没有出现"验证失败/验证超时"提示。
    只看"验证码消失"会被残留的失败提示骗过（反之亦然），故两者结合。
    """
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

    async def _passed() -> bool:
        if await has_captcha_failure_hint(page):
            logger.info("❌ 页面出现验证失败/超时提示")
            return False
        if not await detect_slider_captcha(page):
            return True
        return False

    await asyncio.sleep(1.5 + random.random() * 1.0)
    if await _passed():
        logger.info("✅ 验证通过")
        return True

    logger.info("⏳ 验证码仍存在,等待动画完成...")
    await asyncio.sleep(2.0 + random.random() * 1.0)
    if await _passed():
        logger.info("✅ 验证码动画完成后消失,验证成功")
        return True

    logger.info("⏳ 等待滑块归位...")
    await asyncio.sleep(1.5 + random.random() * 0.5)
    if await _passed():
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


# ====================== 验证码整体处理 ======================

# 各检测方法对应的首选偏移扫描范围：越可靠的方法范围越小
_METHOD_MAX_OFFSET = {
    "highlight": 8,
    "highlight_full": 8,
    "template": 12,
    "edge": 20,
    "edge_lowconf": 24,
}


async def handle_slider_captcha(page) -> bool:
    """处理滑动验证码 - 以屏幕截图坐标系为基准,多候选 + 高密度微调

    关键处理:
    1. 始终优先使用 page.screenshot(clip=container_box) 作为缺口检测输入,
       保证检测坐标与页面渲染 1:1。
    2. 若只能拿到 DOM 原始背景图,则按 natural/rendered 比例换算 gap_x。
    3. 检测完成后保存背景图、拼图块、带标记的检测图,便于定位问题。
    4. 主候选失败后在 gap_x ±10px 内高密度微调;仍失败则改试其它候选位置
       （该站点首次滑动失败常隐藏滑块,所以"首选就对"最关键,备选是补救）。
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

        # 检测缺口(背景图坐标系),返回按可靠度排序的多候选
        detect_debug_path = GPTQTCOOL_SCREENSHOT_DIR / f"captcha_detect_{ts}.png"
        # 缺口宽 = 拼图块宽（实验证明传滑块宽会偏 53px：缺口实际与拼图块等宽）
        slider_width = (
            int(captcha_images["tile_rect"]["width"])
            if captcha_images.get("tile_rect") else int(handle_box["width"])
        )
        gap_x, conf, method, candidates = await detect_gap(
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
        max_dist = bg_rect["width"] - handle_box["width"] - 10

        def clamp(d: float) -> float:
            return max(5, min(max_dist, d))

        logger.info(
            f"📏 首选 gap_x={gap_x}px, 背景图左={bg_rect['x']:.0f}px, "
            f"滑块左={handle_box['x']:.0f}px, method={method}, confidence={conf:.2f}"
        )

        async def try_offsets(base_x: int, offsets: list[int], tag: str) -> bool | None:
            """在 base_x 附近按 offsets 依次滑动。

            返回 True=命中；False=本组偏移全失败但滑块仍在；None=滑块已消失应中止。
            """
            nonlocal slider_handle, miss_streak
            for offset in offsets:
                # 每次重新解析滑块坐标：滑动失败后 DOM 可能刷新，旧 locator 会卡 30s 超时
                fresh_loc, fresh_box = await _resolve_slider_box(page, slider_handle)
                if not fresh_box:
                    miss_streak += 1
                    if miss_streak >= 2:
                        logger.info("⚠️ 滑块已消失(连续重定位失败),放弃当前验证码,等待刷新重试")
                        return None
                    logger.info(f"ℹ️ {tag} offset={offset:+d}: 滑块重新定位失败,跳过")
                    continue
                miss_streak = 0
                slider_handle = fresh_loc or slider_handle
                dist = clamp(bg_rect["x"] + base_x * scale_x - fresh_box["x"] + offset)
                logger.info(f"🔄 {tag} 基准x={base_x} offset={offset:+d} → 滑动距离={dist:.0f}px")
                if await perform_slide(page, fresh_box, dist):
                    logger.info(f"✅ {tag} 命中! (基准x={base_x}, offset={offset:+d})")
                    return True
                await asyncio.sleep(0.6 + random.random() * 0.4)
            return False

        # 第一阶段: 首选候选，按方法可靠度确定偏移范围
        max_offset = _METHOD_MAX_OFFSET.get(method, 24)
        all_offsets = [0, 4, -4, 8, -8, 12, -12, 16, -16, 20, -20, 24, -24]
        primary_offsets = [o for o in all_offsets if abs(o) <= max_offset]
        logger.info(f"🎯 第一阶段: 首选 {method}@{gap_x}, 最大偏移={max_offset}px, {len(primary_offsets)} 次")
        r = await try_offsets(gap_x, primary_offsets, "首选")
        if r is True:
            return True
        if r is None:
            return False
        logger.info("❌ 第一阶段首选候选尝试均失败")

        # 第二阶段: 改试其它候选（每个候选只做小范围扫描，候选本身已较精确）
        for idx, cand in enumerate(candidates[1:], start=1):
            logger.info(f"🎯 第二阶段: 备选#{idx} {cand['method']}@{cand['x']} (conf={cand['conf']:.1f})")
            r = await try_offsets(cand["x"], [0, 4, -4, 8, -8], f"备选#{idx}")
            if r is True:
                return True
            if r is None:
                return False
        logger.info("❌ 第二阶段备选候选尝试均失败")

        # 第三阶段: 首选候选高密度微调(gap_x ±10px,步长2px)
        logger.info("🔄 第三阶段: 首选候选高密度微调(±10px,步长2px)...")
        r = await try_offsets(gap_x, list(range(-10, 11, 2)), "微调")
        if r is True:
            return True
        if r is None:
            return False
        logger.info("❌ 第三阶段高密度微调均失败")

        # 第四阶段: 终极降级(纯滑块模式,拖到右端)
        right_ratios = [0.75, 0.80, 0.85, 0.90, 0.93, 0.96]
        logger.info("🔄 第四阶段: 纯滑块模式(拖到右端)...")
        for ratio in right_ratios:
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


async def wait_and_handle_captcha_with_retry(page, max_rounds: int = 2) -> bool | None:
    """等待并处理验证码(多轮检测 + 重试)

    返回:
        None —— 全程未出现验证码（无需处理，不是失败）
        True —— 验证通过
        False —— 出现验证码但多轮未通过

    内层轮数刻意保持较小（默认 2）：外层还有整轮重载重试，内层只负责
    "单次验证码内多枪 + 刷新重来"，避免内外层叠加把单次运行拖得过长。
    """
    wait_per_round = 4.0
    seen_captcha = False

    for attempt in range(1, max_rounds + 1):
        logger.info(f"⏳ 第{attempt}/{max_rounds}轮:检测验证码或结果...")
        # 先检测再等待：多数签到无验证码，先睡 4 秒会让无验证码场景白白空转
        has_captcha = await detect_slider_captcha(page)
        if not has_captcha:
            if seen_captcha:
                logger.info("✅ 验证码已消失,疑似验证通过")
                return True
            logger.info(f"ℹ️ 第{attempt}轮未检测到滑动验证码")
            if attempt < max_rounds:
                await asyncio.sleep(wait_per_round + random.random())
            continue

        seen_captcha = True
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

        try:
            if await page.locator("button#checkinBtn.ci-btn.renew").count() > 0:
                await page.evaluate("""() => {
                    const button = document.querySelector('button#checkinBtn.ci-btn.renew');
                    if (button) button.click();
                }""")
                logger.info("🖱️ 重新点击签到按钮(JavaScript 执行)")
                await asyncio.sleep(1.0 + random.random() * 0.5)
        except Exception:
            pass

        logger.warning(f"⚠️ 第{attempt}轮处理后验证码仍存在,准备下一轮重试")

    if not seen_captcha:
        logger.info("ℹ️ 多轮检测均未出现验证码,判定无需处理")
        return None
    logger.error("❌ 多轮检测后仍未通过验证码")
    return False
