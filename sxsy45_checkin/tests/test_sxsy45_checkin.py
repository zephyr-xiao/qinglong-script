# -*- coding: utf-8 -*-
"""sxsy45_checkin 单元测试（纯函数 + mock 分支判定，不发真实请求）"""
import contextlib
import io
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import sxsy45_checkin as msc
except SystemExit:
    # requests 缺失时打桩后重新加载（青龙外环境兜底）
    stub = types.ModuleType("requests")
    stub.Session = object
    sys.modules["requests"] = stub
    import sxsy45_checkin as msc


# ---------- 实测样本（2026-11-06 真机抓取，sx_signin 插件） ----------
def _signin_page(signed=0, formhash="414fc3f0", streak=32, total=37):
    """构造签到页样本：结构与真实页面一致（window.SIGNIN 配置 + 统计数字）。"""
    cfg = {
        "ajax": "https://sxsy33.com/plugin.php?id=sx_signin:ajax",
        "formhash": formhash,
        "signed": signed,
        "captcha": 1,
        "captchaUrl": "https://sxsy33.com/plugin.php?id=sx_signin:captcha",
    }
    return ('<p class="signin-status-text" id="signin-status-text">今日未签到</p>'
            f'<span class="num" id="signin-streak">{streak}</span>'
            f'<span class="num" id="signin-total">{total}</span>'
            f'<script type="text/javascript">window.SIGNIN = {json.dumps(cfg)};</script>')


# 登录页模板里也有 succeedlocation，登录成功判定不能只看它
SAMPLE_LOGIN_PAGE = ('<form id="loginform_LNX0i" action="member.php?mod=logging&action=login'
                     '&loginsubmit=yes&loginhash=LNX0i">'
                     '<input type="hidden" name="formhash" value="9a17bb38" />'
                     '</form><p id="succeedlocation" class="alert_btnleft"></p>')

# 已登录页面：退出链接同样命中 "member.php?mod=logging" 子串
SAMPLE_LOGGED_IN_PAGE = (
    '<a href="member.php?mod=logging&amp;action=logout&amp;formhash=bc7adba7">退出</a>'
    "<script>var discuz_uid = '534151';</script>")

SAMPLE_SECCODE_UPDATE = '<input type="hidden" name="seccodehash" type="hidden" value="S12ab" />'


class TestParseSigninConfig(unittest.TestCase):
    """window.SIGNIN 配置解析：签到流程的 formhash/ajax/验证码开关都来自这里。"""

    def test_valid(self):
        cfg = msc.parse_signin_config(_signin_page())
        self.assertEqual(cfg["formhash"], "414fc3f0")
        self.assertEqual(cfg["signed"], 0)
        self.assertTrue(cfg["captchaUrl"].endswith("sx_signin:captcha"))

    def test_missing_returns_empty(self):
        self.assertEqual(msc.parse_signin_config("<html>插件不存在或已关闭</html>"), {})

    def test_bad_json_returns_empty(self):
        self.assertEqual(msc.parse_signin_config("window.SIGNIN = {oops};"), {})

    def test_non_dict_json_returns_empty(self):
        self.assertEqual(msc.parse_signin_config("window.SIGNIN = [1,2];"), {})

    def test_empty_input(self):
        self.assertEqual(msc.parse_signin_config(""), {})
        self.assertEqual(msc.parse_signin_config(None), {})


class TestIsLoginPage(unittest.TestCase):
    """登录态判据回归：已登录页面的「退出」链接不得被当成登录页。"""

    def test_logged_in_page_with_logout_link(self):
        self.assertFalse(msc.is_login_page(SAMPLE_LOGGED_IN_PAGE))

    def test_guest_login_form(self):
        self.assertTrue(msc.is_login_page(SAMPLE_LOGIN_PAGE))

    def test_guest_uid_zero(self):
        self.assertTrue(msc.is_login_page("discuz_uid = '0'"))

    def test_normal_page(self):
        self.assertFalse(msc.is_login_page("<html><title>每日签到 - 尚香书苑</title></html>"))

    def test_empty(self):
        self.assertFalse(msc.is_login_page(""))


class TestHasAuthCookie(unittest.TestCase):
    """Discuz 的 *_auth Cookie 是登录成功的确定信号。"""

    class _Cookie:
        def __init__(self, name, value):
            self.name, self.value = name, value

    def _session(self, cookies):
        s = mock.MagicMock()
        s.cookies = cookies
        return s

    def test_auth_cookie_present(self):
        self.assertTrue(msc.has_auth_cookie(
            self._session([self._Cookie("u52q_2132_saltkey", "K"),
                           self._Cookie("u52q_2132_auth", "V")])))

    def test_no_auth_cookie(self):
        self.assertFalse(msc.has_auth_cookie(
            self._session([self._Cookie("u52q_2132_saltkey", "K")])))

    def test_empty_auth_value_ignored(self):
        self.assertFalse(msc.has_auth_cookie(
            self._session([self._Cookie("u52q_2132_auth", "")])))


