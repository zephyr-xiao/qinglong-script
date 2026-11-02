# -*- coding: utf-8 -*-
"""拟人化轨迹生成单测。"""
import pytest

# core.slider 依赖 cv2/numpy；缺失时整文件跳过，不能让收集阶段直接崩
pytest.importorskip("cv2", reason="core.slider 依赖 opencv")
pytest.importorskip("numpy", reason="core.slider 依赖 numpy")

from core.slider import generate_human_track


@pytest.mark.parametrize("distance", [40, 100, 180, 260])
def test_track_reaches_distance(distance):
    for _ in range(20):
        tracks = generate_human_track(distance)
        assert tracks, "轨迹不应为空"
        total = sum(t["x"] for t in tracks)
        # 允许 ±3px 的末段过冲/回调
        assert abs(total - distance) <= 3.0


@pytest.mark.parametrize("distance", [40, 100, 180, 260])
def test_track_shape_and_bounds(distance):
    for _ in range(20):
        tracks = generate_human_track(distance)
        # 目标点数受 10~25 约束，但 <0.5px 的步会被跳过，短距离下点数会更少
        assert 5 <= len(tracks) <= 28
        for t in tracks:
            assert set(t.keys()) == {"x", "y", "delay"}
            assert t["delay"] >= 8  # 每步延时下限
            assert abs(t["y"]) < 5  # Y 轴抖动幅度


# ====================== 滑动距离 / 缺口行换算 ======================

from core.slider import compute_slide_distance, resolve_gap_y


def test_slide_distance_equals_gap_x():
    """站点语义：鼠标位移 = 缺口 x（拼图块预览固定在 left:0，手柄偏移不参与计算）。"""
    assert compute_slide_distance(128) == 128
    assert compute_slide_distance(128, 1.0, 268) == 128


def test_slide_distance_applies_render_scale():
    # 背景图被 CSS 缩放一半时，渲染像素位移应为缺口 x 的一半
    assert compute_slide_distance(200, 0.5, 268) == 100


def test_slide_distance_clamps_to_site_max():
    # 站点 maxX = width - pieceSize，超出会被前端 clamp
    assert compute_slide_distance(300, 1.0, 268) == 268
    assert compute_slide_distance(0, 1.0, 268) == 5  # 下限兜底，避免原地不动


def test_slide_distance_offset_for_retry():
    assert compute_slide_distance(128, 1.0, 268, offset=-4) == 124


def test_resolve_gap_y_subtracts_bg_origin():
    """回归：DOM 给的是屏幕坐标，必须先减背景图原点（本次失败的根因之一）。"""
    tile = {"x": 800, "y": 486, "width": 52, "height": 52}
    bg = {"x": 800, "y": 415, "width": 320, "height": 180}
    assert resolve_gap_y(tile, bg) == 71


def test_resolve_gap_y_prefers_fresh_api_value():
    tile = {"x": 800, "y": 486, "width": 52, "height": 52}
    bg = {"x": 800, "y": 415, "width": 320, "height": 180}
    # 接口值与 DOM 一致（±3 内）→ 用接口值
    assert resolve_gap_y(tile, bg, net_y=72) == 72
    # 接口值明显是上一张验证码的 → 以 DOM 为准
    assert resolve_gap_y(tile, bg, net_y=20) == 71


def test_resolve_gap_y_scales_to_natural_coords():
    tile = {"x": 0, "y": 90, "width": 52, "height": 52}
    bg = {"x": 0, "y": 0, "width": 160, "height": 90}
    # 渲染 160x90、原始 320x180 → 自然坐标是渲染坐标的 2 倍
    assert resolve_gap_y(tile, bg, None, 2.0) == 180
