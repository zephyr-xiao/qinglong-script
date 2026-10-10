# -*- coding: utf-8 -*-
"""xsijishe_checkin 单元测试（标准库 unittest，全部 mock，不发真实请求）

覆盖：账号解析（含 Cookie/@ 歧义回归）/ 邮箱脱敏 / 域名候选与运行时排序学习 /
签到接口三态判定 / 签到统计解析 / 网络错误判定 / Cookie 缓存单账号读写与删除 /
同账号连续登录失败熔断 / Playwright 兜底开关。
真实网络请求不进单测（本地干跑负责）。
"""
import io
import os
import sys
import json
import types
import tempfile
import unittest
import contextlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import xsijishe_checkin as msc
except SystemExit:
    # 依赖缺失时打桩后重新加载（青龙外最小环境兜底）
    for name in ("requests", "bs4"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["requests"].Session = object
    sys.modules["bs4"].BeautifulSoup = object
    import xsijishe_checkin as msc


@contextlib.contextmanager
def quiet():
    """屏蔽被测函数的 print 输出，保持测试输出干净。"""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


# ---------- 实测样本（2026-10-10 真机抓取） ----------
REAL_COOKIE = ("SgL6_2132_saltkey=AbCd1234; SgL6_2132_lastvisit=1759999999; "
               "SgL6_2132_auth=abcdef%09xyz; server_session_5db84476=deadbeef")
SAMPLE_SIGN_ALREADY = ('<?xml version="1.0" encoding="utf-8"?>\r\n'
                       '<root><![CDATA[今日已签]]></root>')
SAMPLE_SIGN_PAGE_STATS = ('<input type="hidden" id="lxdays" value="49" />'
                          '<input type="hidden" id="lxlevel" value="6" />'
                          '<input type="hidden" id="lxreward" value="234" />'
                          '<input type="hidden" id="lxtdays" value="91" />')
# 2026-10-10 真机抓取的登录成功响应片段（站点模板色值为 #2B8BD6，非历史值 #00FFCC）
SAMPLE_LOGIN_WELCOME = ("$('succeedlocation').innerHTML = '欢迎您回来，"
                        '<font color="#2B8BD6">Lv.3新手司机</font> zephyr_xiao，'
                        "现在将转入登录前页面';</script>")


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """按顺序返回预置响应；只剩一条时反复返回它。记录请求过的 URL。"""

    def __init__(self, *texts):
        self._texts = list(texts)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        text = self._texts[0] if len(self._texts) == 1 else self._texts.pop(0)
        return FakeResponse(text)


def _ok(host):
    return msc._result(True, "ok", "✅ 签到成功", "账号 1 [Cookie]", host)


def _login_failed(host):
    return msc._result(False, "login_failed", "❌ 登录失败", "账号 1 [x***x@mail.com]", host)


class TestParseAccounts(unittest.TestCase):
    def test_real_cookie_string(self):
        accs = msc.parse_accounts(REAL_COOKIE)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0]["type"], "cookie")
        self.assertEqual(accs[0]["cookie"], REAL_COOKIE)

    def test_email_password(self):
        accs = msc.parse_accounts("user@mail.com&mypassword")
        self.assertEqual(accs[0]["type"], "credential")
        self.assertEqual(accs[0]["email"], "user@mail.com")
        self.assertEqual(accs[0]["password"], "mypassword")

    def test_cookie_with_at_and_amp_is_cookie(self):
        """回归：含 @ 的 Cookie 值不能被误判成邮箱密码账号。"""
        raw = "sid=abc@def; auth=xyz&123; lastvisit=1"
        accs = msc.parse_accounts(raw)
        self.assertEqual(accs[0]["type"], "cookie")
        self.assertEqual(accs[0]["cookie"], raw)

    def test_single_cookie_pair_still_cookie(self):
        self.assertEqual(msc.parse_accounts("onlykey=onlyvalue")[0]["type"], "cookie")

    def test_password_containing_ampersand(self):
        accs = msc.parse_accounts("a@b.com&p&ss")
        self.assertEqual(accs[0]["type"], "credential")
        self.assertEqual(accs[0]["password"], "p&ss")

    def test_multi_line_with_blank_lines(self):
        raw = "user1@mail.com&pw1\n\n  \ncookie_a=1; cookie_b=2\n"
        accs = msc.parse_accounts(raw)
        self.assertEqual([a["type"] for a in accs], ["credential", "cookie"])
        self.assertEqual(accs[0]["label"], "账号 1 [u***1@mail.com]")

    def test_empty(self):
        self.assertEqual(msc.parse_accounts(""), [])
        self.assertEqual(msc.parse_accounts("   \n  "), [])


