# -*- coding: utf-8 -*-
"""
wnflb_fx_checkin 单元测试(不触网)。

运行:
    cd qinglong-script/wnflb_fx_checkin
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wnflb_fx_checkin as w  # noqa: E402


# 游客页关键片段(2026-10-19 实测)
PAGE_GUEST = """
<script>var discuz_uid = '0', cookiepre = 'S5r8_2132_';</script>
<div id="fx_checkin_menu"><em>签到成功!</em><p>您今天第<i>{todayrank}</i>个签到</p></div>
<form method="post" action="member.php?mod=logging&amp;action=login">
<input type="hidden" name="formhash" value="aaaaaaaa">
</form>
"""

# 已签到页面关键片段(2026-10-19 实测)
PAGE_SIGNED = """
<script>var discuz_uid = '123456', cookiepre = 'S5r8_2132_';</script>
<script>var fx_chk_menu=true;function fx_checkin(){if (!fx_chk_menu){showWindow('fx_checkin', 'plugin.php?id=fx_checkin:checkin&formhash=bbbbbbbb&bbbbbbbb');}}</script>
<a id="fx_checkin_topb" onclick="fx_checkin()"><img id="fx_checkin_b" src="source/plugin/fx_checkin/images/mini2.gif" alt="已签到"></a>
<div id="fx_checkin2_menu" class="tip tip_2"><div class="tip_c">您今日第1008个签到,已连续签到1天,累计签到1天!</div></div>
<div class="calendar_foo" id="fx_checkin_menub">已连续签到:<i>1</i>天，累计签到:<i>1</i>天</div>
<input type="hidden" name="formhash" value="bbbbbbbb">
"""

# 已登录但今天未签到(依据站点 JS 语义:fx_chk_menu=false 时点击才弹签到窗)
PAGE_UNSIGNED = """
<script>var discuz_uid = '123456', cookiepre = 'S5r8_2132_';</script>
<script>var fx_chk_menu=false;function fx_checkin(){if (!fx_chk_menu){showWindow('fx_checkin', 'plugin.php?id=fx_checkin:checkin&formhash=cccccccc&cccccccc');}}</script>
<a id="fx_checkin_topb" onclick="fx_checkin()"><img id="fx_checkin_b" src="source/plugin/fx_checkin/images/mini.gif" alt="签到"></a>
<div class="calendar_foo" id="fx_checkin_menub">已连续签到:<i>0</i>天，累计签到:<i>0</i>天</div>
<input type="hidden" name="formhash" value="cccccccc">
"""

RESP_ALREADY_AJAX = (
    '<?xml version="1.0" encoding="utf-8"?><root><![CDATA[提示信息'
    '<script type="text/javascript">showDialog();</script>关闭 '
    '您今日已签到，无需重复签到！'
    '<script type="text/javascript">hideWindow();</script>确定 ]]></root>'
)

RESP_SUCCESS_AJAX = (
    '<?xml version="1.0" encoding="utf-8"?><root><![CDATA[提示信息关闭 '
    '签到成功，获得 5 金币！确定 ]]></root>'
)

RESP_LOGIN_AJAX = (
    '<?xml version="1.0" encoding="utf-8"?><root><![CDATA[提示信息关闭 '
    '抱歉，您尚未登录，无法进行此操作确定 ]]></root>'
)

RESP_FORMHASH_AJAX = (
    '<?xml version="1.0" encoding="utf-8"?><root><![CDATA[提示信息关闭 '
    '抱歉，您的请求来路不正确或表单验证串不符，无法提交确定 ]]></root>'
)

RESP_UNKNOWN_AJAX = (
    '<?xml version="1.0" encoding="utf-8"?><root><![CDATA[提示信息关闭 '
    '系统繁忙，请稍后再试确定 ]]></root>'
)


class _FakeResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")
        self.url = "https://www.example.com/"


class TestPageParsing(unittest.TestCase):
    def test_guest_page(self):
        self.assertTrue(w.is_guest_page(PAGE_GUEST))
        self.assertFalse(w.is_guest_page(PAGE_SIGNED))
        self.assertFalse(w.is_guest_page(PAGE_UNSIGNED))

    def test_signed_page(self):
        self.assertTrue(w.is_signed_page(PAGE_SIGNED))
        self.assertFalse(w.is_signed_page(PAGE_UNSIGNED))
        # 游客页也含站点全局「签到成功!」模板,绝不能误判为已签到
        self.assertFalse(w.is_signed_page(PAGE_GUEST))
        # 未渲染的占位符 / 排行榜文案不应被当成已签到
        self.assertFalse(w.is_signed_page("<p>您今日第 {todayrank} 个签到</p>"))
        self.assertFalse(w.is_signed_page("<p>您今天第<i>{todayrank}</i>个签到</p>"))

    def test_extract_formhash(self):
        self.assertEqual(w.extract_formhash(PAGE_SIGNED), "bbbbbbbb")
        self.assertEqual(w.extract_formhash(PAGE_UNSIGNED), "cccccccc")
        self.assertEqual(w.extract_formhash("<p>无表单</p>"), "")

    def test_extract_ajax_message(self):
        self.assertEqual(w.extract_ajax_message(RESP_ALREADY_AJAX), "您今日已签到，无需重复签到！")
        self.assertEqual(w.extract_ajax_message(RESP_SUCCESS_AJAX), "签到成功，获得 5 金币！")
        self.assertEqual(w.extract_ajax_message("<html>普通页面</html>"), "")

    def test_extract_discuz_message(self):
        page = ('<div id="messagetext" class="alert_right">'
                '<p>您今日已签到，无需重复签到！</p></div>')
        self.assertEqual(w.extract_discuz_message(page), "您今日已签到，无需重复签到！")

    def test_parse_stats(self):
        self.assertEqual(
            w.parse_stats(PAGE_SIGNED),
            "今日第 1008 名 │ 连续 1 天 │ 累计 1 天",
        )
        self.assertEqual(w.parse_stats(PAGE_UNSIGNED), "连续 0 天 │ 累计 0 天")
        self.assertEqual(w.parse_stats("<p>无统计</p>"), "")

    def test_classify_sign_message(self):
        self.assertEqual(w._classify_sign_message("您今日已签到，无需重复签到！"), "already")
        self.assertEqual(w._classify_sign_message("签到成功，获得 5 金币"), "success")
        self.assertEqual(w._classify_sign_message("抱歉，您尚未登录"), "login")
        self.assertEqual(w._classify_sign_message("表单验证串不符"), "formhash")
        self.assertEqual(w._classify_sign_message("系统繁忙"), "unknown")


class TestCredential(unittest.TestCase):
    def test_classify_credential(self):
        self.assertEqual(w.classify_credential("user#pass")[0], "account")
        self.assertEqual(w.classify_credential("a=1; b=2")[0], "cookie")
        self.assertEqual(w.classify_credential("garbage")[0], "unknown")

    def test_collect_tasks(self):
        env = {
            "WNFX_ACCOUNTS": "alice#secret\n#bad\n\n bob#pwd ",
            "WNFX_COOKIE": "k=v; k2=v2",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            tasks = w.collect_tasks()
        accounts = [t for t in tasks if t["kind"] == "account"]
        cookies = [t for t in tasks if t["kind"] == "cookie"]
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["payload"], ("alice", "secret"))
        self.assertEqual(accounts[1]["payload"], ("bob", "pwd"))
        self.assertEqual(len(cookies), 1)

    def test_mask(self):
        self.assertEqual(w.mask("example_user"), "e**********r")
        self.assertEqual(w.mask("abc"), "a*c")
        self.assertEqual(w.mask("ab"), "a*")
        self.assertNotIn("secret", w.mask_cookie("auth=secret; saltkey=xyz"))

    def test_mask_proxy(self):
        self.assertEqual(
            w.mask_proxy("http://user:pass@proxy.example.com:7890"),
            "http://***@proxy.example.com:7890",
        )
        self.assertEqual(w.mask_proxy("http://proxy.example.com:7890"),
                         "http://proxy.example.com:7890")


class TestLoginClassification(unittest.TestCase):
    def test_captcha_wins_over_auth_cookie(self):
        """待验证码时服务端也会下发 auth Cookie,必须先判验证码。"""
        session = mock.Mock()
        session.cookies = [mock.Mock(name="S5r8_2132_auth", value="xxx")]
        # mock 的 name 属性不会自动生效,显式构造
        c = mock.Mock()
        c.name = "S5r8_2132_auth"
        c.value = "xxx"
        session.cookies = [c]
        text = ("<root><![CDATA[请输入验证码后继续登录"
                "<script>location.href='member.php?mod=logging&action=login&auth=TOKEN'</script>"
                "]]></root>")
        state, detail = w.classify_login_response(text, session)
        self.assertEqual(state, "captcha")
        self.assertIn("auth=TOKEN", detail)

    def test_login_ok_by_marker(self):
        session = mock.Mock()
        session.cookies = []
        state, detail = w.classify_login_response("欢迎您回来", session)
        self.assertEqual((state, detail), ("ok", ""))

    def test_login_fail_counts_remaining(self):
        session = mock.Mock()
        session.cookies = []
        state, detail = w.classify_login_response(
            "登录失败，您还可以尝试 4 次", session)
        self.assertEqual(state, "fail")
        self.assertIn("4", detail)


class TestDoCheckin(unittest.TestCase):
    def setUp(self):
        # 这一组用例验证单次状态机,关掉业务重试以免响应队列被额外消耗
        self.patch_retry = mock.patch.object(w, "SIGN_RETRY", 0)
        self.patch_retry.start()

    def tearDown(self):
        self.patch_retry.stop()

    def _run(self, page, responses):
        """responses: 依次返回给 http_request 的响应文本队列。"""
        calls = []

        def fake_http(session, method, url, **kwargs):
            calls.append(url)
            if not responses:
                raise AssertionError(f"多余的请求: {url}")
            return _FakeResp(responses.pop(0))

        session = mock.Mock()
        with mock.patch.object(w, "http_request", side_effect=fake_http):
            result = w.do_checkin(session, "www.example.com", page)
        return result, calls

    def test_already_signed_skips_request(self):
        result, calls = self._run(PAGE_SIGNED, [])
        self.assertTrue(result["success"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(calls, [])
        self.assertIn("今日第 1008 名", result["stats"])

    def test_guest_page(self):
        result, calls = self._run(PAGE_GUEST, [])
        self.assertFalse(result["success"])
        self.assertTrue(result["cookie_invalid"])
        self.assertEqual(calls, [])

    def test_success_confirmed_by_after_page(self):
        result, calls = self._run(PAGE_UNSIGNED, [RESP_SUCCESS_AJAX, PAGE_SIGNED])
        self.assertTrue(result["success"])
        self.assertFalse(result["duplicate"])
        self.assertIn("签到成功", result["message"])
        self.assertIn("今日第 1008 名", result["stats"])
        self.assertEqual(len(calls), 2)
        self.assertIn("id=fx_checkin:checkin", calls[0])
        self.assertIn("formhash=cccccccc", calls[0])

    def test_already_message_is_idempotent(self):
        result, calls = self._run(PAGE_UNSIGNED, [RESP_ALREADY_AJAX, PAGE_SIGNED])
        self.assertTrue(result["success"])
        self.assertTrue(result["duplicate"])
        self.assertIn("无需重复签到", result["message"])

    def test_unknown_message_but_page_signed_is_success(self):
        """站点改了成功文案时,以页面已签到状态为准,不冒报失败。"""
        result, calls = self._run(PAGE_UNSIGNED, [RESP_UNKNOWN_AJAX, PAGE_SIGNED])
        self.assertTrue(result["success"])
        self.assertFalse(result["duplicate"])

    def test_unknown_message_and_page_unsigned_is_failure(self):
        result, calls = self._run(PAGE_UNSIGNED, [RESP_UNKNOWN_AJAX, PAGE_UNSIGNED])
        self.assertFalse(result["success"])
        self.assertIn("系统繁忙", result["message"])

    def test_login_required(self):
        result, calls = self._run(PAGE_UNSIGNED, [RESP_LOGIN_AJAX])
        self.assertFalse(result["success"])
        self.assertTrue(result["cookie_invalid"])
        self.assertEqual(len(calls), 1)

    def test_formhash_retry_once(self):
        result, calls = self._run(
            PAGE_UNSIGNED,
            [RESP_FORMHASH_AJAX, PAGE_UNSIGNED, RESP_SUCCESS_AJAX, PAGE_SIGNED],
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 4)

    def test_formhash_retry_fresh_page_signed(self):
        """formhash 过期重取页面时,如果页面已变为已签到,直接按幂等成功返回。"""
        result, calls = self._run(PAGE_UNSIGNED, [RESP_FORMHASH_AJAX, PAGE_SIGNED])
        self.assertTrue(result["success"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(len(calls), 2)

    def test_after_page_fetch_error_is_reported_as_unconfirmed(self):
        def fake_http(session, method, url, **kwargs):
            if "id=fx_checkin:checkin" in url:
                return _FakeResp(RESP_UNKNOWN_AJAX)
            raise w.requests.exceptions.ConnectionError("boom")

        with mock.patch.object(w, "http_request", side_effect=fake_http):
            result = w.do_checkin(mock.Mock(), "www.example.com", PAGE_UNSIGNED)
        self.assertFalse(result["success"])
        self.assertIn("无法确认", result["message"])

    def test_missing_formhash(self):
        # 页面里的 input 与 JS 都可能带 formhash,测试「完全没有」的场景
        page = PAGE_UNSIGNED.replace("cccccccc", "")
        result, calls = self._run(page, [])
        self.assertFalse(result["success"])
        self.assertIn("formhash", result["message"])
        self.assertEqual(calls, [])


class TestHttpRetry(unittest.TestCase):
    class _BoomSession:
        verify = True

        def __init__(self):
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            raise w.requests.exceptions.ConnectionError("boom")

    def test_retry_false_calls_once(self):
        session = self._BoomSession()
        with self.assertRaises(w.requests.exceptions.ConnectionError):
            w.http_request(session, "POST", "https://www.example.com/", retry=False)
        self.assertEqual(session.calls, 1)

    def test_retry_true_calls_three_times(self):
        session = self._BoomSession()
        with mock.patch.object(w.time, "sleep"):
            with self.assertRaises(w.requests.exceptions.ConnectionError):
                w.http_request(session, "GET", "https://www.example.com/")
        self.assertEqual(session.calls, 3)

    def test_login_post_uses_retry_false(self):
        with mock.patch.object(w, "http_request", return_value=_FakeResp("ok")) as req:
            w._post_login(mock.Mock(), "www.example.com", "u", "p", "fh", "lh")
        self.assertFalse(req.call_args.kwargs.get("retry", True))


class TestCookieCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache.json"
        self.patch = mock.patch.object(w, "COOKIE_CACHE_FILE", self.cache)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_corrupt_json_returns_empty(self):
        self.cache.write_text("{not json", encoding="utf-8")
        self.assertEqual(w.read_cookie_cache(), {})

    def test_non_dict_entry_is_skipped(self):
        self.cache.write_text(json.dumps({"accounts": {"u1": "oops", "u2": {"cookies": "k=v"}}}),
                              encoding="utf-8")
        cache = w.read_cookie_cache()
        self.assertNotIn("u1", cache)
        self.assertEqual(cache["u2"]["cookies"], "k=v")

    def test_host_mismatch_is_ignored(self):
        self.cache.write_text(json.dumps({"accounts": {
            "u1": {"cookies": "k=v", "host": "old.example.com"},
            "u2": {"cookies": "k=v", "host": "www.example.com"},
        }}), encoding="utf-8")
        cache = w.read_cookie_cache("www.example.com")
        self.assertNotIn("u1", cache)
        self.assertIn("u2", cache)

    def test_write_then_read(self):
        w.write_cookie_cache("u1", "k=v", "www.example.com")
        cache = w.read_cookie_cache("www.example.com")
        self.assertEqual(cache["u1"]["cookies"], "k=v")
        self.assertEqual(cache["u1"]["host"], "www.example.com")


class TestLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "test.lock"
        self.patch = mock.patch.object(w, "LOCK_FILE", self.lock)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_acquire_release(self):
        self.assertTrue(w.acquire_lock())
        self.assertTrue(self.lock.exists())
        self.assertFalse(w.acquire_lock())
        w.release_lock()
        self.assertFalse(self.lock.exists())
        self.assertTrue(w.acquire_lock())
        w.release_lock()

    def test_stale_lock_is_reclaimed(self):
        self.lock.write_text("old", encoding="utf-8")
        old = time.time() - w.LOCK_STALE_SECONDS - 10
        os.utime(self.lock, (old, old))
        self.assertTrue(w.acquire_lock())
        w.release_lock()


class TestCaptchaLogin(unittest.TestCase):
    AUTH_PAGE = (
        '<input type="hidden" name="formhash" value="dddddddd">'
        '<input type="hidden" name="auth" value="TOKEN">'
        "<script>updateseccode('IDHASH', 'member::logging');</script>"
        '<a href="member.php?mod=logging&action=login&loginhash=LamiM">'
    )
    LOGIN_OK = "欢迎您回来"

    class _FakeOCR:
        def __init__(self, codes):
            self.codes = list(codes)

        def classification(self, img):
            return self.codes.pop(0)

    def _run(self, codes, responses):
        ocr = self._FakeOCR(codes)

        def fake_http(session, method, url, retry=True, **kwargs):
            return _FakeResp(responses.pop(0))

        with mock.patch.object(w, "_load_ocr", return_value=(ocr, "")), \
                mock.patch.object(w, "http_request", side_effect=fake_http):
            return w._complete_captcha_login(
                mock.Mock(), "www.example.com", "u", "p",
                "member.php?mod=logging&action=login&auth=TOKEN")

    def test_success(self):
        ok, err = self._run(["abcd"], [self.AUTH_PAGE, "PNG", self.LOGIN_OK])
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_empty_code_retries(self):
        ok, err = self._run(
            ["", "abcd"],
            [self.AUTH_PAGE, "PNG", self.AUTH_PAGE, "PNG", self.LOGIN_OK],
        )
        self.assertTrue(ok)

    def test_ocr_missing(self):
        with mock.patch.object(w, "_load_ocr", return_value=(None, "未安装 ddddocr")):
            ok, err = w._complete_captcha_login(
                mock.Mock(), "www.example.com", "u", "p", "member.php?auth=X")
        self.assertFalse(ok)
        self.assertIn("ddddocr", err)


class TestRunOneAccount(unittest.TestCase):
    CRED = {"kind": "account", "payload": ("alice", "secret")}

    def test_cache_hit_skips_login(self):
        sign = {"success": True, "duplicate": True, "cookie_invalid": False,
                "message": "今日已签到", "stats": ""}
        with mock.patch.object(w, "read_cookie_cache",
                               return_value={"alice": {"cookies": "k=v"}}), \
                mock.patch.object(w, "new_session", return_value=mock.Mock()), \
                mock.patch.object(w, "_checkin_with_session", return_value=sign), \
                mock.patch.object(w, "login_with_password") as login:
            result = w.run_one_account(self.CRED, 0, "www.example.com")
        login.assert_not_called()
        self.assertTrue(result["success"])
        self.assertTrue(result["duplicate"])

    def test_cache_invalid_falls_back_to_login_failure(self):
        invalid = {"success": False, "duplicate": False, "cookie_invalid": True,
                   "message": "Cookie 已失效", "stats": ""}
        with mock.patch.object(w, "read_cookie_cache",
                               return_value={"alice": {"cookies": "k=v"}}), \
                mock.patch.object(w, "new_session", return_value=mock.Mock()), \
                mock.patch.object(w, "_checkin_with_session", return_value=invalid), \
                mock.patch.object(w, "login_with_password",
                                  return_value=(None, "账号或密码错误")) as login:
            result = w.run_one_account(self.CRED, 0, "www.example.com")
        login.assert_called_once()
        self.assertFalse(result["success"])
        self.assertTrue(result["auth_failed"])


class TestBusinessRetry(unittest.TestCase):
    """业务失败自动重试(SIGN_RETRY)。"""

    def setUp(self):
        self.patches = [
            mock.patch.object(w, "SIGN_RETRY", 1),
            mock.patch.object(w, "SIGN_RETRY_DELAY", 1),
            mock.patch.object(w, "rand_sleep", lambda *a, **k: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _run(self, responses):
        calls = []

        def fake_http(session, method, url, retry=True, **kwargs):
            calls.append(url)
            if not responses:
                raise AssertionError(f"多余的请求: {url}")
            return _FakeResp(responses.pop(0))

        with mock.patch.object(w, "http_request", side_effect=fake_http):
            result = w.do_checkin(mock.Mock(), "www.example.com", PAGE_UNSIGNED)
        return result, calls

    def test_business_failure_then_success(self):
        # 第 1 次:签到未知文案 + 回查未签到;刷新页面;第 2 次:签到成功 + 回查已签到
        result, calls = self._run([
            RESP_UNKNOWN_AJAX, PAGE_UNSIGNED,   # 第 1 次尝试
            PAGE_UNSIGNED,                      # 重试前刷新
            RESP_SUCCESS_AJAX, PAGE_SIGNED,     # 第 2 次尝试
        ])
        self.assertTrue(result["success"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(len(calls), 5)

    def test_business_failure_exhausted(self):
        result, calls = self._run([
            RESP_UNKNOWN_AJAX, PAGE_UNSIGNED,
            PAGE_UNSIGNED,
            RESP_UNKNOWN_AJAX, PAGE_UNSIGNED,
        ])
        self.assertFalse(result["success"])
        self.assertIn("系统繁忙", result["message"])
        self.assertEqual(len(calls), 5)

    def test_unconfirmed_then_page_shows_signed(self):
        """签到后回查网络失败,重试前刷新发现已签到:按成功处理,不再重复提交。"""
        calls = []

        def fake_http(session, method, url, retry=True, **kwargs):
            calls.append(url)
            if "id=fx_checkin:checkin" in url:
                return _FakeResp(RESP_UNKNOWN_AJAX)
            if len(calls) == 2:
                # 第一次回查失败
                raise w.requests.exceptions.ConnectionError("boom")
            return _FakeResp(PAGE_SIGNED)

        with mock.patch.object(w, "http_request", side_effect=fake_http):
            result = w.do_checkin(mock.Mock(), "www.example.com", PAGE_UNSIGNED)
        self.assertTrue(result["success"])
        self.assertIn("重试前页面已显示已签到", result["message"])
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
