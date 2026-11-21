# -*- coding: utf-8 -*-
"""laowangfip_checkin 单元测试（unittest，纯函数 + 合成图回归，不发真实请求）。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
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
        return mod
    except SystemExit:
        pass
    # 脚本缺依赖会在 import 阶段 sys.exit；只对确实缺失的模块打桩，且绝不
    # 覆盖 sys.modules 里已存在的真实模块（同进程其它测试模块会被殃及）。
    # cv2/numpy 缺失时脚本可加载，但合成图用例无法运行 → 由 _HAS_CV_STACK 跳过。
    for name in ("requests", "cv2", "numpy"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            if name == "requests":
                stub.Session = object
                stub.exceptions = types.SimpleNamespace(RequestException=Exception)
            sys.modules[name] = stub
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:
        raise unittest.SkipTest(
            "laowangfip_checkin 缺少 requests 之外的依赖，无法加载被测模块") from e
    return mod


m = _load_module()

try:
    import numpy as _np_probe  # noqa: E402
    import cv2 as _cv2_probe  # noqa: E402
except ImportError:
    _np_probe = None
    _cv2_probe = None
# 加载器可能往 sys.modules 塞过裸 stub（缺真实库时），import 仍会"成功"，
# 因此按能力探测判断，而不是按导入是否抛错
_HAS_CV_STACK = bool(
    _np_probe is not None
    and hasattr(_np_probe, "random")
    and hasattr(_np_probe, "uint8")
    and hasattr(_cv2_probe, "imencode")
)
np = _np_probe   # 仅在 _HAS_CV_STACK 为真时被用例使用
cv2 = _cv2_probe


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


@unittest.skipUnless(_HAS_CV_STACK, "合成图回归需要 numpy + opencv-python-headless")
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
    def __init__(self, text, url="", status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code
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
            s.cookies.set("sid", "abc", domain="laowang.vip", path="/")
            s.cookies.set("tok", "xyz", domain="laowang.vip", path="/")
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


# ---- 页面类型判定（真实页面特征串，取自 2026-11-21 实测） ----
_CF_PAGE = ('<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
            '<script>window._cf_chl_opt = {cRay: "abc"};</script></head></html>')
_AGE_GATE_PAGE = (
    '<html><body><h2>性材料警告:</h2>'
    '<button class="agree" onclick="agree();">同意 / I Agree</button>'
    '<script>function agree() { setCookie("is_agree", "1", 30); }</script>'
    '</body></html>'
)
_LOGIN_PAGE = (
    '<html><head><title>登录老王论坛</title></head><body>'
    '<form method="post" action="member.php?mod=logging&amp;action=login&amp;loginhash=Lx8Es">'
    '<input type="hidden" name="formhash" value="daa0efa8">'
    '</form></body></html>'
)


class TestCloudflareDetection(unittest.TestCase):
    def test_detects_challenge_page(self):
        self.assertTrue(m.is_cloudflare_challenge(_CF_PAGE))

    def test_detects_by_cf_opt_only(self):
        # 只有 _cf_chl_opt 没有 title 也算（CF 页面结构会变）
        self.assertTrue(m.is_cloudflare_challenge('<script>window._cf_chl_opt={}</script>'))

    def test_login_and_gate_pages_are_not_cf(self):
        self.assertFalse(m.is_cloudflare_challenge(_LOGIN_PAGE))
        self.assertFalse(m.is_cloudflare_challenge(_AGE_GATE_PAGE))


class TestAgeGateDetection(unittest.TestCase):
    def test_detects_gate_page(self):
        self.assertTrue(m.is_age_gate_page(_AGE_GATE_PAGE))

    def test_login_page_not_gate(self):
        self.assertFalse(m.is_age_gate_page(_LOGIN_PAGE))

    def test_requires_both_markers(self):
        # 只有 is_agree 没有 agree() 不算，避免普通页面误判
        self.assertFalse(m.is_age_gate_page('<span>is_agree</span>'))


class TestDomainBlockError(unittest.TestCase):
    def test_cloudflare_error(self):
        self.assertTrue(m.is_domain_block_error("Cloudflare 拦截（当前出口 IP 被质询）"))

    def test_age_gate_error(self):
        self.assertTrue(m.is_domain_block_error("年龄确认网关拦截（置 is_agree 后仍被拦）"))

    def test_business_error_not_domain_block(self):
        self.assertFalse(m.is_domain_block_error("登录失败: 密码错误"))
        self.assertFalse(m.is_domain_block_error("验证码识别失败（滑块多次未通过）"))


class TestParseBaseUrls(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("LWFIP_BASE_URLS", "LWFIP_BASE_URL")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_both_domains(self):
        self.assertEqual(m.parse_base_urls(), list(m.DEFAULT_BASE_URLS))
        self.assertEqual(len(m.DEFAULT_BASE_URLS), 2)   # 主域名 + 备用域名

    def test_multi_env_overrides(self):
        os.environ["LWFIP_BASE_URLS"] = "https://a.com, https://b.com"
        self.assertEqual(m.parse_base_urls(), ["https://a.com", "https://b.com"])

    def test_legacy_single_env_disables_failover(self):
        os.environ["LWFIP_BASE_URL"] = "https://only.example.com/"
        self.assertEqual(m.parse_base_urls(), ["https://only.example.com"])  # 尾斜杠被去掉

    def test_multi_env_wins_over_legacy(self):
        os.environ["LWFIP_BASE_URLS"] = "https://a.com,https://b.com"
        os.environ["LWFIP_BASE_URL"] = "https://legacy.com"
        self.assertEqual(m.parse_base_urls(), ["https://a.com", "https://b.com"])

    def test_dedup_preserves_order(self):
        os.environ["LWFIP_BASE_URLS"] = "https://a.com,https://b.com,https://a.com"
        self.assertEqual(m.parse_base_urls(), ["https://a.com", "https://b.com"])

    def test_invalid_scheme_dropped(self):
        os.environ["LWFIP_BASE_URLS"] = "a.com,https://good.com"
        self.assertEqual(m.parse_base_urls(), ["https://good.com"])

    def test_all_invalid_falls_back_to_default(self):
        os.environ["LWFIP_BASE_URLS"] = "a.com,b.com"
        self.assertEqual(m.parse_base_urls(), list(m.DEFAULT_BASE_URLS))


class TestNextBaseUrl(unittest.TestCase):
    CANDIDATES = ["https://a.com", "https://b.com", "https://c.com"]

    def test_cycles_through_candidates(self):
        self.assertEqual(m.next_base_url("https://a.com", self.CANDIDATES), "https://b.com")
        self.assertEqual(m.next_base_url("https://b.com", self.CANDIDATES), "https://c.com")
        self.assertEqual(m.next_base_url("https://c.com", self.CANDIDATES), "https://a.com")

    def test_single_candidate_returns_same(self):
        self.assertEqual(m.next_base_url("https://a.com", ["https://a.com"]), "https://a.com")

    def test_unknown_current_returns_first(self):
        self.assertEqual(m.next_base_url("https://zzz.com", self.CANDIDATES), "https://a.com")


class TestCookieCachePerDomain(unittest.TestCase):
    def test_different_domains_get_different_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.COOKIE_CACHE_DIR = Path(tmp)
            p1 = m.cookie_cache_path("user1", "https://a.com")
            p2 = m.cookie_cache_path("user1", "https://b.com")
            self.assertNotEqual(p1, p2)

    def test_same_domain_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.COOKIE_CACHE_DIR = Path(tmp)
            self.assertEqual(m.cookie_cache_path("u", "https://a.com"),
                             m.cookie_cache_path("u", "https://a.com"))

    def test_empty_base_url_matches_legacy_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.COOKIE_CACHE_DIR = Path(tmp)
            # base_url 缺省时应与旧版（只哈希用户名）一致，保证老缓存仍可命中
            import hashlib
            expected = "cookie_" + hashlib.sha1("user1".encode()).hexdigest()[:12] + ".json"
            self.assertEqual(m.cookie_cache_path("user1").name, expected)
            self.assertEqual(m.cookie_cache_path("user1"), m.cookie_cache_path("user1", ""))

    def test_per_domain_roundtrip(self):
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            m.COOKIE_CACHE_DIR = Path(tmp)
            s = requests.Session()
            s.cookies.set("sid", "abc", domain="a.com", path="/")
            self.assertTrue(m.save_cookies_to_cache(s, "u", "https://a.com"))
            self.assertEqual(m.load_cookies_from_cache("u", "https://a.com")[0]["name"], "sid")
            # 另一个域名不该命中
            self.assertIsNone(m.load_cookies_from_cache("u", "https://b.com"))
            m.remove_cookie_cache("u", "https://a.com")
            self.assertIsNone(m.load_cookies_from_cache("u", "https://a.com"))


class TestFetchPageAgeGate(unittest.TestCase):
    """fetch_page 命中网关页时应置 cookie 并重试一次。"""

    class _GateSession:
        """首次返回网关页，置 cookie 后返回正常页。"""

        def __init__(self):
            self._timeout = 30
            self.cookies = _CookieJar()
            self.calls = 0

        def get(self, url, **kw):
            self.calls += 1
            if self.cookies.has(m.AGE_GATE_COOKIE_NAME):
                return _FakeResp(_LOGIN_PAGE, url)
            return _FakeResp(_AGE_GATE_PAGE, url)

    class _StubbornSession(_GateSession):
        """置了 cookie 仍返回网关页，应抛 AgeGateBlocked。"""

        def get(self, url, **kw):
            self.calls += 1
            return _FakeResp(_AGE_GATE_PAGE, url)

    def test_retries_after_setting_cookie(self):
        sess = self._GateSession()
        r = m.fetch_page(sess, "https://a.com/login", "https://a.com")
        self.assertEqual(sess.calls, 2)          # 网关页 -> 置 cookie -> 重试
        self.assertIn("formhash", r.text)

    def test_raises_when_still_gated(self):
        with self.assertRaises(m.AgeGateBlocked):
            m.fetch_page(self._StubbornSession(), "https://a.com/login", "https://a.com")

    def test_normal_page_passes_through(self):
        class _Plain:
            _timeout = 30
            cookies = _CookieJar()
            calls = 0

            def get(self, url, **kw):
                self.calls += 1
                return _FakeResp(_LOGIN_PAGE, url)

        sess = _Plain()
        r = m.fetch_page(sess, "https://a.com/login", "https://a.com")
        self.assertEqual(sess.calls, 1)          # 非网关页不该重试
        self.assertIn("formhash", r.text)


class _CookieJar:
    """最小 cookie 容器，模拟 requests.cookies.RequestsCookieJar 的 set/has。"""

    def __init__(self):
        self._items = {}

    def set(self, name, value, domain="", path="/"):
        self._items[name] = (value, domain, path)

    def has(self, name):
        return name in self._items

    def __bool__(self):
        return bool(self._items)

    def clear(self):
        self._items.clear()

    def __iter__(self):
        return iter([])


class TestProbeBaseUrls(unittest.TestCase):
    """探测应跳过被拦的域名，采用第一个可用的。"""

    def _patch_fetch(self, responses):
        """按 URL 里的域名返回预设响应（模拟不同域名被拦/可用）。"""
        def fake_fetch(session, url, base_url, debug=False, **kw):
            for domain, resp in responses.items():
                if domain in url:
                    return resp
            return _FakeResp("", url)
        return mock.patch.object(m, "fetch_page", side_effect=fake_fetch)

    def test_skips_blocked_picks_second(self):
        candidates = ["https://blocked.com", "https://good.com"]
        responses = {
            "blocked.com": _FakeResp(_CF_PAGE, "https://blocked.com/login"),
            "good.com": _FakeResp(_LOGIN_PAGE, "https://good.com/login"),
        }
        with self._patch_fetch(responses):
            with mock.patch.object(m, "build_session", return_value=object()):
                base, records = m.probe_base_urls(candidates, "", 30)
        self.assertEqual(base, "https://good.com")
        self.assertEqual(len(records), 2)
        self.assertIn("Cloudflare", records[0][1])
        self.assertEqual(records[1][1], "可用")

    def test_all_blocked_returns_empty(self):
        candidates = ["https://a.com", "https://b.com"]
        responses = {d: _FakeResp(_CF_PAGE, f"{d}/login") for d in ("a.com", "b.com")}
        with self._patch_fetch(responses):
            with mock.patch.object(m, "build_session", return_value=object()):
                base, records = m.probe_base_urls(candidates, "", 30)
        self.assertEqual(base, "")
        self.assertEqual(len(records), 2)

    def test_network_error_tries_next(self):
        import requests as _rq
        candidates = ["https://dead.com", "https://good.com"]

        def fake_fetch(session, url, base_url, debug=False, **kw):
            if "dead.com" in url:
                raise _rq.exceptions.ConnectionError("boom")
            return _FakeResp(_LOGIN_PAGE, url)

        with mock.patch.object(m, "fetch_page", side_effect=fake_fetch):
            with mock.patch.object(m, "build_session", return_value=object()):
                base, records = m.probe_base_urls(candidates, "", 30)
        self.assertEqual(base, "https://good.com")
        self.assertIn("网络异常", records[0][1])


class TestRetryableDomainBlock(unittest.TestCase):
    def test_domain_block_errors_are_retryable(self):
        # 换域名就能恢复，必须纳入重试集合，否则 main() 的重试分支直接放弃
        self.assertTrue(m.is_retryable_error("Cloudflare 拦截（当前出口 IP 被质询）"))
        self.assertTrue(m.is_retryable_error("年龄确认网关拦截"))


class TestRunOneAccountLoginNotSkipped(unittest.TestCase):
    """
    回归：预置 is_agree cookie 后 session.cookies 恒非空，
    若用 `if not session.cookies` 判断是否已登录，登录会被静默跳过。
    """

    def test_login_still_runs_when_gate_cookie_preset(self):
        calls = {"login": 0}

        def fake_login(session, base_url, username, password, debug, retry):
            calls["login"] += 1
            return True, "登录成功"

        with mock.patch.object(m, "build_session", return_value=_StubSession()), \
             mock.patch.object(m, "login_with_requests", side_effect=fake_login), \
             mock.patch.object(m, "sign_with_requests",
                               return_value={"success": True, "message": "签到成功"}), \
             mock.patch.object(m, "fetch_sign_stats", return_value={}):
            result = m.run_one_account("u", "p", "https://a.com", "", 30, False, 3, False)

        self.assertEqual(calls["login"], 1)          # 必须真的走登录
        self.assertTrue(result["success"])


class _StubSession:
    """供 run_one_account 使用的最小会话：cookies 可 set/clear，可判空。"""

    _timeout = 30

    def __init__(self):
        self.cookies = _CookieJar()


if __name__ == "__main__":
    unittest.main()
