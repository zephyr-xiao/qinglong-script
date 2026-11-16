"""quark_checkin 单元测试（unittest，全部打桩，不发真实请求）。

运行：python -m unittest discover -s tests -v
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests  # noqa: F401
except ImportError:  # 本地裸环境兜底：给模块一个占位 requests，避免导入期退出
    import types

    _stub = types.ModuleType("requests")

    class _RequestException(Exception):
        pass

    _stub.RequestException = _RequestException
    _stub.Session = object
    sys.modules["requests"] = _stub

import quark_checkin as mod


def info_data(signed=False, reward=0, progress=1, target=7, total=1024**3):
    """成长信息接口返回的 data 字段。"""
    return {
        "88VIP": False,
        "total_capacity": total,
        "cap_composition": {"sign_reward": 100 * 1024**2},
        "cap_sign": {
            "sign_daily": signed,
            "sign_daily_reward": reward,
            "sign_progress": progress,
            "sign_target": target,
        },
    }


def info_payload(**kwargs):
    """完整响应体；QuarkClient 内部会取出 data。"""
    return {"status": 200, "code": 0, "message": "", "data": info_data(**kwargs)}


def sign_payload(reward=1024**2):
    return {"status": 200, "code": 0, "message": "", "data": {"sign_daily_reward": reward}}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """按队列返回响应或抛异常，记录每次调用。"""

    def __init__(self, get_items=None, post_items=None):
        self.get_items = list(get_items or [])
        self.post_items = list(post_items or [])
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        item = self.get_items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        item = self.post_items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_account(label="测试", kps="KPS-VALUE", sign="SIGN-VALUE", vcode="VCODE-VALUE"):
    return {"label": label, "kps": kps, "sign": sign, "vcode": vcode}


class ParsingTests(unittest.TestCase):
    def test_split_supports_newline(self):
        entries = mod.split_entries(" one \r\n\r\n two ")
        self.assertEqual(entries, ["one", "two"])

    def test_split_does_not_cut_double_ampersand_inside_plain_text(self):
        # 只有后面紧跟 key= 或 URL 的 && 才算账号分隔，避免切坏 URL 查询串
        entries = mod.split_entries("one && two")
        self.assertEqual(entries, ["one && two"])

    def test_split_does_not_cut_url_with_single_ampersand(self):
        entries = mod.split_entries("user=a; url=https://x.test/p?kps=1&sign=2&vcode=3")
        self.assertEqual(len(entries), 1)

    def test_split_keeps_double_ampersand_before_next_account(self):
        entries = mod.split_entries(
            "user=一; kps=a; sign=b; vcode=c && user=二; kps=d; sign=e; vcode=f"
        )
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[1].startswith("user=二"))

    def test_split_rejects_empty(self):
        with self.assertRaises(mod.ConfigError):
            mod.split_entries("   ")

    def test_parse_legacy_three_params(self):
        account = mod.parse_account("user=张三; kps=k; sign=s; vcode=v;", 1)
        self.assertEqual(account["label"], "张三")
        self.assertEqual(account["kps"], "k")
        self.assertEqual(account["sign"], "s")
        self.assertEqual(account["vcode"], "v")

    def test_parse_url_format(self):
        account = mod.parse_account(
            "user=李四; url=https://drive-m.quark.cn/x?kps=k1&sign=s1&vcode=v1", 1
        )
        self.assertEqual(account["kps"], "k1")
        self.assertEqual(account["sign"], "s1")
        self.assertEqual(account["vcode"], "v1")

    def test_parse_bare_url_entry(self):
        account = mod.parse_account("https://x.test/p?kps=k&sign=s&vcode=v", 1)
        self.assertEqual(account["label"], "账号1")
        self.assertEqual(account["kps"], "k")

    def test_extract_preserves_literal_plus(self):
        params = mod.extract_url_params(
            "https://x.test/p?kps=AASx+abc%2Bencoded%3D&sign=AAQH+sig&vcode=1790"
        )
        self.assertEqual(params["kps"], "AASx+abc+encoded=")
        self.assertEqual(params["sign"], "AAQH+sig")
        self.assertEqual(params["vcode"], "1790")

    def test_parse_missing_param_names_the_field(self):
        with self.assertRaisesRegex(mod.ConfigError, "sign, vcode"):
            mod.parse_account("user=张三; kps=k;", 1)

    def test_parse_rejects_cookie_entry(self):
        cookie = "__pus=abc; _UP_A4A_11_=xyz; b-user-id=123"
        with self.assertRaisesRegex(mod.ConfigError, "Cookie"):
            mod.parse_account(cookie, 1)

    def test_parse_rejects_cookie_field(self):
        with self.assertRaisesRegex(mod.ConfigError, "Cookie"):
            mod.parse_account("user=张三; cookie=__pus=abc", 1)

    def test_parse_rejects_field_without_equals(self):
        with self.assertRaisesRegex(mod.ConfigError, "无效字段"):
            mod.parse_account("user=张三; garbage", 1)


class ClientTests(unittest.TestCase):
    def build(self, session):
        client = mod.QuarkClient(make_account(), timeout=5)
        client.session = session
        return client

    def test_get_growth_info_retries_network_errors(self):
        session = FakeSession(
            get_items=[
                mod.requests.RequestException("boom"),
                FakeResponse(info_payload()),
            ]
        )
        with mock.patch.object(mod.time, "sleep"):
            data = self.build(session).get_growth_info()
        self.assertIn("cap_sign", data)
        self.assertEqual(len(session.calls), 2)

    def test_get_growth_info_retries_503(self):
        session = FakeSession(
            get_items=[
                FakeResponse({"code": 0}, status_code=503),
                FakeResponse(info_payload()),
            ]
        )
        with mock.patch.object(mod.time, "sleep"):
            data = self.build(session).get_growth_info()
        self.assertIn("cap_sign", data)
        self.assertEqual(len(session.calls), 2)

    def test_get_growth_info_gives_up_after_retries(self):
        session = FakeSession(
            get_items=[
                mod.requests.RequestException("a"),
                mod.requests.RequestException("b"),
                mod.requests.RequestException("c"),
            ]
        )
        with mock.patch.object(mod.time, "sleep"), self.assertRaises(mod.NetworkError):
            self.build(session).get_growth_info()
        self.assertEqual(len(session.calls), 3)

    def test_get_growth_info_401_raises_auth(self):
        session = FakeSession(
            get_items=[
                FakeResponse(
                    {"status": 401, "code": 31001, "message": "require login [guest]"},
                    status_code=401,
                )
            ]
        )
        with self.assertRaises(mod.AuthError):
            self.build(session).get_growth_info()

    def test_get_growth_info_empty_data_raises(self):
        """核心回归：旧脚本把 data:{} 当假值静默跳过，这里必须报错。"""
        session = FakeSession(get_items=[FakeResponse({"status": 200, "code": 0, "data": {}})])
        with self.assertRaisesRegex(mod.ApiError, "空数据"):
            self.build(session).get_growth_info()

    def test_get_growth_info_non_dict_data_raises(self):
        session = FakeSession(get_items=[FakeResponse({"status": 200, "code": 0, "data": []})])
        with self.assertRaisesRegex(mod.ApiError, "空数据"):
            self.build(session).get_growth_info()

    def test_get_growth_info_non_zero_code_raises(self):
        session = FakeSession(
            get_items=[FakeResponse({"status": 200, "code": 12345, "message": "服务异常"})]
        )
        with self.assertRaisesRegex(mod.ApiError, "服务异常"):
            self.build(session).get_growth_info()

    def test_get_growth_info_non_json_raises(self):
        session = FakeSession(get_items=[FakeResponse(ValueError("not json"))])
        with self.assertRaisesRegex(mod.ApiError, "不是 JSON"):
            self.build(session).get_growth_info()

    def test_get_growth_info_non_dict_payload_raises(self):
        session = FakeSession(get_items=[FakeResponse(["unexpected"])])
        with self.assertRaisesRegex(mod.ApiError, "格式异常"):
            self.build(session).get_growth_info()

    def test_sign_parses_reward(self):
        session = FakeSession(post_items=[FakeResponse(sign_payload(2048))])
        self.assertEqual(self.build(session).sign(), 2048)

    def test_sign_missing_reward_raises(self):
        session = FakeSession(post_items=[FakeResponse({"status": 200, "code": 0, "data": {"x": 1}})])
        with self.assertRaisesRegex(mod.ApiError, "sign_daily_reward"):
            self.build(session).sign()

    def test_sign_does_not_retry(self):
        session = FakeSession(post_items=[mod.requests.RequestException("boom")])
        with self.assertRaises(mod.NetworkError):
            self.build(session).sign()
        self.assertEqual(len(session.calls), 1)

    def test_sign_401_raises_auth(self):
        session = FakeSession(
            post_items=[
                FakeResponse({"status": 401, "code": 31001, "message": "require login"}, status_code=401)
            ]
        )
        with self.assertRaises(mod.AuthError):
            self.build(session).sign()


class RunAccountTests(unittest.TestCase):
    def build_client(self, get_items=None, post_items=None):
        client = mock.Mock()
        client.get_growth_info = mock.Mock(side_effect=get_items or [])
        client.sign = mock.Mock(side_effect=post_items or [])
        return client

    def run_with(self, client):
        with mock.patch.object(mod, "QuarkClient", return_value=client), \
                mock.patch.object(mod.time, "sleep"):
            return mod.run_account(make_account(), {"timeout": 5})

    def test_already_signed_skips_post(self):
        client = self.build_client([info_data(signed=True, reward=1024)])
        result = self.run_with(client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "already")
        client.sign.assert_not_called()

    def test_signed_success(self):
        client = self.build_client([info_data(signed=False)], [2048])
        result = self.run_with(client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "signed")
        self.assertIn("2.00 KB", result["message"])
        client.sign.assert_called_once()

    def test_post_network_error_then_verify_signed(self):
        client = self.build_client(
            [info_data(signed=False), info_data(signed=True, reward=4096)],
            [mod.NetworkError("timeout")],
        )
        result = self.run_with(client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "verified")

    def test_post_network_error_then_verify_unsigned_raises(self):
        client = self.build_client(
            [info_data(signed=False), info_data(signed=False)],
            [mod.NetworkError("timeout")],
        )
        with self.assertRaisesRegex(mod.ApiError, "无法确认"):
            self.run_with(client)

    def test_missing_cap_sign_raises(self):
        client = self.build_client([{"88VIP": False, "total_capacity": 1}])
        with self.assertRaisesRegex(mod.ApiError, "cap_sign"):
            self.run_with(client)


class CheckModeTests(unittest.TestCase):
    def test_check_mode_does_not_post(self):
        client = mock.Mock()
        client.get_growth_info.return_value = info_data(signed=False)
        with mock.patch.object(mod, "QuarkClient", return_value=client), \
                mock.patch.object(mod.time, "sleep"):
            result = mod.check_account(make_account(), {"timeout": 5})
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "checked")
        client.sign.assert_not_called()


class MainTests(unittest.TestCase):
    def run_main(self, env, argv=None, client_factory=None):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(mod, "send_notify") as notify, \
                mock.patch.object(mod, "sleep_between_accounts"), \
                mock.patch.object(mod.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            if client_factory is not None:
                with mock.patch.object(mod, "QuarkClient", side_effect=client_factory):
                    code = mod.main(argv or [])
            else:
                code = mod.main(argv or [])
        return code, notify

    def test_missing_accounts_returns_2(self):
        code, notify = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIn("配置错误", notify.call_args[0][0])

    def test_invalid_account_returns_2(self):
        code, _ = self.run_main({"QUARK_ACCOUNTS": "user=张三; kps=k"})
        self.assertEqual(code, 2)

    def test_all_success_returns_0(self):
        def factory(account, timeout=15):
            client = mock.Mock()
            client.get_growth_info.return_value = info_data(signed=True)
            return client

        code, notify = self.run_main(
            {"QUARK_ACCOUNTS": "user=张三; kps=k; sign=s; vcode=v"},
            client_factory=factory,
        )
        self.assertEqual(code, 0)
        self.assertIn("全部成功", notify.call_args[0][0])

    def test_partial_failure_returns_1(self):
        state = {"n": 0}

        def factory(account, timeout=15):
            state["n"] += 1
            client = mock.Mock()
            if state["n"] == 1:
                client.get_growth_info.side_effect = mod.AuthError("凭证失效")
            else:
                client.get_growth_info.return_value = info_data(signed=True)
            return client

        code, notify = self.run_main(
            {
                "QUARK_ACCOUNTS": "user=一; kps=a; sign=b; vcode=c\nuser=二; kps=d; sign=e; vcode=f"
            },
            client_factory=factory,
        )
        self.assertEqual(code, 1)
        self.assertIn("部分失败", notify.call_args[0][0])

    def test_account_isolation_keeps_later_accounts_running(self):
        state = {"n": 0}

        def factory(account, timeout=15):
            state["n"] += 1
            client = mock.Mock()
            if state["n"] == 1:
                client.get_growth_info.side_effect = RuntimeError("意外崩溃")
            else:
                client.get_growth_info.return_value = info_data(signed=True)
            return client

        code, _ = self.run_main(
            {
                "QUARK_ACCOUNTS": "user=一; kps=a; sign=b; vcode=c\nuser=二; kps=d; sign=e; vcode=f"
            },
            client_factory=factory,
        )
        self.assertEqual(state["n"], 2)
        self.assertEqual(code, 1)

    def test_check_flag_via_argv(self):
        client = mock.Mock()
        client.get_growth_info.return_value = info_data(signed=False)
        code, notify = self.run_main(
            {"QUARK_ACCOUNTS": "user=张三; kps=k; sign=s; vcode=v"},
            argv=["--check"],
            client_factory=lambda account, timeout=15: client,
        )
        self.assertEqual(code, 0)
        client.sign.assert_not_called()
        self.assertIn("自检报告", notify.call_args[0][1])

    def test_check_flag_via_env(self):
        client = mock.Mock()
        client.get_growth_info.return_value = info_data(signed=True)
        code, notify = self.run_main(
            {"QUARK_ACCOUNTS": "user=张三; kps=k; sign=s; vcode=v", "QUARK_CHECK": "true"},
            client_factory=lambda account, timeout=15: client,
        )
        self.assertEqual(code, 0)
        client.sign.assert_not_called()
        self.assertIn("自检报告", notify.call_args[0][1])

    def test_error_message_does_not_leak_credentials(self):
        client = mock.Mock()
        client.get_growth_info.side_effect = mod.AuthError("凭证失效，请重新抓包")
        _, notify = self.run_main(
            {"QUARK_ACCOUNTS": "user=张三; kps=SECRET-KPS; sign=SECRET-SIGN; vcode=SECRET-VCODE"},
            client_factory=lambda account, timeout=15: client,
        )
        content = notify.call_args[0][1]
        self.assertNotIn("SECRET-KPS", content)
        self.assertNotIn("SECRET-SIGN", content)
        self.assertNotIn("SECRET-VCODE", content)

    def test_notify_failure_does_not_change_exit_code(self):
        def factory(account, timeout=15):
            client = mock.Mock()
            client.get_growth_info.return_value = info_data(signed=True)
            return client

        with mock.patch.dict(
            os.environ, {"QUARK_ACCOUNTS": "user=张三; kps=k; sign=s; vcode=v"}, clear=True
        ), mock.patch.object(mod, "QuarkClient", side_effect=factory), \
                mock.patch.object(mod, "_NOTIFY", side_effect=RuntimeError("push failed")), \
                mock.patch.object(mod.time, "sleep"), redirect_stdout(io.StringIO()):
            code = mod.main([])
        self.assertEqual(code, 0)


class ConfigTests(unittest.TestCase):
    def test_env_int_falls_back_on_invalid(self):
        with mock.patch.dict(os.environ, {"QUARK_TIMEOUT": "abc"}, clear=True), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(mod.env_int("QUARK_TIMEOUT", 15, 5, 120), 15)

    def test_env_int_falls_back_out_of_range(self):
        with mock.patch.dict(os.environ, {"QUARK_TIMEOUT": "999"}, clear=True), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(mod.env_int("QUARK_TIMEOUT", 15, 5, 120), 15)

    def test_env_int_accepts_valid(self):
        with mock.patch.dict(os.environ, {"QUARK_TIMEOUT": "30"}, clear=True):
            self.assertEqual(mod.env_int("QUARK_TIMEOUT", 15, 5, 120), 30)

    def test_env_bool_variants(self):
        for raw, expected in (("true", True), ("1", True), ("on", True), ("false", False), ("0", False)):
            with mock.patch.dict(os.environ, {"QUARK_CHECK": raw}, clear=True):
                self.assertEqual(mod.env_bool("QUARK_CHECK"), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
