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
        with mock.patch.object(msc, "_get_sign_page", return_value=SAMPLE_STATS):
            r = msc.do_signin(self._make_session())
        self.assertEqual(r["state"], "signed_today")
        self.assertEqual(r["message"], "今日已签到")
        self.assertIn("累计 5 天", r["stats"])

    def test_cookie_dead(self):
        """被踢回登录页：无 JD_sign、无统计、含登录链接 -> failed 失效"""
        html = '<a href="member.php?mod=logging&action=login">登录</a>'
        with mock.patch.object(msc, "_get_sign_page", return_value=html):
            r = msc.do_signin(self._make_session())
        self.assertEqual(r["state"], "failed")
        self.assertIn("登录态失效", r["message"])

    def test_unknown_page(self):
        """无 JD_sign 无统计无登录链接 -> failed 结构变化"""
        with mock.patch.object(msc, "_get_sign_page", return_value="<html>x</html>"):
            r = msc.do_signin(self._make_session())
        self.assertEqual(r["state"], "failed")
        self.assertIn("无签到按钮", r["message"])

    def test_success_flow(self):
        """未签 -> 出题 -> 答对 -> 回查 JD_sign 消失 -> success"""
        session = self._make_session()
        resp_q = mock.MagicMock(text=SAMPLE_MATH_Q_ADD)
        resp_ok = mock.MagicMock(text=SAMPLE_RESULT_XML)
        # 签到页第一次有 JD_sign，回查时无 JD_sign（已签状态带统计）
        sign_pages = [SAMPLE_STATS.replace('value="1"', 'value="2"') + SAMPLE_JD_SIGN_HREF + 'formhash=9a17bb38',
                      SAMPLE_STATS]
        # 注意：第一次页面要同时含 JD_sign 完整 href（含 formhash 提取）
        sign_pages[0] = SAMPLE_JD_SIGN_HREF + SAMPLE_STATS
        with mock.patch.object(msc, "_get_sign_page", side_effect=sign_pages), \
             mock.patch.object(session, "get", side_effect=[resp_q, resp_ok]):
            r = msc.do_signin(session)
        self.assertEqual(r["state"], "success")
        self.assertIn("签到成功", r["message"])
        self.assertIn("累计 5 天", r["stats"])

    def test_wrong_answer_still_signed(self):
        """答错（回查 JD_sign 仍在）-> failed"""
        session = self._make_session()
        resp_q = mock.MagicMock(text=SAMPLE_MATH_Q_SUB)
        resp_re = mock.MagicMock(text='<div>var q="签到验证：3 + 4 = ?";</div>')
        page_with_btn = SAMPLE_JD_SIGN_HREF + SAMPLE_STATS
        with mock.patch.object(msc, "_get_sign_page", side_effect=[page_with_btn, page_with_btn]), \
             mock.patch.object(session, "get", side_effect=[resp_q, resp_re]):
            r = msc.do_signin(session)
        self.assertEqual(r["state"], "failed")
        self.assertIn("签到未生效", r["message"])

    def test_no_math_question(self):
        """首次请求无算术题（站点关闭验证/改版）-> failed 并带原始返回"""
        session = self._make_session()
        resp_plain = mock.MagicMock(text="<root><![CDATA[非法请求]]></root>")
        page_with_btn = SAMPLE_JD_SIGN_HREF + SAMPLE_STATS
        with mock.patch.object(msc, "_get_sign_page", side_effect=[page_with_btn, page_with_btn]), \
             mock.patch.object(session, "get", return_value=resp_plain):
            r = msc.do_signin(session)
        self.assertEqual(r["state"], "failed")
        self.assertIn("未返回算术验证题", r["message"])


class TestJdSignFormhashExtraction(unittest.TestCase):
    def test_real_href(self):
        m = msc.re.search(
            r'id="JD_sign"\s+href="plugin\.php\?id=k_misign:sign&operation=qiandao'
            r'&formhash=([a-f0-9]+)&format=text"', SAMPLE_JD_SIGN_HREF)
        self.assertEqual(m.group(1), "9a17bb38")


if __name__ == "__main__":
    unittest.main(verbosity=2)