class TestMaskEmail(unittest.TestCase):
    def test_long_local_part(self):
        self.assertEqual(msc.mask_email("abcdef@gmail.com"), "a***f@gmail.com")

    def test_short_local_part(self):
        self.assertEqual(msc.mask_email("ab@gmail.com"), "a*@gmail.com")

    def test_not_email(self):
        self.assertEqual(msc.mask_email("not-an-email"), "not-an-email")
        self.assertEqual(msc.mask_email(""), "未知")


class TestHostCandidates(unittest.TestCase):
    def test_known_hosts_excludes_dead_domains(self):
        for dead in ("xsijishe.ink", "sjslt.cc", "sjs47.com"):
            self.assertNotIn(dead, msc.KNOWN_HOSTS)
        for alive in ("xsijishe.net", "dlsjs.net", "sjs96.com", "dlsjs.com",
                      "sjs66.net", "xsijishe.com", "sjs66.com"):
            self.assertIn(alive, msc.KNOWN_HOSTS)

    def test_get_hosts_manual_override(self):
        with mock.patch.dict(os.environ, {"SIJISHE_HOST": "dlsjs.net"}), quiet():
            hosts, source = msc.get_hosts(False)
        self.assertEqual(hosts, ["dlsjs.net"])
        self.assertIn("手动", source)

    def test_get_hosts_falls_back_to_builtin(self):
        with mock.patch.dict(os.environ, {"SIJISHE_HOST": ""}), quiet():
            hosts, source = msc.get_hosts(False)
        self.assertEqual(hosts, list(msc.KNOWN_HOSTS))
        self.assertIn("内置", source)


class TestOrderHosts(unittest.TestCase):
    def test_preferred_moved_to_front(self):
        self.assertEqual(msc._order_hosts(["a.com", "b.com", "c.com"], "c.com"),
                         ["c.com", "a.com", "b.com"])

    def test_preferred_not_in_list(self):
        self.assertEqual(msc._order_hosts(["a.com", "b.com"], "z.com"),
                         ["a.com", "b.com"])

    def test_no_preferred(self):
        self.assertEqual(msc._order_hosts(["a.com"], None), ["a.com"])


class TestResultHelper(unittest.TestCase):
    def test_shape(self):
        r = msc._result(True, "ok", "msg", "label", "host.com")
        self.assertEqual(r, {"success": True, "reason": "ok", "message": "msg",
                             "label": "label", "host": "host.com"})


class TestParseLoginWelcome(unittest.TestCase):
    def test_real_sample(self):
        """回归：站点字体色值已改，不能再写死 #00FFCC，且昵称不能带 HTML 标签。"""
        self.assertEqual(msc.parse_login_welcome(SAMPLE_LOGIN_WELCOME),
                         "等级: Lv.3新手司机，昵称: zephyr_xiao")

    def test_without_nickname(self):
        inner = '欢迎您回来，<font color="#123456">Lv.1新手上路</font>，现在将转入'
        self.assertEqual(msc.parse_login_welcome(inner), "等级: Lv.1新手上路")

    def test_fallback_strips_tags(self):
        inner = "欢迎您回来，<b>someone</b>，现在将转入登录前页面"
        self.assertEqual(msc.parse_login_welcome(inner), "someone")

    def test_no_welcome_text(self):
        self.assertEqual(msc.parse_login_welcome("登录失败，请重试"), "")


