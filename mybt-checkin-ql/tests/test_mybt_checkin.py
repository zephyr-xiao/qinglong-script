# -*- coding: utf-8 -*-
"""mybt_checkin 单元测试（纯函数 + mock 判定分支，不发真实请求）。

重点覆盖本次加固的两处 P0：
  1. 幂等/成功判定分层：401/403 与业务层"未登录"绝不被文案关键字吞掉；
  2. HTTP 200 但业务失败（业务码非零 / success:false）必须判失败，形态未知判「未确认」。
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import mybt_checkin as msc
except SystemExit:
    # requests 缺失时打桩后重新加载（青龙外环境兜底）
    stub = types.ModuleType("requests")
    stub.Session = object
    stub.RequestException = Exception
    sys.modules["requests"] = stub
    import mybt_checkin as msc


class TestJudge(unittest.TestCase):
    """judge() 分层判定"""

    def test_network_error_is_fail(self):
        v, _ = msc.judge("签到", 0, None, "network error: Timeout")
        self.assertEqual(v, msc.V_FAIL)

    def test_401_is_auth_even_with_already_keyword(self):
        # 回归 P0-2：body 里含 "signed" 也不能把 401 吞成成功
        v, _ = msc.judge("登录态检查", 401, {"error": "already signed"}, "{}")
        self.assertEqual(v, msc.V_AUTH)

    def test_403_is_auth(self):
        v, _ = msc.judge("签到", 403, {"error": "forbidden"}, "{}")
        self.assertEqual(v, msc.V_AUTH)

    def test_business_auth_phrase_is_auth(self):
        v, _ = msc.judge("签到", 200, {"msg": "请先登录"}, "{}")
        self.assertEqual(v, msc.V_AUTH)

    def test_business_auth_phrase_not_signed_in(self):
        v, _ = msc.judge("签到", 200, {"message": "not signed in"}, "{}")
        self.assertEqual(v, msc.V_AUTH)

    def test_200_biz_code_nonzero_is_fail(self):
        # 回归 P0-1：HTTP 200 但业务码非零必须判失败
        v, msg = msc.judge("签到", 200, {"code": 500, "msg": "签到失败"}, "{}")
        self.assertEqual(v, msc.V_FAIL)
        self.assertIn("500", msg)

    def test_200_success_false_is_fail(self):
        v, _ = msc.judge("签到", 200, {"success": False, "msg": "no"}, "{}")
        self.assertEqual(v, msc.V_FAIL)

    def test_200_fail_text_is_fail(self):
        # 无业务码但文案明确失败 → 判失败（不是「未确认」）
        v, _ = msc.judge("签到", 200, {"msg": "签到失败"}, "{}")
        self.assertEqual(v, msc.V_FAIL)

    def test_200_success_text_is_ok(self):
        v, _ = msc.judge("签到", 200, {"message": "签到成功"}, "{}")
        self.assertEqual(v, msc.V_OK)

    def test_200_success_true_is_ok(self):
        v, _ = msc.judge("访问任务", 200, {"success": True}, "{}")
        self.assertEqual(v, msc.V_OK)

    def test_200_code_zero_is_ok(self):
        v, _ = msc.judge("访问任务", 200, {"code": 0, "msg": "ok"}, "{}")
        self.assertEqual(v, msc.V_OK)

    def test_200_status_error_string_is_fail(self):
        v, _ = msc.judge("签到", 200, {"status": "error"}, "{}")
        self.assertEqual(v, msc.V_FAIL)

    def test_200_added_is_ok(self):
        v, msg = msc.judge("签到", 200, {"added": 5}, "{}")
        self.assertEqual(v, msc.V_OK)
        self.assertIn("5", msg)

    def test_200_me_user_is_ok(self):
        v, _ = msc.judge("登录态检查", 200, {"user": {"username": "u", "points": 1}}, "{}")
        self.assertEqual(v, msc.V_OK)

    def test_200_me_sign_secret_is_ok(self):
        v, _ = msc.judge("登录态检查", 200, {"sign_secret": "abc"}, "{}")
        self.assertEqual(v, msc.V_OK)

    def test_200_already_phrase_is_already(self):
        v, _ = msc.judge("签到", 200, {"msg": "今日已签到"}, "{}")
        self.assertEqual(v, msc.V_ALREADY)

    def test_200_unknown_dict_is_unconfirmed(self):
        v, msg = msc.judge("访问任务", 200, {"foo": "bar"}, "{}")
        self.assertEqual(v, msc.V_UNCONFIRMED)
        self.assertIn("未确认", msg)

    def test_200_empty_dict_is_unconfirmed(self):
        v, _ = msc.judge("访问任务", 200, {}, "{}")
        self.assertEqual(v, msc.V_UNCONFIRMED)

    def test_200_plain_text_unknown_is_unconfirmed(self):
        v, _ = msc.judge("访问任务", 200, "whatever", "whatever")
        self.assertEqual(v, msc.V_UNCONFIRMED)

    def test_200_plain_text_success_is_ok(self):
        v, _ = msc.judge("签到", 200, "签到成功", "签到成功")
        self.assertEqual(v, msc.V_OK)

    def test_500_with_error_is_fail(self):
        v, _ = msc.judge("签到", 500, {"error": "boom"}, "{}")
        self.assertEqual(v, msc.V_FAIL)

    def test_400_already_phrase_is_already(self):
        v, _ = msc.judge("签到", 400, {"error": "今日已签到"}, "{}")
        self.assertEqual(v, msc.V_ALREADY)


class TestPredicates(unittest.TestCase):
    def test_already_phrases_precise(self):
        self.assertTrue(msc._is_already_done("今日已签到"))
        self.assertTrue(msc._is_already_done("already signed"))
        self.assertTrue(msc._is_already_done("任务已完成"))

    def test_already_phrases_not_bare(self):
        # 收紧后：裸词不再命中
        self.assertFalse(msc._is_already_done("signed"))
        self.assertFalse(msc._is_already_done("unsigned"))
        self.assertFalse(msc._is_already_done("not signed in"))
        self.assertFalse(msc._is_already_done("already"))
        self.assertFalse(msc._is_already_done(""))

    def test_auth_error_phrases(self):
        self.assertTrue(msc._is_auth_error("token expired"))
        self.assertTrue(msc._is_auth_error("请先登录"))
        self.assertFalse(msc._is_auth_error("签到成功"))

    def test_biz_code_failure(self):
        self.assertEqual(msc._biz_code_failure({"code": 1}), 1)
        self.assertIsNone(msc._biz_code_failure({"code": 0}))
        self.assertIsNone(msc._biz_code_failure({"code": 200}))
        self.assertIsNone(msc._biz_code_failure({"status": "ok"}))
        self.assertEqual(msc._biz_code_failure({"status": "error"}), "error")
        self.assertIsNone(msc._biz_code_failure({}))
        # bool 不能当成业务码
        self.assertIsNone(msc._biz_code_failure({"code": True}))


class TestSign(unittest.TestCase):
    def test_sort_query(self):
        self.assertEqual(msc.sort_query("b=2&a=1"), "a=1&b=2")
        self.assertEqual(msc.sort_query(""), "")
        # 重复 key 保留首次位置、取末次值
        self.assertEqual(msc.sort_query("a=1&a=2"), "a=2")

    def test_build_sign_headers_format(self):
        with mock.patch.object(msc.time, "time", return_value=1000):
            headers = msc.build_sign_headers("POST", "/api/x?b=2&a=1", "{}", "s")
        self.assertEqual(headers["X-Timestamp"], "1000")
        body_hash = hashlib.sha256(b"{}").hexdigest()
        canonical = f"POST\n/api/x\na=1&b=2\n{body_hash}\n1000"
        expected = hmac.new(b"s", canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Sign"], expected)

    def test_build_sign_headers_empty_secret_bootstrap(self):
        # 空 secret 用于首次 me() 引导，不应抛异常
        with mock.patch.object(msc.time, "time", return_value=1):
            headers = msc.build_sign_headers("GET", "/api/auth/me", "", "")
        self.assertIn("X-Sign", headers)


class TestCredentials(unittest.TestCase):
    def test_single(self):
        self.assertEqual(msc.parse_credentials("a#b"), [("a", "b")])

    def test_multi_ampersand_and_newline(self):
        self.assertEqual(msc.parse_credentials("a#b&c#d"), [("a", "b"), ("c", "d")])
        self.assertEqual(msc.parse_credentials("a#b\nc#d"), [("a", "b"), ("c", "d")])

    def test_password_with_hash(self):
        # '#' 只切第一次，密码可含 '#'
        self.assertEqual(msc.parse_credentials("a#p#ss"), [("a", "p#ss")])

    def test_password_with_ampersand_is_truncated(self):
        # 已知限制：密码含 '&' 会被截断（README 已标注）
        self.assertEqual(msc.parse_credentials("a#p&ss"), [("a", "p")])

    def test_invalid_entries_skipped(self):
        self.assertEqual(msc.parse_credentials("no-sep"), [])
        self.assertEqual(msc.parse_credentials(""), [])
        self.assertEqual(msc.parse_credentials("#onlypwd"), [])


class TestTokenCache(unittest.TestCase):
    def test_token_reusable_by_exp(self):
        now = 1_000_000
        self.assertTrue(msc.token_reusable(
            {"token": "t", "exp": now + msc.TOKEN_MARGIN_SECONDS + 60}, now=now))
        self.assertFalse(msc.token_reusable(
            {"token": "t", "exp": now + msc.TOKEN_MARGIN_SECONDS - 60}, now=now))

    def test_token_reusable_fallback_saved_at(self):
        from datetime import datetime, timedelta
        now = 1_000_000
        recent = (datetime.fromtimestamp(now) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        old = (datetime.fromtimestamp(now) - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(msc.token_reusable({"token": "t", "saved_at": recent}, now=now))
        self.assertFalse(msc.token_reusable({"token": "t", "saved_at": old}, now=now))

    def test_token_reusable_no_token(self):
        self.assertFalse(msc.token_reusable({}))
        self.assertFalse(msc.token_reusable({"token": ""}))

    def test_save_and_load_roundtrip_no_password(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "mybt_token.json"
            with mock.patch.object(msc, "TOKEN_CACHE_FILE", cache):
                msc.save_token_cache("user1", {
                    "token": "tok", "refresh_token": "ref", "expires_in": 3600})
                loaded = msc._load_token_cache()
            entry = loaded["user1"]
            self.assertEqual(entry["token"], "tok")
            self.assertEqual(entry["refresh_token"], "ref")
            self.assertNotIn("password", entry)
            # 原子写不残留临时文件
            self.assertFalse(cache.with_name(cache.name + ".tmp").exists())

    def test_save_token_cache_string_compat(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "mybt_token.json"
            with mock.patch.object(msc, "TOKEN_CACHE_FILE", cache):
                msc.save_token_cache("user2", "raw-token")
                entry = msc._load_token_cache()["user2"]
            self.assertEqual(entry["token"], "raw-token")

    def test_load_missing_file_returns_empty(self):
        with mock.patch.object(msc, "TOKEN_CACHE_FILE", Path("/nonexistent/nope.json")):
            self.assertEqual(msc._load_token_cache(), {})


class TestCollectAccounts(unittest.TestCase):
    def test_accounts_with_dedupe(self):
        with mock.patch.dict(os.environ, {"MYBT_ACCOUNT": "A#p&a#q", "MYBT_TOKEN": ""}):
            tasks = msc.collect_accounts()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["username"], "A")

    def test_token_fallback(self):
        with mock.patch.dict(os.environ, {"MYBT_ACCOUNT": "", "MYBT_TOKEN": "jwt-here"}):
            tasks = msc.collect_accounts()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["token"], "jwt-here")


class TestMaskAndEnv(unittest.TestCase):
    def test_mask_email(self):
        self.assertEqual(msc.mask("alice@example.com"), "al***@example.com")

    def test_mask_short(self):
        self.assertEqual(msc.mask("ab"), "a*")
        self.assertEqual(msc.mask(""), "")

    def test_env_bool(self):
        with mock.patch.dict(os.environ, {"X_FLAG": "yes"}):
            self.assertTrue(msc.env_bool("X_FLAG", False))
        with mock.patch.dict(os.environ, {"X_FLAG": "0"}):
            self.assertFalse(msc.env_bool("X_FLAG", True))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(msc.env_bool("X_FLAG", True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
