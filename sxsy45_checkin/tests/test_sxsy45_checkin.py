# -*- coding: utf-8 -*-
"""sxsy45_checkin 单元测试（纯函数 + mock 分支判定，不发真实请求）"""
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


# ---------- 实测样本（2026-09-29 真机抓取） ----------
SAMPLE_JD_SIGN_HREF = (
    '<a id="JD_sign" href="plugin.php?id=k_misign:sign&operation=qiandao'
    '&formhash=9a17bb38&format=text" onclick="ajaxget(this.href, \'k_misign_sign_tip\','
    ' \'\', \'\', \'\', \'kMisignPcAfterSign();\');return false;" class="btn J_chkitot">'
)
SAMPLE_STATS = ('<input type="hidden" id="lxdays" value="1" />'
                '<input type="hidden" id="lxlevel" value="2" />'
                '<input type="hidden" id="lxreward" value="2" />'
                '<input type="hidden" id="lxtdays" value="5" />')
SAMPLE_MATH_Q_ADD = '<div>var q="签到验证：9 + 15 = ?";var a=prompt(q,"");</div>'
SAMPLE_MATH_Q_SUB = '<div>var q="签到验证：12 - 2 = ?";var a=prompt(q,"");</div>'
SAMPLE_MATH_Q_MUL = '<div>var q="签到验证：7 × 6 = ?";var a=prompt(q,"");</div>'
SAMPLE_RESULT_XML = '<?xml version="1.0" encoding="utf-8"?>\r\n<root><![CDATA[已签到]]></root>'


class TestSolveMath(unittest.TestCase):
    """算术验证题解析（k_misign mathverify）"""

    def test_add(self):
        self.assertEqual(msc.solve_math("签到验证：9 + 15 = ?"), 24)

    def test_sub(self):
        self.assertEqual(msc.solve_math("签到验证：12 - 2 = ?"), 10)

    def test_sub_negative(self):
        self.assertEqual(msc.solve_math("签到验证：2 - 5 = ?"), -3)

    def test_mul_fullwidth(self):
        self.assertEqual(msc.solve_math("签到验证：7 × 6 = ?"), 42)

    def test_div_even(self):
        self.assertEqual(msc.solve_math("签到验证：8 ÷ 2 = ?"), 4)

    def test_div_uneven_returns_float(self):
        self.assertEqual(msc.solve_math("签到验证：7 ÷ 2 = ?"), 3.5)

    def test_div_by_zero(self):
        self.assertIsNone(msc.solve_math("签到验证：5 ÷ 0 = ?"))

    def test_garbage_returns_none(self):
        self.assertIsNone(msc.solve_math("签到验证：abc = ?"))
        self.assertIsNone(msc.solve_math(""))
        self.assertIsNone(msc.solve_math(None))


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
        stats = msc.fetch_sign_stats(SAMPLE_STATS)
        self.assertIn("连续 1 天", stats)
        self.assertIn("Lv.2", stats)
        self.assertIn("积分+2", stats)
        self.assertIn("累计 5 天", stats)

    def test_empty(self):
        self.assertEqual(msc.fetch_sign_stats("<html></html>"), "")


class TestMathQuestionExtraction(unittest.TestCase):
    """从实测 XML 返回中提取算术题再求解的完整链路"""

    def test_add_roundtrip(self):
        q = msc.re.search(r'var q="([^"]*)"', SAMPLE_MATH_Q_ADD).group(1)
        self.assertEqual(msc.solve_math(q), 24)

    def test_sub_roundtrip(self):
        q = msc.re.search(r'var q="([^"]*)"', SAMPLE_MATH_Q_SUB).group(1)
        self.assertEqual(msc.solve_math(q), 10)

    def test_mul_roundtrip(self):
        q = msc.re.search(r'var q="([^"]*)"', SAMPLE_MATH_Q_MUL).group(1)
        self.assertEqual(msc.solve_math(q), 42)


class TestCdataExtraction(unittest.TestCase):
    def test_result_xml(self):
        cdata = msc.re.search(r"<!\[CDATA\[(.*?)\]\]>", SAMPLE_RESULT_XML, msc.re.S)
        self.assertEqual(cdata.group(1).strip(), "已签到")


