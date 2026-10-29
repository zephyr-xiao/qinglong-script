# -*- coding: utf-8 -*-
"""vikacg_checkin 单元测试（标准库 unittest，无第三方依赖）

覆盖：凭据解析 / 账号脱敏 / 幂等关键词判定 / JWT exp 解析 /
凭证缓存读写与可用性判定 / 签到结果解析各分支 / 代理三级回退。
真实网络请求不进单测（本地干跑负责）。
"""
import json
import os
import socket
import sys
import time
import unittest
from unittest import mock

import requests

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
        self.assertTrue(vc.cached_token_usable(
            {"token": token, "deviceCode": "d", "clientCode": "c"}))

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
            {"token": "plain-token", "update_time": fresh,
             "deviceCode": "d", "clientCode": "c"}))
        self.assertFalse(vc.cached_token_usable(
            {"token": "plain-token", "update_time": stale,
             "deviceCode": "d", "clientCode": "c"}))
        # update_time 缺失/损坏 → 不可用
        self.assertFalse(vc.cached_token_usable(
            {"token": "plain-token", "deviceCode": "d", "clientCode": "c"}))

    def test_jwt_without_exp_falls_back_to_ttl(self):
        from datetime import datetime, timedelta
        fresh = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        token = TestJwtExp._make_jwt({"uid": 1})  # 合法 JWT 但无 exp 字段
        self.assertTrue(vc.cached_token_usable(
            {"token": token, "update_time": fresh,
             "deviceCode": "d", "clientCode": "c"}))


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
    """默认直连：只认 VIKACG_PROXY，全局代理变量不再回退。"""

    def test_explicit_proxy(self):
        with mock.patch.dict(os.environ, {"VIKACG_PROXY": "http://specific:1"}, clear=False):
            self.assertEqual(vc.resolve_proxy(), ("http://specific:1", "VIKACG_PROXY"))

    def test_global_env_ignored(self):
        # 青龙给任务注入 HTTPS_PROXY 时，站点仍应直连（走代理会撞 CF 风控出口）
        env = {"VIKACG_PROXY": "", "HTTPS_PROXY": "http://global:2",
               "HTTP_PROXY": "http://global:2", "ALL_PROXY": "socks5://global:3"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(vc.resolve_proxy(), ("", "直连"))

    def test_direct_aliases(self):
        for value in ("none", "direct", "DIRECT", "off", "0", "false"):
            with mock.patch.dict(os.environ, {"VIKACG_PROXY": value}, clear=False):
                self.assertEqual(vc.resolve_proxy()[0], "", value)

    def test_empty_means_direct(self):
        with mock.patch.dict(os.environ, {"VIKACG_PROXY": "  "}, clear=False):
            self.assertEqual(vc.resolve_proxy(), ("", "直连"))


class TestEnvProxyFallback(unittest.TestCase):
    """全局代理变量只作直连失败时的备用出口。"""

    def test_reads_global_env(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://g:2"}, clear=False):
            self.assertEqual(vc.resolve_env_proxy(), ("http://g:2", "HTTPS_PROXY"))

    def test_none_when_unset(self):
        env = {k: "" for k in vc.GLOBAL_PROXY_KEYS}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(vc.resolve_env_proxy(), ("", ""))


class TestFailoverSession(unittest.TestCase):
    """直连被掐断时自动切备用代理，且只切一次。"""

    def setUp(self):
        self.primary = mock.MagicMock()
        self.fallback = mock.MagicMock()

    def test_switches_on_connection_error(self):
        self.primary.post.side_effect = requests.exceptions.ConnectionError("reset")
        ok = mock.MagicMock(status_code=200)
        self.fallback.post.return_value = ok
        session = vc.FailoverSession(self.primary, self.fallback, "直连", "代理 http://p:1")
        self.assertIs(session.post("https://example.com/"), ok)
        self.assertTrue(session.switched)

    def test_switches_on_timeout(self):
        self.primary.post.side_effect = requests.exceptions.ReadTimeout("slow")
        ok = mock.MagicMock(status_code=200)
        self.fallback.post.return_value = ok
        session = vc.FailoverSession(self.primary, self.fallback, "直连", "代理")
        self.assertIs(session.post("https://example.com/"), ok)

    def test_no_fallback_reraises(self):
        self.primary.post.side_effect = requests.exceptions.ConnectionError("reset")
        session = vc.FailoverSession(self.primary, None, "直连", "")
        with self.assertRaises(requests.exceptions.ConnectionError):
            session.post("https://example.com/")

    def test_primary_ok_no_switch(self):
        ok = mock.MagicMock(status_code=200)
        self.primary.post.return_value = ok
        session = vc.FailoverSession(self.primary, self.fallback, "直连", "代理")
        self.assertIs(session.post("https://example.com/"), ok)
        self.fallback.post.assert_not_called()
        self.assertFalse(session.switched)

    def test_switch_logged_once(self):
        self.primary.post.side_effect = requests.exceptions.ConnectionError("reset")
        self.fallback.post.return_value = mock.MagicMock(status_code=200)
        session = vc.FailoverSession(self.primary, self.fallback, "直连", "代理 http://p:1")
        with mock.patch("builtins.print") as mp:
            session.post("https://example.com/")
            session.post("https://example.com/")
        self.assertEqual(mp.call_count, 1)


class TestForceIpv4(unittest.TestCase):
    """直连时禁用 IPv6：AAAA 记录在大陆常被 reset。"""

    def test_hook_returns_af_inet(self):
        from urllib3.util import connection
        original = connection.allowed_gai_family
        try:
            vc.force_ipv4()
            self.assertEqual(connection.allowed_gai_family(), socket.AF_INET)
        finally:
            connection.allowed_gai_family = original


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


class TestCachedTokenUsableExtra(unittest.TestCase):
    """设备指纹完整性：缺任一字段的缓存判无效，走续期/重登链补齐。"""

    def test_missing_device_code(self):
        exp = int(time.time()) + 24 * 3600
        token = TestJwtExp._make_jwt({"exp": exp})
        self.assertFalse(vc.cached_token_usable({"token": token, "clientCode": "c"}))

    def test_missing_client_code(self):
        exp = int(time.time()) + 24 * 3600
        token = TestJwtExp._make_jwt({"exp": exp})
        self.assertFalse(vc.cached_token_usable({"token": token, "deviceCode": "d"}))

    def test_complete_entry_ok(self):
        exp = int(time.time()) + 24 * 3600
        token = TestJwtExp._make_jwt({"exp": exp})
        self.assertTrue(vc.cached_token_usable(
            {"token": token, "deviceCode": "d", "clientCode": "c"}))


class TestWriteTokenCacheAtomic(unittest.TestCase):
    """原子写：落盘后无 .tmp 残留；写坏旧缓存也能自愈重建。"""

    def setUp(self):
        self._orig_file = vc.TOKEN_CACHE_FILE
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._tmp_path = path
        vc.TOKEN_CACHE_FILE = vc.Path(path)

    def tearDown(self):
        vc.TOKEN_CACHE_FILE = self._orig_file
        for p in (self._tmp_path, self._tmp_path + ".tmp"):
            if os.path.exists(p):
                os.remove(p)

    def test_no_tmp_leftover(self):
        vc.write_token_cache("a@x.com", {"token": "t", "refreshToken": "r",
                                         "deviceCode": "d", "clientCode": "c"})
        self.assertFalse(os.path.exists(self._tmp_path + ".tmp"))
        self.assertTrue(os.path.exists(self._tmp_path))

    def test_corrupted_old_cache_rebuilt(self):
        with open(self._tmp_path, "w", encoding="utf-8") as f:
            f.write("{corrupted json!!")
        vc.write_token_cache("a@x.com", {"token": "t", "refreshToken": "r",
                                         "deviceCode": "d", "clientCode": "c"})
        cache = vc.read_token_cache()
        self.assertEqual(cache["a@x.com"]["token"], "t")


class TestNetworkErrorHelpers(unittest.TestCase):
    """网络异常分类与重试耗尽异常。"""

    def test_describe_hierarchy_order(self):
        # ConnectTimeout/ProxyError/SSLError 都是 ConnectionError 子类，必须先特判
        self.assertEqual(vc.describe_network_error(
            requests.exceptions.ConnectTimeout("x")), "连接超时")
        self.assertEqual(vc.describe_network_error(
            requests.exceptions.ReadTimeout("x")), "响应超时")
        self.assertIn("代理", vc.describe_network_error(
            requests.exceptions.ProxyError("x")))
        self.assertIn("TLS", vc.describe_network_error(
            requests.exceptions.SSLError("x")))
        self.assertIn("VIKACG_PROXY", vc.describe_network_error(
            requests.exceptions.ConnectionError("x")))

    def test_describe_fallback(self):
        self.assertEqual(vc.describe_network_error(ValueError("boom")), "boom")
        self.assertEqual(vc.describe_network_error(ValueError()), "ValueError")

    def test_is_runtime_error(self):
        self.assertTrue(issubclass(vc.NetworkError, RuntimeError))


def _html_resp(status=403,
               body="<html><head><title>Just a moment...</title></head></html>",
               headers=None):
    """构造 CF 挑战页 / 网关错误页这类非 JSON 响应。"""
    resp = mock.MagicMock()
    resp.status_code = status
    resp.headers = ({"Content-Type": "text/html; charset=UTF-8",
                     "Cf-Mitigated": "challenge"} if headers is None else headers)
    resp.text = body
    resp.json.side_effect = ValueError("not json")
    return resp


def _json_resp(status, payload):
    """构造业务 JSON 响应。"""
    resp = mock.MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": "application/json"}
    resp.text = json.dumps(payload, ensure_ascii=False)
    resp.json.return_value = payload
    return resp


class TestApiPostRetry(unittest.TestCase):
    """api_post 网络重试：连接类异常重试 2 次，业务响应不重试。"""

    def setUp(self):
        self.session = mock.MagicMock()

    def test_retry_then_success(self):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "success"}
        self.session.post.side_effect = [
            requests.exceptions.ConnectionError("reset"),
            requests.exceptions.Timeout("t"), resp]
        with mock.patch.object(vc.time, "sleep") as ms:
            r = vc.api_post(self.session, "/userMission", {}, "d", "c", token="t")
        self.assertEqual(r["http_code"], 200)
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(ms.call_count, 2)  # 两次失败各 sleep 一次（3s、6s）

    def test_exhausted_raises_network_error(self):
        self.session.post.side_effect = requests.exceptions.ConnectionError("reset")
        with mock.patch.object(vc.time, "sleep"):
            with self.assertRaises(vc.NetworkError) as cm:
                vc.api_post(self.session, "/userMission", {}, "d", "c", token="t")
        self.assertIn("VIKACG_PROXY", str(cm.exception))
        self.assertEqual(self.session.post.call_count, 3)  # 首次 + 2 次重试

    def test_business_response_not_retried(self):
        resp = mock.MagicMock()
        resp.status_code = 500
        resp.json.return_value = {"status": "fail", "message": "server error"}
        self.session.post.return_value = resp
        r = vc.api_post(self.session, "/userMission", {}, "d", "c", token="t")
        self.assertEqual(r["http_code"], 500)
        self.assertEqual(self.session.post.call_count, 1)


class TestTransientIntercept(unittest.TestCase):
    """CF 人机校验页识别：专属响应头最准，JSON 业务响应一律排除，正文特征兜底。"""

    def test_header_challenge(self):
        self.assertTrue(vc.is_transient_intercept(_html_resp()))

    def test_json_business_response_excluded(self):
        # 站点自己的 400/403 JSON（如「非法的客户端」）不是拦截页，不能重试
        self.assertFalse(vc.is_transient_intercept(
            _json_resp(400, {"status": "fail", "message": "非法的客户端"})))

    def test_html_body_without_header(self):
        # 部分前置网关不带 Cf-Mitigated 头，用正文特征兜底
        self.assertTrue(vc.is_transient_intercept(
            _html_resp(403, headers={"Content-Type": "text/html"})))

    def test_gateway_page_detected(self):
        self.assertTrue(vc.is_transient_intercept(
            _html_resp(502, body="<html><body>Bad Gateway</body></html>",
                       headers={"Content-Type": "text/html"})))

    def test_normal_html_not_intercept(self):
        self.assertFalse(vc.is_transient_intercept(
            _html_resp(200, body="<html><body>hello</body></html>",
                       headers={"Content-Type": "text/html"})))


class TestApiPostChallengeRetry(unittest.TestCase):
    """api_post 对 CF 挑战页也重试：换一条 TCP 连接可能落到没被风控的出口。"""

    def setUp(self):
        self.session = mock.MagicMock()

    def test_challenge_then_success(self):
        self.session.post.side_effect = [
            _html_resp(), _json_resp(401, {"status": "fail", "message": "未登录"})]
        with mock.patch.object(vc.time, "sleep") as ms:
            r = vc.api_post(self.session, "/userMission", {}, "d", "c", token="t")
        self.assertEqual(r["http_code"], 401)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(ms.call_count, 1)

    def test_challenge_exhausted_reports_actionable_hint(self):
        self.session.post.return_value = _html_resp()
        with mock.patch.object(vc.time, "sleep") as ms:
            r = vc.api_post(self.session, "/userMission", {}, "d", "c", token="t")
        self.assertEqual(r["http_code"], 403)
        self.assertIn("Cloudflare 人机校验", r["json"]["message"])
        self.assertIn("VIKACG_PROXY", r["json"]["message"])
        self.assertEqual(self.session.post.call_count, 3)  # 首次 + 2 次重试
        self.assertEqual(ms.call_count, 2)

    def test_business_json_403_not_retried(self):
        self.session.post.return_value = _json_resp(
            403, {"status": "fail", "message": "非法的客户端，请下载官方版本"})
        r = vc.api_post(self.session, "/userMission", {}, "d", "c", token="t")
        self.assertEqual(r["http_code"], 403)
        self.assertEqual(self.session.post.call_count, 1)


class TestErrorTextCleaning(unittest.TestCase):
    """非 JSON 响应清洗与拦截页检测。"""

    def test_clean_strips_tags_and_whitespace(self):
        raw = "<html>  <body>   Access\n denied\t </body></html>"
        self.assertEqual(vc.clean_error_text(raw), "Access denied")

    def test_clean_truncates(self):
        self.assertEqual(len(vc.clean_error_text("a" * 500)), 201)  # 200 + 省略号
        self.assertTrue(vc.clean_error_text("a" * 500).endswith("…"))

    def test_clean_empty(self):
        self.assertEqual(vc.clean_error_text(""), "")
        self.assertEqual(vc.clean_error_text(None), "")

    def test_intercept_page_detected(self):
        self.assertTrue(vc.looks_like_intercept_page(
            "<html><title>Just a moment...</title></html>"))
        self.assertTrue(vc.looks_like_intercept_page("502 Bad Gateway"))
        self.assertTrue(vc.looks_like_intercept_page(
            "Attention Required! | Cloudflare"))

    def test_normal_payload_not_flagged(self):
        self.assertFalse(vc.looks_like_intercept_page('{"status":"success"}'))
        self.assertFalse(vc.looks_like_intercept_page("user today signed"))
        self.assertFalse(vc.looks_like_intercept_page(""))

    def test_api_post_non_json_cleaned(self):
        """非 JSON 响应进 message 前必须清洗，不能把 HTML 原文带进推送。"""
        session = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 403
        resp.headers = {"Content-Type": "text/html; charset=UTF-8"}
        resp.text = "<html><title>Just a moment...</title>\n  <p>checking</p></html>"
        resp.json.side_effect = ValueError("no json")
        session.post.return_value = resp
        with mock.patch.object(vc.time, "sleep"):
            r = vc.api_post(session, "/userMission", {}, "d", "c", token="t")
        msg = r["json"]["message"]
        self.assertNotIn("<", msg)
        self.assertIn("疑似 CF/网关拦截页", msg)
        self.assertIn("checking", msg)


class TestRunOneTaskHardened(unittest.TestCase):
    """加固后的判定分支：幂等双门槛 + 网络异常收口。"""

    def _make_auth(self):
        return {"token": "tok", "refreshToken": "r", "deviceCode": "d",
                "clientCode": "c", "nickname": "tester", "from": "cache"}

    @staticmethod
    def _api_result(code, payload):
        return {"http_code": code, "json": payload}

    def test_success_message_with_signed_keyword(self):
        # 200+success 且文案含「已签」→ 按成功解析并如实输出「今日已签到」，不丢余额
        auth = self._make_auth()
        resp = self._api_result(200, {"status": "success",
                                      "message": "今日已签到，明天再来",
                                      "data": {"count": 591, "sign_days": 3}})
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", return_value=resp):
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertTrue(result["success"])
        self.assertIn("今日已签到", result["message"])
        self.assertIn("积分余额 591", result["message"])

    def test_fail_with_signed_keyword_is_not_idempotent(self):
        # 非 409 的失败响应即使文案含「已签」也不冒领已签（双门槛）
        auth = self._make_auth()
        resp = self._api_result(400, {"status": "fail", "message": "您已签到过活动"})
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", return_value=resp):
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertFalse(result["success"])

    def test_409_without_keyword_not_idempotent(self):
        # 409 但文案不含已签关键词 → 不算已签，按失败报原文
        auth = self._make_auth()
        resp = self._api_result(409, {"status": "fail", "message": "冲突"})
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", return_value=resp):
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertFalse(result["success"])
        self.assertIn("409", result["error"])

    def test_network_error_returns_classified_failure(self):
        auth = self._make_auth()
        err = vc.NetworkError("网络请求失败：连接失败（请配置 VIKACG_PROXY）")
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", side_effect=err):
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertFalse(result["success"])
        self.assertIn("VIKACG_PROXY", result["error"])

    def test_401_refresh_network_error_stops(self):
        # 401 后续期遇网络耗尽 → 直接失败收口，不再重登白跑
        auth = self._make_auth()
        resp_401 = self._api_result(401, {"status": "fail", "message": "未登录"})
        err = vc.NetworkError("网络请求失败：连接超时")
        with mock.patch.object(vc, "ensure_token", return_value=auth), \
             mock.patch.object(vc, "api_post", return_value=resp_401), \
             mock.patch.object(vc, "do_refresh", side_effect=err), \
             mock.patch.object(vc, "do_login") as ml:
            result = vc.run_one_task(mock.MagicMock(), "a@x.com", "pw", 30, False)
        self.assertFalse(result["success"])
        self.assertIn("连接超时", result["error"])
        ml.assert_not_called()


class TestEnsureTokenNetworkFastFail(unittest.TestCase):
    """续期遇网络耗尽 → 快速失败，不再走重登白跑一轮重试。"""

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

    def test_refresh_network_error_skips_relogin(self):
        token = TestJwtExp._make_jwt({"exp": int(time.time()) + 3600})
        vc.write_token_cache("a@x.com", {"token": token, "refreshToken": "r",
                                         "deviceCode": "d", "clientCode": "c"})
        with mock.patch.object(vc, "do_refresh",
                               side_effect=vc.NetworkError("网络请求失败：连接超时")), \
             mock.patch.object(vc, "do_login") as ml:
            auth = vc.ensure_token(mock.MagicMock(), "a@x.com", "pw", 30)
        self.assertIn("error", auth)
        self.assertIn("连接超时", auth["error"])
        ml.assert_not_called()


if __name__ == "__main__":
    unittest.main()
