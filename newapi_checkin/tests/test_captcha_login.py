# -*- coding: utf-8 -*-
"""图形验证码登录与 token 刷新的单元测试（全部打桩，不发真实请求）。

覆盖：算术复算、视觉模型回复解析、验证码参数拼接、验证码错误重试、
配置缺失时的明确报错、会话刷新免登录、缓存载荷扩展。
测试数据一律使用 example.com / test123 之类的占位值。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import newapi_checkin as nap  # noqa: E402


class FakeResponse:
    """最小化的 requests.Response 替身。"""

    def __init__(self, status_code=200, data=None, text=None):
        self.status_code = status_code
        self._data = data
        self.text = text if text is not None else json.dumps(data or {}, ensure_ascii=False)

    def json(self):
        if self._data is None:
            raise ValueError("response has no json body")
        return self._data


class FakeCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class FakeCookieJar:
    def __init__(self, items=None):
        self._items = [FakeCookie(name, value) for name, value in (items or [])]

    def __iter__(self):
        return iter(self._items)

    def set(self, name, value, **kwargs):
        self._items.append(FakeCookie(name, value))

    def names(self):
        return [item.name for item in self._items]


class FakeSession:
    """按顺序吐出预置响应，同时记录被调用的 URL。"""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.cookies = FakeCookieJar()
        self.verify = True
        self.trust_env = False
        self.proxies = {}

    def _call(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError(f"未预期的请求: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)

    def close(self):
        pass


def base_config():
    """带验证码 + 刷新接口的假站点配置（占位域名）。"""
    return {
        "login_url": "https://example.com/api/user/login",
        "login_method": "POST",
        "login_body_template": '{"username": "{{username}}", "password": "{{password}}"}',
        "login_content_type": "application/json",
        "token_path": "data.access_token",
        "token_expires_path": "data.access_expires_at",
        "session_sid_path": "data.session.sid",
        "token_type_path": "data.token_type",
        "refresh_url": "https://example.com/api/user/auth/refresh",
        "captcha_config": {
            "url": "https://example.com/api/captcha",
            "id_field": "data.captcha_id",
            "image_field": "data.image",
            "login_query": {
                "turnstile": "",
                "captcha_id": "{{captcha_id}}",
                "captcha": "{{captcha}}",
            },
            "error_codes": ["CAPTCHA_INVALID", "CAPTCHA_REQUIRED"],
        },
        "signin_url": "https://example.com/api/user/checkin",
        "signin_method": "POST",
    }


class TestEvalArith(unittest.TestCase):
    def test_basic_operations(self):
        self.assertEqual(nap._eval_arith("9+19"), 28)
        self.assertEqual(nap._eval_arith("11-3"), 8)
        self.assertEqual(nap._eval_arith("5×4"), 20)
        self.assertEqual(nap._eval_arith("28÷4"), 7)

    def test_operator_variants_normalized(self):
        self.assertEqual(nap._eval_arith("12x4"), 48)
        self.assertEqual(nap._eval_arith("12X4"), 48)
        self.assertEqual(nap._eval_arith("12*4"), 48)
        self.assertEqual(nap._eval_arith("12/4"), 3)
        self.assertEqual(nap._eval_arith("12 − 3"), 9)

    def test_invalid_expressions(self):
        for bad in ("", "abc", "1+", "+1", "7÷0", "10÷4", "1+2+3", None):
            self.assertIsNone(nap._eval_arith(bad), msg=f"{bad!r} 应判为非法")


class TestParseCaptchaAnswer(unittest.TestCase):
    def test_plain_json(self):
        answer, expression, err = nap._parse_captcha_answer('{"expression": "11-3", "answer": 8}')
        self.assertEqual((answer, expression, err), (8, "11-3", ""))

    def test_fenced_json(self):
        content = '```json\n{"expression": "28÷4", "answer": 7}\n```'
        answer, expression, err = nap._parse_captcha_answer(content)
        self.assertEqual(answer, 7)
        self.assertEqual(expression, "28÷4")
        self.assertEqual(err, "")

    def test_answer_mismatch_uses_computed_value(self):
        """模型把 4×8 算成 12 时，用本地复算的 32，避免把明显不自洽的答案提交上去。"""
        answer, _, err = nap._parse_captcha_answer('{"expression": "4×8", "answer": 12}')
        self.assertEqual(answer, 32)
        self.assertEqual(err, "")

    def test_plain_number_fallback(self):
        answer, expression, err = nap._parse_captcha_answer("17")
        self.assertEqual((answer, expression), (17, ""))

    def test_unparsable(self):
        answer, _, err = nap._parse_captcha_answer("我看不清这张图")
        self.assertIsNone(answer)
        self.assertIn("无法从模型回复解析答案", err)

    def test_expression_embedded_in_plain_text(self):
        """模型没按 JSON 回、但文本里带完整算式时也要能算出来。"""
        answer, expression, err = nap._parse_captcha_answer("图中算式为 11-3=8")
        self.assertEqual((answer, expression, err), (8, "11-3", ""))


class TestCaptchaLoginUrl(unittest.TestCase):
    def test_query_includes_empty_turnstile(self):
        url = nap._build_captcha_login_url(
            "https://example.com/api/user/login",
            base_config()["captcha_config"],
            "cid-1",
            8,
        )
        self.assertIn("?turnstile=&captcha_id=cid-1&captcha=8", url)

    def test_appends_to_existing_query(self):
        url = nap._build_captcha_login_url(
            "https://example.com/api/user/login?foo=1",
            base_config()["captcha_config"],
            "cid-1",
            8,
        )
        self.assertIn("foo=1&turnstile=&captcha_id=cid-1&captcha=8", url)

    def test_without_login_query_returns_original(self):
        url = nap._build_captcha_login_url("https://example.com/login", {}, "cid", 1)
        self.assertEqual(url, "https://example.com/login")


class TestCaptchaLoginFlow(unittest.TestCase):
    def test_retry_then_success(self):
        config = base_config()
        session = FakeSession([
            FakeResponse(200, {"code": "CAPTCHA_INVALID", "message": "Captcha is incorrect", "success": False}),
            FakeResponse(200, {"success": True, "data": {"access_token": "tok-new", "access_expires_at": 1800000000}}),
        ])
        with mock.patch.object(nap, "_fetch_captcha",
                               side_effect=[("cid-1", "data:image/png;base64,AAAA", ""),
                                            ("cid-2", "data:image/png;base64,BBBB", "")]), \
             mock.patch.object(nap, "_solve_captcha_with_vlm", return_value=(8, "11-3", "")):
            ok, token, err, _ = nap._do_login(session, config, "demo_user", "test123")

        self.assertTrue(ok, msg=err)
        self.assertEqual(token, "tok-new")
        self.assertEqual(len(session.calls), 2)
        self.assertIn("captcha_id=cid-1", session.calls[0]["url"])
        self.assertIn("captcha_id=cid-2", session.calls[1]["url"])

    def test_exhausts_attempts(self):
        config = base_config()
        invalid = FakeResponse(200, {"code": "CAPTCHA_INVALID", "message": "Captcha is incorrect", "success": False})
        session = FakeSession([invalid, invalid])
        with mock.patch.dict(os.environ, {"QZD_CAPTCHA_ATTEMPTS": "2"}), \
             mock.patch.object(nap, "_fetch_captcha",
                               side_effect=[("cid-1", "data:image/png;base64,AAAA", ""),
                                            ("cid-2", "data:image/png;base64,BBBB", "")]), \
             mock.patch.object(nap, "_solve_captcha_with_vlm", return_value=(3, "1+2", "")):
            ok, token, err, _ = nap._do_login(session, config, "demo_user", "test123")

        self.assertFalse(ok)
        self.assertEqual(token, "")
        self.assertIn("图形验证码", err)
        self.assertEqual(len(session.calls), 2)

    def test_captcha_error_not_reported_as_wrong_password(self):
        """站点文案 “Captcha is incorrect” 含 incorrect，不能被误判成密码错误。"""
        config = base_config()
        session = FakeSession([
            FakeResponse(200, {"code": "CAPTCHA_INVALID", "message": "Captcha is incorrect", "success": False}),
        ])
        ok, _, err, _ = nap._post_login(session, config, "demo_user", "test123",
                                        login_url="https://example.com/api/user/login")
        self.assertFalse(ok)
        self.assertIn("图形验证码", err)
        self.assertNotIn("密码错误", err)

    def test_missing_vlm_settings(self):
        with mock.patch.dict(os.environ, {"QZD_VLM": ""}):
            answer, expression, err = nap._solve_captcha_with_vlm("data:image/png;base64,AAAA")
        self.assertIsNone(answer)
        self.assertEqual(expression, "")
        self.assertIn("QZD_VLM", err)

    def test_malformed_vlm_settings(self):
        """少一段或空段时明确报格式错误，而不是拼出一个坏请求。"""
        for raw in ("https://example.com/v1#test123", "https://example.com/v1##model", "#test123#model"):
            with mock.patch.dict(os.environ, {"QZD_VLM": raw}):
                settings, err = nap._vlm_settings()
            self.assertIsNone(settings, msg=raw)
            self.assertIn("base_url#api_key#model", err)

    def test_vlm_settings_parsed(self):
        with mock.patch.dict(os.environ, {
            "QZD_VLM": " https://example.com/v1 # test123 # test-vision-model ",
            "QZD_VLM_TIMEOUT": "90",
        }):
            settings, err = nap._vlm_settings()
        self.assertEqual(err, "")
        self.assertEqual(settings["base_url"], "https://example.com/v1")
        self.assertEqual(settings["api_key"], "test123")
        self.assertEqual(settings["model"], "test-vision-model")
        self.assertEqual(settings["timeout"], 90)


class TestVlmCall(unittest.TestCase):
    def test_request_payload_and_parsing(self):
        reply = {"choices": [{"message": {"content": '```json\n{"expression": "28÷4", "answer": 7}\n```'}}]}
        fake_session = FakeSession([FakeResponse(200, reply)])
        env = {
            "QZD_VLM": "https://example.com/v1#test123#test-vision-model",
        }
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(nap.requests, "Session", return_value=fake_session):
            answer, expression, err = nap._solve_captcha_with_vlm("data:image/png;base64,AAAA")

        self.assertEqual((answer, expression, err), (7, "28÷4", ""))
        call = fake_session.calls[0]
        self.assertEqual(call["url"], "https://example.com/v1/chat/completions")
        payload = call["kwargs"]["json"]
        self.assertEqual(payload["model"], "test-vision-model")
        self.assertEqual(payload["messages"][0]["content"][1]["image_url"]["url"], "data:image/png;base64,AAAA")
        # 思考型模型需要足够的输出预算，默认值不能是小数字
        self.assertEqual(payload["max_tokens"], 3000)

    def test_max_tokens_override(self):
        reply = {"choices": [{"message": {"content": "8"}}]}
        fake_session = FakeSession([FakeResponse(200, reply)])
        env = {
            "QZD_VLM": "https://example.com/v1#test123#test-vision-model",
            "QZD_VLM_MAX_TOKENS": "4096",
        }
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(nap.requests, "Session", return_value=fake_session):
            answer, _, _ = nap._solve_captcha_with_vlm("data:image/png;base64,AAAA")
        self.assertEqual(answer, 8)
        self.assertEqual(fake_session.calls[0]["kwargs"]["json"]["max_tokens"], 4096)

    def test_reasoning_content_fallback(self):
        """思考型模型只回 reasoning_content 时，从思考过程里捞答案。"""
        reply = {"choices": [{"message": {
            "content": "",
            "reasoning_content": '图中是 3×7=?，计算结果为 21。{"expression": "3×7", "answer": 21}',
        }}]}
        fake_session = FakeSession([FakeResponse(200, reply)])
        env = {"QZD_VLM": "https://example.com/v1#test123#test-vision-model"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(nap.requests, "Session", return_value=fake_session):
            answer, expression, err = nap._solve_captcha_with_vlm("data:image/png;base64,AAAA")
        self.assertEqual((answer, expression, err), (21, "3×7", ""))

    def test_empty_content_reports_max_tokens_hint(self):
        reply = {"choices": [{"message": {"content": ""}}]}
        fake_session = FakeSession([FakeResponse(200, reply)])
        env = {"QZD_VLM": "https://example.com/v1#test123#test-vision-model"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(nap.requests, "Session", return_value=fake_session):
            answer, _, err = nap._solve_captcha_with_vlm("data:image/png;base64,AAAA")
        self.assertIsNone(answer)
        self.assertIn("max_tokens", err)
        self.assertIn("QZD_VLM_MAX_TOKENS", err)


class TestRefreshFlow(unittest.TestCase):
    def test_expired_cache_refreshes_without_login(self):
        config = base_config()
        refresh_reply = FakeResponse(200, {
            "access_token": "tok-refreshed",
            "token_type": "Bearer",
            "access_expires_at": 1800000000,
        })
        session = FakeSession([refresh_reply])

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            cache_path = nap.token_cache_path("dzzi", "demo_user")
            cache_path = cache_dir / cache_path.name
            cache_path.write_text(json.dumps({
                "token": "tok-expired",
                "expires_at": 1,
                "cookies": [{"name": "session", "value": "placeholder-cookie"}],
                "session_sid": "sid-abc",
            }), encoding="utf-8")

            with mock.patch.object(nap, "TOKEN_DIR", cache_dir), \
                 mock.patch.object(nap, "_new_session", return_value=session), \
                 mock.patch.object(nap, "_fetch_quota", return_value=None), \
                 mock.patch.object(nap, "_do_signin", return_value={"success": True, "message": "签到成功"}), \
                 mock.patch.object(nap, "_do_login", side_effect=AssertionError("不应触发登录")):
                result = nap.execute_signin("dzzi", config, "demo_user", "test123")

            self.assertTrue(result["success"], msg=result)
            self.assertEqual(session.calls[0]["url"], "https://example.com/api/user/auth/refresh")
            self.assertEqual(session.calls[0]["kwargs"]["headers"].get("X-Auth-Session"), "sid-abc")
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["token"], "tok-refreshed")
            self.assertEqual(saved["expires_at"], 1800000000)

    def test_refresh_failure_falls_back_to_login(self):
        config = base_config()
        session = FakeSession([FakeResponse(401, {"success": False, "message": "Unauthorized"})])

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            cache_path = cache_dir / nap.token_cache_path("dzzi", "demo_user").name
            cache_path.write_text(json.dumps({
                "token": "tok-expired",
                "expires_at": 1,
                "cookies": [{"name": "session", "value": "placeholder-cookie"}],
            }), encoding="utf-8")

            with mock.patch.object(nap, "TOKEN_DIR", cache_dir), \
                 mock.patch.object(nap, "_new_session", return_value=session), \
                 mock.patch.object(nap, "_fetch_quota", return_value=None), \
                 mock.patch.object(nap, "_do_signin", return_value={"success": True, "message": "签到成功"}), \
                 mock.patch.object(nap, "_do_login",
                                   return_value=(True, "tok-login", "",
                                                 {"success": True, "data": {"access_token": "tok-login"}})) as login_mock:
                result = nap.execute_signin("dzzi", config, "demo_user", "test123")

        self.assertTrue(result["success"], msg=result)
        login_mock.assert_called_once()


class TestCachePayload(unittest.TestCase):
    def test_payload_keeps_sid_and_token_type(self):
        session = FakeSession()
        session.cookies = FakeCookieJar([("session", "placeholder-cookie")])
        login_data = {
            "success": True,
            "data": {
                "access_token": "tok",
                "access_expires_at": 1800000000,
                "token_type": "Bearer",
                "session": {"sid": "sid-9"},
            },
        }
        payload = nap._build_cache_payload(session, base_config(), "tok", login_data)
        self.assertEqual(payload["session_sid"], "sid-9")
        self.assertEqual(payload["token_type"], "Bearer")
        self.assertEqual(payload["expires_at"], 1800000000)
        self.assertEqual(payload["cookies"], [{"name": "session", "value": "placeholder-cookie"}])

    def test_legacy_cache_without_new_fields_still_valid(self):
        self.assertTrue(nap._cache_is_valid({"token": "tok", "cookies": []}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
