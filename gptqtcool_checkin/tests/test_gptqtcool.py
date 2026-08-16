# -*- coding: utf-8 -*-
"""
gptqtcool_checkin 单元测试（unittest 标准库，零新依赖）。

运行方式（在 gptqtcool_checkin 目录下）：
  python -m unittest discover -s tests -v

依赖：opencv-python/numpy/playwright（与脚本运行时一致）。
"""
import os
import sys
import json
import asyncio
import unittest
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gptqtcool_checkin as gq


def _make_synthetic_captcha(piece_x=20, gap_x=250, piece_y=60, size=40,
                            w=400, h=200) -> tuple[bytes, bytes]:
    """
    生成模拟滑块验证码背景图与拼图块（确定性 seed）：
      - 平滑高斯纹理背景
      - 拼图块（piece_x 处）：缺口图案 + 轻微噪声（模拟渲染差异）+ 绿边
      - 缺口（gap_x 处）：黑边方框，内部为背景原图案
    返回 (master_png_bytes, tile_png_bytes)。
    """
    rng = np.random.default_rng(5)
    bg = rng.normal(128, 45, (h, w)).astype(np.float32)
    bg = cv2.GaussianBlur(bg, (9, 9), 0)
    bg8 = np.clip(bg, 0, 255).astype(np.uint8)
    # 中频结构特征（随机椭圆块）：让背景图案具有唯一性，模板匹配区分度高
    # （纯平滑纹理会让 TM_CCORR_NORMED 在平坦区域误报）
    for _ in range(8):
        cx = int(rng.integers(30, w - 30))
        cy = int(rng.integers(30, h - 30))
        r1 = int(rng.integers(12, 35))
        r2 = int(rng.integers(12, 35))
        shade = int(rng.integers(60, 200))
        cv2.ellipse(bg8, (cx, cy), (r1, r2), int(rng.integers(0, 180)), 0, 360, shade, -1)
    bg8 = cv2.GaussianBlur(bg8, (5, 5), 0)
    master = cv2.cvtColor(bg8, cv2.COLOR_GRAY2BGR)

    # 缺口图案 = 背景 (gap_x..gap_x+size) 区域（缺口处显示的是背景原样）
    pattern = master[piece_y:piece_y + size, gap_x:gap_x + size].copy()
    # 拼图块 = 从缺口处"抠"下的图案 + 噪声 + 绿边（真实场景拼图块内容与缺口一致）
    noise = rng.normal(0, 8, (size, size, 1)).astype(np.float32)
    tile_region = np.clip(pattern.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    master[piece_y:piece_y + size, piece_x:piece_x + size] = tile_region
    cv2.rectangle(master, (piece_x, piece_y), (piece_x + size, piece_y + size),
                  (0, 255, 0), 1)
    # 缺口黑框
    cv2.rectangle(master, (gap_x, piece_y), (gap_x + size, piece_y + size),
                  (0, 0, 0), 1)

    ok1, buf_m = cv2.imencode(".png", master)
    # tile = 拼图块处内容（含噪声；inset 1px 去掉绿边，模拟真实 tile 截图）
    tile_img = master[piece_y + 1:piece_y + size - 1, piece_x + 1:piece_x + size - 1]
    ok2, buf_t = cv2.imencode(".png", tile_img)
    assert ok1 and ok2
    return buf_m.tobytes(), buf_t.tobytes()


class TestTemplateMatchingAntiSelfMatch(unittest.TestCase):
    """模板匹配防自匹配回归测试：拼图块区域必须涂黑，否则返回拼图块自身位置。"""

    def test_masked_master_locates_real_gap(self):
        master_bytes, tile_bytes = _make_synthetic_captcha()
        # 模拟 detect_gap 的涂黑逻辑：拼图块位于背景图 (20,60) 尺寸 40，pad 4
        img = cv2.imdecode(np.frombuffer(master_bytes, np.uint8), cv2.IMREAD_COLOR)
        cv2.rectangle(img, (20 - 4, 60 - 4), (20 + 40 + 4, 60 + 40 + 4), (0, 0, 0), -1)
        ok, buf = cv2.imencode(".png", img)
        assert ok

        gap_x, conf, _method = gq.detect_gap_by_template(buf.tobytes(), tile_bytes)
        # 涂黑后必须命中真缺口（250 附近），而非拼图块位置（20 附近）
        self.assertLess(abs(gap_x - 250), 10, f"缺口定位偏差过大: gap_x={gap_x}")
        self.assertGreater(conf, 0.3, f"置信度不足: {conf}")

    def test_unmasked_master_returns_piece_position(self):
        # 回归对照：不涂黑时，模板与拼图块自身位置完全一致（分数最高），
        # 返回的是拼图块位置（20 附近）——这正是修复前验证码必失败的原因
        master_bytes, tile_bytes = _make_synthetic_captcha()
        gap_x, conf, _method = gq.detect_gap_by_template(master_bytes, tile_bytes)
        self.assertLess(gap_x, 80, f"未涂黑时应命中拼图块位置，实际 gap_x={gap_x}")


class TestEdgeDetectionWidth(unittest.TestCase):
    """边缘检测宽度参数回归测试：缺口宽必须用拼图块宽，而非滑块宽。"""

    def test_width_must_equal_gap_width(self):
        # 合成缺口/拼图块宽 52 的图（与真实站点拼图块尺寸一致）
        master_bytes, _ = _make_synthetic_captcha(piece_x=20, gap_x=250, size=52,
                                                  w=500, h=250)
        gap52, _ = gq.detect_gap_by_edge(master_bytes, 52)
        gap38, _ = gq.detect_gap_by_edge(master_bytes, 38)
        # w=52（缺口真实宽度）应准确定位
        self.assertLess(abs(gap52 - 250), 10, f"w=52 应准确定位缺口,实际 gap_x={gap52}")
        # w=38（滑块宽度，修复前误用）应明显偏差
        self.assertGreaterEqual(abs(gap38 - 250), 10, f"w=38 应明显偏差,实际 gap_x={gap38}")


class TestTemplateDriftRejection(unittest.TestCase):
    """模板匹配"漂移拒绝"回归测试：匹配窗口落在拼图块区域(涂黑区)必须被拒绝。"""

    def test_reject_match_overlapping_piece_area(self):
        # 缺口/拼图块宽 52（与真实站点一致）
        master_bytes, tile_bytes = _make_synthetic_captcha(size=52)
        # 模拟 detect_gap_by_template 返回拼图块位置(25)且置信度虚高(0.99)：
        # 拼图块位于 (20,60) 尺寸 52，涂黑区约 (13,79)，窗口(25-77)与其重叠。
        # 缺少涂黑区约束时 detect_gap 会错误采信为 template 分支（今日日志 gapX=25 即此问题）。
        with mock.patch.object(
            gq, 'detect_gap_by_template',
            return_value=(25, 0.99, 'TM_CCORR_NORMED'),
        ):
            gap_x, conf, method = asyncio.run(gq.detect_gap(
                master_bytes,
                52,
                tile_bytes,
                master_rendered_size=(400, 200),
                tile_rendered_size=(52, 52),
                tile_rect={'x': 20, 'y': 60, 'width': 52, 'height': 52},
                bg_rect={'x': 0, 'y': 0, 'width': 400, 'height': 200},
            ))
        self.assertNotEqual(method, 'template',
                            "漂移到拼图块区域的模板匹配结果应被拒绝并回退边缘检测")
        # 回退的边缘检测（w=52）应定位到真缺口 250 附近
        self.assertLess(abs(gap_x - 250), 30, f"回退边缘检测应命中真缺口,实际 gap_x={gap_x}")


class TestServerJNotify(unittest.TestCase):
    """Server酱 自实现推送:成功判定、重试、失败上限、URL 编码。"""

    def _patch_urlopen(self, return_value=None, side_effect=None):
        """mock urllib.request.urlopen。

        注意: MagicMock 实例可迭代,直接作为 side_effect 会被 mock 当作序列逐个消费,
        导致 urlopen 返回默认 MagicMock。因此单个响应用 return_value,异常/响应序列才用 side_effect。
        """
        patcher = mock.patch.object(
            gq.urllib.request, "urlopen",
            return_value=return_value,
            side_effect=side_effect,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ok_response(self, code=0):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({"code": code, "message": "ok"}).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    def test_success_single_request(self):
        self._patch_urlopen(return_value=self._ok_response())
        with mock.patch.object(gq, "_SERVERJ_KEY", "TESTKEY"):
            ok = gq.send_serverj("签到成功", "已续期")
        self.assertTrue(ok)
        self.assertEqual(gq.urllib.request.urlopen.call_count, 1)

    def test_retry_then_success(self):
        # 首次网络异常,重试后成功 → 应返回 True 且实际请求 2 次
        self._patch_urlopen(side_effect=[ConnectionError("Connection reset by peer"), self._ok_response()])
        with mock.patch.object(gq, "_SERVERJ_KEY", "TESTKEY"), mock.patch.object(gq.time, "sleep"):
            ok = gq.send_serverj("签到成功", "已续期")
        self.assertTrue(ok)
        self.assertEqual(gq.urllib.request.urlopen.call_count, 2)

    def test_all_fail_returns_false(self):
        # 持续失败 → 返回 False 且仅重试到 3 次上限
        self._patch_urlopen(side_effect=[ConnectionError("x"), ConnectionError("x"), ConnectionError("x")])
        with mock.patch.object(gq, "_SERVERJ_KEY", "TESTKEY"), mock.patch.object(gq.time, "sleep"):
            ok = gq.send_serverj("签到成功", "已续期")
        self.assertFalse(ok)
        self.assertEqual(gq.urllib.request.urlopen.call_count, 3)

    def test_nonzero_code_fails_fast(self):
        # 服务端明确返回非 0 code → 不重试,直接失败
        self._patch_urlopen(return_value=self._ok_response(code=400))
        with mock.patch.object(gq, "_SERVERJ_KEY", "TESTKEY"):
            ok = gq.send_serverj("签到成功", "已续期")
        self.assertFalse(ok)
        self.assertEqual(gq.urllib.request.urlopen.call_count, 1)

    def test_no_key_skips(self):
        self._patch_urlopen(return_value=self._ok_response())
        with mock.patch.object(gq, "_SERVERJ_KEY", None):
            ok = gq.send_serverj("签到成功", "已续期")
        self.assertFalse(ok)
        self.assertEqual(gq.urllib.request.urlopen.call_count, 0)

    def test_url_contains_encoded_params(self):
        self._patch_urlopen(return_value=self._ok_response())
        with mock.patch.object(gq, "_SERVERJ_KEY", "TESTKEY"):
            gq.send_serverj("标题 A&B", "正文=中文")
        url = gq.urllib.request.urlopen.call_args[0][0].full_url
        self.assertIn("https://sctapi.ftqq.com/TESTKEY.send?", url)
        self.assertIn("title=", url)
        self.assertIn("desp=", url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
