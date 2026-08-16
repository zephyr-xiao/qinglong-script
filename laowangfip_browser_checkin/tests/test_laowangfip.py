# -*- coding: utf-8 -*-
"""
laowangfip_browser_checkin 单元测试（unittest 标准库，零新依赖）。

运行方式（在 laowangfip_browser_checkin 目录下）：
  python -m unittest discover -s tests -v

依赖：opencv-python-headless / numpy / playwright（与脚本运行时一致）。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import laowangfip_browser_checkin as lw


class TestParseCredentials(unittest.TestCase):
    """LWFIP_ACCOUNTS 解析：& 与换行分隔、# 在密码内、格式错误跳过。"""

    def test_single_account(self):
        self.assertEqual(lw.parse_credentials("user1#pass1"), [("user1", "pass1")])

    def test_ampersand_multiple(self):
        self.assertEqual(
            lw.parse_credentials("u1#p1&u2#p2"),
            [("u1", "p1"), ("u2", "p2")],
        )

    def test_newline_multiple(self):
        self.assertEqual(
            lw.parse_credentials("u1#p1\nu2#p2"),
            [("u1", "p1"), ("u2", "p2")],
        )

    def test_password_contains_hash(self):
        # split("#", 1) 只切第一处，# 之后全部算密码
        self.assertEqual(lw.parse_credentials("u1#p1#p2"), [("u1", "p1#p2")])

    def test_skip_malformed(self):
        self.assertEqual(
            lw.parse_credentials("u1#p1&badline&u2#p2"),
            [("u1", "p1"), ("u2", "p2")],
        )

    def test_whitespace_trimmed(self):
        self.assertEqual(lw.parse_credentials("  u1  #  p1  "), [("u1", "p1")])

    def test_empty_input(self):
        self.assertEqual(lw.parse_credentials(""), [])
        self.assertEqual(lw.parse_credentials("   "), [])


class TestMaskAccount(unittest.TestCase):
    """账号脱敏：邮箱/短名/普通名。"""

    def test_email(self):
        self.assertEqual(lw.mask_account("abc123@qq.com"), "ab****@qq.com")

    def test_short_email(self):
        self.assertEqual(lw.mask_account("a@qq.com"), "a*@qq.com")

    def test_normal_name(self):
        self.assertEqual(lw.mask_account("abcdefgh"), "ab****gh")

    def test_short_name(self):
        self.assertEqual(lw.mask_account("ab"), "a*")

    def test_empty(self):
        self.assertEqual(lw.mask_account(""), "")


class TestCountqianStats(unittest.TestCase):
    """签到统计解析：白名单字段、单位映射、未知字段忽略。"""

    def test_format_value_with_known_units(self):
        self.assertEqual(lw._format_countqian_value("连续签到", "12", "天"), "12 天")
        self.assertEqual(lw._format_countqian_value("签到等级", "3", "级"), "3 级")
        self.assertEqual(lw._format_countqian_value("积分奖励", "10", "分"), "10 分")

    def test_format_value_unknown_label(self):
        self.assertEqual(lw._format_countqian_value("未知字段", "5"), "5")

    def test_format_value_empty(self):
        self.assertEqual(lw._format_countqian_value("连续签到", ""), "")

    def test_parse_full_list(self):
        items = ["连续签到：12天", "签到等级：3级", "积分奖励：10分", "总天数：30天"]
        self.assertEqual(lw.parse_countqian_stats(items), {
            "连续签到": "12 天",
            "签到等级": "3 级",
            "积分奖励": "10 分",
            "总天数": "30 天",
        })

    def test_parse_ignores_unknown_labels(self):
        self.assertEqual(lw.parse_countqian_stats(["未知字段：5", "随便写"]), {})

    def test_parse_duplicate_label_takes_first(self):
        self.assertEqual(
            lw.parse_countqian_stats(["连续签到：1天", "连续签到：2天"]),
            {"连续签到": "1 天"},
        )

    def test_parse_empty(self):
        self.assertEqual(lw.parse_countqian_stats([]), {})


class TestGenerateTrajectory(unittest.TestCase):
    """人类化拖动轨迹：端点精确、时间单调、y 抖动受限。"""

    def test_zero_distance_returns_empty(self):
        self.assertEqual(lw.generate_trajectory(0), [])

    def test_negative_distance_returns_empty(self):
        self.assertEqual(lw.generate_trajectory(-5), [])

    def test_endpoint_lands_on_target(self):
        for _ in range(10):
            pts = lw.generate_trajectory(100)
            self.assertGreater(len(pts), 20)
            self.assertEqual(pts[-1][0], 100)

    def test_timestamps_strictly_increasing(self):
        pts = lw.generate_trajectory(100)
        times = [p[2] for p in pts]
        self.assertTrue(all(times[i] < times[i + 1] for i in range(len(times) - 1)))

    def test_y_jitter_bounded(self):
        for _ in range(20):
            for _, y, _ in lw.generate_trajectory(50):
                self.assertLessEqual(abs(y), 2)

    def test_x_nonnegative_and_within_overshoot(self):
        for _ in range(10):
            for x, _, _ in lw.generate_trajectory(200):
                self.assertGreaterEqual(x, 0)
                self.assertLessEqual(x, 215)  # 200 + 过冲上限 15