class TestIsNetworkError(unittest.TestCase):
    def test_ssl_and_connection(self):
        self.assertTrue(msc._is_network_error(Exception("SSLError: bad handshake")))
        self.assertTrue(msc._is_network_error(Exception("Connection reset by peer")))
        self.assertTrue(msc._is_network_error(Exception("HTTPSConnectionPool timeout")))

    def test_business_error(self):
        self.assertFalse(msc._is_network_error(Exception("登录失败")))
        self.assertFalse(msc._is_network_error(Exception("HTTP 404")))


class TestDoSignin(unittest.TestCase):
    def test_already_signed(self):
        session = FakeSession(SAMPLE_SIGN_ALREADY, SAMPLE_SIGN_PAGE_STATS)
        with quiet():
            result = msc.do_signin("sjs96.com", "deadbeef", session, False)
        self.assertEqual(result["status"], "already")
        self.assertIn("今日已签", result["message"])
        # 已签分支会附带统计
        self.assertIn("连续 49 天", result["message"])

    def test_success_with_reward(self):
        success_xml = ('<?xml version="1.0" encoding="utf-8"?><root><![CDATA['
                       '签到成功，获得随机奖励 3 车票和 2 威望。]]></root>')
        session = FakeSession(success_xml, SAMPLE_SIGN_PAGE_STATS)
        with quiet():
            result = msc.do_signin("sjs96.com", "deadbeef", session, False)
        self.assertEqual(result["status"], "success")
        self.assertIn("车票", result["message"])

    def test_unknown_response_is_fail(self):
        session = FakeSession('<root><![CDATA[抱歉，操作过于频繁]]></root>')
        with quiet():
            result = msc.do_signin("sjs96.com", "deadbeef", session, False)
        self.assertEqual(result["status"], "fail")
        self.assertIn("签到失败", result["message"])


class TestFetchSignStats(unittest.TestCase):
    def test_parse_real_sample(self):
        session = FakeSession(SAMPLE_SIGN_PAGE_STATS)
        with quiet():
            stats = msc.fetch_sign_stats("sjs96.com", session, False)
        self.assertEqual(stats, "连续 49 天  │  Lv.6  │  积分 234  │  累计 91 天")

    def test_missing_fields_returns_empty(self):
        session = FakeSession("<html>没有统计字段</html>")
        with quiet():
            self.assertEqual(msc.fetch_sign_stats("sjs96.com", session, False), "")


