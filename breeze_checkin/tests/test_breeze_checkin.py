#!/usr/bin/env python3
"""breeze_checkin 单测：全部用假响应，不发真实请求。

运行：
    cd breeze_checkin
    python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import breeze_checkin as mod


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self.status_code = status_code
        if text is None:
            text = json.dumps(payload if payload is not None else {}, ensure_ascii=False)
        self.text = text

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """按队列返回响应或抛异常，并记录每次请求。"""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.calls = []
        self.trust_env = True
        self.proxies = {}

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.items:
            raise AssertionError("FakeSession 没有更多响应")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_account(label="主号", cookie="breeze_blue_session=test-session"):
    return {"label": label, "cookie": cookie}


def make_config(timeout=20, proxy=""):
    return {"timeout": timeout, "proxy": proxy}


class ParsingTests(unittest.TestCase):
    def test_single_account_bare_cookie(self):
        accounts = mod.parse_accounts("user=主号; cookie=abc123")
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["label"], "主号")
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=abc123")

    def test_bare_cookie_with_base64_padding(self):
        accounts = mod.parse_accounts("user=主号; cookie=abc==")
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=abc==")

    def test_multi_account_newline(self):
        raw = "user=主号; cookie=a\nuser=小号; cookie=b"
        accounts = mod.parse_accounts(raw)
        self.assertEqual([item["label"] for item in accounts], ["主号", "小号"])
        self.assertEqual(accounts[1]["cookie"], "breeze_blue_session=b")

    def test_multi_account_windows_newline(self):
        raw = "user=主号; cookie=a\r\nuser=小号; cookie=b"
        self.assertEqual(len(mod.parse_accounts(raw)), 2)

    def test_multi_account_double_amp(self):
        raw = "user=主号; cookie=a&&user=小号; cookie=b"
        accounts = mod.parse_accounts(raw)
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=a")

    def test_default_label(self):
        accounts = mod.parse_accounts("cookie=abc")
        self.assertEqual(accounts[0]["label"], "账号1")

    def test_full_cookie_header_kept(self):
        raw = "user=主号; cookie=breeze_blue_session=x; __cf_bm=y"
        accounts = mod.parse_accounts(raw)
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=x; __cf_bm=y")

    def test_cookie_prefix_stripped(self):
        raw = "user=主号; cookie=Cookie: breeze_blue_session=x"
        accounts = mod.parse_accounts(raw)
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=x")

    def test_session_alias(self):
        accounts = mod.parse_accounts("user=主号; session=abc")
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=abc")

    def test_whitespace_collapsed(self):
        accounts = mod.parse_accounts("  user=主号 ;  cookie=  abc  ")
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=abc")

    def test_missing_accounts_env(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("")

    def test_blank_accounts_env(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("  \n  \n")

    def test_missing_cookie_field(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("user=主号")

    def test_empty_cookie_value(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("user=主号; cookie=")

    def test_invalid_field_without_equals(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("user=主号; broken; cookie=abc")

    def test_empty_field_name(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("=x; cookie=abc")

    def test_cookie_last_with_semicolon(self):
        raw = "user=主号; cookie=abc; def=ghi"
        accounts = mod.parse_accounts(raw)
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=abc; def=ghi")

    def test_cookie_value_not_split_by_and(self):
        raw = "user=主号; cookie=abc&&user=小号; cookie=def"
        accounts = mod.parse_accounts(raw)
        self.assertEqual(accounts[0]["cookie"], "breeze_blue_session=abc")

    def test_malformed_cookie_field_no_leak(self):
        secret = "super-secret-cookie-value"
        with self.assertRaises(mod.ConfigError) as ctx:
            mod.parse_accounts(f"user=主号; cookie {secret}")
        self.assertNotIn(secret, str(ctx.exception))

    def test_cookie_colon_form_is_config_error(self):
        secret = "super-secret-cookie-value"
        with self.assertRaises(mod.ConfigError) as ctx:
            mod.parse_accounts(f"user=主号; cookie: {secret}")
        self.assertNotIn(secret, str(ctx.exception))

    def test_empty_cookie_after_normalize(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("user=主号; cookie=;")

    def test_cookie_not_last_field(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("user=主号; cookie=abc; cookie=小号")

    def test_cookie_header_with_session_named_cookie(self):
        raw = "user=主号; cookie=breeze_blue_session=abc; session=xyz; user=u"
        accounts = mod.parse_accounts(raw)
        self.assertEqual(
            accounts[0]["cookie"], "breeze_blue_session=abc; session=xyz; user=u"
        )

    def test_cookie_tail_without_equals(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("user=主号; cookie=abc; secret-tail-value")

    def test_cookie_without_session_name(self):
        with self.assertRaises(mod.ConfigError):
            mod.parse_accounts("user=主号; cookie=__cf_bm=xyz")


class ClientTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(mod.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def _client(self, items, cookie="breeze_blue_session=abc", **kwargs):
        session = FakeSession(items)
        return mod.BreezeClient(cookie, session=session, **kwargs), session

    def test_summary_ok(self):
        client, session = self._client([FakeResponse({"ok": True, "balance": 400})])
        data = client.summary()
        self.assertEqual(data["balance"], 400)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["method"], "GET")

    def test_headers_include_origin_and_referer(self):
        client, session = self._client([FakeResponse({"ok": True})])
        client.summary()
        headers = session.calls[0]["kwargs"]["headers"]
        self.assertEqual(headers["origin"], "https://breezeblue.ai")
        self.assertEqual(headers["referer"], "https://breezeblue.ai/app/home")
        self.assertEqual(headers["cookie"], "breeze_blue_session=abc")
        self.assertEqual(headers["content-type"], "application/json")

    def test_timeout_passed(self):
        client, session = self._client([FakeResponse({"ok": True})], timeout=7)
        client.summary()
        self.assertEqual(session.calls[0]["kwargs"]["timeout"], 7)

    def test_proxy_applied(self):
        client, session = self._client(
            [FakeResponse({"ok": True})], proxy="http://127.0.0.1:7890"
        )
        client.summary()
        self.assertEqual(
            session.proxies, {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
        )
        self.assertFalse(session.trust_env)

    def test_post_sends_json_body(self):
        client, session = self._client(
            [FakeResponse({"claimed": True, "credits_issued": 400, "balance": 400})]
        )
        client.claim()
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["kwargs"]["data"], b"{}")

    def test_summary_401_raises_auth(self):
        client, _ = self._client(
            [FakeResponse({"ok": False, "code": "AUTH_REQUIRED"}, 401)]
        )
        with self.assertRaises(mod.AuthError):
            client.summary()

    def test_summary_403_raises_auth(self):
        client, _ = self._client([FakeResponse({"detail": "forbidden"}, 403)])
        with self.assertRaises(mod.AuthError):
            client.summary()

    def test_summary_auth_code_in_200(self):
        client, _ = self._client([FakeResponse({"ok": False, "code": "AUTH_REQUIRED"}, 200)])
        with self.assertRaises(mod.AuthError):
            client.summary()

    def test_summary_500_retries_then_success(self):
        client, session = self._client(
            [FakeResponse({}, 500), FakeResponse({"ok": True, "balance": 1})]
        )
        data = client.summary()
        self.assertEqual(data["balance"], 1)
        self.assertEqual(len(session.calls), 2)

    def test_summary_500_exhausted(self):
        client, session = self._client(
            [FakeResponse({}, 500), FakeResponse({}, 500), FakeResponse({}, 500)]
        )
        with self.assertRaises(mod.ApiError):
            client.summary()
        self.assertEqual(len(session.calls), 3)

    def test_summary_network_error_exhausted(self):
        error = mod.requests.ConnectionError("boom")
        client, session = self._client([error, error, error])
        with self.assertRaises(mod.NetworkError):
            client.summary()
        self.assertEqual(len(session.calls), 3)

    def test_summary_non_json(self):
        client, _ = self._client([FakeResponse(None, 200, text="<html>oops</html>")])
        with self.assertRaises(mod.ApiError):
            client.summary()

    def test_summary_non_dict_json(self):
        client, _ = self._client([FakeResponse(None, 200, text="[]")])
        with self.assertRaises(mod.ApiError):
            client.summary()

    def test_claim_ok(self):
        client, _ = self._client(
            [FakeResponse({"claimed": True, "credits_issued": 400, "balance": 400})]
        )
        data = client.claim()
        self.assertTrue(data["claimed"])
        self.assertEqual(data["credits_issued"], 400)

    def test_claim_success_with_yi_ling_qu_message(self):
        client, _ = self._client(
            [
                FakeResponse(
                    {
                        "claimed": True,
                        "credits_issued": 400,
                        "balance": 400,
                        "message": "您已领取 400 积分",
                    }
                )
            ]
        )
        data = client.claim()
        self.assertTrue(data["claimed"])
        self.assertNotIn("already_claimed", data)

    def test_claim_401_raises_auth(self):
        client, _ = self._client([FakeResponse({"code": "AUTH_REQUIRED"}, 401)])
        with self.assertRaises(mod.AuthError):
            client.claim()

    def test_claim_403_raises_auth(self):
        client, _ = self._client([FakeResponse({"detail": "forbidden"}, 403)])
        with self.assertRaises(mod.AuthError):
            client.claim()

    def test_claim_429_retries_then_success(self):
        client, session = self._client(
            [
                FakeResponse({}, 429),
                FakeResponse({"claimed": True, "credits_issued": 400, "balance": 400}),
            ]
        )
        data = client.claim()
        self.assertTrue(data["claimed"])
        self.assertEqual(len(session.calls), 2)

    def test_claim_500_exhausted(self):
        client, session = self._client(
            [FakeResponse({}, 500), FakeResponse({}, 500), FakeResponse({}, 500)]
        )
        with self.assertRaises(mod.ApiError):
            client.claim()
        self.assertEqual(len(session.calls), 3)

    def test_summary_network_error_then_success(self):
        error = mod.requests.ConnectionError("boom")
        client, session = self._client(
            [error, FakeResponse({"ok": True, "balance": 1})]
        )
        data = client.summary()
        self.assertEqual(data["balance"], 1)
        self.assertEqual(len(session.calls), 2)

    def test_claim_already_400(self):
        client, _ = self._client(
            [FakeResponse({"ok": False, "detail": "already claimed today"}, 400)]
        )
        data = client.claim()
        self.assertFalse(data["claimed"])
        self.assertTrue(data["already_claimed"])

    def test_claim_already_409(self):
        client, _ = self._client(
            [FakeResponse({"ok": False, "detail": "already claimed"}, 409)]
        )
        self.assertTrue(client.claim()["already_claimed"])

    def test_claim_400_other(self):
        client, _ = self._client([FakeResponse({"ok": False, "detail": "bad request"}, 400)])
        with self.assertRaises(mod.ApiError):
            client.claim()

    def test_claim_200_ok_false(self):
        client, _ = self._client([FakeResponse({"ok": False, "detail": "nope"}, 200)])
        with self.assertRaises(mod.ApiError):
            client.claim()

    def test_claim_200_ok_false_already(self):
        client, _ = self._client(
            [FakeResponse({"ok": False, "detail": "already claimed today"}, 200)]
        )
        data = client.claim()
        self.assertTrue(data["already_claimed"])

    def test_claim_400_yiling_wan_is_error(self):
        client, _ = self._client(
            [FakeResponse({"ok": False, "detail": "今日已领完"}, 400)]
        )
        with self.assertRaises(mod.ApiError):
            client.claim()

    def test_claim_400_yi_ling_qu_is_already(self):
        client, _ = self._client(
            [FakeResponse({"ok": False, "detail": "已领取"}, 400)]
        )
        self.assertTrue(client.claim()["already_claimed"])

    def test_claim_400_not_already_claimed_is_error(self):
        client, _ = self._client(
            [FakeResponse({"ok": False, "detail": "not already claimed"}, 400)]
        )
        with self.assertRaises(mod.ApiError):
            client.claim()

    def test_claim_non_json(self):
        client, _ = self._client([FakeResponse(None, 200, text="<html>oops</html>")])
        with self.assertRaises(mod.ApiError):
            client.claim()


class RunAccountTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(mod.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, items, check=False, probe=False):
        session = FakeSession(items)
        with mock.patch.object(mod.requests, "Session", return_value=session):
            if check:
                result = mod.check_account(make_account(), make_config(), probe)
            else:
                result = mod.run_account(make_account(), make_config(), probe)
        return result, session

    def test_already_claimed_skips_post(self):
        result, session = self._run(
            [FakeResponse({"ok": True, "balance": 800, "daily_claim_claimed_today": True})]
        )
        self.assertTrue(result["ok"])
        self.assertIn("今天已经领过了", result["message"])
        self.assertEqual([call["method"] for call in session.calls], ["GET"])

    def test_claim_success(self):
        result, session = self._run(
            [
                FakeResponse(
                    {
                        "ok": True,
                        "balance": 100,
                        "daily_claim_claimed_today": False,
                        "daily_claim_available": True,
                        "daily_claim_amount": 400,
                    }
                ),
                FakeResponse(
                    {
                        "claimed": True,
                        "credits_issued": 400,
                        "balance": 500,
                        "daily_claim_claimed_today": True,
                    }
                ),
            ]
        )
        self.assertTrue(result["ok"])
        self.assertIn("领到 +400", result["message"])
        self.assertEqual([call["method"] for call in session.calls], ["GET", "POST"])

    def test_claim_reports_already_claimed_flag(self):
        result, _ = self._run(
            [
                FakeResponse({"ok": True, "daily_claim_claimed_today": False}),
                FakeResponse(
                    {"claimed": False, "daily_claim_claimed_today": True, "balance": 900}
                ),
            ]
        )
        self.assertTrue(result["ok"])
        self.assertIn("今天已经领过了", result["message"])

    def test_claim_400_already_success(self):
        result, _ = self._run(
            [
                FakeResponse({"ok": True, "daily_claim_claimed_today": False}),
                FakeResponse({"ok": False, "detail": "already claimed today"}, 400),
            ]
        )
        self.assertTrue(result["ok"])
        self.assertIn("今天已经领过了", result["message"])

    def test_claim_api_error_then_summary_claimed(self):
        result, session = self._run(
            [
                FakeResponse({"ok": True, "daily_claim_claimed_today": False}),
                FakeResponse({}, 500),
                FakeResponse({}, 500),
                FakeResponse({}, 500),
                FakeResponse(
                    {"ok": True, "daily_claim_claimed_today": True, "balance": 700}
                ),
            ]
        )
        self.assertTrue(result["ok"])
        self.assertIn("积分已到账", result["message"])
        self.assertEqual(len(session.calls), 5)

    def test_string_false_claimed_not_effective(self):
        with self.assertRaises(mod.ApiError):
            self._run(
                [
                    FakeResponse(
                        {"ok": True, "daily_claim_claimed_today": "false"}
                    ),
                    FakeResponse(
                        {"claimed": "false", "daily_claim_claimed_today": "false"}
                    ),
                ]
            )

    def test_string_true_claimed_success(self):
        result, _ = self._run(
            [
                FakeResponse({"ok": True, "daily_claim_claimed_today": "false"}),
                FakeResponse(
                    {"claimed": "true", "credits_issued": 400, "balance": 500}
                ),
            ]
        )
        self.assertTrue(result["ok"])
        self.assertIn("领到 +400", result["message"])

    def test_already_claimed_string_false_is_error(self):
        with self.assertRaises(mod.ApiError):
            self._run(
                [
                    FakeResponse({"ok": True, "daily_claim_claimed_today": False}),
                    FakeResponse(
                        {
                            "claimed": False,
                            "already_claimed": "false",
                            "daily_claim_claimed_today": False,
                        }
                    ),
                ]
            )

    def test_available_empty_string_proceeds(self):
        result, session = self._run(
            [
                FakeResponse(
                    {
                        "ok": True,
                        "daily_claim_claimed_today": False,
                        "daily_claim_available": "",
                    }
                ),
                FakeResponse({"claimed": True, "credits_issued": 400, "balance": 500}),
            ]
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(session.calls), 2)

    def test_check_auth_failure_raises(self):
        with self.assertRaises(mod.AuthError):
            self._run([FakeResponse({"code": "AUTH_REQUIRED"}, 401)], check=True)

    def test_unavailable_raises(self):
        with self.assertRaises(mod.ApiError):
            self._run(
                [
                    FakeResponse(
                        {
                            "ok": True,
                            "daily_claim_claimed_today": False,
                            "daily_claim_available": False,
                        }
                    )
                ]
            )

    def test_claim_not_effective_raises(self):
        with self.assertRaises(mod.ApiError):
            self._run(
                [
                    FakeResponse({"ok": True, "daily_claim_claimed_today": False}),
                    FakeResponse({"claimed": False, "daily_claim_claimed_today": False}),
                ]
            )

    def test_claim_network_then_summary_claimed(self):
        error = mod.requests.ConnectionError("boom")
        result, session = self._run(
            [
                FakeResponse({"ok": True, "daily_claim_claimed_today": False}),
                error,
                error,
                error,
                FakeResponse(
                    {"ok": True, "daily_claim_claimed_today": True, "balance": 700}
                ),
            ]
        )
        self.assertTrue(result["ok"])
        self.assertIn("积分已到账", result["message"])
        self.assertEqual(len(session.calls), 5)

    def test_claim_network_then_summary_not_claimed(self):
        error = mod.requests.ConnectionError("boom")
        with self.assertRaises(mod.NetworkError):
            self._run(
                [
                    FakeResponse({"ok": True, "daily_claim_claimed_today": False}),
                    error,
                    error,
                    error,
                    FakeResponse({"ok": True, "daily_claim_claimed_today": False}),
                ]
            )

    def test_check_does_not_post(self):
        result, session = self._run(
            [
                FakeResponse(
                    {
                        "ok": True,
                        "balance": 1200,
                        "daily_claim_claimed_today": True,
                        "daily_claim_amount": 400,
                    }
                )
            ],
            check=True,
        )
        self.assertTrue(result["ok"])
        self.assertIn("登录态有效", result["message"])
        self.assertEqual([call["method"] for call in session.calls], ["GET"])

    def test_probe_prints_summary(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self._run([FakeResponse({"ok": True, "balance": 1})], check=True, probe=True)
        self.assertIn("[DEBUG] summary", buffer.getvalue())


class BuildReportTests(unittest.TestCase):
    def test_all_success_title(self):
        title, content = mod.build_report(
            [{"label": "主号", "ok": True, "message": "✅ ok"}], False
        )
        self.assertIn("全部成功", title)
        self.assertIn("## 成功", content)

    def test_partial_failure_title(self):
        title, content = mod.build_report(
            [
                {"label": "主号", "ok": True, "message": "✅ ok"},
                {"label": "小号", "ok": False, "error": "失效"},
            ],
            False,
        )
        self.assertIn("部分失败", title)
        self.assertIn("## 失败", content)

    def test_all_failure_title(self):
        title, _ = mod.build_report(
            [{"label": "主号", "ok": False, "error": "失效"}], False
        )
        self.assertIn("全部失败", title)

    def test_check_mode_report_title(self):
        _, content = mod.build_report([{"label": "主号", "ok": True, "message": "ok"}], True)
        self.assertIn("自检报告", content)


class MainTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(mod.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

        env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        for key in list(os.environ):
            if key.startswith("BREEZE_"):
                os.environ.pop(key, None)
        os.environ["BREEZE_ACCOUNTS"] = "user=主号; cookie=abc"
        os.environ["BREEZE_NOTIFY"] = "true"
        os.environ["BREEZE_NOTIFY_ONLY_FAIL"] = "true"

        self.send_patcher = mock.patch.object(mod, "send_notify")
        self.send = self.send_patcher.start()
        self.addCleanup(self.send_patcher.stop)

    def test_config_error_exit_2(self):
        os.environ["BREEZE_ACCOUNTS"] = ""
        code = mod.main([])
        self.assertEqual(code, 2)
        self.send.assert_called_once()
        self.assertIn("配置错误", self.send.call_args.args[0])

    def test_all_success_exit_0_and_no_push(self):
        with mock.patch.object(
            mod, "run_account", return_value={"label": "主号", "ok": True, "message": "✅ ok"}
        ):
            code = mod.main([])
        self.assertEqual(code, 0)
        self.send.assert_not_called()

    def test_all_success_pushes_when_only_fail_false(self):
        os.environ["BREEZE_NOTIFY_ONLY_FAIL"] = "false"
        with mock.patch.object(
            mod, "run_account", return_value={"label": "主号", "ok": True, "message": "✅ ok"}
        ):
            code = mod.main([])
        self.assertEqual(code, 0)
        self.send.assert_called_once()

    def test_partial_failure_exit_1_and_push(self):
        os.environ["BREEZE_ACCOUNTS"] = "user=主号; cookie=a\nuser=小号; cookie=b"
        with mock.patch.object(
            mod,
            "run_account",
            side_effect=[
                {"label": "主号", "ok": True, "message": "✅ ok"},
                mod.AuthError("登录态失效"),
            ],
        ):
            code = mod.main([])
        self.assertEqual(code, 1)
        self.send.assert_called_once()
        self.assertIn("部分失败", self.send.call_args.args[0])

    def test_account_isolation(self):
        os.environ["BREEZE_ACCOUNTS"] = "user=主号; cookie=a\nuser=小号; cookie=b"
        with mock.patch.object(
            mod,
            "run_account",
            side_effect=[
                RuntimeError("boom"),
                {"label": "小号", "ok": True, "message": "✅ ok"},
            ],
        ):
            code = mod.main([])
        self.assertEqual(code, 1)
        content = self.send.call_args.args[1]
        self.assertIn("未知错误", content)
        self.assertIn("## 成功", content)

    def test_all_failure_exit_1(self):
        with mock.patch.object(
            mod, "run_account", side_effect=mod.AuthError("登录态失效")
        ):
            code = mod.main([])
        self.assertEqual(code, 1)
        self.send.assert_called_once()
        self.assertIn("全部失败", self.send.call_args.args[0])

    def test_check_failure_exit_1(self):
        with mock.patch.object(
            mod, "check_account", side_effect=mod.AuthError("登录态失效")
        ):
            code = mod.main(["--check"])
        self.assertEqual(code, 1)
        self.send.assert_called_once()

    def test_second_account_config_error_exit_2(self):
        os.environ["BREEZE_ACCOUNTS"] = "user=主号; cookie=abc\nuser=小号"
        code = mod.main([])
        self.assertEqual(code, 2)
        self.assertIn("配置错误", self.send.call_args.args[0])

    def test_malformed_config_does_not_leak_cookie(self):
        secret = "super-secret-cookie-value"
        os.environ["BREEZE_ACCOUNTS"] = f"user=主号; cookie {secret}"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = mod.main([])
        self.assertEqual(code, 2)
        title, content = self.send.call_args.args
        self.assertNotIn(secret, buffer.getvalue())
        self.assertNotIn(secret, title)
        self.assertNotIn(secret, content)

    def test_check_mode_uses_check_account(self):
        with mock.patch.object(
            mod, "check_account", return_value={"label": "主号", "ok": True, "message": "ok"}
        ) as check, mock.patch.object(mod, "run_account") as run:
            code = mod.main(["--check"])
        self.assertEqual(code, 0)
        check.assert_called_once()
        run.assert_not_called()

    def test_check_env_switch(self):
        os.environ["BREEZE_CHECK"] = "true"
        with mock.patch.object(
            mod, "check_account", return_value={"label": "主号", "ok": True, "message": "ok"}
        ) as check:
            code = mod.main([])
        self.assertEqual(code, 0)
        check.assert_called_once()

    def test_debug_env_enables_probe(self):
        os.environ["BREEZE_DEBUG"] = "true"
        with mock.patch.object(
            mod, "run_account", return_value={"label": "主号", "ok": True, "message": "ok"}
        ) as run:
            code = mod.main([])
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.args[2])

    def test_notify_failure_does_not_change_exit_code(self):
        os.environ["BREEZE_NOTIFY_ONLY_FAIL"] = "false"
        self.send.return_value = False
        with mock.patch.object(
            mod, "run_account", return_value={"label": "主号", "ok": True, "message": "✅ ok"}
        ):
            code = mod.main([])
        self.assertEqual(code, 0)

    def test_notify_disabled(self):
        os.environ["BREEZE_NOTIFY"] = "false"
        with mock.patch.object(
            mod, "run_account", side_effect=mod.AuthError("登录态失效")
        ):
            code = mod.main([])
        self.assertEqual(code, 1)
        self.send.assert_not_called()

    def test_cookie_not_in_report(self):
        os.environ["BREEZE_ACCOUNTS"] = "user=主号; cookie=super-secret-cookie"
        session = FakeSession([FakeResponse({"code": "AUTH_REQUIRED"}, 401)])
        with mock.patch.object(mod.requests, "Session", return_value=session):
            code = mod.main([])
        self.assertEqual(code, 1)
        title, content = self.send.call_args.args
        self.assertNotIn("super-secret-cookie", title)
        self.assertNotIn("super-secret-cookie", content)

    def test_duplicate_labels_do_not_crash(self):
        os.environ["BREEZE_ACCOUNTS"] = "user=主号; cookie=a\nuser=主号; cookie=b"
        with mock.patch.object(
            mod, "run_account", return_value={"label": "主号", "ok": True, "message": "✅ ok"}
        ):
            code = mod.main([])
        self.assertEqual(code, 0)


class EnvTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in list(os.environ):
            if key.startswith("BREEZE_"):
                os.environ.pop(key, None)

    def test_env_bool_variants(self):
        for raw in ("1", "true", "YES", "y", "On"):
            os.environ["BREEZE_FLAG"] = raw
            self.assertTrue(mod.env_bool("BREEZE_FLAG"))
        for raw in ("0", "false", "no", "off", ""):
            os.environ["BREEZE_FLAG"] = raw
            self.assertFalse(mod.env_bool("BREEZE_FLAG"))
        self.assertTrue(mod.env_bool("BREEZE_MISSING", True))

    def test_env_int_invalid(self):
        os.environ["BREEZE_NUM"] = "abc"
        self.assertEqual(mod.env_int("BREEZE_NUM", 20, 5, 120), 20)

    def test_env_int_out_of_range(self):
        os.environ["BREEZE_NUM"] = "999"
        self.assertEqual(mod.env_int("BREEZE_NUM", 20, 5, 120), 20)

    def test_resolve_proxy_specific_wins(self):
        os.environ["BREEZE_PROXY"] = "http://127.0.0.1:7900"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        value, source = mod.resolve_proxy("BREEZE_PROXY")
        self.assertEqual(value, "http://127.0.0.1:7900")
        self.assertEqual(source, "BREEZE_PROXY")

    def test_resolve_proxy_global_fallback(self):
        for key in mod.GLOBAL_PROXY_KEYS:
            os.environ.pop(key, None)
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        value, source = mod.resolve_proxy("BREEZE_PROXY")
        self.assertEqual(value, "http://127.0.0.1:7890")
        self.assertEqual(source, "HTTPS_PROXY")

    def test_resolve_proxy_empty(self):
        for key in mod.GLOBAL_PROXY_KEYS:
            os.environ.pop(key, None)
        self.assertEqual(mod.resolve_proxy("BREEZE_PROXY"), ("", ""))

    def test_resolve_proxy_direct_sentinel(self):
        os.environ["BREEZE_PROXY"] = "none"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        self.assertEqual(mod.resolve_proxy("BREEZE_PROXY"), ("", "BREEZE_PROXY"))

    def test_mask_proxy(self):
        masked = mod.mask_proxy("http://user:pass@127.0.0.1:7890")
        self.assertEqual(masked, "http://***:***@127.0.0.1:7890")

    def test_mask_proxy_without_scheme(self):
        self.assertEqual(mod.mask_proxy("user:pass@127.0.0.1:7890"), "***:***@127.0.0.1:7890")

    def test_mask_proxy_username_only(self):
        self.assertEqual(mod.mask_proxy("http://user@127.0.0.1:7890"), "http://***@127.0.0.1:7890")

    def test_mask_proxy_password_with_at(self):
        self.assertEqual(
            mod.mask_proxy("http://user:p@ss@127.0.0.1:7890"),
            "http://***:***@127.0.0.1:7890",
        )

    def test_mask_proxy_without_credentials(self):
        self.assertEqual(mod.mask_proxy("http://127.0.0.1:7890"), "http://127.0.0.1:7890")


class SendNotifyTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(mod.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_notify_module_prints(self):
        buffer = io.StringIO()
        with mock.patch.object(mod, "_NOTIFY", None), redirect_stdout(buffer):
            result = mod.send_notify("标题", "正文")
        self.assertFalse(result)
        self.assertIn("标题", buffer.getvalue())

    def test_retry_then_success(self):
        notify = mock.Mock(side_effect=[RuntimeError("boom"), None])
        with mock.patch.object(mod, "_NOTIFY", notify):
            result = mod.send_notify("标题", "正文")
        self.assertTrue(result)
        self.assertEqual(notify.call_count, 2)

    def test_all_retries_fail(self):
        notify = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(mod, "_NOTIFY", notify):
            result = mod.send_notify("标题", "正文")
        self.assertFalse(result)
        self.assertEqual(notify.call_count, 3)


if __name__ == "__main__":
    unittest.main()
