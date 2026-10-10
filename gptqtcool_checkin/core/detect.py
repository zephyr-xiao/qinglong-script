# -*- coding: utf-8 -*-
"""页面检测与缺口定位。

职责：
- 页面干扰弹窗清理（dismiss_overlays）
- 滑动验证码存在性 / 失败提示检测（detect_slider_captcha、has_captcha_failure_hint）
- 验证码图片提取（extract_captcha_images）
- 缺口定位纯算法（detect_gap_by_highlight / _template / _edge / detect_gap）

其中缺口算法不触碰 page，可脱离浏览器单测。
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import cv2
import numpy as np

from .config import logger


# ====================== 通用页面操作 ======================

async def click_if_visible(page, selector: str, *, timeout: int = 2000) -> bool:
    """若选择器命中的首个元素存在且可见则点击，返回是否点击成功。

    统一"存在→可见→点击"样板，避免各处重复三段式判断。
    """
    try:
        loc = page.locator(selector)
        if await loc.count() == 0:
            return False
        el = loc.first
        try:
            visible = await el.is_visible(timeout=1000)
        except Exception:
            visible = False
        if not visible:
            return False
        await el.click(timeout=timeout)
        return True
    except Exception:
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
        if await click_if_visible(page, selector):
            logger.info(f"✅ 已关闭弹窗: {selector}")
            overlay_closed = True
            await asyncio.sleep(0.5)

    # 策略 2: 直接移除覆盖层元素
    # 用 locator.nth(i).evaluate 而非 f-string 拼 querySelectorAll（后者 selector 含引号即语法崩，
    # 且循环内重查 DOM 会导致索引与实际元素错位）
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
        except Exception:
            continue
        for i in range(count):
            try:
                await overlays.nth(i).evaluate("el => el.remove()")
            except Exception:
                pass

    if overlay_closed:
        await asyncio.sleep(0.8)


# ====================== 验证码存在性检测 ======================

# 出现这些文案说明"当前正要求完成验证"
CAPTCHA_TEXT_HINTS = [
    "请完成滑块验证", "请先完成滑块验证", "请完成人机验证",
    "按住滑块向右拖动", "向右拖动完成验证", "拖动滑块",
    "滑动验证", "滑块验证", "安全验证",
]
# 这些是"上一次验证失败/超时"的提示，只表示失败结果、不代表当前有验证码在等待，
# 混进存在性判断会把残留文案误判成"仍有验证码"而空转，故单独判定
CAPTCHA_FAILURE_HINTS = ["验证失败", "验证超时"]

# 滑块选择器只取语义明确的：div[style*="position: absolute"][style*="left"] 这类过宽选择器误判率高，不用
CAPTCHA_SLIDER_SELECTORS = [
    'div[class*="slider"]',
    ".captcha-slider",
    ".geetest",
    ".verify-slider",
]


async def detect_slider_captcha(page) -> bool:
    """检测当前页面是否出现滑动验证码(文本关键词 + 元素选择器)"""
    try:
        page_text = await page.inner_text("body")
    except Exception:
        page_text = ""

    for text in CAPTCHA_TEXT_HINTS:
        if text in page_text:
            logger.info(f'✅ 通过页面文本检测到验证码: "{text}"')
            return True

    for selector in CAPTCHA_SLIDER_SELECTORS:
        try:
            if await page.locator(selector).count() > 0:
                logger.info(f"✅ 通过元素选择器检测到验证码: {selector}")
                return True
        except Exception:
            pass

    return False


async def has_captcha_failure_hint(page) -> bool:
    """检测页面是否出现"验证失败/验证超时"提示（失败结果，不等于当前仍有验证码）"""
    try:
        page_text = await page.inner_text("body")
    except Exception:
        return False
    return any(h in page_text for h in CAPTCHA_FAILURE_HINTS)


# ====================== 缺口检测：边缘检测 ======================

def detect_gap_by_edge(screenshot: bytes, slider_width: int) -> tuple[int, float]:
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
        f"🎯 边缘检测缺口左边缘: {gap_x}px (置信度: {confidence:.2f}, "
        f"左边缘={best['left_edge']:.1f}, 右边缘={best['right_edge']:.1f}, "
        f"亮度差={best['brightness_diff']:.1f})"
    )
    top_str = " | ".join(
        f"x={c['x']} score={c['score']:.1f}" for c in candidates[:3]
    )
    logger.info(f"🔍 Top 候选: {top_str}")

    return gap_x, max(0.0, confidence)


# ====================== 缺口检测：模板匹配 ======================

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


# ====================== 缺口检测：高亮方块 ======================

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
                 该 y 附近,抑制远处阳光亮斑干扰。传 None 则全图扫描
                 (实测带限版本偶发漏检,全图版本可作独立候选补位)。
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

    logger.info(f"🔍 高亮方块检测(prior_y={'有' if prior_y is not None else '无'}): "
                f"x={best_x}, y={best_y}, score={best_score:.1f}")
    return best_x, best_y, best_score


# ====================== 缺口检测：四层递进 + 多候选 ======================

def _draw_candidates(screenshot: bytes, candidates: list[dict], save_debug_path: Path) -> None:
    """把候选位置画到调试图上：首选红色粗线，备选橙色细线，便于回看检测是否偏位。"""
    try:
        img = cv2.imdecode(np.frombuffer(screenshot, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        h = img.shape[0]
        for idx, c in enumerate(candidates):
            color = (0, 0, 255) if idx == 0 else (0, 165, 255)
            thickness = 2 if idx == 0 else 1
            cv2.line(img, (int(c["x"]), 0), (int(c["x"]), h), color, thickness)
        cv2.imwrite(str(save_debug_path), img)
        logger.info(f"📸 已保存缺口检测调试图: {save_debug_path}")
    except Exception as e:
        logger.info(f"ℹ️ 保存调试图失败: {e}")


async def detect_gap(
    screenshot: bytes,
    slider_width: int,
    tile_screenshot: bytes | None = None,
    master_rendered_size: tuple[float, float] | None = None,
    tile_rendered_size: tuple[float, float] | None = None,
    save_debug_path: Path | None = None,
    tile_rect: dict | None = None,
    bg_rect: dict | None = None,
) -> tuple[int, float, str, list[dict]]:
    """四层递进缺口检测: 高亮方块(带限) → 高亮方块(全图) → 模板匹配 → 边缘检测

    返回 (gap_x, confidence, method, candidates)。
    candidates 是按可靠度排序的候选位置列表（每个为 {"x","method","conf"}）：
    该站点首次滑动失败后会隐藏滑块，所以"首选就对"比"多试几次"更重要——
    带限高亮若缺失/低分，全图高亮可作首选补位（实测失败样本中全图版本稳定命中真缺口）。
    """
    candidates: list[dict] = []

    # 策略 0: 高亮方块（带限，需要拼图块/背景图 DOM 位置）
    if tile_rect and bg_rect:
        hl_x, hl_y, hl_score = detect_gap_by_highlight(
            screenshot,
            int(tile_rect["width"]) or 52,
            prior_y=(tile_rect["y"] + tile_rect["height"] / 2),
            bg_render_h=bg_rect["height"],
            tile_rect=tile_rect,
            bg_rect=bg_rect,
        )
        if hl_score >= HIGHLIGHT_MIN_CONF:
            logger.info(f"✅ 高亮方块(带限)命中: gap_x={hl_x}, y={hl_y}, score={hl_score:.1f}")
            candidates.append({"x": hl_x, "method": "highlight", "conf": hl_score})
        else:
            logger.info(f"⚠️ 高亮方块(带限)低分 ({hl_score:.1f} < {HIGHLIGHT_MIN_CONF})")

        # 策略 0b: 高亮方块（全图，去掉 prior_y 限制）作为独立候选补位
        hf_x, hf_y, hf_score = detect_gap_by_highlight(
            screenshot, int(tile_rect["width"]) or 52, prior_y=None,
            tile_rect=tile_rect, bg_rect=bg_rect,
        )
        if hf_score >= HIGHLIGHT_MIN_CONF and all(abs(hf_x - c["x"]) > 6 for c in candidates):
            logger.info(f"✅ 高亮方块(全图)候选: gap_x={hf_x}, y={hf_y}, score={hf_score:.1f}")
            candidates.append({"x": hf_x, "method": "highlight_full", "conf": hf_score})
    else:
        # 缺 DOM 位置时仍做全图高亮（不做拼图块预览排除），覆盖 DOM 提取失败的情况
        hf_x, hf_y, hf_score = detect_gap_by_highlight(
            screenshot, int(slider_width) or 52, prior_y=None,
        )
        if hf_score >= HIGHLIGHT_MIN_CONF:
            logger.info(f"✅ 高亮方块(全图/无DOM)候选: gap_x={hf_x}, y={hf_y}, score={hf_score:.1f}")
            candidates.append({"x": hf_x, "method": "highlight_full", "conf": hf_score})
        else:
            logger.info(f"⚠️ 高亮方块(全图/无DOM)低分 ({hf_score:.1f} < {HIGHLIGHT_MIN_CONF})")

    # 策略 1: 模板匹配(有拼图块截图时)
    if tile_screenshot:
        logger.info("🔍 尝试模板匹配...")
        # ★ 防自匹配：拼图块图案与缺口图案相同，若不涂黑拼图块自身区域，
        # 匹配分数最高点必然是拼图块自身位置（gap_x 返回拼图块位置导致拖动错位）。
        # 按 tile_rect/bg_rect 把拼图块在背景图中的区域涂黑后再匹配。
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
            # 自身附近而非真缺口（涂黑区边缘的亮度突变会产生虚假高响应），拒绝采信
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
                candidates.append({"x": tm_x, "method": "template", "conf": tm_conf})
            else:
                logger.info(
                    f"⚠️ 模板匹配未采信 (conf={tm_conf:.2f} < {min_conf} 或位置漂移)"
                )

    # 策略 2: 边缘检测（始终计算，作为最后兜底候选）
    edge_x, edge_conf = detect_gap_by_edge(screenshot, slider_width)
    edge_method = "edge" if edge_conf >= 1.5 else "edge_lowconf"
    candidates.append({"x": edge_x, "method": edge_method, "conf": edge_conf})

    # 去重：x 相差 ≤4px 视为同一位置，保留先出现（更高优先）的候选
    deduped: list[dict] = []
    for c in candidates:
        if all(abs(c["x"] - d["x"]) > 4 for d in deduped):
            deduped.append(c)
    candidates = deduped

    primary = candidates[0]
    logger.info(
        "🎯 缺口候选(按可靠度): "
        + " | ".join(f"{c['method']}@{c['x']}({c['conf']:.1f})" for c in candidates)
    )
    if save_debug_path:
        _draw_candidates(screenshot, candidates, save_debug_path)

    return primary["x"], primary["conf"], primary["method"], candidates


# ====================== 验证码图片提取 ======================

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
