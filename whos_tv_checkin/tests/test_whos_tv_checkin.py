# -*- coding: utf-8 -*-
"""
whos_tv_checkin 单元测试（unittest 标准库，零新依赖）。

运行方式（在 whos_tv_checkin 目录下）：
  python -m unittest discover -s tests -v

依赖：drissionpage（与脚本运行时一致；本机未安装时跳过浏览器相关用例，
      纯函数用例仍执行）。

重点覆盖网络级失败链路（v3 增强）：
  - browser_fetch 的哨兵前缀分类（__TIMEOUT__ / __NETWORK__ / __JSERROR__）
  - fetch_with_retry 仅对网络级失败重试、业务响应不重试
  - _do_signin 网络断连短路（不再白跑白名单探测）
"""
import sys
import types
import unittest
from unittest import mock

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import whos_tv_checkin as wtc
    HAS_DP = True
except SystemExit:
    # 本机无 drissionpage 时脚本会 print 提示并 sys.exit(1)，此处打桩后重新加载
    dp_stub = types.ModuleType("DrissionPage")
    dp_stub.ChromiumOptions = type("ChromiumOptions", (), {})
    dp_stub.ChromiumPage = type("ChromiumPage", (), {})
    settings_mod = types.ModuleType("DrissionPage._functions.settings")
    settings_mod.Settings = type("Settings", (), {"set_browser_connect_timeout": staticmethod(lambda v: None)})
    dp_stub._functions = types.SimpleNamespace(settings=settings_mod)
    sys.modules["DrissionPage"] = dp_stub
    sys.modules["DrissionPage._functions"] = types.ModuleType("DrissionPage._functions")
    sys.modules["DrissionPage._functions.settings"] = settings_mod
    import whos_tv_checkin as wtc
    HAS_DP = False


@unittest.skipUnless(HAS_DP, "需要 drissionpage（仅影响 run_js 行为模拟的集成路径）")
class TestBrowserFetch(unittest.TestCase):
    """browser_fetch 哨兵前缀分类：模拟 DrissionPage run_js 的各种返回形态。"""

    def _page_returning(self, value):
        page = mock.MagicMock()
        page.run_js.return_value = value
        return page

    def test_success_response(self):
        page = self._page_returning({"ok": True, "status": 200, "text": '{"code":200000}'})
        status, text = wtc.browser_fetch(page, "https://whos.tv/api/x")
        self.assertEqual(status, 200)
        self.assertIn("200000", text)

    def test_timeout_abort_error(self):
        """JS AbortError → __TIMEOUT__ 哨兵。"""
        page = self._page_returning({"ok": False, "name": "AbortError", "message": "signal timed out"})
        status, text = wtc.browser_fetch(page, "https://whos.tv/api/x", timeout=30)
        self.assertEqual(status, 0)
        self.assertTrue(text.startswith("__TIMEOUT__"))
        self.assertIn("30s", text)

    def test_network_failure(self):
        """fetch TypeError (Failed to fetch) → __NETWORK__ 哨兵。"""
        page = self._page_returning({"ok": False, "name": "TypeError", "message": "Failed to fetch"})
        status, text = wtc.browser_fetch(page, "https://whos.tv/api/x")
        self.assertEqual(status, 0)
        self.assertTrue(text.startswith("__NETWORK__"))
        self.assertIn("Failed to fetch", text)

    def test_run_js_raises(self):
        """run_js 抛 Python 异常 → __JSERROR__ 哨兵。"""
        page = mock.MagicMock()
        page.run_js.side_effect = Exception("CDP timeout")
        status, text = wtc.browser_fetch(page, "https://whos.tv/api/x")
        self.assertEqual(status, 0)
        self.assertTrue(text.startswith("__JSERROR__"))

    def test_non_dict_result(self):
        """run_js 返回 None（老版本丢弃 promise 结果）→ __JSERROR__ 哨兵。"""
        page = self._page_returning(None)
        status, text = wtc.browser_fetch(page, "https://whos.tv/api/x")
        self.assertEqual(status, 0)
        self.assertTrue(text.startswith("__JSERROR__"))


class TestNetworkHelpers(unittest.TestCase):
    """is_network_failure / network_fail_reason 纯函数。"""

    def test_sentinel_prefixes_recognized(self):
        for prefix in ("__TIMEOUT__|x", "__NETWORK__|y", "__JSERROR__|z"):
            self.assertTrue(wtc.is_network_failure(0, prefix))
            self.assertFalse(wtc.is_network_failure(200, prefix), "有 HTTP 状态码不算网络失败")

    def test_plain_http0_not_network_failure(self):
        """历史格式（无哨兵前缀）不误判。"""
        self.assertFalse(wtc.is_network_failure(0, "请求异常: some error"))

    def test_reason_strips_prefix(self):
        self.assertEqual(wtc.network_fail_reason("__TIMEOUT__|30s 内未收到响应"), "30s 内未收到响应")
        self.assertEqual(wtc.network_fail_reason("__NETWORK__|TypeError: Failed to fetch"),
                         "TypeError: Failed to fetch")
        # 非哨兵文本原样返回
        self.assertEqual(wtc.network_fail_reason("普通错误"), "普通错误")