class TestParseCredentials(unittest.TestCase):
    """环境变量账号解析"""

    def test_single(self):
        self.assertEqual(msc.parse_credentials("exampleuser#pw123"), [("exampleuser", "pw123")])

    def test_multi_amp(self):
        self.assertEqual(msc.parse_credentials("a#p1&b#p2"), [("a", "p1"), ("b", "p2")])

    def test_multi_newline(self):
        self.assertEqual(msc.parse_credentials("a#p1\nb#p2"), [("a", "p1"), ("b", "p2")])

    def test_password_contains_hash(self):
        self.assertEqual(msc.parse_credentials("a#p#1"), [("a", "p#1")])

    def test_empty_fragments_skipped(self):
        self.assertEqual(msc.parse_credentials("a#p1&&\n\nb#p2"), [("a", "p1"), ("b", "p2")])

    def test_cookie_string_no_hash(self):
        self.assertEqual(msc.parse_credentials("u52q_2132_saltkey=abc"), [])

    def test_empty(self):
        self.assertEqual(msc.parse_credentials(""), [])


class TestParseCookieStr(unittest.TestCase):
    def test_basic(self):
        jar = msc.parse_cookie_str("a=1; b=2; c=x=y")
        self.assertEqual(jar, {"a": "1", "b": "2", "c": "x=y"})

    def test_bad_parts_skipped(self):
        self.assertEqual(msc.parse_cookie_str("a=1; ;;  "), {"a": "1"})


class TestMask(unittest.TestCase):
    def test_username(self):
        self.assertEqual(msc.mask("exampleuser"), "ex*******er")

    def test_email(self):
        # mask 规范：@前长度 <=2 时保留首字符
        self.assertEqual(msc.mask("ab@test.com"), "a*@test.com")
        self.assertEqual(msc.mask("abcde@test.com"), "ab***@test.com")

    def test_short(self):
        self.assertEqual(msc.mask("ab"), "a*")
        self.assertEqual(msc.mask(""), "")


class TestFetchSignStats(unittest.TestCase):
    def test_full(self):
        stats = msc.fetch_sign_stats(_signin_page(streak=32, total=37))
        self.assertIn("连续 32 天", stats)
        self.assertIn("累计 37 天", stats)

    def test_empty(self):
        self.assertEqual(msc.fetch_sign_stats("<html></html>"), "")