class TestDoSigninBranches(unittest.TestCase):
    """do_signin 状态判定分支（mock 网络层）"""

    def _make_session(self):
        s = mock.MagicMock()
        s._timeout = 30
        return s

    def test_already_signed(self):
        """已签页面：无 JD_sign、有 lxdays -> signed_today"""
        with mock.patch.object(msc, "_get_sign_page", return_value=(SAMPLE_STATS, "")):
            r = msc.do_signin(self._make_session())
        self.assertEqual(r["state"], "signed_today")
        self.assertEqual(r["message"], "今日已签到")
        self.assertIn("累计 5 天", r["stats"])

    def test_cookie_dead(self):
        """被踢回登录页：无 JD_sign、无统计、含登录链接 -> failed 失效"""
        html = '<a href="member.php?mod=logging&action=login">登录</a>'
        with mock.patch.object(msc, "_get_sign_page", return_value=(html, "")):
            r = msc.do_signin(self._make_session())
        self.assertEqual(r["state"], "failed")
        self.assertIn("登录态失效", r["message"])

    def test_unknown_page(self):
        """无 JD_sign 无统计无登录链接 -> failed 结构变化"""
        with mock.patch.object(msc, "_get_sign_page", return_value=("<html>x</html>", "")):
            r = msc.do_signin(self._make_session())
        self.assertEqual(r["state"], "failed")
        self.assertIn("无签到按钮", r["message"])

    def test_success_flow(self):
        """未签 -> 出题 -> 答对 -> 回查 JD_sign 消失 -> success"""
        session = self._make_session()
        resp_q = mock.MagicMock(text=SAMPLE_MATH_Q_ADD, status_code=200)
        resp_ok = mock.MagicMock(text=SAMPLE_RESULT_XML, status_code=200)
        # 注意：第一次页面要同时含 JD_sign 完整 href（含 formhash 提取）
        sign_pages = [(SAMPLE_JD_SIGN_HREF + SAMPLE_STATS, ""), (SAMPLE_STATS, "")]
        with mock.patch.object(msc, "_get_sign_page", side_effect=sign_pages), \
             mock.patch.object(msc, "get_retry", side_effect=[(resp_q, ""), (resp_ok, "")]):
            r = msc.do_signin(session)
        self.assertEqual(r["state"], "success")
        self.assertIn("签到成功", r["message"])
        self.assertIn("累计 5 天", r["stats"])

    def test_wrong_answer_still_signed(self):
        """答错（回查 JD_sign 仍在）-> failed"""
        session = self._make_session()
        resp_q = mock.MagicMock(text=SAMPLE_MATH_Q_SUB, status_code=200)
        resp_re = mock.MagicMock(text='<div>var q="签到验证：3 + 4 = ?";</div>', status_code=200)
        page_with_btn = SAMPLE_JD_SIGN_HREF + SAMPLE_STATS
        with mock.patch.object(msc, "_get_sign_page",
                               side_effect=[(page_with_btn, ""), (page_with_btn, "")]), \
             mock.patch.object(msc, "get_retry", side_effect=[(resp_q, ""), (resp_re, "")]):
            r = msc.do_signin(session)
        self.assertEqual(r["state"], "failed")
        self.assertIn("签到未生效", r["message"])

    def test_no_math_question(self):
        """首次请求无算术题（站点关闭验证/改版）-> failed 并带原始返回"""
        session = self._make_session()
        resp_plain = mock.MagicMock(text="<root><![CDATA[非法请求]]></root>", status_code=200)
        page_with_btn = SAMPLE_JD_SIGN_HREF + SAMPLE_STATS
        with mock.patch.object(msc, "_get_sign_page",
                               side_effect=[(page_with_btn, ""), (page_with_btn, "")]), \
             mock.patch.object(msc, "get_retry", return_value=(resp_plain, "")):
            r = msc.do_signin(session)
        self.assertEqual(r["state"], "failed")
        self.assertIn("未返回算术验证题", r["message"])

    def test_sign_page_unavailable(self):
        """签到页重试后仍不可用 -> failed 且消息带错误原因"""
        with mock.patch.object(msc, "_get_sign_page",
                               return_value=("", "HTTP 522「sxsy45.com | 522: Connection timed out」")):
            r = msc.do_signin(self._make_session())
        self.assertEqual(r["state"], "failed")
        self.assertIn("签到页请求失败", r["message"])
        self.assertIn("522", r["message"])

    def test_recheck_unavailable_trusts_result_text(self):
        """签到成功但回查页 522：接口文案含'已签到'才判成功，并标注未复核"""
        session = self._make_session()
        resp_q = mock.MagicMock(text=SAMPLE_MATH_Q_ADD, status_code=200)
        resp_ok = mock.MagicMock(text=SAMPLE_RESULT_XML, status_code=200)
        page_with_btn = SAMPLE_JD_SIGN_HREF + SAMPLE_STATS
        with mock.patch.object(msc, "_get_sign_page",
                               side_effect=[(page_with_btn, ""), ("", "HTTP 522")]), \
             mock.patch.object(msc, "get_retry", side_effect=[(resp_q, ""), (resp_ok, "")]):
            r = msc.do_signin(session)
        self.assertEqual(r["state"], "success")
        self.assertIn("回查页暂不可用", r["message"])

    def test_recheck_unavailable_unknown_result_fails(self):
        """回查失败且接口文案无法确认 -> failed，不能误报成功"""
        session = self._make_session()
        resp_q = mock.MagicMock(text=SAMPLE_MATH_Q_ADD, status_code=200)
        resp_plain = mock.MagicMock(text="<root><![CDATA[非法请求]]></root>", status_code=200)
        page_with_btn = SAMPLE_JD_SIGN_HREF + SAMPLE_STATS
        with mock.patch.object(msc, "_get_sign_page",
                               side_effect=[(page_with_btn, ""), ("", "HTTP 522")]), \
             mock.patch.object(msc, "get_retry", side_effect=[(resp_q, ""), (resp_plain, "")]):
            r = msc.do_signin(session)
        self.assertEqual(r["state"], "failed")
        self.assertIn("未确认", r["message"])


class TestJdSignFormhashExtraction(unittest.TestCase):
    def test_real_href(self):
        m = msc.re.search(
            r'id="JD_sign"\s+href="plugin\.php\?id=k_misign:sign&operation=qiandao'
            r'&formhash=([a-f0-9]+)&format=text"', SAMPLE_JD_SIGN_HREF)
        self.assertEqual(m.group(1), "9a17bb38")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
