# -*- coding: utf-8 -*-
"""拟人化轨迹生成单测。"""
import pytest

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