class TestDoSigninBranches(unittest.TestCase):
    """do_signin 状态判定分支（mock 网络层，sx_signin 插件）"""

    def _session(self):
        s = mock.MagicMock()
        s._timeout = 30
        return s

    @staticmethod
    def _json_resp(payload, status=200):
        r = mock.MagicMock(status_code=status, text=json.dumps(payload, ensure_ascii=False))
        r.json.return_value = payload
        return r

    @staticmethod
    def _html_resp(html, status=200):
        r = mock.MagicMock(status_code=status, text=html)
        r.json.side_effect = ValueError("not json")
        return r

    def _run(self, pages, post_resp=None, captcha="ab12"):
        """pages: _get_sign_page 依次返回的 (html, err) 列表"""
        session = self._session()
        session.post.return_value = post_resp or self._json_resp({"code": 0, "data": {}})
        with mock.patch.object(msc, "_get_sign_page", side_effect=pages), \
             mock.patch.object(msc, "_sign_captcha_code", return_value=captcha), \
             mock.patch.object(msc.time, "sleep"):
            return msc.do_signin(session, ocr=mock.MagicMock())

    def test_already_signed(self):
        """signed=1 -> 幂等跳过"""
        r = self._run([(_signin_page(signed=1, streak=1, total=38), "")])
        self.assertEqual(r["state"], "signed_today")
        self.assertEqual(r["message"], "今日已签到")
        self.assertIn("累计 38 天", r["stats"])

    def test_success_flow(self):
        """未签 -> 验证码 -> code 0 -> 回查 signed=1 -> success"""
        payload = {"code": 0, "msg": "签到成功",
                   "data": {"signed": 1, "streak": 33, "total": 38, "credit": 1}}
        r = self._run([(_signin_page(), ""), (_signin_page(signed=1, streak=33, total=38), "")],
                      post_resp=self._json_resp(payload))
        self.assertEqual(r["state"], "success")
        self.assertIn("本次奖励 +1", r["message"])
        self.assertIn("累计 38 天", r["stats"])

    def test_captcha_error_retries_then_succeeds(self):
        """code=11 换图重试，第二次成功"""
        session = self._session()
        session.post.side_effect = [
            self._json_resp({"code": 11, "msg": "验证码错误"}),
            self._json_resp({"code": 0, "msg": "签到成功", "data": {"streak": 33, "total": 38}}),
        ]
        with mock.patch.object(msc, "_get_sign_page",
                               side_effect=[(_signin_page(), ""),
                                            (_signin_page(signed=1, streak=33, total=38), "")]), \
             mock.patch.object(msc, "_sign_captcha_code", return_value="ab12"), \
             mock.patch.object(msc.time, "sleep"):
            r = msc.do_signin(session, ocr=mock.MagicMock())
        self.assertEqual(r["state"], "success")
        self.assertEqual(session.post.call_count, 2)

    def test_captcha_always_wrong_fails(self):
        session = self._session()
        session.post.return_value = self._json_resp({"code": 11, "msg": "验证码错误"})
        with mock.patch.object(msc, "_get_sign_page", return_value=(_signin_page(), "")), \
             mock.patch.object(msc, "_sign_captcha_code", return_value="ab12"), \
             mock.patch.object(msc.time, "sleep"):
            r = msc.do_signin(session, ocr=mock.MagicMock())
        self.assertEqual(r["state"], "failed")
        self.assertIn("验证码识别错误", r["message"])
        self.assertEqual(session.post.call_count, msc.SIGN_CAPTCHA_TRIES)

    def test_code_2_already_signed(self):
        r = self._run([(_signin_page(), ""), (_signin_page(signed=1), "")],
                      post_resp=self._json_resp({"code": 2, "msg": "今天已经签到过了"}))
        self.assertEqual(r["state"], "signed_today")
        self.assertIn("今日已签到", r["message"])

    def test_other_code_fails(self):
        r = self._run([(_signin_page(), "")],
                      post_resp=self._json_resp({"code": 5, "msg": "签到功能已关闭"}))
        self.assertEqual(r["state"], "failed")
        self.assertIn("签到功能已关闭", r["message"])

    def test_no_signin_config(self):
        """旧插件页面/结构变化 -> 明确报结构变化，而不是误判登录失效"""
        r = self._run([("<html><title>提示信息 - 尚香书苑</title>插件不存在或已关闭</html>", "")])
        self.assertEqual(r["state"], "failed")
        self.assertIn("结构变化", r["message"])

    def test_cookie_dead(self):
        r = self._run([(SAMPLE_LOGIN_PAGE, "")])
        self.assertEqual(r["state"], "failed")
        self.assertIn("登录态失效", r["message"])

    def test_logged_in_page_with_logout_link_not_dead(self):
        """回归：已登录页面含退出链接时不得判登录失效（2026-11-05 故障根因）"""
        r = self._run([(SAMPLE_LOGGED_IN_PAGE, "")])
        self.assertEqual(r["state"], "failed")
        self.assertNotIn("登录态失效", r["message"])
        self.assertIn("结构变化", r["message"])

    def test_sign_page_unavailable(self):
        r = self._run([("", "HTTP 522「sxsy45.com | 522: Connection timed out」")])
        self.assertEqual(r["state"], "failed")
        self.assertIn("签到页请求失败", r["message"])
        self.assertIn("522", r["message"])

    def test_recheck_unavailable(self):
        """接口 code 0 但回查页 522：判成功并标注未复核"""
        payload = {"code": 0, "data": {"streak": 33, "total": 38}}
        r = self._run([(_signin_page(), ""), ("", "HTTP 522")],
                      post_resp=self._json_resp(payload))
        self.assertEqual(r["state"], "success")
        self.assertIn("回查页暂不可用", r["message"])

    def test_recheck_still_unsigned(self):
        """接口 code 0 但回查仍 signed=0：不能误报成功"""
        r = self._run([(_signin_page(), ""), (_signin_page(signed=0), "")],
                      post_resp=self._json_resp({"code": 0, "data": {"streak": 33}}))
        self.assertEqual(r["state"], "failed")
        self.assertIn("回查仍未签到", r["message"])

    def test_non_json_login_redirect(self):
        """接口返回登录页 HTML（掉登录）-> 登录态失效"""
        r = self._run([(_signin_page(), "")], post_resp=self._html_resp(SAMPLE_LOGIN_PAGE))
        self.assertEqual(r["state"], "failed")
        self.assertIn("登录态失效", r["message"])

    def test_non_json_other(self):
        r = self._run([(_signin_page(), "")],
                      post_resp=self._html_resp("<html><title>403 Forbidden</title></html>", 403))
        self.assertEqual(r["state"], "failed")
        self.assertIn("非 JSON", r["message"])

    def test_missing_formhash(self):
        page = ('<script>window.SIGNIN = {"ajax":"https://x/plugin.php?id=sx_signin:ajax",'
                '"signed":0};</script>')
        r = self._run([(page, "")])
        self.assertEqual(r["state"], "failed")
        self.assertIn("formhash", r["message"])

    def test_missing_ocr(self):
        """ddddocr 缺失时给出明确提示，不空跑"""
        session = self._session()
        with mock.patch.object(msc, "_get_sign_page", return_value=(_signin_page(), "")), \
             mock.patch.object(msc, "get_ocr", return_value=None):
            r = msc.do_signin(session, ocr=None)
        self.assertEqual(r["state"], "failed")
        self.assertIn("ddddocr", r["message"])


class TestLoginSuccessDetection(unittest.TestCase):
    """登录成功判定：登录页模板也含 succeedlocation，必须看 succeedhandle 或 auth Cookie。"""

    class _Cookie:
        def __init__(self, name, value):
            self.name, self.value = name, value

    def _run(self, post_resp, cookies=None):
        session = mock.MagicMock()
        session._timeout = 30
        session.cookies = list(cookies or [])
        session.post.return_value = post_resp
        page = mock.MagicMock(status_code=200, text=SAMPLE_LOGIN_PAGE, headers={})
        upd = mock.MagicMock(status_code=200, text=SAMPLE_SECCODE_UPDATE, headers={})

        def fake_get(sess, url, **kw):
            return (upd, "") if "misc.php" in url else (page, "")

        with mock.patch.object(msc, "get_retry", side_effect=fake_get), \
             mock.patch.object(msc, "_ocr", return_value="abcd"), \
             mock.patch.object(msc.time, "sleep"):
            return msc.discuz_login(session, "exampleuser", "pw123", ocr=mock.MagicMock())

    def test_login_page_with_succeedlocation_is_not_success(self):
        """登录页被原样返回（只有 succeedlocation）不得判成功"""
        resp = mock.MagicMock(status_code=200, text=SAMPLE_LOGIN_PAGE, headers={})
        ok, msg = self._run(resp)
        self.assertFalse(ok)
        self.assertIn("登录失败", msg)

    def test_succeedhandle_is_success(self):
        resp = mock.MagicMock(
            status_code=200,
            text="<script>succeedhandle_login('https://sxsy33.com/forum.php')</script>",
            headers={})
        ok, _ = self._run(resp)
        self.assertTrue(ok)

    def test_auth_cookie_is_success(self):
        resp = mock.MagicMock(status_code=200, text="<html>正在跳转</html>", headers={})
        ok, _ = self._run(resp, cookies=[self._Cookie("u52q_2132_auth", "V")])
        self.assertTrue(ok)


