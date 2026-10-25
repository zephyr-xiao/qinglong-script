# -*- coding: utf-8 -*-
"""xhcyv_checkin 单元测试（标准库 unittest，无第三方依赖）

覆盖：账号解析 / 脱敏 / JWT exp / token 缓存读写与可用性判定 /
接口响应分类（业务码、5xx、非 JSON、网络重试）/ 签到结果格式化。
真实网络请求不进单测（本地干跑负责）。
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xhcyv_checkin as xc  # noqa: E402


def make_jwt(exp: int) -> str:
    """构造仅含 exp 的假 JWT（测试用，无签名）。"""
    def enc(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{enc({'alg': 'HS256'})}.{enc({'uid': 1, 'exp': exp})}.sig"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSiteSession(FakeSession):
    """带 Cookie 罐的假 session，用于模拟登录成功后站点下发 x_bbs_token。"""

    def __init__(self, responses):
        super().__init__(responses)
        self.cookies = requests.cookies.RequestsCookieJar()

    def request(self, method, url, **kwargs):
        resp = super().request(method, url, **kwargs)
        token = getattr(resp, "set_token", None)
        if token:
            self.cookies.set(xc.TOKEN_COOKIE, token)
        return resp


class TestParseAccounts(unittest.TestCase):
    def test_hash_separator(self):
        self.assertEqual(
            xc.parse_accounts("test_user#test123"),
            [{"type": "credential", "account": "test_user",
              "password": "test123", "label": "tes****er"}],
        )

    def test_colon_separator(self):
        self.assertEqual(
            xc.parse_accounts("test_user:test123"),
            [{"type": "credential", "account": "test_user",
              "password": "test123", "label": "tes****er"}],
        )

    def test_ampersand_separator(self):
        entries = xc.parse_accounts("user1&pass1")
        self.assertEqual(entries[0]["account"], "user1")
        self.assertEqual(entries[0]["password"], "pass1")

    def test_password_containing_colon_keeps_rest(self):
        entries = xc.parse_accounts("user:p:a:ss")
        self.assertEqual(entries[0]["password"], "p:a:ss")

    def test_hash_format_keeps_other_symbols_in_password(self):
        entries = xc.parse_accounts("user#p:a&ss")
        self.assertEqual(entries[0]["account"], "user")
        self.assertEqual(entries[0]["password"], "p:a&ss")

    def test_earliest_separator_wins(self):
        entries = xc.parse_accounts("user:p#a:ss")
        self.assertEqual(entries[0]["account"], "user")
        self.assertEqual(entries[0]["password"], "p#a:ss")

    def test_multiple_lines(self):
        entries = xc.parse_accounts("a:1\nb:2\n\n")
        self.assertEqual([e["account"] for e in entries], ["a", "b"])

    def test_raw_jwt_line(self):
        token = make_jwt(int(time.time()) + 86400)
        entries = xc.parse_accounts(token)
        self.assertEqual(entries[0]["type"], "token")
        self.assertEqual(entries[0]["token"], token)

    def test_skip_invalid_lines(self):
        self.assertEqual(xc.parse_accounts("no-separator\n\n:"), [])

    def test_empty(self):
        self.assertEqual(xc.parse_accounts(""), [])
        self.assertEqual(xc.parse_accounts(None), [])


class TestCollectAccounts(unittest.TestCase):
    def test_single_account_env_inserted_first(self):
        env = {"XHCYV_ACCOUNTS": "b:2", "XHCYV_ACCOUNT": "a", "XHCYV_PASSWORD": "1"}
        with mock.patch.dict(os.environ, env, clear=True):
            entries = xc.collect_accounts()
        self.assertEqual([e["account"] for e in entries], ["a", "b"])

    def test_missing_password_ignored(self):
        with mock.patch.dict(os.environ, {"XHCYV_ACCOUNT": "a"}, clear=True):
            self.assertEqual(xc.collect_accounts(), [])


class TestMasking(unittest.TestCase):
    def test_mask_account_short_and_long(self):
        self.assertEqual(xc.mask_account("abc"), "ab*")
        self.assertEqual(xc.mask_account("test_user"), "tes****er")
        # 短账号不能被脱敏撑长（7 字符仍走短分支）
        self.assertEqual(len(xc.mask_account("abcdefg")), len("abcdefg"))

    def test_mask_token(self):
        token = make_jwt(int(time.time()) + 60)
        masked = xc.mask_token(token)
        self.assertNotIn(token, masked)
        self.assertTrue(masked.endswith(token[-6:]))

    def test_empty(self):
        self.assertEqual(xc.mask_account(""), "")
        self.assertEqual(xc.mask_token(""), "")


class TestJwtExp(unittest.TestCase):
    def test_parse_exp(self):
        exp = int(time.time()) + 3600
        self.assertEqual(xc.jwt_exp(make_jwt(exp)), exp)

    def test_invalid_returns_zero(self):
        self.assertEqual(xc.jwt_exp("not-a-jwt"), 0)
        self.assertEqual(xc.jwt_exp(""), 0)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self.tmp.name) / "xhcyv_token.json"
        self._orig = xc.CACHE_FILE
        xc.CACHE_FILE = self.cache_file

    def tearDown(self):
        xc.CACHE_FILE = self._orig
        self.tmp.cleanup()

    def test_save_read_drop(self):
        cache = {}
        token = make_jwt(int(time.time()) + 86400)
        xc.save_token(cache, "user1", token)
        self.assertTrue(self.cache_file.exists())
        data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(data["accounts"]["user1"]["token"], token)
        self.assertEqual(xc.read_cache()["user1"]["token"], token)

        xc.drop_token(cache, "user1")
        self.assertEqual(xc.read_cache(), {})

    def test_usable_valid_token(self):
        cache = {"u": {"token": make_jwt(int(time.time()) + 86400)}}
        self.assertTrue(xc.cached_token_usable(cache, "u"))

    def test_usable_rejects_expiring_token(self):
        cache = {"u": {"token": make_jwt(int(time.time()) + 600)}}
        self.assertEqual(xc.cached_token_usable(cache, "u"), "")

    def test_usable_missing(self):
        self.assertEqual(xc.cached_token_usable({}, "u"), "")

    def test_corrupt_entry_types_do_not_raise(self):
        self.assertEqual(xc.cached_token_usable({"u": "not-a-dict"}, "u"), "")
        self.assertEqual(xc.cached_token_usable({"u": {"token": "eyJ.x.y", "exp": "abc"}}, "u"), "eyJ.x.y")

    def test_read_corrupt_cache(self):
        self.cache_file.write_text("{not json", encoding="utf-8")
        self.assertEqual(xc.read_cache(), {})


class TestApiCall(unittest.TestCase):
    def setUp(self):
        self.session = None
        self._orig_delays = xc.NETWORK_RETRY_DELAYS
        xc.NETWORK_RETRY_DELAYS = (0, 0)

    def tearDown(self):
        xc.NETWORK_RETRY_DELAYS = self._orig_delays

    def test_success_returns_data(self):
        session = FakeSession([FakeResponse(200, {"code": 0, "data": {"signed": True}})])
        data = xc.api_call(session, "GET", "https://x/y", False, token="tk")
        self.assertEqual(data, {"signed": True})
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer tk")

    def test_business_error_raises_api_error(self):
        session = FakeSession([FakeResponse(200, {"code": 11006, "message": "already signed in today"})])
        with self.assertRaises(xc.ApiError) as ctx:
            xc.api_call(session, "POST", "https://x/y", False)
        self.assertEqual(ctx.exception.code, 11006)
        self.assertFalse(ctx.exception.is_unauthorized)

    def test_unauthorized_flag(self):
        session = FakeSession([FakeResponse(200, {"code": 10002, "message": "unauthorized"})])
        with self.assertRaises(xc.ApiError) as ctx:
            xc.api_call(session, "GET", "https://x/y", False, token="bad")
        self.assertTrue(ctx.exception.is_unauthorized)

    def test_server_error_not_retried(self):
        session = FakeSession([FakeResponse(502, None, text="bad gateway")])
        with self.assertRaises(xc.ApiError) as ctx:
            xc.api_call(session, "GET", "https://x/y", False)
        self.assertEqual(ctx.exception.http_status, 502)
        self.assertEqual(len(session.calls), 1)

    def test_non_json_raises(self):
        session = FakeSession([FakeResponse(200, None, text="<html>oops</html>")])
        with self.assertRaises(xc.ApiError):
            xc.api_call(session, "GET", "https://x/y", False)

    def test_network_retry_then_success(self):
        session = FakeSession([
            requests.ConnectionError("reset"),
            FakeResponse(200, {"code": 0, "data": {"ok": 1}}),
        ])
        data = xc.api_call(session, "GET", "https://x/y", False)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(len(session.calls), 2)

    def test_network_retry_exhausted(self):
        session = FakeSession([requests.Timeout("t1"), requests.Timeout("t2"), requests.Timeout("t3")])
        with self.assertRaises(xc.NetworkError):
            xc.api_call(session, "GET", "https://x/y", False)
        self.assertEqual(len(session.calls), 3)


class TestDoSignin(unittest.TestCase):
    def test_success_parses_fields(self):
        payload = {"points": 20, "day_order": 2354, "streak": 1, "bonus": 0, "member_bonus": 0}
        session = FakeSession([FakeResponse(200, {"code": 0, "data": payload})])
        result = xc.do_signin(session, "tk", False)
        self.assertFalse(result["already"])
        self.assertEqual(result["points"], 20)
        self.assertEqual(result["day_order"], 2354)

    def test_already_signed_maps_to_flag(self):
        session = FakeSession([FakeResponse(200, {"code": 11006, "message": "already signed in today"})])
        self.assertEqual(xc.do_signin(session, "tk", False), {"already": True})

    def test_other_business_error_propagates(self):
        session = FakeSession([FakeResponse(200, {"code": 10006, "message": "too many"})])
        with self.assertRaises(xc.ApiError) as ctx:
            xc.do_signin(session, "tk", False)
        self.assertEqual(ctx.exception.code, 10006)


class TestSessionConfig(unittest.TestCase):
    def test_trust_env_disabled_even_without_proxy(self):
        session = xc.build_session(30, "")
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {})

    def test_proxy_applied(self):
        session = xc.build_session(30, "http://127.0.0.1:7890")
        self.assertEqual(session.proxies["https"], "http://127.0.0.1:7890")

    def test_proxy_precedence(self):
        with mock.patch.dict(os.environ, {"XHCYV_PROXY": "http://a:1",
                                          "HTTPS_PROXY": "http://b:2"}, clear=True):
            self.assertEqual(xc.resolve_proxy(), "http://a:1")
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://b:2"}, clear=True):
            self.assertEqual(xc.resolve_proxy(), "http://b:2")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(xc.resolve_proxy(), "")


class TestDoLogin(unittest.TestCase):
    def test_success_reads_token_from_cookie(self):
        resp = FakeResponse(200, {"code": 0, "data": {"user": {"id": 1}, "authenticated": True}})
        resp.set_token = "eyJ.abc.def"
        session = FakeSiteSession([resp])
        result = xc.do_login(session, "test_user", "test123", False)
        self.assertEqual(result["token"], "eyJ.abc.def")
        self.assertEqual(result["user"], {"id": 1})

    def test_missing_token_raises(self):
        session = FakeSiteSession([FakeResponse(200, {"code": 0, "data": {"user": {}}})])
        with self.assertRaises(xc.ApiError) as ctx:
            xc.do_login(session, "u", "p", False)
        self.assertIn("x_bbs_token", str(ctx.exception))

    def test_captcha_required_then_success(self):
        first = FakeResponse(200, {"code": 10007, "message": "captcha required"})
        second = FakeResponse(200, {"code": 0, "data": {"user": {}}})
        second.set_token = "eyJ.abc.def"
        session = FakeSiteSession([first, second])
        with mock.patch.object(xc, "solve_captcha", return_value=("cid1", "a1b2")):
            result = xc.do_login(session, "u", "p", False)
        self.assertEqual(result["token"], "eyJ.abc.def")
        self.assertEqual(session.calls[1][2]["json"],
                         {"account": "u", "password": "p",
                          "captcha_id": "cid1", "captcha_code": "a1b2"})

    def test_captcha_twice_fails(self):
        first = FakeResponse(200, {"code": 10007, "message": "captcha required"})
        second = FakeResponse(200, {"code": 10008, "message": "captcha wrong"})
        session = FakeSiteSession([first, second])
        with mock.patch.object(xc, "solve_captcha", return_value=("cid1", "zzzz")):
            with self.assertRaises(xc.ApiError) as ctx:
                xc.do_login(session, "u", "p", False)
        self.assertIn("验证码多次识别失败", str(ctx.exception))

    def test_two_factor_required(self):
        session = FakeSiteSession([
            FakeResponse(200, {"code": 0, "data": {"two_factor_required": True, "challenge": "c1"}}),
        ])
        with self.assertRaises(xc.ApiError) as ctx:
            xc.do_login(session, "u", "p", False)
        self.assertIn("两步验证", str(ctx.exception))


class TestRunOneAccount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_cache = xc.CACHE_FILE
        xc.CACHE_FILE = Path(self.tmp.name) / "xhcyv_token.json"
        self.cache = {}

    def tearDown(self):
        xc.CACHE_FILE = self._orig_cache
        self.tmp.cleanup()

    def _run(self, entry, session, check_only=False):
        with mock.patch.object(xc, "build_session", lambda *a, **k: session),                 mock.patch.object(xc, "resolve_proxy", return_value=""):
            return xc.run_one_account(entry, self.cache, 30, False, check_only=check_only)

    def test_cached_token_already_signed(self):
        xc.save_token(self.cache, "test_user", make_jwt(int(time.time()) + 86400))
        session = FakeSiteSession([
            FakeResponse(200, {"code": 0, "data": {"signed": True,
                                                   "record": {"points": 20, "day_order": 7, "streak": 3}}}),
        ])
        result = self._run({"type": "credential", "account": "test_user",
                            "password": "test123", "label": "tes****er"}, session)
        self.assertTrue(result["success"])
        self.assertIn("今日已签到", result["message"])
        self.assertEqual(len(session.calls), 1)   # 只查状态，不重复签到

    def test_cached_token_expired_relogin_then_signin(self):
        xc.save_token(self.cache, "test_user", make_jwt(int(time.time()) + 86400))
        login_ok = FakeResponse(200, {"code": 0, "data": {"user": {"points": 0}}})
        login_ok.set_token = "eyJ.new.token"
        session = FakeSiteSession([
            FakeResponse(200, {"code": 10002, "message": "unauthorized"}),
            login_ok,
            FakeResponse(200, {"code": 0, "data": {"signed": False, "record": None, "streak": 0}}),
            FakeResponse(200, {"code": 0, "data": {"points": 20, "day_order": 9, "streak": 1,
                                                   "bonus": 0, "member_bonus": 0}}),
            FakeResponse(200, {"code": 0, "data": {"points": 20, "level_name": "Lv.1"}}),
        ])
        result = self._run({"type": "credential", "account": "test_user",
                            "password": "test123", "label": "tes****er"}, session)
        self.assertTrue(result["success"])
        self.assertIn("+20 积分", result["message"])
        self.assertEqual(self.cache["test_user"]["token"], "eyJ.new.token")

    def test_token_mode_invalid(self):
        session = FakeSiteSession([
            FakeResponse(200, {"code": 10002, "message": "unauthorized"}),
        ])
        result = self._run({"type": "token", "token": "eyJ.bad.token", "label": "凭证 [x]"}, session)
        self.assertFalse(result["success"])
        self.assertIn("XHCYV_TOKEN", result["error"])

    def test_check_only_does_not_signin(self):
        xc.save_token(self.cache, "test_user", make_jwt(int(time.time()) + 86400))
        session = FakeSiteSession([
            FakeResponse(200, {"code": 0, "data": {"signed": False, "record": None, "streak": 0}}),
        ])
        result = self._run({"type": "credential", "account": "test_user",
                            "password": "test123", "label": "tes****er"},
                           session, check_only=True)
        self.assertTrue(result["success"])
        self.assertIn("尚未签到", result["message"])
        self.assertEqual(len(session.calls), 1)


class TestFormatting(unittest.TestCase):
    def test_signin_result_full(self):
        msg = xc.fmt_signin_result({"points": 20, "day_order": 2354, "streak": 1,
                                    "bonus": 0, "member_bonus": 0})
        self.assertEqual(msg, "签到成功 +20 积分，今日第 2354 位，连签 1 天")

    def test_signin_result_already(self):
        self.assertEqual(xc.fmt_signin_result({"already": True}), "今日已签到")

    def test_signin_result_bonus_fields(self):
        msg = xc.fmt_signin_result({"points": 10, "bonus": 5, "member_bonus": 2})
        self.assertIn("连签奖励 +5", msg)
        self.assertIn("会员加成 +2", msg)

    def test_status_from_record(self):
        msg = xc.fmt_status({"signed": True, "record": {"points": 20, "day_order": 7, "streak": 3}})
        self.assertEqual(msg, "今日已签到 +20 积分，今日第 7 位，连签 3 天")

    def test_status_fallback_streak(self):
        msg = xc.fmt_status({"signed": True, "record": None, "streak": 2})
        self.assertEqual(msg, "今日已签到 连签 2 天")

    def test_points_suffix(self):
        self.assertEqual(xc.fmt_points_suffix({"points": 20, "level_name": "Lv.1"}),
                         "（当前 20 积分，Lv.1）")
        self.assertEqual(xc.fmt_points_suffix({}), "")


class TestTokenExpiryText(unittest.TestCase):
    def test_valid(self):
        exp = int(time.time()) + 86400
        self.assertTrue(xc.token_expiry_text(make_jwt(exp)).startswith("20"))

    def test_invalid(self):
        self.assertEqual(xc.token_expiry_text("x"), "")


if __name__ == "__main__":
    unittest.main()