class TestFetchWithRetry(unittest.TestCase):
    """重试包装：仅网络级失败触发重试。"""

    def test_retry_then_success(self):
        with mock.patch.object(wtc, "browser_fetch",
                               side_effect=[(0, "__TIMEOUT__|30s 内未收到响应"), (200, '{"code":200000}')]), \
             mock.patch("time.sleep") as sleep_mock:
            status, text = wtc.fetch_with_retry(None, "https://whos.tv/api/x", debug=False)
        self.assertEqual(status, 200)
        self.assertEqual(sleep_mock.call_count, 1)

    def test_exhausted_retries_returns_last_failure(self):
        fails = [(0, "__NETWORK__|TypeError: Failed to fetch")] * 3
        with mock.patch.object(wtc, "browser_fetch", side_effect=fails), \
             mock.patch("time.sleep"):
            status, text = wtc.fetch_with_retry(None, "https://whos.tv/api/x",
                                                retries=2, debug=False)
        self.assertEqual(status, 0)
        self.assertTrue(text.startswith("__NETWORK__"))

    def test_business_response_not_retried(self):
        """403/500 属服务器已有响应，不得重试。"""
        with mock.patch.object(wtc, "browser_fetch",
                               return_value=(403, "<html>Cloudflare</html>")) as bf:
            status, text = wtc.fetch_with_retry(None, "https://whos.tv/api/x")
        self.assertEqual(status, 403)
        self.assertEqual(bf.call_count, 1)


class TestDoSigninShortCircuit(unittest.TestCase):
    """_do_signin 网络断连短路：已知接口拿不到响应时不再白跑探测。"""

    def _run(self, first_post_result):
        calls = []

        def fake_try_signin(page, url, timeout, debug):
            calls.append(url)
            if url.endswith("/api/user/tasks/signin"):
                return first_post_result
            return False, f"HTTP 200 | {url}"

        with mock.patch.object(wtc, "try_signin_post", side_effect=fake_try_signin), \
             mock.patch.object(wtc, "fetch_with_retry",
                               return_value=(200, "<html>checkin-btn</html>")):
            ok, msg = wtc._do_signin(None, "账号 1", 30, False)
        return ok, msg, len(calls)

    def test_known_api_timeout_short_circuits(self):
        """已知接口超时 → 立即短路返回，不进入候选探测循环。"""
        ok, msg, call_cnt = self._run((False, "HTTP 0 | __TIMEOUT__|30s 内未收到响应"))
        self.assertFalse(ok)
        self.assertIn("网络异常", msg)
        self.assertIn("30s 内未收到响应", msg)
        self.assertEqual(call_cnt, 1, "不应继续探测候选 URL")

    def test_known_api_network_error_short_circuits(self):
        ok, msg, call_cnt = self._run((False, "HTTP 0 | __NETWORK__|TypeError: Failed to fetch"))
        self.assertFalse(ok)
        self.assertIn("Failed to fetch", msg)
        self.assertEqual(call_cnt, 1)

    def test_business_failure_still_probes(self):
        """业务失败（非网络级）→ 保持原逻辑走候选探测。"""
        ok, msg, call_cnt = self._run((False, "HTTP 500"))
        self.assertFalse(ok)
        self.assertGreater(call_cnt, 1, "业务失败应继续探测候选 URL")


class TestLoginMessage(unittest.TestCase):
    """login_one_account 网络失败的文案分支。"""

    def test_login_network_error_message(self):
        with mock.patch.object(wtc, "fetch_with_retry",
                               return_value=(0, "__TIMEOUT__|30s 内未收到响应")), \
             mock.patch("builtins.print"):
            ok, msg = wtc.login_one_account(mock.MagicMock(), "u@x.com", "pwd", 30, False, "label")
        self.assertFalse(ok)
        self.assertIn("登录网络异常", msg)
        self.assertIn("30s 内未收到响应", msg)

    def test_login_wrong_password_still_classified(self):
        resp = '{"code":400001,"message":"密码错误"}'
        with mock.patch.object(wtc, "fetch_with_retry",
                               return_value=(200, resp)):
            ok, msg = wtc.login_one_account(mock.MagicMock(), "u@x.com", "pwd", 30, False, "label")
        self.assertFalse(ok)
        self.assertIn("密码错误", msg)


if __name__ == "__main__":
    unittest.main()