class TestSiteErrorDetection(unittest.TestCase):
    """Cloudflare 522 等错误页必须被识别为瞬时故障，而不是站点改版。"""

    def _resp(self, status, body=""):
        return mock.MagicMock(status_code=status, text=body, content=b"")

    def test_cloudflare_522_by_status(self):
        body = '<html><head><title>sxsy45.com | 522: Connection timed out</title></head></html>'
        self.assertTrue(msc.is_site_error(self._resp(522, body)))

    def test_cloudflare_522_with_200_status(self):
        """个别 CDN 用 200 包装错误页，按标题兜底识别。"""
        body = '<html><head><title>example.com | 522: Connection timed out</title></head></html>'
        self.assertTrue(msc.is_site_error(self._resp(200, body)))

    def test_normal_page_not_error(self):
        self.assertFalse(msc.is_site_error(self._resp(200, "<title>登录 - 尚香书苑</title>")))

    def test_404_not_treated_as_site_error(self):
        self.assertFalse(msc.is_site_error(self._resp(404, "<title>404 Not Found</title>")))


class TestCfChallengeDetection(unittest.TestCase):
    """Cloudflare 人机质询页必须单独识别，不能被当成 Cookie 失效或站点改版。"""

    def _resp(self, status, body="", headers=None):
        return mock.MagicMock(status_code=status, text=body, headers=headers or {})

    def test_403_just_a_moment(self):
        body = ("<html><head><title>Just a moment...</title></head>"
                "<body>Checking your browser before accessing</body></html>")
        self.assertTrue(msc.is_cf_challenge(self._resp(403, body)))

    def test_cf_mitigated_header(self):
        self.assertTrue(msc.is_cf_challenge(
            self._resp(403, "<title>任意标题</title>", {"cf-mitigated": "challenge"})))

    def test_challenge_platform_body(self):
        """CF 有时用 503 + challenge-platform 脚本下发质询。"""
        body = ('<html><title>请稍候...</title><script src="/cdn-cgi/challenge-platform/'
                'h/b/orchestrate/chl_page/v1"></script></html>')
        self.assertTrue(msc.is_cf_challenge(self._resp(503, body)))

    def test_normal_403_not_challenge(self):
        self.assertFalse(msc.is_cf_challenge(self._resp(403, "<title>403 Forbidden</title>")))

    def test_normal_page_not_challenge(self):
        self.assertFalse(msc.is_cf_challenge(self._resp(200, "<title>登录 - 尚香书苑</title>")))

    def test_none_response(self):
        self.assertFalse(msc.is_cf_challenge(None))