class TestRunOneAccount(unittest.TestCase):
    def setUp(self):
        self.account = {"type": "cookie", "cookie": REAL_COOKIE,
                        "label": "账号 1 [Cookie]"}

    def _run(self, hosts, side_effect, run_state=None, fallback=None):
        with mock.patch.object(msc, "_try_signin_with_host", side_effect=side_effect), \
                mock.patch.object(msc, "_fetch_hosts_via_playwright", return_value=fallback or []), \
                quiet():
            return msc.run_one_account(self.account, hosts, {}, False, "", run_state)

    def test_learns_working_host(self):
        state = {}
        calls = []

        def fake(account, host, cache, debug, proxy):
            calls.append(host)
            # 域名不可达时真实契约是返回 None（_try_signin_with_host 的约定）
            return _ok(host) if host == "b.com" else None

        result = self._run(["a.com", "b.com", "c.com"], fake, state)
        self.assertTrue(result["success"])
        self.assertEqual(result["host"], "b.com")
        self.assertEqual(state["preferred_host"], "b.com")
        self.assertEqual(calls, ["a.com", "b.com"])

    def test_preferred_host_tried_first(self):
        calls = []

        def fake(account, host, cache, debug, proxy):
            calls.append(host)
            return _ok(host)

        state = {"preferred_host": "c.com", "playwright_tried": False, "allow_fallback": False}
        result = self._run(["a.com", "b.com", "c.com"], fake, state)
        self.assertTrue(result["success"])
        self.assertEqual(calls, ["c.com"])

    def test_login_failure_circuit_breaker(self):
        """同一账号连续 2 次登录失败即中止，不再遍历第三个域名。"""
        calls = []

        def fake(account, host, cache, debug, proxy):
            calls.append(host)
            return _login_failed(host)

        state = {"allow_fallback": False}
        result = self._run(["a.com", "b.com", "c.com"], fake, state)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "login_aborted")
        self.assertEqual(len(calls), msc.MAX_LOGIN_FAILURES)
        self.assertEqual(len(calls), 2)

    def test_business_failure_stops_immediately(self):
        """Cookie 失效属业务失败，换域名无意义，只试一个域名。"""
        calls = []

        def fake(account, host, cache, debug, proxy):
            calls.append(host)
            return msc._result(False, "cookie_invalid", "❌ Cookie 已失效", "label", host)

        result = self._run(["a.com", "b.com"], fake, {"allow_fallback": False})
        self.assertEqual(result["reason"], "cookie_invalid")
        self.assertEqual(calls, ["a.com"])

    def test_playwright_fallback_used_when_all_hosts_fail(self):
        calls = []

        def fake(account, host, cache, debug, proxy):
            calls.append(host)
            return _ok(host) if host == "new.com" else None

        state = {"allow_fallback": True}
        result = self._run(["a.com"], fake, state, fallback=["new.com"])
        self.assertTrue(result["success"])
        self.assertEqual(result["host"], "new.com")
        self.assertEqual(calls, ["a.com", "new.com"])
        self.assertTrue(state["playwright_tried"])

    def test_playwright_fallback_skipped_when_disabled(self):
        """手动锁定 SIJISHE_HOST 时不应触发 Playwright 兜底。"""
        def fake(account, host, cache, debug, proxy):
            return None

        with mock.patch.object(msc, "_fetch_hosts_via_playwright") as pw:
            state = {"allow_fallback": False}
            with mock.patch.object(msc, "_try_signin_with_host", side_effect=fake), quiet():
                result = msc.run_one_account(self.account, ["a.com"], {}, False, "", state)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "host_unreachable")
        pw.assert_not_called()

    def test_all_hosts_failed_result(self):
        def fake(account, host, cache, debug, proxy):
            return None

        result = self._run(["a.com"], fake, {"allow_fallback": False})
        self.assertEqual(result["reason"], "host_unreachable")


class TestCookieCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sjs_test_")
        self.cache_file = Path(self.tmpdir) / "xsijishe_cookie.json"
        self._patch = mock.patch.object(msc, "COOKIE_FILE", self.cache_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_write_and_read_roundtrip(self):
        with quiet():
            msc.write_cookie_cache("a@mail.com", "sid=1")
            msc.write_cookie_cache("b@mail.com", "sid=2")
        cache = msc.read_cookie_cache()
        self.assertEqual(set(cache.keys()), {"a@mail.com", "b@mail.com"})
        self.assertEqual(cache["a@mail.com"]["cookies"], "sid=1")
        data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(data["site"], "司机社")
        self.assertIn("update_time", data)

    def test_remove_single_account_keeps_others(self):
        with quiet():
            msc.write_cookie_cache("a@mail.com", "sid=1")
            msc.write_cookie_cache("b@mail.com", "sid=2")
            msc.remove_cached_cookie("a@mail.com")
        cache = msc.read_cookie_cache()
        self.assertEqual(set(cache.keys()), {"b@mail.com"})

    def test_remove_unknown_account_is_noop(self):
        with quiet():
            msc.write_cookie_cache("a@mail.com", "sid=1")
            msc.remove_cached_cookie("zzz@mail.com")
        self.assertEqual(set(msc.read_cookie_cache().keys()), {"a@mail.com"})

    def test_remove_when_no_cache_file(self):
        with quiet():
            msc.remove_cached_cookie("a@mail.com")  # 不应抛异常
        self.assertFalse(self.cache_file.exists())

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(msc.read_cookie_cache(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