class TestRetryableError(unittest.TestCase):
    """重试错误分类：网络/验证码/签到流程可重试，业务错误不重试。"""

    def test_network_and_timeout_retryable(self):
        for msg in (
            "登录超时: 页面加载超时",
            "浏览器启动失败: Executable doesn't exist",
            "Task timeout",
            "proxy error 连接代理失败",
            "connection refused 连接被拒",
            "net::ERR_PROXY_CONNECTION_FAILED",
            # 导航被中断 / Chromium 错误页（本脚本曾因此误判判死）
            'Page.goto: Navigation to "https://laowangfip372.vip/member.php'
            '?mod=logging&action=login" is interrupted by another navigation to '
            '"chrome-error://chromewebdata/"',
            "签到超时: 等待超时",
        ):
            self.assertTrue(lw.is_retryable_error(msg), f"应可重试: {msg}")

    def test_captcha_retryable(self):
        for msg in (
            "验证码识别失败（服务端判定未通过 尝试5, conf=0.21）",
            "签到验证码识别失败（找不到滑块或弹窗位置）",
            "登录失败: 验证码未通过",
        ):
            self.assertTrue(lw.is_retryable_error(msg), f"应可重试: {msg}")

    def test_sign_flow_retryable(self):
        for msg in (
            "签到后未到达验证页（当前 /forum.php）",
            "提交后仍在验证页（验证码可能未真正通过）",
            "签到结果未知（最终 URL: /forum.php）",
            "找不到 J_chkitot 签到按钮（可能页面结构变化）",
            "找不到签到提交按钮",
        ):
            self.assertTrue(lw.is_retryable_error(msg), f"应可重试: {msg}")

    def test_business_error_not_retryable(self):
        for msg in (
            "登录失败: 密码错误",
            "登录失败: 用户名不存在",
            "登录失败: 您的账号已被禁用",
            "登录可能失败（仍在登录页）",
            "找不到用户名输入框（页面可能未加载完，或弹窗未关掉）",
            "签到失败: 操作失败",
        ):
            self.assertFalse(lw.is_retryable_error(msg), f"不应重试: {msg}")


class TestCookieDomain(unittest.TestCase):
    """Cookie domain 提取：hostname、去端口、换域名支持。"""

    def test_https_normal(self):
        self.assertEqual(lw.extract_cookie_domain("https://laowangfip372.vip"),
                         "laowangfip372.vip")

    def test_with_path(self):
        self.assertEqual(lw.extract_cookie_domain("https://laowangfip372.vip/forum.php"),
                         "laowangfip372.vip")

    def test_with_port(self):
        self.assertEqual(lw.extract_cookie_domain("https://laowangfip372.vip:8443"),
                         "laowangfip372.vip")

    def test_http(self):
        self.assertEqual(lw.extract_cookie_domain("http://example.com"), "example.com")

    def test_trailing_slash(self):
        self.assertEqual(lw.extract_cookie_domain("https://laowangfip372.vip/"),
                         "laowangfip372.vip")

    def test_empty_falls_back_to_default(self):
        self.assertEqual(lw.extract_cookie_domain(""), "laowangfip372.vip")