class TestGetRetry(unittest.TestCase):
    """CDN 522 / 网络异常必须退避重试，不能一次失败就放弃。"""

    def _session(self):
        s = mock.MagicMock()
        s._timeout = 30
        return s

    def test_retry_then_success(self):
        s = self._session()
        err_resp = mock.MagicMock(status_code=522, text="<title>x | 522: Connection timed out</title>")
        ok = mock.MagicMock(status_code=200, text="<title>登录 - 尚香书苑</title>")
        s.get.side_effect = [err_resp, ok]
        with mock.patch.object(msc.time, "sleep") as sleeper:
            r, err = msc.get_retry(s, "https://example.com", label="测试")
        self.assertIs(r, ok)
        self.assertEqual(err, "")
        self.assertEqual(s.get.call_count, 2)
        self.assertEqual(sleeper.call_count, 1)

    def test_all_fail_returns_none(self):
        s = self._session()
        err_resp = mock.MagicMock(status_code=522, text="<title>x | 522: Connection timed out</title>")
        s.get.return_value = err_resp
        with mock.patch.object(msc.time, "sleep"):
            r, err = msc.get_retry(s, "https://example.com", label="测试", tries=3)
        self.assertIsNone(r)
        self.assertIn("522", err)
        self.assertEqual(s.get.call_count, 3)

    def test_network_exception_retried(self):
        s = self._session()
        ok = mock.MagicMock(status_code=200, text="ok")
        s.get.side_effect = [ConnectionError("timed out"), ok]
        with mock.patch.object(msc.time, "sleep"):
            r, err = msc.get_retry(s, "https://example.com", label="测试")
        self.assertIs(r, ok)

    def test_challenge_retried_once_then_gives_up(self):
        """CF 质询绑定出口 IP：最多试 CF_CHALLENGE_TRIES 次，不能无限刷。"""
        s = self._session()
        challenge = mock.MagicMock(status_code=403,
                                   text="<title>Just a moment...</title>", headers={})
        s.get.return_value = challenge
        with mock.patch.object(msc.time, "sleep") as sleeper:
            r, err = msc.get_retry(s, "https://example.com", label="测试", tries=3)
        self.assertIsNone(r)
        self.assertIn(msc.CF_CHALLENGE_MARK, err)
        self.assertEqual(s.get.call_count, msc.CF_CHALLENGE_TRIES)
        self.assertEqual(sleeper.call_count, msc.CF_CHALLENGE_TRIES - 1)

    def test_challenge_then_success(self):
        """质询后重试命中正常页：应继续流程。"""
        s = self._session()
        challenge = mock.MagicMock(status_code=403,
                                   text="<title>Just a moment...</title>", headers={})
        ok = mock.MagicMock(status_code=200, text="<title>登录 - 尚香书苑</title>", headers={})
        s.get.side_effect = [challenge, ok]
        with mock.patch.object(msc.time, "sleep"):
            r, err = msc.get_retry(s, "https://example.com", label="测试")
        self.assertIs(r, ok)
        self.assertEqual(err, "")

    def test_no_sleep_after_last_attempt(self):
        """最后一次尝试命中质询时不能空睡，直接返回。"""
        s = self._session()
        err_resp = mock.MagicMock(status_code=522,
                                  text="<title>x | 522: Connection timed out</title>", headers={})
        challenge = mock.MagicMock(status_code=403,
                                   text="<title>Just a moment...</title>", headers={})
        s.get.side_effect = [err_resp, challenge]
        with mock.patch.object(msc.time, "sleep") as sleeper:
            r, err = msc.get_retry(s, "https://example.com", label="测试", tries=2)
        self.assertIsNone(r)
        self.assertIn(msc.CF_CHALLENGE_MARK, err)
        self.assertEqual(s.get.call_count, 2)
        self.assertEqual(sleeper.call_count, 1)  # 只该有一次 522 的退避

    def test_403_without_challenge_returns_none_immediately(self):
        """非质询类 403（WAF 直接拦截）不是可用页面，也不能被当成"未登录"。"""
        s = self._session()
        blocked = mock.MagicMock(status_code=403,
                                 text="<title>403 Forbidden</title>", headers={})
        s.get.return_value = blocked
        with mock.patch.object(msc.time, "sleep") as sleeper:
            r, err = msc.get_retry(s, "https://example.com", label="测试", tries=3)
        self.assertIsNone(r)
        self.assertIn("403", err)
        self.assertEqual(s.get.call_count, 1)
        self.assertEqual(sleeper.call_count, 0)


class TestCheckCookieValid(unittest.TestCase):
    """Cookie 检测三态：站点故障时必须返回 None，避免误清有效缓存。"""

    def _session(self):
        s = mock.MagicMock()
        s._timeout = 30
        return s

    def test_valid(self):
        resp = mock.MagicMock(status_code=200, text="discuz_uid = '123456'")
        with mock.patch.object(msc, "get_retry", return_value=(resp, "")):
            self.assertTrue(msc.check_cookie_valid(self._session()))

    def test_invalid(self):
        resp = mock.MagicMock(status_code=200, text="discuz_uid = '0'")
        with mock.patch.object(msc, "get_retry", return_value=(resp, "")):
            self.assertFalse(msc.check_cookie_valid(self._session()))

    def test_unknown_on_site_error(self):
        with mock.patch.object(msc, "get_retry", return_value=(None, "HTTP 522「522: Connection timed out」")):
            self.assertIsNone(msc.check_cookie_valid(self._session()))

    def test_unknown_on_cf_challenge(self):
        """CF 质询页没有 discuz_uid，必须判"无法确认"，绝不能让调用方清缓存。"""
        err = f"{msc.CF_CHALLENGE_MARK}（HTTP 403）"
        buf = io.StringIO()
        with mock.patch.object(msc, "get_retry", return_value=(None, err)), \
             contextlib.redirect_stdout(buf):
            self.assertIsNone(msc.check_cookie_valid(self._session()))
        self.assertIn("保留缓存", buf.getvalue())


class TestRunOneAccountKeepsCacheOnChallenge(unittest.TestCase):
    """站点无法判定登录态时（CF 质询/522）不得重登、不得丢缓存。"""

    def test_skips_login_when_cookie_state_unknown(self):
        cached = {"exampleuser": {"cookies": "u52q_2132_saltkey=K; u52q_2132_auth=V"}}
        with mock.patch.object(msc, "read_cookie_cache", return_value=cached), \
             mock.patch.object(msc, "check_cookie_valid", return_value=None), \
             mock.patch.object(msc, "get_ocr", return_value=mock.MagicMock()), \
             mock.patch.object(msc, "discuz_login") as login, \
             mock.patch.object(msc, "do_signin", return_value={
                 "state": "failed",
                 "message": f"签到页请求失败：{msc.CF_CHALLENGE_MARK}（HTTP 403）",
                 "stats": ""}):
            result = msc.run_one_account(("exampleuser", "pw123"), "", 30, False)
        self.assertFalse(result["success"])
        login.assert_not_called()


