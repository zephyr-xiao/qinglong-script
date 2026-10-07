# -*- coding: utf-8 -*-
"""vikacg_checkin 单元测试（标准库 unittest，无第三方依赖）

覆盖：凭据解析 / 账号脱敏 / 幂等关键词判定 / JWT exp 解析 /
凭证缓存读写与可用性判定 / 签到结果解析各分支 / 代理三级回退。
真实网络请求不进单测（本地干跑负责）。
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vikacg_checkin as vc  # noqa: E402


class TestParseCredentials(unittest.TestCase):
    def test_single_account(self):
        self.assertEqual(vc.parse_credentials("a@x.com#pw"), [("a@x.com", "pw")])

    def test_multi_by_amp(self):
        self.assertEqual(
            vc.parse_credentials("a@x.com#p1&b@y.com#p2"),
            [("a@x.com", "p1"), ("b@y.com", "p2")])

    def test_multi_by_newline(self):
        self.assertEqual(
            vc.parse_credentials("a@x.com#p1\nb@y.com#p2"),
            [("a@x.com", "p1"), ("b@y.com", "p2")])

    def test_password_contains_hash(self):
        # 密码含 # 按首个 # 切分
        self.assertEqual(
            vc.parse_credentials("a@x.com#p#1"),
            [("a@x.com", "p#1")])

    def test_empty_and_invalid_skipped(self):
        self.assertEqual(vc.parse_credentials("  &nohash\n"), [])
        self.assertEqual(vc.parse_credentials(""), [])


class TestMask(unittest.TestCase):
    def test_email(self):
        self.assertEqual(vc.mask("testuser1234@gmail.com"), "te**********@gmail.com")

    def test_short_head(self):
        self.assertEqual(vc.mask("a@x.com"), "a*@x.com")

    def test_non_email(self):
        self.assertEqual(vc.mask("abcdef"), "ab**ef")
        self.assertEqual(vc.mask("ab"), "a*")

    def test_short_non_email_no_overlap(self):
        # 3~4 字符串头尾切片会重叠（len=4 时保留首尾二等于全裸），应只保留首字符
        self.assertEqual(vc.mask("abc"), "a**")
        self.assertEqual(vc.mask("abcd"), "a***")


class TestAlreadySignedIn(unittest.TestCase):
    def test_already(self):
        self.assertTrue(vc._is_already_signed_in("用户今天已签到"))
        self.assertTrue(vc._is_already_signed_in("You already signed in today"))

    def test_first_success_not_idempotent(self):
        # "签到成功"是首次成功标志，绝不能命中幂等关键词
        self.assertFalse(vc._is_already_signed_in("签到成功"))
        self.assertFalse(vc._is_already_signed_in("钱包任务操作成功"))

    def test_english_success_not_idempotent(self):
        # 裸 "signed in" 已收紧为组合词，英文成功文案不应误判
        self.assertFalse(vc._is_already_signed_in("signed in successfully"))

    def test_empty(self):
        self.assertFalse(vc._is_already_signed_in(""))
        self.assertFalse(vc._is_already_signed_in(None))


class TestJwtExp(unittest.TestCase):
    @staticmethod
    def _make_jwt(payload: dict) -> str:
        import base64
        def b64(obj):
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{b64({'alg':'HS256'})}.{b64(payload)}.sig"

    def test_valid_exp(self):
        token = self._make_jwt({"exp": 1793946155})
        self.assertEqual(vc.jwt_exp(token), 1793946155)

    def test_invalid_token_returns_zero(self):
        self.assertEqual(vc.jwt_exp("not-a-jwt"), 0)
        self.assertEqual(vc.jwt_exp(""), 0)
        self.assertEqual(vc.jwt_exp("a.b.c"), 0)


class TestTokenCache(unittest.TestCase):
    def setUp(self):
        self._orig_file = vc.TOKEN_CACHE_FILE
        # 临时缓存文件，避免污染真实缓存
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._tmp_path = path
        vc.TOKEN_CACHE_FILE = vc.Path(path)

    def tearDown(self):
        vc.TOKEN_CACHE_FILE = self._orig_file
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)

    def test_roundtrip_and_no_password(self):
        vc.write_token_cache("a@x.com", {"token": "t1", "refreshToken": "r1",
                                         "deviceCode": "d1", "clientCode": "c1"})
        cache = vc.read_token_cache()
        self.assertEqual(cache["a@x.com"]["token"], "t1")
        self.assertIn("update_time", cache["a@x.com"])
        # 缓存文件里绝不出现密码字段
        raw = vc.TOKEN_CACHE_FILE.read_text(encoding="utf-8")
        self.assertNotIn("password", raw.lower())

    def test_update_keeps_other_accounts(self):
        vc.write_token_cache("a@x.com", {"token": "t1", "refreshToken": "r1",
                                         "deviceCode": "d1", "clientCode": "c1"})
        vc.write_token_cache("b@x.com", {"token": "t2", "refreshToken": "r2",
                                         "deviceCode": "d2", "clientCode": "c2"})
        vc.write_token_cache("a@x.com", {"token": "t3", "refreshToken": "r1",
                                         "deviceCode": "d1", "clientCode": "c1"})
        cache = vc.read_token_cache()
        self.assertEqual(cache["a@x.com"]["token"], "t3")
        self.assertEqual(cache["b@x.com"]["token"], "t2")

    def test_usable_by_jwt_exp(self):
        exp = int(time.time()) + 24 * 3600  # 24h 后过期，> 12h 余量
        token = TestJwtExp._make_jwt({"exp": exp})
        self.assertTrue(vc.cached_token_usable({"token": token}))

    def test_expired_by_jwt_exp(self):
        exp = int(time.time()) + 1 * 3600  # 1h 后过期，< 12h 余量 → 视为不可用
        token = TestJwtExp._make_jwt({"exp": exp})
        self.assertFalse(vc.cached_token_usable({"token": token}))

    def test_no_token(self):
        self.assertFalse(vc.cached_token_usable({}))

    def test_ttl_fallback_without_jwt_exp(self):
        # token 解不出 exp（非标准 JWT）→ 按 update_time 14 天 TTL 兜底
        from datetime import datetime, timedelta
        fresh = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        stale = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(vc.cached_token_usable(
            {"token": "plain-token", "update_time": fresh}))
        self.assertFalse(vc.cached_token_usable(
            {"token": "plain-token", "update_time": stale}))
        # update_time 缺失/损坏 → 不可用
        self.assertFalse(vc.cached_token_usable({"token": "plain-token"}))

    def test_jwt_without_exp_falls_back_to_ttl(self):
        from datetime import datetime, timedelta
        fresh = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        token = TestJwtExp._make_jwt({"uid": 1})  # 合法 JWT 但无 exp 字段
        self.assertTrue(vc.cached_token_usable({"token": token, "update_time": fresh}))


class TestRunOneTask(unittest.TestCase):
    """签到结果解析各分支（mock 掉 HTTP 层）"""

    def _make_auth(self):
        return {"token": "tok", "refreshToken": "r", "deviceCode": "d",
                "clientCode": "c", "nickname": "tester", "from": "cache"}

    @staticmethod
    def _api_result(code, payload):
        return {"http_code": code, "json": payload}

    def test_sign_success(self):
        auth = self._make_auth()
        resp = self._api_result(200, {"status": "success", "message": "钱包任务操作成功",
                                      "data": {"count": 591, "sign_days": 1,
                                               "sign_count": 191, "sign_time": 1791354177363}})
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", return_value=resp):
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertTrue(result["success"])
        self.assertIn("签到成功（tester）", result["message"])
        self.assertIn("连续签到 1 天", result["message"])
        self.assertIn("积分余额 591", result["message"])

    def test_already_signed_409(self):
        auth = self._make_auth()
        resp = self._api_result(409, {"status": "fail", "code": 409,
                                      "message": "用户今天已签到", "data": {}})
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", return_value=resp):
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertTrue(result["success"])
        self.assertIn("已签到", result["message"])

    def test_unauthorized_then_relogin_success(self):
        auth = self._make_auth()
        new_auth = dict(auth, token="tok2")
        resp_401 = self._api_result(401, {"status": "fail", "message": "未登录"})
        resp_ok = self._api_result(200, {"status": "success", "data": {"count": 100}})
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", side_effect=[resp_401, resp_ok]), \
             mock.patch.object(vc, "do_refresh", return_value=None), \
             mock.patch.object(vc, "do_login", return_value=dict(new_auth, from_="login")) as ml, \
             mock.patch.object(vc, "write_token_cache") as mw:
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertTrue(result["success"])
        ml.assert_called_once()       # 重登只做一次
        mw.assert_called_once()       # 重登后回写缓存

    def test_unauthorized_refresh_path(self):
        auth = self._make_auth()
        refreshed = dict(auth, token="tok-refreshed")
        resp_401 = self._api_result(401, {"status": "fail", "message": "未登录"})
        resp_ok = self._api_result(200, {"status": "success", "data": {"count": 100}})
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", side_effect=[resp_401, resp_ok]), \
             mock.patch.object(vc, "do_refresh", return_value=refreshed) as dr, \
             mock.patch.object(vc, "do_login") as ml, \
             mock.patch.object(vc, "write_token_cache"):
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertTrue(result["success"])
        dr.assert_called_once()
        ml.assert_not_called()        # 续期成功就不该走重登

    def test_other_error(self):
        auth = self._make_auth()
        resp = self._api_result(400, {"status": "fail", "message": "非法的客户端，请下载官方版本"})
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", return_value=resp):
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertFalse(result["success"])
        self.assertIn("非法的客户端", result["error"])


class TestResolveProxy(unittest.TestCase):
    def test_specific_first(self):
        with mock.patch.dict(os.environ, {"VIKACG_PROXY": "http://specific:1",
                                          "HTTPS_PROXY": "http://global:2"}, clear=False):
            proxy, source = vc.resolve_proxy("VIKACG_PROXY")
        self.assertEqual((proxy, source), ("http://specific:1", "VIKACG_PROXY"))

    def test_global_fallback(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://global:2",
                                          "VIKACG_PROXY": ""}, clear=False):
            proxy, source = vc.resolve_proxy("VIKACG_PROXY")
        self.assertEqual((proxy, source), ("http://global:2", "HTTPS_PROXY"))

    def test_direct_when_none(self):
        env = {k: "" for k in vc.GLOBAL_PROXY_KEYS}
        env["VIKACG_PROXY"] = ""
        with mock.patch.dict(os.environ, env, clear=False):
            proxy, source = vc.resolve_proxy("VIKACG_PROXY")
        self.assertEqual((proxy, source), ("", ""))


class TestEnsureToken(unittest.TestCase):
    def setUp(self):
        self._orig_file = vc.TOKEN_CACHE_FILE
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._tmp_path = path
        vc.TOKEN_CACHE_FILE = vc.Path(path)

    def tearDown(self):
        vc.TOKEN_CACHE_FILE = self._orig_file
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)

    def test_cache_hit_no_login(self):
        exp = int(time.time()) + 24 * 3600
        token = TestJwtExp._make_jwt({"exp": exp})
        vc.write_token_cache("a@x.com", {"token": token, "refreshToken": "r",
                                         "deviceCode": "d", "clientCode": "c"})
        with mock.patch.object(vc, "do_login") as ml:
            auth = vc.ensure_token(mock.MagicMock(), "a@x.com", "pw", 30)
        self.assertEqual(auth["from"], "cache")
        ml.assert_not_called()

    def test_expired_refresh_then_no_relogin(self):
        token = TestJwtExp._make_jwt({"exp": int(time.time()) + 3600})
        vc.write_token_cache("a@x.com", {"token": token, "refreshToken": "r",
                                         "deviceCode": "d", "clientCode": "c"})
        refreshed = {"token": "t2", "refreshToken": "r2", "deviceCode": "d",
                     "clientCode": "c", "nickname": ""}
        with mock.patch.object(vc, "do_refresh", return_value=refreshed) as dr, \
             mock.patch.object(vc, "do_login") as ml, \
             mock.patch.object(vc, "write_token_cache"):
            auth = vc.ensure_token(mock.MagicMock(), "a@x.com", "pw", 30)
        self.assertEqual(auth["from"], "refresh")
        dr.assert_called_once()
        ml.assert_not_called()

    def test_no_cache_full_login(self):
        with mock.patch.object(vc, "do_login", return_value={
                "token": "t", "refreshToken": "r", "deviceCode": "d",
                "clientCode": "c", "nickname": "n"}) as ml, \
             mock.patch.object(vc, "write_token_cache") as mw:
            auth = vc.ensure_token(mock.MagicMock(), "a@x.com", "pw", 30)
        self.assertEqual(auth["from"], "login")
        ml.assert_called_once()
        mw.assert_called_once()

    def test_expired_without_refresh_goes_login(self):
        # 缓存过期且无 refreshToken → 跳过续期直接重登
        token = TestJwtExp._make_jwt({"exp": int(time.time()) + 3600})
        vc.write_token_cache("a@x.com", {"token": token, "refreshToken": "",
                                         "deviceCode": "d", "clientCode": "c"})
        with mock.patch.object(vc, "do_refresh") as dr, \
             mock.patch.object(vc, "do_login", return_value={
                 "token": "t2", "refreshToken": "r2", "deviceCode": "d2",
                 "clientCode": "c2", "nickname": ""}) as ml, \
             mock.patch.object(vc, "write_token_cache"):
            auth = vc.ensure_token(mock.MagicMock(), "a@x.com", "pw", 30)
        self.assertEqual(auth["from"], "login")
        dr.assert_not_called()
        ml.assert_called_once()

    def test_login_error_passthrough(self):
        with mock.patch.object(vc, "do_login", return_value={"error": "登录失败"}), \
             mock.patch.object(vc, "write_token_cache"):
            auth = vc.ensure_token(mock.MagicMock(), "a@x.com", "pw", 30)
        self.assertIn("error", auth)


if __name__ == "__main__":
    unittest.main()
