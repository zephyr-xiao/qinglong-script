# -*- coding: utf-8 -*-
"""
wnflb_checkin 纯函数单元测试(不触网)。

运行:
    cd qinglong-script/wnflb_checkin
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import wnflb_checkin as w
    HAS_REQUESTS = True
except SystemExit:
    # 本机无 requests 时脚本 print 提示并 sys.exit(1),打桩后重新加载。
    # 纯函数用例不依赖 requests 的真实行为。
    stub = types.ModuleType("requests")

    class _RequestException(Exception):
        pass

    class _Session:
        pass

    stub.RequestException = _RequestException
    stub.Session = _Session
    sys.modules["requests"] = stub
    import wnflb_checkin as w
    HAS_REQUESTS = False


# 真实签到页(已签到)与游客页的关键片段,取自 2026-09-22 抓包
PAGE_SIGNED = (
    '<div class="flp-bd">'
    '<div class="flp-progresswrap"><div class="flp-bignum">3 <span>/ 30 天</span></div>'
    '<div class="flp-bar"><i style="width:10%"></i></div>'
    '<div class="flp-sub">已连续签到 3 天，距离转正还差 <b>27</b> 天</div></div>'
    '<button class="flp-btn flp-btn-done" disabled>今日已签到 ✓</button>'
    '<form method="post" action="...&ac=redeem">'
    '<input type="hidden" name="formhash" value="1e187fe5">'
    '<input type="text" name="invitecode"></form></div>'
)

PAGE_GUEST = (
    '<div class="flp-head"><h1>游客预注册签到</h1><p>连续签到 30 天，免费转为正式会员</p></div>'
    '<div class="flp-sub">游客预注册 · 连续签到 30 天即可转为正式会员</div>'
    '<input type="text" name="username"><input type="password" name="password2">'
    '<button class="flp-btn flp-btn-primary">立即预注册并签到</button>'
)

PAGE_RESULT_ALREADY = (
    '<div class="flp-toast">您今日已签到，明天再来哦！</div>'
    '<div class="flp-bignum">3 <span>/ 30 天</span></div>'
    '<div class="flp-sub">已连续签到 3 天，距离转正还差 <b>27</b> 天</div>'
)


class TestPageDetection(unittest.TestCase):
    def test_guest_page_detected(self):
        self.assertTrue(w.is_guest_page(PAGE_GUEST))

    def test_signed_page_not_guest(self):
        self.assertFalse(w.is_guest_page(PAGE_SIGNED))

    def test_already_signed_mark(self):
        self.assertIn(w.ALREADY_SIGNED_MARK, PAGE_SIGNED)
        self.assertNotIn(w.ALREADY_SIGNED_MARK, PAGE_GUEST)


class TestExtractFormhash(unittest.TestCase):
    def test_name_before_value(self):
        self.assertEqual(w.extract_formhash(PAGE_SIGNED), "1e187fe5")

    def test_value_before_name(self):
        self.assertEqual(
            w.extract_formhash('<input value="abc123" name="formhash">'), "abc123")

    def test_url_form(self):
        self.assertEqual(w.extract_formhash("...&formhash=deadbeef00"), "deadbeef00")

    def test_missing(self):
        self.assertEqual(w.extract_formhash("<html>no hash here</html>"), "")


class TestExtractToast(unittest.TestCase):
    def test_toast(self):
        self.assertEqual(w.extract_toast(PAGE_RESULT_ALREADY), "您今日已签到，明天再来哦！")

    def test_no_toast(self):
        self.assertEqual(w.extract_toast(PAGE_SIGNED), "")


class TestExtractDiscuzMessage(unittest.TestCase):
    def test_messagetext(self):
        html = '<div id="messagetext"><p>抱歉，您尚未登录，无法进行此操作</p></div>'
        self.assertEqual(w.extract_discuz_message(html), "抱歉，您尚未登录，无法进行此操作")

    def test_alert_fallback(self):
        html = '<div class="alert_error">请通过签到页上的按钮提交本操作。</div>'
        self.assertIn("请通过签到页上的按钮", w.extract_discuz_message(html))

    def test_missing(self):
        self.assertEqual(w.extract_discuz_message("<html></html>"), "")


class TestParseStats(unittest.TestCase):
    def test_full_stats(self):
        self.assertEqual(
            w.parse_stats(PAGE_SIGNED),
            "连续 3 天  │  进度 3/30  │  还差 27 天转正")

    def test_partial_stats(self):
        self.assertEqual(w.parse_stats("<b>已连续签到 9 天</b>"), "连续 9 天")

    def test_no_stats(self):
        self.assertEqual(w.parse_stats("<html>nothing</html>"), "")


class TestParseDoneCode(unittest.TestCase):
    BASE = "https://www.wnflb2023.com/plugin.php?id=fuliba_prereg:checkin"

    def test_success(self):
        self.assertEqual(w._parse_done_code(self.BASE + "&done=1&d=3"), (1, 3))

    def test_already(self):
        self.assertEqual(w._parse_done_code(self.BASE + "&done=-2&d=3"), (-2, 3))

    def test_unknown_code(self):
        self.assertEqual(w._parse_done_code(self.BASE + "&done=-9&d=1"), (-9, 1))

    def test_missing(self):
        self.assertEqual(w._parse_done_code(self.BASE), (None, None))

    def test_codes_are_distinct(self):
        self.assertNotEqual(w.DONE_SUCCESS, w.DONE_ALREADY)


class TestCookieHelpers(unittest.TestCase):
    def test_parse_cookies_keeps_equals_in_value(self):
        jar = w.parse_cookies("a=1; S5r8_2132_auth=abc%3D%3D; b=")
        self.assertEqual(jar["a"], "1")
        self.assertEqual(jar["S5r8_2132_auth"], "abc%3D%3D")
        self.assertEqual(jar["b"], "")

    def test_parse_cookies_skips_junk(self):
        self.assertEqual(w.parse_cookies("garbage; a=1"), {"a": "1"})

    def test_mask_cookie_hides_values(self):
        masked = w.mask_cookie("S5r8_2132_auth=SUPERSECRET; x=1")
        self.assertNotIn("SUPERSECRET", masked)
        self.assertIn("S5r8_2132_auth", masked)
        self.assertIn("2 项", masked)

    def test_mask_cookie_truncates_long_list(self):
        masked = w.mask_cookie("; ".join(f"k{i}=v{i}" for i in range(8)))
        self.assertIn("8 项", masked)
        self.assertTrue(masked.endswith("...>"))


class TestProxyResolution(unittest.TestCase):
    def test_specific_wins(self):
        env = {"WNFLB_PROXY": "http://specific:1", "HTTPS_PROXY": "http://global:2"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(w.resolve_proxy("WNFLB_PROXY"), ("http://specific:1", "WNFLB_PROXY"))

    def test_global_fallback(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://global:2"}, clear=True):
            self.assertEqual(w.resolve_proxy("WNFLB_PROXY"), ("http://global:2", "HTTPS_PROXY"))

    def test_lowercase_global(self):
        with mock.patch.dict(os.environ, {"all_proxy": "socks5://x:3"}, clear=True):
            url, src = w.resolve_proxy("WNFLB_PROXY")
            self.assertEqual(url, "socks5://x:3")
            # Windows 环境变量名大小写不敏感,os.getenv("ALL_PROXY") 会先命中,
            # 所以来源变量名只在大小写敏感平台(Linux / 青龙容器)上断言。
            if os.name != "nt":
                self.assertEqual(src, "all_proxy")

    def test_empty_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(w.resolve_proxy("WNFLB_PROXY"), ("", ""))

    def test_whitespace_is_unset(self):
        with mock.patch.dict(os.environ, {"WNFLB_PROXY": "   "}, clear=True):
            self.assertEqual(w.resolve_proxy("WNFLB_PROXY"), ("", ""))


class TestEnvHelpers(unittest.TestCase):
    def test_env_bool_variants(self):
        for raw, expected in [("1", True), ("true", True), ("YES", True),
                              ("on", True), ("0", False), ("no", False), ("", True)]:
            with mock.patch.dict(os.environ, {"X": raw}, clear=True):
                self.assertEqual(w.env_bool("X", True), expected, raw)

    def test_env_bool_missing_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(w.env_bool("X", False))

    def test_env_int(self):
        with mock.patch.dict(os.environ, {"X": "42"}, clear=True):
            self.assertEqual(w.env_int("X", 25), 42)
        with mock.patch.dict(os.environ, {"X": "abc"}, clear=True):
            self.assertEqual(w.env_int("X", 25), 25)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(w.env_int("X", 25), 25)


class TestHtmlToText(unittest.TestCase):
    def test_strips_tags_and_scripts(self):
        html = "<style>a{}</style><p>你好</p><script>var x=1;</script><b>世界</b>"
        self.assertEqual(w.html_to_text(html), "你好 世界")


class FakeResp:
    """http_request 的假响应。"""

    def __init__(self, status_code=200, text="", headers=None, content=None, url="https://h/x"):
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}
        self.url = url


class FakeOCR:
    """ddddocr 打桩。"""

    def __init__(self, code="C2FC"):
        self.code = code
        self.calls = 0

    def classification(self, data):
        self.calls += 1
        return self.code


# 已登录但今日未签到:有 formhash,按钮不是「今日已签到」
PAGE_NOT_SIGNED = (
    '<input type="hidden" name="formhash" value="1e187fe5">'
    '<button class="flp-btn flp-btn-primary">立即签到</button>'
)

PAGE_RESULT_SUCCESS = (
    '<div class="flp-toast">签到成功，已连续签到 4 天！</div>'
    '<div class="flp-bignum">4 <span>/ 30 天</span></div>'
    '<div class="flp-sub">已连续签到 4 天，距离转正还差 <b>26</b> 天</div>'
)

LOC_DONE_SUCCESS = "https://www.wnflb2023.com/plugin.php?id=fuliba_prereg:checkin&done=1&d=4"
LOC_DONE_ALREADY = "https://www.wnflb2023.com/plugin.php?id=fuliba_prereg:checkin&done=-2&d=4"


class TestDoCheckinFlow(unittest.TestCase):
    """用 mock 覆盖 do_checkin 的成功 / 幂等 / 失效 / formhash 重试分支。"""

    def _run(self, responses, page_html):
        with mock.patch.object(w, "http_request", side_effect=responses) as m:
            result = w.do_checkin(mock.MagicMock(), "www.wnflb2023.com", page_html)
        return result, m

    def test_already_signed_page_skips_post(self):
        """页面已带「今日已签到」时不应发起任何请求。"""
        result, m = self._run([], PAGE_SIGNED)
        self.assertTrue(result["success"])
        self.assertTrue(result["duplicate"])
        m.assert_not_called()

    def test_done_1_is_success(self):
        result, m = self._run(
            [FakeResp(302, headers={"Location": LOC_DONE_SUCCESS}),
             FakeResp(200, text=PAGE_RESULT_SUCCESS)],
            PAGE_NOT_SIGNED)
        self.assertTrue(result["success"])
        self.assertFalse(result["duplicate"])
        self.assertIn("签到成功", result["message"])
        self.assertEqual(result["stats"], "连续 4 天  │  进度 4/30  │  还差 26 天转正")
        # 第一次 POST 必须带 formhash
        self.assertEqual(m.call_args_list[0].kwargs["data"], {"formhash": "1e187fe5"})
        self.assertFalse(m.call_args_list[0].kwargs["allow_redirects"])

    def test_done_minus2_is_idempotent_success(self):
        result, _ = self._run(
            [FakeResp(302, headers={"Location": LOC_DONE_ALREADY}),
             FakeResp(200, text=PAGE_RESULT_ALREADY)],
            PAGE_NOT_SIGNED)
        self.assertTrue(result["success"])
        self.assertTrue(result["duplicate"])

    def test_unknown_done_code_is_failure(self):
        result, _ = self._run(
            [FakeResp(302, headers={"Location": LOC_DONE_ALREADY.replace("done=-2", "done=-9")}),
             FakeResp(200, text="<div class=\"flp-toast\">签到功能已关闭</div>")],
            PAGE_NOT_SIGNED)
        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "签到功能已关闭")

    def test_not_logged_in_marks_cookie_invalid(self):
        html = '<div id="messagetext"><p>抱歉，您尚未登录，无法进行此操作</p></div>'
        result, _ = self._run([FakeResp(200, text=html)], PAGE_NOT_SIGNED)
        self.assertFalse(result["success"])
        self.assertTrue(result["cookie_invalid"])

    def test_stale_formhash_retries_once_with_fresh_page(self):
        """formhash 失配 -> 重取页面 -> 重试一次后成功。"""
        bad = '<div id="messagetext"><p>请通过签到页上的按钮提交本操作。</p></div>'
        fresh = '<input type="hidden" name="formhash" value="newhash99">'
        with mock.patch.object(w, "rand_sleep"), \
                mock.patch.object(w, "http_request", side_effect=[
                    FakeResp(200, text=bad),                 # 第 1 次 POST 被拒
                    FakeResp(200, text=fresh),               # 重取页面
                    FakeResp(302, headers={"Location": LOC_DONE_SUCCESS}),
                    FakeResp(200, text=PAGE_RESULT_SUCCESS),
                ]) as m:
            result = w.do_checkin(mock.MagicMock(), "www.wnflb2023.com", PAGE_NOT_SIGNED)
        self.assertTrue(result["success"])
        self.assertEqual(m.call_count, 4)
        # 第 3 次调用(重试的 POST)应使用新 formhash
        self.assertEqual(m.call_args_list[2].kwargs["data"], {"formhash": "newhash99"})

    def test_stale_formhash_twice_gives_up(self):
        bad = '<div id="messagetext"><p>请通过签到页上的按钮提交本操作。</p></div>'
        fresh = '<input type="hidden" name="formhash" value="newhash99">'
        with mock.patch.object(w, "rand_sleep"), \
                mock.patch.object(w, "http_request", side_effect=[
                    FakeResp(200, text=bad),
                    FakeResp(200, text=fresh),
                    FakeResp(200, text=bad),
                ]):
            result = w.do_checkin(mock.MagicMock(), "www.wnflb2023.com", PAGE_NOT_SIGNED)
        self.assertFalse(result["success"])
        self.assertIn("formhash", result["message"])

    def test_missing_formhash_reports_structure_change(self):
        result, m = self._run([], "<html>无 formhash 的页面</html>")
        self.assertFalse(result["success"])
        self.assertIn("formhash", result["message"])
        m.assert_not_called()


class TestClassifyCredential(unittest.TestCase):
    def test_cookie(self):
        raw = "S5r8_2132_auth=abc; S5r8_2132_saltkey=xyz"
        self.assertEqual(w.classify_credential(raw), ("cookie", raw))

    def test_account(self):
        self.assertEqual(w.classify_credential("alice#Passw0rd"),
                         ("account", ("alice", "Passw0rd")))

    def test_account_password_keeps_hash_after_first(self):
        self.assertEqual(w.classify_credential("alice#pa#ss"),
                         ("account", ("alice", "pa#ss")))

    def test_account_with_at_sign_email(self):
        self.assertEqual(w.classify_credential("a@b.com#pw"),
                         ("account", ("a@b.com", "pw")))

    def test_unknown(self):
        self.assertEqual(w.classify_credential("garbage"), ("unknown", "garbage"))

    def test_whitespace_trimmed(self):
        self.assertEqual(w.classify_credential("  alice#pw  "),
                         ("account", ("alice", "pw")))


class TestMask(unittest.TestCase):
    def test_masks_middle(self):
        self.assertEqual(w.mask("exampleuser"), "ex*******er")

    def test_short(self):
        self.assertEqual(w.mask("ab"), "a*")
        self.assertEqual(w.mask(""), "")


LOGIN_PAGE_HTML = (
    '<input type="hidden" name="formhash" value="7e2c558d" />'
    '<a href="member.php?mod=logging&amp;action=login&amp;loginhash=LO3zu">登录</a>'
)


class TestClassifyLoginResponse(unittest.TestCase):
    def _session(self, cookie_name=None, cookie_value="v"):
        s = mock.MagicMock()
        s.cookies = []
        if cookie_name:
            c = mock.MagicMock()
            c.name = cookie_name
            c.value = cookie_value
            s.cookies = [c]
        return s

    def test_success_by_marker(self):
        state, detail = w.classify_login_response("欢迎您回来，现在将转入登录前页面", self._session())
        self.assertEqual(state, "ok")
        self.assertEqual(detail, "")

    def test_success_by_auth_cookie(self):
        state, _ = w.classify_login_response("随便什么文本", self._session("S5r8_2132_auth"))
        self.assertEqual(state, "ok")

    def test_empty_auth_cookie_is_not_success(self):
        state, _ = w.classify_login_response("随便什么文本", self._session("S5r8_2132_auth", ""))
        self.assertNotEqual(state, "ok")

    def test_captcha_takes_priority_over_auth_cookie(self):
        """回归:服务端要求验证码时**也会下发 auth Cookie**,
        此时必须判为 captcha 而不是 ok,否则会把待验证码误判成登录成功。"""
        text = ("请输入验证码后继续登录"
                "<script>location.href='member.php?mod=logging&action=login&auth=TOK123&referer=x'</script>")
        state, detail = w.classify_login_response(text, self._session("S5r8_2132_auth"))
        self.assertEqual(state, "captcha")
        self.assertIn("auth=TOK123", detail)

    def test_captcha_without_redirect_still_detected(self):
        state, detail = w.classify_login_response("请输入验证码后继续登录", self._session())
        self.assertEqual(state, "captcha")
        self.assertEqual(detail, "")

    def test_failure_reports_remaining_attempts(self):
        text = "errorhandle_login('登录失败，您还可以尝试 3 次', {'loginperm':'3'});"
        state, detail = w.classify_login_response(text, self._session())
        self.assertEqual(state, "fail")
        self.assertIn("3 次", detail)
        self.assertIn("密码错误", detail)

    def test_security_question(self):
        state, detail = w.classify_login_response("请选择安全提问以及填写答案", self._session())
        self.assertEqual(state, "fail")
        self.assertIn("安全提问", detail)


CAPTCHA_PAGE = (
    '<input type="hidden" name="formhash" value="fca03270" />'
    '<input type="hidden" name="auth" value="AUTHTOKEN123" />'
    "<script>updateseccode('cSJ7Hx4X', '<div class=\"rfm\"><sec></div>');</script>"
    '<form action="member.php?mod=logging&amp;action=login&amp;loginsubmit=yes&amp;loginhash=Ls7tl">'
)


class TestCaptchaLogin(unittest.TestCase):
    """验证码登录流程(ddddocr 打桩)。"""

    def test_captcha_flow_success(self):
        ocr = FakeOCR("C2FC")
        with mock.patch.object(w, "_load_ocr", return_value=(ocr, "")), \
                mock.patch.object(w, "http_request", side_effect=[
                    FakeResp(200, text=CAPTCHA_PAGE),                # GET 验证码页
                    FakeResp(200, content=b"\x89PNG\r\n\x1a\n"),     # GET 验证码图
                    FakeResp(200, text="欢迎您回来，预注册待转正 exampleuser"),  # POST
                ]) as m:
            ok, err = w._complete_captcha_login(
                mock.MagicMock(), "host", "u", "p", "member.php?mod=logging&action=login&auth=X")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(ocr.calls, 1)
        # 第 3 次请求是带验证码的 POST
        data = m.call_args_list[2].kwargs["data"]
        self.assertEqual(data["seccodeverify"], "C2FC")
        self.assertEqual(data["seccodehash"], "cSJ7Hx4X")
        self.assertEqual(data["auth"], "AUTHTOKEN123")
        self.assertEqual(data["seccodemodid"], "member::logging")
        self.assertEqual(data["username"], "u")
        # 验证码图 URL 必须带 idhash
        self.assertIn("idhash=cSJ7Hx4X", m.call_args_list[1].args[2])

    def test_ocr_missing_gives_actionable_message(self):
        with mock.patch.object(w, "_load_ocr", return_value=(None, "未安装 ddddocr")), \
                mock.patch.object(w, "http_request") as m:
            ok, err = w._complete_captcha_login(
                mock.MagicMock(), "host", "u", "p", "member.php?auth=X")
        self.assertFalse(ok)
        self.assertIn("ddddocr", err)
        self.assertIn("Cookie", err)
        m.assert_not_called()

    def test_captcha_page_structure_change(self):
        with mock.patch.object(w, "_load_ocr", return_value=(FakeOCR(), "")), \
                mock.patch.object(w, "http_request",
                                  return_value=FakeResp(200, text="<html>没有 idhash</html>")):
            ok, err = w._complete_captcha_login(
                mock.MagicMock(), "host", "u", "p", "member.php?auth=X")
        self.assertFalse(ok)
        self.assertIn("结构", err)

    def test_wrong_captcha_retries_then_gives_up(self):
        """识别错 -> 重新取图重试,达上限后给出明确提示。"""
        retry_page = ('<input type="hidden" name="formhash" value="f" />'
                      '<input type="hidden" name="auth" value="A" />'
                      "<script>updateseccode('IDH', 'x');</script>"
                      '<form action="member.php?loginsubmit=yes&amp;loginhash=LH">')
        wrong = ("请输入验证码后继续登录<script>location.href='member.php?auth=NEWTOK'</script>")
        side = []
        for _ in range(w.CAPTCHA_MAX_TRY):
            side += [FakeResp(200, text=retry_page),
                     FakeResp(200, content=b"\x89PNG"),
                     FakeResp(200, text=wrong)]
        with mock.patch.object(w, "_load_ocr", return_value=(FakeOCR("XXXX"), "")), \
                mock.patch.object(w, "rand_sleep"), \
                mock.patch.object(w, "http_request", side_effect=side) as m:
            ok, err = w._complete_captcha_login(
                mock.MagicMock(), "host", "u", "p", "member.php?auth=X")
        self.assertFalse(ok)
        self.assertIn(str(w.CAPTCHA_MAX_TRY), err)
        self.assertEqual(m.call_count, w.CAPTCHA_MAX_TRY * 3)


class TestLoginWithPassword(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = w.COOKIE_CACHE_FILE
        w.COOKIE_CACHE_FILE = Path(self._tmp.name) / "wnflb_cookie.json"

    def tearDown(self):
        w.COOKIE_CACHE_FILE = self._orig
        self._tmp.cleanup()

    def test_success_returns_session(self):
        with mock.patch.object(w, "http_request", side_effect=[
                FakeResp(200, text=LOGIN_PAGE_HTML),
                FakeResp(200, text="欢迎您回来")]) as m:
            session, err = w.login_with_password("host", "alice", "pw")
        self.assertIsNotNone(session)
        self.assertEqual(err, "")
        # 第 2 次请求必须是 POST,且带 formhash
        self.assertEqual(m.call_args_list[1].args[1], "POST")
        self.assertEqual(m.call_args_list[1].kwargs["data"]["formhash"], "7e2c558d")
        self.assertEqual(m.call_args_list[1].kwargs["data"]["username"], "alice")

    def test_auth_failure_does_not_retry(self):
        """认证失败只发一次 POST,绝不重试(避免消耗 IP 失败额度)。"""
        fail = "errorhandle_login('登录失败，您还可以尝试 2 次', {'loginperm':'2'});"
        with mock.patch.object(w, "http_request", side_effect=[
                FakeResp(200, text=LOGIN_PAGE_HTML),
                FakeResp(200, text=fail)]) as m:
            session, err = w.login_with_password("host", "alice", "bad")
        self.assertIsNone(session)
        self.assertIn("2 次", err)
        self.assertEqual(m.call_count, 2)

    def test_login_page_structure_change(self):
        with mock.patch.object(w, "http_request", side_effect=[
                FakeResp(200, text="<html>没有 formhash 也没有 loginhash</html>")]) as m:
            session, err = w.login_with_password("host", "alice", "pw")
        self.assertIsNone(session)
        self.assertIn("formhash", err)
        self.assertEqual(m.call_count, 1)

    def test_captcha_required_enters_captcha_flow(self):
        """第一步返回「需要验证码」时,应转入验证码流程而不是报登录失败。"""
        captcha_resp = ("请输入验证码后继续登录"
                        "<script>location.href='member.php?auth=TOK'</script>")
        with mock.patch.object(w, "http_request", side_effect=[
                FakeResp(200, text=LOGIN_PAGE_HTML),
                FakeResp(200, text=captcha_resp)]), \
                mock.patch.object(w, "_complete_captcha_login",
                                  return_value=(True, "")) as cap:
            session, err = w.login_with_password("host", "alice", "pw")
        self.assertIsNotNone(session)
        cap.assert_called_once()
        self.assertEqual(cap.call_args.args[4], "member.php?auth=TOK")


class TestCookieCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = w.COOKIE_CACHE_FILE
        w.COOKIE_CACHE_FILE = Path(self._tmp.name) / "wnflb_cookie.json"

    def tearDown(self):
        w.COOKIE_CACHE_FILE = self._orig
        self._tmp.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(w.read_cookie_cache(), {})

    def test_write_then_read_roundtrip(self):
        w.write_cookie_cache("alice", "S5r8_2132_auth=AAA")
        self.assertEqual(w.read_cookie_cache()["alice"]["cookies"], "S5r8_2132_auth=AAA")

    def test_accounts_are_isolated(self):
        w.write_cookie_cache("alice", "auth=A")
        w.write_cookie_cache("bob", "auth=B")
        cache = w.read_cookie_cache()
        self.assertEqual(cache["alice"]["cookies"], "auth=A")
        self.assertEqual(cache["bob"]["cookies"], "auth=B")

    def test_rewrite_updates_same_key(self):
        w.write_cookie_cache("alice", "auth=OLD")
        w.write_cookie_cache("alice", "auth=NEW")
        cache = w.read_cookie_cache()
        self.assertEqual(list(cache.keys()), ["alice"])
        self.assertEqual(cache["alice"]["cookies"], "auth=NEW")

    def test_never_stores_password(self):
        w.write_cookie_cache("alice", "auth=A")
        raw = w.COOKIE_CACHE_FILE.read_text(encoding="utf-8")
        self.assertNotIn("password", raw)

    def test_corrupted_file_returns_empty(self):
        w.COOKIE_CACHE_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(w.read_cookie_cache(), {})


class TestRunOneAccountModes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = w.COOKIE_CACHE_FILE
        w.COOKIE_CACHE_FILE = Path(self._tmp.name) / "wnflb_cookie.json"

    def tearDown(self):
        w.COOKIE_CACHE_FILE = self._orig
        self._tmp.cleanup()

    def test_account_mode_reuses_cache_without_login(self):
        """缓存有效时不应发起登录请求。"""
        w.write_cookie_cache("alice", "S5r8_2132_auth=CACHED")
        with mock.patch.object(w, "http_request",
                               return_value=FakeResp(200, text=PAGE_SIGNED)), \
                mock.patch.object(w, "login_with_password") as login:
            r = w.run_one_account({"kind": "account", "payload": ("alice", "pw")}, 0, "host")
        self.assertTrue(r["success"])
        self.assertTrue(r["duplicate"])
        login.assert_not_called()

    def test_account_mode_logs_in_when_cache_stale(self):
        """缓存失效 -> 登录 -> 写回缓存 -> 签到。"""
        w.write_cookie_cache("alice", "S5r8_2132_auth=STALE")
        fresh = mock.MagicMock()
        fresh.cookies = []
        with mock.patch.object(w, "http_request", side_effect=[
                FakeResp(200, text=PAGE_GUEST),    # 缓存访问 -> 游客页,失效
                FakeResp(200, text=PAGE_SIGNED)]), \
                mock.patch.object(w, "login_with_password", return_value=(fresh, "")) as login, \
                mock.patch.object(w, "rand_sleep"):
            r = w.run_one_account({"kind": "account", "payload": ("alice", "pw")}, 0, "host")
        login.assert_called_once_with("host", "alice", "pw")
        self.assertTrue(r["success"])

    def test_account_mode_login_failure_marks_auth_failed(self):
        with mock.patch.object(w, "http_request", return_value=FakeResp(200, text=PAGE_GUEST)), \
                mock.patch.object(w, "login_with_password",
                                  return_value=(None, "账号或密码错误")) as login, \
                mock.patch.object(w, "rand_sleep"):
            r = w.run_one_account({"kind": "account", "payload": ("alice", "pw")}, 0, "host")
        self.assertFalse(r["success"])
        self.assertTrue(r["auth_failed"])
        self.assertIn("密码错误", r["message"])

    def test_cookie_mode_invalid_cookie(self):
        with mock.patch.object(w, "http_request",
                               return_value=FakeResp(200, text=PAGE_GUEST)):
            r = w.run_one_account({"kind": "cookie", "payload": "a=1; b=2"}, 0, "host")
        self.assertFalse(r["success"])
        self.assertTrue(r["cookie_invalid"])
        self.assertFalse(r["auth_failed"])


class TestCollectTasks(unittest.TestCase):
    def test_both_env_vars(self):
        env = {"WNFLB_COOKIE": "a=1; b=2\na=3; b=4",
               "WNFLB_ACCOUNTS": "alice#pw\nbob#pw2"}
        with mock.patch.dict(os.environ, env, clear=True):
            tasks = w.collect_tasks()
        self.assertEqual([t["kind"] for t in tasks],
                         ["cookie", "cookie", "account", "account"])
        self.assertEqual(tasks[3]["payload"], ("bob", "pw2"))

    def test_blank_lines_and_junk_skipped(self):
        env = {"WNFLB_COOKIE": "\n  \nnot-a-cookie\n",
               "WNFLB_ACCOUNTS": "\nalice\n\n"}
        with mock.patch.dict(os.environ, env, clear=True):
            tasks = w.collect_tasks()
        self.assertEqual(tasks, [])

    def test_account_with_empty_password_skipped(self):
        with mock.patch.dict(os.environ, {"WNFLB_ACCOUNTS": "alice#"}, clear=True):
            self.assertEqual(w.collect_tasks(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