class TestLoginChallengeAbort(unittest.TestCase):
    """登录页被 CF 质询时必须立刻停手：不消耗登录重试次数，也不加重风控。"""

    def test_aborts_on_first_challenge(self):
        session = mock.MagicMock()
        session._timeout = 30
        stub = types.ModuleType("ddddocr")
        stub.DdddOcr = lambda *a, **k: mock.MagicMock()
        err = f"{msc.CF_CHALLENGE_MARK}（HTTP 403）"
        with mock.patch.dict(sys.modules, {"ddddocr": stub}), \
             mock.patch.object(msc, "get_retry", return_value=(None, err)) as mocked_get, \
             mock.patch.object(msc.time, "sleep"):
            ok, msg = msc.discuz_login(session, "exampleuser", "pw123", ocr=mock.MagicMock())
        self.assertFalse(ok)
        self.assertIn(msc.CF_CHALLENGE_MARK, msg)
        self.assertIn("出口 IP", msg)
        self.assertEqual(mocked_get.call_count, 1)

    def test_aborts_when_seccode_query_challenged(self):
        """验证码接口被质询同样立刻失败。"""
        session = mock.MagicMock()
        session._timeout = 30
        login_page = mock.MagicMock(
            status_code=200,
            text=('<input type="hidden" name="formhash" value="9a17bb38" />'
                  "<script>updateseccode('cS','member::logging')</script>"),
            headers={})
        err = f"{msc.CF_CHALLENGE_MARK}（HTTP 403）"
        stub = types.ModuleType("ddddocr")
        stub.DdddOcr = lambda *a, **k: mock.MagicMock()
        with mock.patch.dict(sys.modules, {"ddddocr": stub}), \
             mock.patch.object(msc, "get_retry",
                               side_effect=[(login_page, ""), (None, err)]), \
             mock.patch.object(msc.time, "sleep"):
            ok, msg = msc.discuz_login(session, "exampleuser", "pw123", ocr=mock.MagicMock())
        self.assertFalse(ok)
        self.assertIn("验证码接口被拦截", msg)
        session.post.assert_not_called()

    def test_aborts_when_login_post_challenged(self):
        """账密 POST 也被质询时同样立刻失败，不再浪费下一轮登录尝试。"""
        session = mock.MagicMock()
        session._timeout = 30
        session.post.return_value = mock.MagicMock(
            status_code=403, text="<title>Just a moment...</title>", headers={})
        login_page = mock.MagicMock(
            status_code=200,
            text=('<input type="hidden" name="formhash" value="9a17bb38" />'
                  "<script>updateseccode('cS','member::logging')</script>"),
            headers={})
        seccode_update = mock.MagicMock(
            status_code=200,
            text='<input type="hidden" name="seccodehash" type="hidden" value="S12ab" />',
            headers={})
        stub = types.ModuleType("ddddocr")
        stub.DdddOcr = lambda *a, **k: mock.MagicMock()
        with mock.patch.dict(sys.modules, {"ddddocr": stub}), \
             mock.patch.object(msc, "get_retry",
                               side_effect=[(login_page, ""), (seccode_update, "")]), \
             mock.patch.object(msc, "_ocr", return_value="abcd"), \
             mock.patch.object(msc.time, "sleep"):
            ok, msg = msc.discuz_login(session, "exampleuser", "pw123", ocr=mock.MagicMock())
        self.assertFalse(ok)
        self.assertIn("登录接口被拦截", msg)
        self.assertEqual(session.post.call_count, 1)


