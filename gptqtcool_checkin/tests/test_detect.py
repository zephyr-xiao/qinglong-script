# -*- coding: utf-8 -*-
"""缺口检测单测：合成图验证高亮定位、多候选排序，以及"带限漏检→全图补位"回归。"""
import asyncio

import cv2
import numpy as np

from core.detect import (
    CAPTCHA_FAILURE_HINTS,
    CAPTCHA_TEXT_HINTS,
    HIGHLIGHT_MIN_CONF,
    detect_gap,
    detect_gap_by_edge,
    detect_gap_by_highlight,
)

W, H, TILE = 320, 180, 52


def _bg_with_square(x: int, y: int, size: int = TILE, bright: int = 230, bg: int = 90, seed: int = 1):
    """构造一张带噪纹理背景 + 一块明亮方块（模拟该站点的高亮缺口）。"""
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), bg, dtype=np.uint8)
    noise = rng.integers(-15, 15, size=(H, W, 3))
    img = np.clip(img.astype(int) + noise, 0, 255).astype(np.uint8)
    img[y:y + size, x:x + size] = bright
    return img


def _png(img) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def test_highlight_finds_square_fullscan():
    img = _bg_with_square(x=200, y=20)
    bx, by, score = detect_gap_by_highlight(_png(img), TILE, prior_y=None)
    assert score >= HIGHLIGHT_MIN_CONF
    assert abs(bx - 200) <= 3
    assert abs(by - 20) <= 3


def test_highlight_band_limits_scan():
    # 方块在 y=20，把拼图块中心 y 放到下方 → 带限扫描应扫不到它（低分）
    img = _bg_with_square(x=200, y=20)
    _, _, band_score = detect_gap_by_highlight(
        _png(img), TILE, prior_y=110 + TILE / 2, bg_render_h=H,
        tile_rect={"x": 0, "y": 110, "width": TILE, "height": TILE},
        bg_rect={"x": 0, "y": 0, "width": W, "height": H},
    )
    assert band_score < HIGHLIGHT_MIN_CONF


def test_detect_gap_primary_is_band_highlight_when_it_hits():
    img = _bg_with_square(x=200, y=20)
    tile_rect = {"x": 0, "y": 20, "width": TILE, "height": TILE}
    bg_rect = {"x": 0, "y": 0, "width": W, "height": H}
    gap_x, _conf, method, cands = asyncio.run(
        detect_gap(_png(img), TILE, tile_screenshot=None, tile_rect=tile_rect, bg_rect=bg_rect)
    )
    assert abs(gap_x - 200) <= 4
    assert method == "highlight"
    assert cands and cands[0]["method"] == "highlight"


def test_detect_gap_falls_back_to_fullscan_when_band_misses():
    """回归：带限高亮漏检时，全图高亮应成为首选（对应 2026-08-14 真实失败样本）。

    真实样本中带限版本未采信、回退到边缘检测并锁在路面结构上，偏 52~75px；
    全图高亮则稳定命中真缺口。
    """
    img = _bg_with_square(x=200, y=20)
    tile_rect = {"x": 0, "y": 110, "width": TILE, "height": TILE}  # 中心 y=136 → 带限扫不到 y=20
    bg_rect = {"x": 0, "y": 0, "width": W, "height": H}
    gap_x, _conf, method, cands = asyncio.run(
        detect_gap(_png(img), TILE, tile_screenshot=None, tile_rect=tile_rect, bg_rect=bg_rect)
    )
    assert abs(gap_x - 200) <= 4, f"全图高亮应命中 200，实际 {gap_x}"
    assert method == "highlight_full"
    assert cands and cands[0]["method"] == "highlight_full"


def test_detect_gap_without_dom_rects_still_scans():
    # DOM 位置缺失时也应能靠全图高亮定位（覆盖 DOM 提取失败的情况）
    img = _bg_with_square(x=180, y=40)
    gap_x, _conf, method, _cands = asyncio.run(
        detect_gap(_png(img), TILE, tile_screenshot=None, tile_rect=None, bg_rect=None)
    )
    assert abs(gap_x - 180) <= 4
    assert method == "highlight_full"


def test_edge_detector_returns_tuple():
    img = _bg_with_square(x=200, y=20)
    gap_x, conf = detect_gap_by_edge(_png(img), TILE)
    assert isinstance(gap_x, int)
    assert isinstance(conf, float)


def test_captcha_keywords_split_failure_hints():
    # C 修复：失败提示词不得混进"验证码存在"判定
    for word in ("验证失败", "验证超时"):
        assert word not in CAPTCHA_TEXT_HINTS
        assert word in CAPTCHA_FAILURE_HINTS