class TestCookieCache(unittest.TestCase):
    """会话 Cookie 缓存：读写往返、会话级 expires 清理、损坏容错。"""

    USERNAME = "test_cookie_user"

    def setUp(self):
        lw.remove_cookie_cache(self.USERNAME)

    def tearDown(self):
        lw.remove_cookie_cache(self.USERNAME)

    def test_save_and_load_roundtrip(self):
        cookies = [
            {"name": "uid", "value": "123", "domain": "laowangfip372.vip",
             "path": "/", "expires": 1800000000},
            {"name": "auth", "value": "abc", "domain": "laowangfip372.vip",
             "path": "/", "expires": -1},  # 会话级 Cookie
        ]
        context = mock.Mock()
        context.cookies = mock.AsyncMock(return_value=cookies)
        self.assertTrue(asyncio.run(lw.save_cookies_to_cache(context, self.USERNAME)))

        loaded = lw.load_cookies_from_cache(self.USERNAME)
        self.assertIsNotNone(loaded)
        by_name = {c["name"]: c for c in loaded}
        # 会话级 Cookie 的 expires 字段应被移除（否则 add_cookies 拒绝）
        self.assertNotIn("expires", by_name["auth"])
        # 持久 Cookie 的 expires 字段保留
        self.assertIn("expires", by_name["uid"])

    def test_load_missing_returns_none(self):
        self.assertIsNone(lw.load_cookies_from_cache("no_such_user_xyz"))

    def test_load_corrupted_returns_none(self):
        path = lw.cookie_cache_path(self.USERNAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertIsNone(lw.load_cookies_from_cache(self.USERNAME))

    def test_remove_deletes_file(self):
        path = lw.cookie_cache_path(self.USERNAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]")
        lw.remove_cookie_cache(self.USERNAME)
        self.assertFalse(os.path.exists(path))


class TestChromiumErrorPage(unittest.TestCase):
    """Chromium 错误页检测：chrome-error:// 命中，正常 URL / about:blank 不命中。"""

    @staticmethod
    def _page_with_url(url: str):
        page = mock.Mock()
        page.url = url
        return page

    def test_chromium_error_page_detected(self):
        page = self._page_with_url("chrome-error://chromewebdata/")
        self.assertTrue(asyncio.run(lw.is_chromium_error_page(page)))

    def test_normal_url_not_detected(self):
        page = self._page_with_url("https://laowangfip372.vip/forum.php")
        self.assertFalse(asyncio.run(lw.is_chromium_error_page(page)))

    def test_about_blank_not_detected(self):
        # 初始空白页不是错误页，不应误判（避免把正常首屏导航当网络失败）
        page = self._page_with_url("about:blank")
        self.assertFalse(asyncio.run(lw.is_chromium_error_page(page)))

    def test_url_read_raises_returns_false(self):
        # page.url 读取本身抛异常时容错返回 False
        page = mock.Mock()
        type(page).url = mock.PropertyMock(side_effect=Exception("boom"))
        self.assertFalse(asyncio.run(lw.is_chromium_error_page(page)))


def _make_synthetic_captcha(piece_x=60, target_x=250, piece_y=70, size=48,
                            w=420, h=300) -> bytes:
    """
    生成模拟 tncode 弹窗截屏（确定性 seed）：
      - 平滑高斯纹理背景（模拟真实背景图）
      - 目标缺口：黑边方框，框内为背景原图案
      - 拼图碎片：绿边方框，内部图案 = 从缺口处"抠"下来的背景图案 + 轻微噪声
        （真实场景碎片就是被抠走的缺口图案，二者内容一致；
        噪声模拟渲染差异，避免匹配分数逼近 1.0 触发退化分数排除）
    """
    rng = np.random.default_rng(7)
    bg = rng.normal(128, 45, (h, w)).astype(np.float32)
    bg = cv2.GaussianBlur(bg, (15, 15), 0)
    bg = np.clip(bg, 0, 255).astype(np.uint8)
    img = cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)

    # 碎片图案 = 缺口处背景图案的拷贝 + 噪声
    pattern = img[piece_y:piece_y + size, target_x:target_x + size].astype(np.float32)
    noise = rng.normal(0, 10, (size, size, 1)).astype(np.float32)
    fragment = np.clip(pattern + noise, 0, 255).astype(np.uint8)
    img[piece_y:piece_y + size, piece_x:piece_x + size] = fragment

    # 目标缺口：黑边方框（框内仍为背景原图案）
    # 注意：边线用 1px（贴近真实站点细边框），粗线会让轮廓 bbox 外扩
    # 并污染模板边缘像素（inset=4 去绿边是按细边框设计的）
    cv2.rectangle(img, (target_x, piece_y), (target_x + size, piece_y + size),
                  (0, 0, 0), 1)
    # 拼图碎片：绿边方框
    cv2.rectangle(img, (piece_x, piece_y), (piece_x + size, piece_y + size),
                  (0, 255, 0), 1)

    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


class TestSliderScreenshot(unittest.TestCase):
    """滑块识别核心算法回归测试：合成图定位误差 ≤5px 且置信度达标。"""

    def test_synthetic_captcha_locates_piece_and_target(self):
        piece_x, target_x, piece_y, size = 60, 250, 70, 48
        png = _make_synthetic_captcha(piece_x, target_x, piece_y, size)

        piece, target, conf = lw.solve_slider_from_screenshot(png, debug=False)

        self.assertGreater(conf, 0.3, f"置信度不足: {conf:.3f}")
        # 算法统一返回"去绿边后的内容左沿"坐标（真实值 + inset 4）
        self.assertAlmostEqual(piece, piece_x + 4, delta=5,
                               msg=f"拼图碎片定位偏差过大: piece={piece}")
        self.assertAlmostEqual(target, target_x + 4, delta=5,
                               msg=f"目标缺口定位偏差过大: target={target}")

    def test_invalid_bytes_returns_failure(self):
        piece, target, conf = lw.solve_slider_from_screenshot(b"not an image", debug=False)
        self.assertEqual((piece, target), (-1, -1))
        self.assertEqual(conf, 0.0)

    def test_no_green_piece_returns_failure(self):
        # 纯纹理背景无绿边碎片 → 应返回失败而不是乱猜
        rng = np.random.default_rng(3)
        bg = rng.integers(0, 255, (300, 420, 3), dtype=np.uint8)
        bg = cv2.GaussianBlur(bg, (11, 11), 0)
        ok, buf = cv2.imencode(".png", bg)
        assert ok
        piece, target, conf = lw.solve_slider_from_screenshot(buf.tobytes(), debug=False)
        self.assertEqual((piece, target), (-1, -1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