class TestCookieCacheAtomicWrite(unittest.TestCase):
    """缓存必须两段写 + 原子替换：写一半崩溃不能毁掉已有全部账号的缓存。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self._cache = Path(self._tmp.name) / "sxsy45_cookie.json"
        self._orig = msc.COOKIE_CACHE
        msc.COOKIE_CACHE = self._cache

    def tearDown(self):
        msc.COOKIE_CACHE = self._orig
        self._tmp.cleanup()

    def _seed(self):
        msc.write_cookie_cache("alice", "salt_auth=OLD")

    def test_write_success_no_part_residue(self):
        self._seed()
        msc.write_cookie_cache("bob", "salt_auth=NEW")
        accounts = msc.read_cookie_cache()
        self.assertEqual(accounts["alice"]["cookies"], "salt_auth=OLD")
        self.assertEqual(accounts["bob"]["cookies"], "salt_auth=NEW")
        # 两段写成功后不允许残留 .part 临时文件
        self.assertFalse(self._cache.with_name(self._cache.name + ".part").exists())

    def test_dump_crash_keeps_original_cache(self):
        """写盘中途失败（如磁盘满）时，旧缓存必须完好，且不留 .part。"""
        self._seed()
        with mock.patch.object(msc.json, "dump", side_effect=OSError("disk full")):
            msc.write_cookie_cache("bob", "salt_auth=NEW")
        # 原缓存未被截断/损坏，旧账号仍在
        accounts = msc.read_cookie_cache()
        self.assertEqual(accounts.get("alice", {}).get("cookies"), "salt_auth=OLD")
        self.assertNotIn("bob", accounts)
        part = self._cache.with_name(self._cache.name + ".part")
        self.assertFalse(part.exists(), "失败后必须清理 .part 临时文件")
        self.assertTrue(self._cache.exists(), "原缓存文件必须保留")

    def test_replace_failure_keeps_original_cache(self):
        """os.replace 失败（如目标被占用）同样不能丢旧缓存。"""
        self._seed()
        with mock.patch.object(msc.os, "replace", side_effect=OSError("busy")):
            msc.write_cookie_cache("bob", "salt_auth=NEW")
        self.assertEqual(msc.read_cookie_cache().get("alice", {}).get("cookies"),
                         "salt_auth=OLD")
        self.assertFalse(
            self._cache.with_name(self._cache.name + ".part").exists())


class TestResolveProxy(unittest.TestCase):
    """本站默认直连：不读全局代理，只有显式 SXSY_PROXY 才走代理。"""

    _KEYS = ("SXSY_PROXY", "HTTPS_PROXY", "https_proxy",
             "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

    def _env(self, **kv):
        # 先放空值、再放 kv：Windows 的 os.environ 大小写不敏感，
        # 空的 https_proxy 与 HTTPS_PROXY 是同一个变量，顺序反了会把注入值清空
        clean = {k: "" for k in self._KEYS if k not in kv}
        clean.update(kv)
        return mock.patch.dict(os.environ, clean)

    def test_default_direct_ignores_global_proxy(self):
        with self._env(HTTPS_PROXY="http://127.0.0.1:7890"):
            proxy, src = msc.resolve_proxy()
        self.assertEqual(proxy, "")
        self.assertIn("HTTPS_PROXY", src)   # 被忽略的变量要写进日志，便于排查

    def test_explicit_proxy(self):
        with self._env(SXSY_PROXY="http://127.0.0.1:7890"):
            proxy, src = msc.resolve_proxy()
        self.assertEqual(proxy, "http://127.0.0.1:7890")
        self.assertEqual(src, "SXSY_PROXY")

    def test_explicit_direct_keywords(self):
        for value in ("direct", "none", "off", "0", "false", "-"):
            with self._env(SXSY_PROXY=value, HTTPS_PROXY="http://127.0.0.1:7890"):
                proxy, src = msc.resolve_proxy()
            self.assertEqual(proxy, "", value)
            self.assertIn("直连", src)

    def test_direct_session_disables_env_proxy(self):
        """直连时 trust_env 必须关掉，否则 requests 仍会读 HTTPS_PROXY 走代理。"""
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}):
            session = msc.build_session("", 30)
        self.assertFalse(session.trust_env)
        self.assertEqual(dict(session.proxies), {})

    def test_proxy_session_sets_both_schemes(self):
        session = msc.build_session("http://127.0.0.1:7890", 30)
        self.assertEqual(session.proxies["http"], "http://127.0.0.1:7890")
        self.assertEqual(session.proxies["https"], "http://127.0.0.1:7890")


class TestDomainFromNav(unittest.TestCase):
    """导航页解析：站点自维护的最新域名是换域名后的权威来源。"""

    def test_website_url_preferred(self):
        html = ('const CONFIG = {latestDomain:"old.com",'
                'websiteUrl:"https://sxsy33.com",telegramUrl:"https://t.me/x"};')
        self.assertEqual(msc._domain_from_nav(html), "https://sxsy33.com")

    def test_latest_domain_only_gets_https(self):
        self.assertEqual(msc._domain_from_nav('latestDomain:"sxsy33.com",'),
                         "https://sxsy33.com")

    def test_login_page_has_no_domain(self):
        self.assertEqual(msc._domain_from_nav("<html><title>登录 - 尚香书苑</title></html>"), "")

    def test_empty_input(self):
        self.assertEqual(msc._domain_from_nav(""), "")


class TestLoginPageProbe(unittest.TestCase):
    """探测结果三分：可用 / 不可用 / 出口被 CF 质询（换域名无用）。"""

    def _session(self, resp=None, exc=None):
        s = mock.MagicMock()
        s._timeout = 10
        if exc is not None:
            s.get.side_effect = exc
        else:
            s.get.return_value = resp
        return s

    def test_ok_when_formhash_present(self):
        resp = mock.MagicMock(status_code=200, headers={},
                              text='<input name="formhash" value="9a17bb38" />')
        state, note, _ = msc._login_page_probe(self._session(resp), "https://a.example")
        self.assertTrue(state)
        self.assertIn("200", note)

    def test_nav_page_without_formhash(self):
        """旧域名 301 落地的导航页：200 但无 formhash，必须判为不可用。"""
        resp = mock.MagicMock(status_code=200, headers={},
                              text='<title>小说论坛 - 最新地址</title>')
        state, note, html = msc._login_page_probe(self._session(resp), "https://a.example")
        self.assertFalse(state)
        self.assertIn("无 formhash", note)
        self.assertIn("最新地址", html)

    def test_cf_challenge_returns_none(self):
        resp = mock.MagicMock(status_code=403, headers={},
                              text='<title>Just a moment...</title>')
        state, note, _ = msc._login_page_probe(self._session(resp), "https://a.example")
        self.assertIsNone(state)
        self.assertIn(msc.CF_CHALLENGE_MARK, note)

    def test_network_error_not_available(self):
        """网络异常重试一次仍失败 -> 不可用，原因带异常类型。"""
        with mock.patch.object(msc.time, "sleep"):
            state, note, _ = msc._login_page_probe(
                self._session(exc=OSError("boom")), "https://a.example")
        self.assertFalse(state)
        self.assertIn("OSError", note)

    def test_network_error_retried_once(self):
        """站点偶发读超时：探测重试一次，第二次成功即采用。"""
        resp = mock.MagicMock(status_code=200, headers={},
                              text='<input name="formhash" value="9a17bb38" />')
        s = mock.MagicMock()
        s._timeout = 30
        s.get.side_effect = [OSError("read timed out"), resp]
        with mock.patch.object(msc.time, "sleep"):
            state, _, _ = msc._login_page_probe(s, "https://a.example")
        self.assertTrue(state)
        self.assertEqual(s.get.call_count, 2)


class TestResolveBase(unittest.TestCase):
    """域名发现：候选域名 -> 导航页最新域名；CF 质询时不再换域名。"""

    CANDIDATES = ("https://old.example", "https://older.example")

    def _run(self, probe_map, nav_html="", nav_err="HTTP 500"):
        def probe(session, base, debug=False):
            return probe_map.get(base, (False, "HTTP 404", ""))

        resp = mock.MagicMock(text=nav_html) if nav_html else None
        with mock.patch.object(msc, "DOMAIN_CANDIDATES", self.CANDIDATES), \
             mock.patch.object(msc, "_login_page_probe", side_effect=probe), \
             mock.patch.object(msc, "get_retry",
                               return_value=(resp, "" if resp else nav_err)):
            return msc.resolve_base(mock.MagicMock())

    def test_first_candidate_ok(self):
        base, note = self._run({"https://old.example": (True, "HTTP 200", "")})
        self.assertEqual(base, "https://old.example")
        self.assertIn("200", note)

    def test_fallback_to_next_candidate(self):
        base, _ = self._run({
            "https://old.example": (False, "HTTP 200 无 formhash（标题「小说论坛 - 最新地址」）", ""),
            "https://older.example": (True, "HTTP 200", ""),
        })
        self.assertEqual(base, "https://older.example")

    def test_nav_html_from_redirected_probe(self):
        """旧域名 301 到导航页：探测拿到的页面本身就是导航页，可直接解析。"""
        nav = 'latestDomain:"https://sxsy33.com"'
        base, _ = self._run({
            "https://old.example": (False, "HTTP 200 无 formhash（标题「小说论坛 - 最新地址」）", nav),
            "https://older.example": (False, "HTTP 404", ""),
            "https://sxsy33.com": (True, "HTTP 200", ""),
        })
        self.assertEqual(base, "https://sxsy33.com")

    def test_discover_from_nav_url(self):
        """候选全挂时主动取导航页，用其中的最新域名。"""
        base, _ = self._run({"https://sxsy33.com": (True, "HTTP 200", "")},
                            nav_html='websiteUrl:"https://sxsy33.com"')
        self.assertEqual(base, "https://sxsy33.com")

    def test_cf_challenge_stops_probing(self):
        """出口被风控时换域名一样被拦，立即返回让上层报风控。"""
        base, note = self._run({
            "https://old.example": (None, f"{msc.CF_CHALLENGE_MARK}（HTTP 403）", ""),
        })
        self.assertEqual(base, "https://old.example")
        self.assertIn("人机质询", note)

    def test_all_fail_returns_empty_with_reasons(self):
        base, note = self._run({})
        self.assertEqual(base, "")
        self.assertIn("404", note)

    def test_nav_domain_already_tried_is_skipped(self):
        """导航页给出的域名若已在候选里试过，不重复请求。"""
        calls = []

        def probe(session, base, debug=False):
            calls.append(base)
            return (False, "HTTP 404", 'websiteUrl:"https://old.example"')

        with mock.patch.object(msc, "DOMAIN_CANDIDATES", self.CANDIDATES), \
             mock.patch.object(msc, "_login_page_probe", side_effect=probe), \
             mock.patch.object(msc, "get_retry", return_value=(None, "HTTP 500")):
            base, _ = msc.resolve_base(mock.MagicMock())
        self.assertEqual(base, "")
        self.assertEqual(calls.count("https://old.example"), 1)


class TestApplyBase(unittest.TestCase):
    """切换域名后所有派生 URL 必须同步，否则仍会打到旧域名。"""

    def test_derived_urls_follow_base(self):
        saved = (msc.BASE, msc.SIGN_URL, msc.SIGN_AJAX_URL, msc.SIGN_CAPTCHA_URL,
                 msc.LOGIN_URL, msc.FORUM_URL)
        try:
            msc.apply_base("https://sxsy33.com/")
            self.assertEqual(msc.BASE, "https://sxsy33.com")
            self.assertEqual(msc.SIGN_URL, "https://sxsy33.com/plugin.php?id=sx_signin:index")
            self.assertEqual(msc.SIGN_AJAX_URL, "https://sxsy33.com/plugin.php?id=sx_signin:ajax")
            self.assertEqual(msc.SIGN_CAPTCHA_URL,
                             "https://sxsy33.com/plugin.php?id=sx_signin:captcha")
            self.assertEqual(msc.LOGIN_URL,
                             "https://sxsy33.com/member.php?mod=logging&action=login")
            self.assertEqual(msc.FORUM_URL, "https://sxsy33.com/forum.php")
        finally:
            (msc.BASE, msc.SIGN_URL, msc.SIGN_AJAX_URL, msc.SIGN_CAPTCHA_URL,
             msc.LOGIN_URL, msc.FORUM_URL) = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
