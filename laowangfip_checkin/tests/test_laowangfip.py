# -*- coding: utf-8 -*-
"""laowangfip_checkin 单元测试（unittest，纯函数 + 合成图回归，不发真实请求）。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# 被测模块在上一级目录；缺 requests 时打桩后重载（保持与仓库其它脚本测试一致）
_MOD_PATH = Path(__file__).resolve().parent.parent / "laowangfip_checkin.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("laowangfip_checkin", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        stub = types.ModuleType("requests")
        stub.Session = object
        stub.exceptions = types.SimpleNamespace(RequestException=Exception)
        sys.modules["requests"] = stub
        spec.loader.exec_module(mod)
    return mod


m = _load_module()

import numpy as np  # noqa: E402
import cv2  # noqa: E402


# ---- 站点真实 JS（node 运行 generateSecurePayload）产出的基准值，用于对拍 ----
JS_GOLDEN_TRACK = [
    {"x": 100, "y": 300, "t": 0},
    {"x": 110, "y": 301, "t": 50},
    {"x": 130, "y": 300, "t": 120},
    {"x": 155, "y": 302, "t": 200},
]
JS_GOLDEN_TS = 1728000000000
JS_GOLDEN_OFFSET = 88.5
JS_GOLDEN_PAYLOAD = (
    "tn_r=88.50&track=PHUyCBkODBEDTBoADFsSGQAmNzMXEgMHGEoRGDM2KD0cCg0RAwpYRUVVRAYbLjUDDUNNEQ5dUFl2"
    "YnBeRlVcAQoNUUVcThxLDjk%2BFBRVXFcWUlVZdWFyUUBSXwMOD1lMW0IBXFtjeyoFSGpDUQ0BVX1nalpEVFwK"
    "AQxYQFlOCV9efmpySBJUWlo7FRIiM2ZTRUlaAwkBUUJcRgJdXX1tdlMIFRFHGAASIwElG1ddWB0JCFpHUEMAW1l%2B"
    "bHJSAQ8GA19TW2UzLRs2DwldXl0bV1NHHEsJJjcmCGgbCQFdGA%3D%3D&ts=1728000000000&sign=26b1d685"
)


class TestParseCredentials(unittest.TestCase):
    def test_multi_and_password_with_hash(self):
        creds = m.parse_credentials("user1#pa#ss&user2#pw2")
        self.assertEqual(creds, [("user1", "pa#ss"), ("user2", "pw2")])

    def test_newline_separator_and_skip_bad(self):
        creds = m.parse_credentials("a#1\nbroken\nb#2\n")
        self.assertEqual(creds, [("a", "1"), ("b", "2")])

    def test_empty(self):
        self.assertEqual(m.parse_credentials(""), [])


class TestMaskAccount(unittest.TestCase):
    def test_email(self):
        self.assertEqual(m.mask_account("abcdef@example.com"), "ab****@example.com")

    def test_short(self):
        self.assertEqual(m.mask_account("ab"), "a*")

    def test_normal(self):
        masked = m.mask_account("example_user")
        self.assertTrue(masked.startswith("ex"))
        self.assertNotIn("_user", masked)


class TestRetryableError(unittest.TestCase):
    def test_network_and_captcha_retryable(self):
        for txt in ("ConnectionError: proxy", "read timed out", "验证码识别失败",
                    "提交后仍在验证页", "签到结果未知"):
            self.assertTrue(m.is_retryable_error(txt), txt)

    def test_business_not_retryable(self):
        for txt in ("登录失败: 密码错误", "登录失败: 用户名不存在", "账号禁用"):
            self.assertFalse(m.is_retryable_error(txt), txt)


class TestTncodePayload(unittest.TestCase):
    def test_matches_site_js_golden(self):
        """与站点真实 JS 的 generateSecurePayload 输出逐字节一致。"""
        got = m.build_tncode_payload(JS_GOLDEN_TRACK, JS_GOLDEN_OFFSET, JS_GOLDEN_TS)
        self.assertEqual(got, JS_GOLDEN_PAYLOAD)

    def test_track_info_shape(self):
        info = m._track_info(JS_GOLDEN_TRACK)
        self.assertTrue(info["valid"])
        self.assertEqual(info["points"], 4)
        self.assertEqual(info["finalX"], 55)


class TestHumanTrack(unittest.TestCase):
    def test_endpoint_and_monotonic_time(self):
        track = m.make_human_track(120, start_x=200)
        self.assertEqual(track[-1]["x"], 320)
        self.assertTrue(all(track[i]["t"] <= track[i + 1]["t"] for i in range(len(track) - 1)))
        self.assertGreaterEqual(len(track), 30)  # 点数足够，避免 error_track


class TestSolveSlider(unittest.TestCase):
    @staticmethod
    def _synthetic_sprite(gap_x, piece_w=50, gap_y=65):
        """构造三联图：块0=带缺口背景，块1=拼图块，块2=完整背景。"""
        rng = np.random.default_rng(1234)
        base = rng.integers(0, 255, (150, 240, 3), dtype=np.uint8)
        panel_gap = base.copy()
        panel_gap[gap_y:gap_y + piece_w, gap_x:gap_x + piece_w] = 0  # 挖黑缺口
        panel_piece = base[gap_y:gap_y + piece_w, gap_x:gap_x + piece_w].copy()
        sprite = np.vstack([
            panel_gap,
            cv2.copyMakeBorder(panel_piece, gap_y, 150 - gap_y - piece_w,
                               0, 240 - piece_w, cv2.BORDER_CONSTANT, value=(0, 0, 0)),
            base,
        ])
        ok, buf = cv2.imencode(".png", sprite)
        assert ok
        return buf.tobytes()

    def test_gap_detection_within_5px(self):
        for gap_x in (20, 77, 118, 170):
            png = self._synthetic_sprite(gap_x)
            got = m.solve_slider_from_sprite(png)
            self.assertIsNotNone(got, f"gap_x={gap_x}")
            self.assertLessEqual(abs(got - gap_x), 5, f"gap_x={gap_x} got={got}")

    @staticmethod
    def _synthetic_double(gap_x, decoy_x, piece_y=65, decoy_y=15, size=48):
        """双缺口：真缺口与拼图块同一行，干扰缺口在别的行。"""
        rng = np.random.default_rng(7)
        base = rng.integers(0, 255, (150, 240, 3), dtype=np.uint8)
        panel_gap = base.copy()
        panel_gap[piece_y:piece_y + size, gap_x:gap_x + size] = 0
        panel_gap[decoy_y:decoy_y + size, decoy_x:decoy_x + size] = 0
        panel_piece = np.zeros((150, 240, 3), dtype=np.uint8)
        panel_piece[piece_y:piece_y + size, 0:size] = base[piece_y:piece_y + size, gap_x:gap_x + size]
        sprite = np.vstack([panel_gap, panel_piece, base])
        ok, buf = cv2.imencode(".png", sprite)
        assert ok
        return buf.tobytes()

    def test_double_gap_prefers_y_aligned_candidate(self):
        """真缺口（与拼图块 y 对齐）必须排在候选列表首位。"""
        png = self._synthetic_double(gap_x=140, decoy_x=60, piece_y=65, decoy_y=15)
        cands = m.solve_slider_candidates(png)
        self.assertIn(140, cands)
        self.assertEqual(cands[0], 140)

    def test_bad_image_returns_none(self):
        self.assertIsNone(m.solve_slider_from_sprite(b"not an image"))
        self.assertEqual(m.solve_slider_candidates(b"not an image"), [])


class TestParseForm(unittest.TestCase):
    HTML = (
        '<html><body>'
        '<form id="v2_captcha_form" action="plugin.php?id=k_misign:sign&x=1">'
        '<input type="hidden" name="formhash" value="abc123">'
        '<input type="text" name="foo" value="bar">'
        '<div id="tncode">点击验证</div>'
        '<button id="submit-btn" type="submit">提交</button>'
        '</form></body></html>'
    )

    def test_extract_action_and_fields(self):
        action, fields = m.parse_form(self.HTML, "v2_captcha_form")
        self.assertEqual(action, "plugin.php?id=k_misign:sign&x=1")
        self.assertEqual(fields.get("formhash"), "abc123")
        self.assertEqual(fields.get("foo"), "bar")

    def test_missing_form(self):
        action, fields = m.parse_form("<html></html>", "v2_captcha_form")
        self.assertIsNone(action)
        self.assertEqual(fields, {})


class TestCookieCache(unittest.TestCase):
    def test_roundtrip_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.COOKIE_CACHE_DIR = Path(tmp)
            path = m.cookie_cache_path("someone")
            self.assertTrue(path.name.startswith("cookie_"))
            self.assertNotIn("someone", path.name)  # 文件名不含明文

            path.write_text(json.dumps([{"name": "sid", "value": "v", "domain": "x", "path": "/"}]),
                            encoding="utf-8")
            loaded = m.load_cookies_from_cache("someone")
            self.assertEqual(loaded[0]["name"], "sid")

            m.remove_cookie_cache("someone")
            self.assertIsNone(m.load_cookies_from_cache("someone"))

    def test_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.COOKIE_CACHE_DIR = Path(tmp)
            m.cookie_cache_path("x").write_text("{not json", encoding="utf-8")
            self.assertIsNone(m.load_cookies_from_cache("x"))


class TestMaskProxy(unittest.TestCase):
    def test_hides_credentials(self):
        self.assertEqual(m.mask_proxy("http://user:pass@host:7890"), "http://***@host:7890")

    def test_plain_unchanged(self):
        self.assertEqual(m.mask_proxy("http://127.0.0.1:7890"), "http://127.0.0.1:7890")

    def test_empty(self):
        self.assertEqual(m.mask_proxy(""), "")


class _FakeResp:
    def __init__(self, text, url=""):
        self.text = text
        self.url = url
        self.content = text.encode("utf-8")


class _FakeSession:
    """按调用顺序返回预设响应的假会话，用于驱动 sign_with_requests（不发真实请求）。"""

    def __init__(self, get_responses, post_response):
        self._timeout = 30
        self._get_responses = list(get_responses)
        self._post_response = post_response
        self.posted = None

    def get(self, url, **kw):
        return self._get_responses.pop(0)

    def post(self, url, data=None, **kw):
        self.posted = data
        return self._post_response


_SIGN_UNSIGNED = ('<a class="J_chkitot" '
                  'href="plugin.php?id=k_misign:sign&amp;operation=qiandao&amp;formhash=abc">签到</a>')
_QIANDAO_PAGE = ('<form id="v2_captcha_form" action="plugin.php?id=k_misign:sign&amp;x=1">'
                 '<input type="hidden" name="formhash" value="abc">'
                 '<div id="tncode"></div><button id="submit-btn">提交</button></form>')
_STATIC_LABELS = '<ul class="countqian cl"><li><h4>连续签到</h4></li></ul>'


class TestSignResultLogic(unittest.TestCase):
    """回归：静态标签「连续签到」不得被误判为签到成功（签到页恒含该标签）。"""

    def test_failure_page_not_reported_success(self):
        fail_page = _STATIC_LABELS + '<form id="v2_captcha_form" action="x"></form>'
        sess = _FakeSession([_FakeResp(_SIGN_UNSIGNED), _FakeResp(_QIANDAO_PAGE)],
                            _FakeResp(fail_page))
        with mock.patch.object(m, "pass_tncode", return_value="token_ok"):
            result = m.sign_with_requests(sess, "https://x", False, 3)
        self.assertFalse(result["success"], result)

    def test_btnvisted_after_submit_is_success(self):
        sess = _FakeSession(
            [_FakeResp(_SIGN_UNSIGNED), _FakeResp(_QIANDAO_PAGE),
             _FakeResp('<span class="btn btnvisted"></span>')],
            _FakeResp("<html>处理中</html>"),
        )
        with mock.patch.object(m, "pass_tncode", return_value="token_ok"):
            result = m.sign_with_requests(sess, "https://x", False, 3)
        self.assertTrue(result["success"], result)

    def test_already_signed_shortcut(self):
        sess = _FakeSession([_FakeResp('<span class="btn btnvisted"></span>')], _FakeResp(""))
        result = m.sign_with_requests(sess, "https://x", False, 3)
        self.assertTrue(result["success"])


class TestFetchSignStats(unittest.TestCase):
    PAGE = (
        '<ul class="countqian cl">'
        '<li><h4>连续签到</h4><p><input type="hidden" class="hidnum" id="lxdays" value="2"></p></li>'
        '<li><h4>签到等级</h4><p><input type="hidden" class="hidnum" value="1"></p></li>'
        '<li><h4>积分奖励</h4><p><input type="hidden" class="hidnum" value="6"></p></li>'
        '<li><h4>总天数</h4><p><input value="2" class="hidnum" type="hidden"></p></li>'
        '<li><h4>今日签到人数</h4><p><input type="hidden" class="hidnum" value="154062"></p></li>'
        '</ul>'
    )

    def test_parse_whitelist_and_attribute_order(self):
        sess = _FakeSession([_FakeResp(self.PAGE)], _FakeResp(""))
        stats = m.fetch_sign_stats(sess, "https://x")
        self.assertEqual(stats.get("连续签到"), "2 天")
        self.assertEqual(stats.get("签到等级"), "1 级")
        self.assertEqual(stats.get("积分奖励"), "6 分")
        self.assertEqual(stats.get("总天数"), "2 天")  # value 在 class 之前也能解析
        self.assertNotIn("今日签到人数", stats)


class TestSaveCookiesToCache(unittest.TestCase):
    def test_real_session_roundtrip(self):
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            m.COOKIE_CACHE_DIR = Path(tmp)
            s = requests.Session()
            s.cookies.set("sid", "abc", domain="laowangfip372.vip", path="/")
            s.cookies.set("tok", "xyz", domain="laowangfip372.vip", path="/")
            self.assertTrue(m.save_cookies_to_cache(s, "someone"))
            loaded = m.load_cookies_from_cache("someone")
            self.assertEqual({c["name"] for c in loaded}, {"sid", "tok"})
            self.assertNotIn("someone", m.cookie_cache_path("someone").name)


class TestFingerprint(unittest.TestCase):
    def test_stable_and_hex(self):
        fp = m.compute_fingerprint()
        self.assertEqual(fp, m.compute_fingerprint())  # 确定性
        self.assertEqual(len(fp), 32)                  # 4 段 8 位十六进制
        int(fp, 16)                                    # 全部为十六进制


if __name__ == "__main__":
    unittest.main()
