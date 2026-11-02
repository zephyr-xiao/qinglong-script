# -*- coding: utf-8 -*-
"""缺口检测单测：合成图验证高亮定位、多候选排序，以及"带限漏检→全图补位"回归。"""
import asyncio

import pytest

# 本文件与 core.detect 都依赖 cv2/numpy；缺失时整文件跳过，不能让收集阶段直接崩
cv2 = pytest.importorskip("cv2", reason="core.detect 依赖 opencv")
np = pytest.importorskip("numpy", reason="core.detect 依赖 numpy")

from core.detect import (
    CAPTCHA_FAILURE_HINTS,
    CAPTCHA_TEXT_HINTS,
    CAPTCHA_OVERLAY_HINTS,
    FRAME_STROKE_MIN_SCORE,
    HIGHLIGHT_MIN_CONF,
    detect_gap,
    detect_gap_by_edge,
    detect_gap_by_frame_stroke,
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
    # 方块在 y=20，把先验行放到下方 → 带限扫描应扫不到它（低分）
    img = _bg_with_square(x=200, y=20)
    _, _, band_score = detect_gap_by_highlight(
        _png(img), TILE, prior_y=110 + TILE / 2,
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
    # 实心亮块同时满足"四边更亮"，描边层会先命中；候选列表按可靠度排序且已去重
    assert method in ("frame", "highlight")
    assert cands and cands[0]["method"] == method


def test_highlight_full_scan_backfills_when_band_misses():
    """回归：带限漏检时，全图高亮应仍能命中真缺口（对应 2026-08-14 真实失败样本）。

    真实样本中带限版本未采信、回退到边缘检测并锁在路面结构上，偏 52~75px；
    全图高亮则稳定命中真缺口。
    """
    img = _bg_with_square(x=200, y=20)
    tile_rect = {"x": 0, "y": 110, "width": TILE, "height": TILE}  # 中心 y=136 → 带限扫不到 y=20
    bg_rect = {"x": 0, "y": 0, "width": W, "height": H}
    band_x, _by, band_score = detect_gap_by_highlight(
        _png(img), TILE, prior_y=110 + TILE / 2, tile_rect=tile_rect, bg_rect=bg_rect
    )
    assert band_score < HIGHLIGHT_MIN_CONF
    full_x, _fy, full_score = detect_gap_by_highlight(
        _png(img), TILE, prior_y=None, tile_rect=tile_rect, bg_rect=bg_rect
    )
    assert full_score >= HIGHLIGHT_MIN_CONF
    assert abs(full_x - 200) <= 4, f"全图高亮应命中 200，实际 {full_x}"
    assert band_x != full_x


def test_detect_gap_without_dom_rects_still_scans():
    # DOM 位置缺失时也应能靠全图高亮定位（覆盖 DOM 提取失败的情况）
    img = _bg_with_square(x=180, y=40)
    gap_x, _conf, method, _cands = asyncio.run(
        detect_gap(_png(img), TILE, tile_screenshot=None, tile_rect=None, bg_rect=None)
    )
    assert abs(gap_x - 180) <= 4
    assert method in ("frame", "highlight_full")


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


# ====================== 描边方框（该站点真实形态） ======================

def _bg_with_stroke_frame(
    x: int, y: int, size: int = TILE,
    glare: tuple[int, int] | None = None, seed: int = 7,
    bg_level: int = 90, inner_level: int = 233,
):
    """构造真实形态样本：噪声背景 + 1px 纯白描边方框 + 半透明白内部。

    站点服务端把拼图块裁走后，会在缺口处画这样一个方框（描边 250~254）。
    glare 用来模拟同一张背景图里固定的太阳反光——旧的高亮检测就是被它抢分的。
    """
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), bg_level, dtype=np.uint8)
    img = np.clip(img.astype(int) + rng.integers(-12, 12, size=(H, W, 3)), 0, 255).astype(np.uint8)
    img[y:y + size, x:x + size] = inner_level
    cv2.rectangle(img, (x, y), (x + size - 1, y + size - 1), (254, 254, 254), 1)
    if glare:
        cv2.circle(img, glare, 26, (252, 252, 252), -1)
    return img


def test_frame_stroke_hits_exact():
    img = _bg_with_stroke_frame(x=179, y=61)
    x, y, score = detect_gap_by_frame_stroke(_png(img), 61, TILE)
    assert score >= FRAME_STROKE_MIN_SCORE
    assert (x, y) == (179, 61), f"描边检测应精确命中 (179,61)，实际 ({x},{y})"


@pytest.mark.parametrize("dy", [-2, -1, 1, 2])
def test_frame_stroke_tolerates_y_offset(dy):
    # DOM 取整/接口换算可能差 1~2px，仍在 y_tol 内应命中同一 x
    img = _bg_with_stroke_frame(x=141, y=56)
    x, _y, score = detect_gap_by_frame_stroke(_png(img), 56 + dy, TILE, y_tol=2)
    assert score >= FRAME_STROKE_MIN_SCORE
    assert x == 141


def test_frame_stroke_ignores_sun_glare():
    """同一张背景图里的固定亮斑不应抢走描边检测（旧高亮检测的翻车点）。"""
    # 亮天空背景 + 反光斑比方框内部更亮，纯亮度判据必然被反光吸走
    img = _bg_with_stroke_frame(x=200, y=90, glare=(128, 47), bg_level=200, inner_level=212)
    x, _y, score = detect_gap_by_frame_stroke(_png(img), 90, TILE)
    assert x == 200, f"描边检测应忽略反光斑命中 200，实际 {x}"
    # 对照：纯亮度判据在亮天空+反光下会被拉偏（真实验证时 6/8 锁在固定假位置），
    # 所以它只能当兜底；这里只断言"它也给出了高分"，不锁死它的具体位置
    _hl_x, _hl_y, hl_score = detect_gap_by_highlight(_png(img), TILE, prior_y=None)
    assert hl_score >= HIGHLIGHT_MIN_CONF


def test_detect_gap_prefers_frame_over_highlight():
    img = _bg_with_stroke_frame(x=200, y=90, glare=(128, 47))
    tile_rect = {"x": 0, "y": 90, "width": TILE, "height": TILE}
    bg_rect = {"x": 0, "y": 0, "width": W, "height": H}
    gap_x, _conf, method, cands = asyncio.run(
        detect_gap(_png(img), TILE, gap_y=90, tile_rect=tile_rect, bg_rect=bg_rect)
    )
    assert (gap_x, method) == (200, "frame")
    assert cands[0]["y"] == 90


def test_highlight_prior_y_out_of_range_falls_back():
    """回归：prior_y 越界（历史上把屏幕坐标当图像坐标传）时不能崩，也不能漏检。"""
    img = _bg_with_square(x=200, y=20)
    x, _y, score = detect_gap_by_highlight(_png(img), TILE, prior_y=512)
    assert score >= HIGHLIGHT_MIN_CONF
    assert abs(x - 200) <= 4
