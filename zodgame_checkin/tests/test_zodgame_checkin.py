# -*- coding: utf-8 -*-
"""zodgame_checkin 单元测试(标准库 unittest,无第三方依赖)

覆盖:配置解析 / 代理回退 / 页面判定(Cookie·CF·改版) / 签到结果各分支 /
BUX 任务解析与状态判定 / 广告观看秒数解析 / do=check 成败判定 /
账号级分类 / 汇总标题与退出码。

fixture 说明:
  · 标注「真实报文」的取自 2026-10-10 现场抓包,已脱敏(昵称/uid/其余用户名全部替换);
  · 标注「模板推导」的按 zodgame 站上 jnbux 插件模板 source/plugin/jnbux/template/*.htm
    的渲染形状构造,因为抓包当天该账号广告任务已耗尽,拿不到真实任务行。
真实网络请求不进单测。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zodgame_checkin as zg  # noqa: E402

# ============================ fixture ============================

# 签到页(真实报文,已脱敏):已签到状态
SIGN_PAGE_SIGNED = """<html><head>
<title>每日签到 -  ZodGame论坛 -  Powered by Discuz!</title>
</head><body>
<div id="um"><strong class="vwmy"><a href="home.php?mod=space&amp;uid=1000001" target="_blank">测试用户</a></strong></div>
<input type="hidden" name="formhash" value="0123abcd" />
<h1 class="mt">您今天已经签到过了或者签到时间还未开始</h1>
<p><font color="#FF0000"><b>测试用户</b></font> , 您累计已签到: <b>42</b> 天</p>
</body></html>"""

# 签到页(真实报文,已脱敏):未签到状态,只有 formhash 与导航
SIGN_PAGE_FRESH = """<html><head>
<title>每日签到 -  ZodGame论坛 -  Powered by Discuz!</title>
</head><body>
<div id="um"><strong class="vwmy"><a href="home.php?mod=space&amp;uid=1000001" target="_blank">测试用户</a></strong></div>
<input type="hidden" name="formhash" value="0123abcd" />
<form method="post"><input type="hidden" name="formhash" value="0123abcd" /></form>
</body></html>"""

# 登录页(真实报文,已脱敏)
LOGIN_PAGE = """<html><head>
<title>登录 -  ZodGame论坛 -  Powered by Discuz!</title>
</head><body><form id="loginform"></form></body></html>"""

# Cloudflare 挑战页(按 CF 实际输出形状构造)
CF_CHALLENGE_PAGE = """<!DOCTYPE html><html><head>
<title>Just a moment...</title>
<script>window._cf_chl_opt = {cvId: '3'};</script>
</head><body>Verifying you are human.</body></html>"""

# 签到接口返回(真实报文):已签到
SIGN_XML_DUPLICATE = """<?xml version="1.0" encoding="utf-8"?>
<root><![CDATA[<script type="text/javascript" reload="1">
setTimeout("hideWindow('qwindow')", 3000);
</script>
<div class="f_c">
<h3 class="flb">
<em id="return_win">签到提示</em>
<span>
<a href="javascript:;" class="flbc" onclick="hideWindow('qwindow')" title="关闭">关闭</a></span>
</h3>
<div class="c">
您今日已经签到，请明天再来！ </div>
</div>
]]></root>"""

# 签到接口返回(按 dsu_paulsign 文案构造,真实成功报文未抓到)
SIGN_XML_SUCCESS = """<?xml version="1.0" encoding="utf-8"?>
<root><![CDATA[<div class="f_c"><h3 class="flb"><em>签到提示</em></h3>
<div class="c">恭喜你签到成功!获得随机奖励 酱油 3 瓶. </div>
</div>]]></root>"""

SIGN_XML_SUCCESS_BARE = """<?xml version="1.0" encoding="utf-8"?>
<root><![CDATA[<div class="c">恭喜你签到成功!</div>]]></root>"""

SIGN_XML_FAILED = """<?xml version="1.0" encoding="utf-8"?>
<root><![CDATA[<div class="f_c"><div class="c">
签到失败，您所在的用户组没有签到权限 </div></div>]]></root>"""

# BUX 无任务区块(真实报文,已脱敏)
BUX_BLOCK_NO_TASK = """<h2>任务列表</h2>
            </div>
            <div class="bm_c">
<table width="100%" border="0" cellspacing="0" cellpadding="5">
  <tr>
    <td>ID</td>
    <td>广告标题</td>
    <td>奖励</td>
        <td>时间</td>
    <td>完成/剩余</td>
    <td>操作</td>
  </tr>
    <tr>
    <td colspan="7"><div align="center">暂时没有广告任务可执行。</div></td>
  </tr>
  </table>"""

# BUX 未加入系统的加入链接(模板推导,source/plugin/jnbux/template/jnbux.htm)
# 同时带上 Discuz 页脚退出链接,它提供 formhash(自动加入需要)
BUX_BLOCK_NOT_JOINED = """<div id="um"><strong class="vwmy"><a href="home.php?mod=space&amp;uid=1000001">测试用户</a></strong></div>
<a href="member.php?mod=logging&amp;action=logout&amp;formhash=0123abcd">退出</a>
<h2>资料</h2><div class="bm_c">
<ul class="xl xl2 cl" style="line-height:10px;">
<li style="width:100%; line-height:14px;"><a href="javascript:;" onclick="showDialog('确定要加入吗?', 'confirm', '', function(){\tshowWindow('join', 'plugin.php?id=jnbux:jnbux&amp;do=join&amp;formhash=0123abcd');return false;},1)"><b><font color="#FF0000">开始参与任务</font></b></a></li>
</ul></div>"""

# BUX 已加入 + 点币余额(真实报文,已脱敏)
BUX_BLOCK_JOINED = """<h2>资料</h2><div class="bm_c">
<ul class="xl xl2 cl" style="line-height:10px;">
<li style="width:100%;line-height:14px;">点币: 8.00 <a href="javascript:;" onclick="showWindow('cashout', 'plugin.php?id=jnbux:jnbux&amp;do=cashout')"><font color="#FF0000"><b>兑出</b></font></a></li>
</ul></div>"""

# 任务行(模板推导):两行,倒计时 5 秒 / 30 秒,奖励 2 / 3 点币
BUX_TASK_ROWS = """
<script type="text/javascript">
function openNewWindow101() {
window.open("plugin.php?id=jnbux:jnbux&do=click&clickid=101&timeo=1787950000&onlyhash=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&formhash=0123abcd&userid=1000001", "newwindow")
}</script>
  <tr>
    <td>101</td>
    <td><a href="javascript:;" onclick="openNewWindow101();showDialog('提示', 'confirm', '', function(){	showWindow('check', 'plugin.php?id=jnbux:jnbux&do=check&clickid=101&userid=1000001&formhash=0123abcd&page=1');return false;},1)">示例广告位</a></td>
    <td>2 点币</td>
    <td>5 秒</td>
    <td>0/1</td>
    <td><a href="javascript:;" onclick="openNewWindow101();showDialog('提示', 'confirm', '', function(){	showWindow('check', 'plugin.php?id=jnbux:jnbux&do=check&clickid=101&userid=1000001&formhash=0123abcd&page=1');return false;},1)">参与任务</a></td>
  </tr>
<script type="text/javascript">
function openNewWindow202() {
window.open("plugin.php?id=jnbux:jnbux&amp;do=click&amp;clickid=202&amp;timeo=1787950100&amp;onlyhash=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&amp;formhash=0123abcd&amp;userid=1000001", "newwindow")
}</script>
  <tr>
    <td>202</td>
    <td><a href="javascript:;" onclick="openNewWindow202();showDialog('提示', 'confirm', '', function(){	showWindow('check', 'plugin.php?id=jnbux:jnbux&do=check&clickid=202&userid=1000001&formhash=0123abcd&page=1');return false;},1)">第二个广告位</a></td>
    <td>3 点币</td>
    <td>30 秒</td>
    <td>0/1</td>
    <td><a href="javascript:;" onclick="openNewWindow202();showDialog('提示', 'confirm', '', function(){	showWindow('check', 'plugin.php?id=jnbux:jnbux&do=check&clickid=202&userid=1000001&formhash=0123abcd&page=1');return false;},1)">参与任务</a></td>
  </tr>
"""

# 任务列表区块:有任务行(模板推导)
BUX_BLOCK_WITH_TASKS = "<h2>任务列表</h2>\n<div class=\"bm_c\">\n<table>\n" + BUX_TASK_ROWS + "\n</table>"

# 任务列表区块:有区块但无链接也无「无任务」文案 → 疑似改版
BUX_BLOCK_STRUCTURE_CHANGED = """<h2>任务列表</h2>
<div class="bm_c"><table><tr><td>ID</td></tr><tr><td>42</td><td>某广告</td></tr></table>"""

# 广告页(模板推导):click.htm 的倒计时脚本
BUX_CLICK_PAGE = """<style type="text/css"><!-- td.container { height: 500px;} --></style>
<META HTTP-EQUIV="refresh" CONTENT="5;URL=plugin.php?id=jnbux:jnbux&do=update&clickid=101">
<script language="javascript">
var count=5;
var counter=setInterval(timer, 1000);
</script>
<div class="jnbux_hd">广告加载中 <span id="timer"></span></div>"""

BUX_UPDATE_PAGE = '<div class="jnbux_hd">奖励已发放</div><div class="jnbux_container"></div>'

# do=check 校验响应(真实报文,已脱敏;成功分支按参考实现的文案构造)
CHECK_XML_FAILED = """<?xml version="1.0" encoding="utf-8"?>
<root><![CDATA[<h3 class="flb"><em>提示信息</em></h3>
<div class="c altw">
<div class="alert_error"><script type="text/javascript" reload="1">if(typeof errorhandle_check=='function') {errorhandle_check('检查失败, 您可能并未完成该任务, 或者您并未加入我们的BUX广告点击赚积分系统的行列, 您可以先在右侧的资料栏内点击加入。', {});}hideWindow('check');showDialog('检查失败, 您可能并未完成该任务, 或者您并未加入我们的BUX广告点击赚积分系统的行列, 您可以先在右侧的资料栏内点击加入。', 'alert', null, null, 0, null, null, null, null, null, null);</script></div>
</div>
]]></root>"""

CHECK_XML_SUCCESS = """<?xml version="1.0" encoding="utf-8"?>
<root><![CDATA[<div class="c"><p>检查成功, 积分已经加入您的帐户中</p></div>]]></root>"""


class FakeResp:
    """极简 Response 替身。"""

    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


def _res(ok, state=zg.CAT_OK, **kw):
    """构造 run_one_account 的返回替身,供 main 退出码测试。"""
    base = {"label": "账号1", "username": None, "ok": ok, "state": state,
            "sign": "签到成功", "sign_ok": ok, "bux": "今日无广告任务",
            "bux_ok": ok, "points": 8.0, "message": "" if ok else "失败", "flags": {}}
    base.update(kw)
    return base


# ============================ 工具函数 ============================

class TestUtils(unittest.TestCase):
    def test_parse_cookies_normal(self):
        self.assertEqual(zg.parse_cookies("a=1; b=2"), {"a": "1", "b": "2"})

    def test_parse_cookies_value_with_equals(self):
        # Cookie 值里的 = 不能被切掉
        self.assertEqual(zg.parse_cookies("k=a=b=c"), {"k": "a=b=c"})

    def test_parse_cookies_skips_garbage(self):
        self.assertEqual(zg.parse_cookies("novalue; a=1; "), {"a": "1"})

    def test_parse_cookies_strips_spaces(self):
        self.assertEqual(zg.parse_cookies(" a = 1 ; b = 2 "), {"a": "1", "b": "2"})

    def test_mask_cookie_keeps_key_names_only(self):
        masked = zg.mask_cookie("k1=v1; k2=v2; k3=v3; k4=v4; k5=v5")
        self.assertIn("5 项", masked)
        self.assertIn("k1", masked)
        self.assertNotIn("v1", masked)

    def test_env_bool(self):
        with mock.patch.dict(os.environ, {"T_FLAG": "TRUE"}, clear=False):
            self.assertTrue(zg.env_bool("T_FLAG", False))
        with mock.patch.dict(os.environ, {"T_FLAG": "0"}, clear=False):
            self.assertFalse(zg.env_bool("T_FLAG", True))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(zg.env_bool("MISSING", True))
            self.assertFalse(zg.env_bool("MISSING", False))

    def test_strip_tags_removes_script_and_cdata(self):
        text = zg.strip_tags(SIGN_XML_DUPLICATE)
        self.assertIn("您今日已经签到", text)
        self.assertNotIn("setTimeout", text)
        self.assertNotIn("CDATA", text)
        self.assertNotIn("<", text)

    def test_strip_tags_unescapes_entities(self):
        self.assertEqual(zg.strip_tags("<td>a&amp;b</td>"), "a&b")

    def test_strip_tags_keep_script_retains_inner_text(self):
        # Discuz 浮窗把成败文案放在 script 里,keep_script=True 必须保留
        raw = "<div><script>errorhandle_check('检查失败, 未完成任务');</script></div>"
        self.assertNotIn("检查失败", zg.strip_tags(raw))
        self.assertIn("检查失败", zg.strip_tags(raw, keep_script=True))

    def test_extract_formhash_from_input(self):
        self.assertEqual(zg.extract_formhash(SIGN_PAGE_SIGNED), "0123abcd")

    def test_extract_formhash_from_logout_link(self):
        html = '<a href="member.php?mod=logging&amp;action=logout&amp;formhash=deadbeef">退出</a>'
        self.assertEqual(zg.extract_formhash(html), "deadbeef")

    def test_extract_formhash_absent(self):
        self.assertIsNone(zg.extract_formhash("<html>nothing</html>"))

    def test_extract_username_discuz_nav(self):
        # vwmy 是 Discuz 通用用户导航,签到页与 BUX 页都在,游客页没有
        self.assertEqual(zg.extract_username(SIGN_PAGE_FRESH), "测试用户")

    def test_extract_username_bux_nickname(self):
        html = "<li>昵称: 测试用户 (ID:1000001)</li>"
        self.assertEqual(zg.extract_username(html), "测试用户")

    def test_extract_username_paulsign_line(self):
        html = '<p><font color="#FF0000"><b>测试用户</b></font> , 您累计已签到: <b>42</b> 天</p>'
        self.assertEqual(zg.extract_username(html), "测试用户")

    def test_extract_username_guest_page(self):
        self.assertIsNone(zg.extract_username("<html><body>游客页面</body></html>"))


class TestProxy(unittest.TestCase):
    def test_own_proxy_wins(self):
        env = {"ZODGAME_PROXY": "http://127.0.0.1:1080", "HTTPS_PROXY": "http://other:1"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(zg.resolve_proxy(), ("http://127.0.0.1:1080", "ZODGAME_PROXY"))

    def test_fallback_to_global(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://global:7890"}, clear=True):
            self.assertEqual(zg.resolve_proxy(), ("http://global:7890", "HTTPS_PROXY"))

    def test_fallback_lowercase_all_proxy(self):
        with mock.patch.dict(os.environ, {"all_proxy": "socks5://x:1"}, clear=True):
            proxy, source = zg.resolve_proxy()
        self.assertEqual(proxy, "socks5://x:1")
        # Windows 环境变量大小写不敏感,来源名可能是任一写法
        self.assertEqual(source.lower(), "all_proxy")

    def test_direct_when_nothing_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(zg.resolve_proxy(), (None, None))


# ============================ 页面判定 ============================

class TestPageVerdict(unittest.TestCase):
    def test_cf_by_header(self):
        self.assertTrue(zg.is_cf_challenge(FakeResp("x", 403, {"cf-mitigated": "challenge"})))

    def test_cf_by_marker(self):
        self.assertTrue(zg.is_cf_challenge(FakeResp(CF_CHALLENGE_PAGE, 403)))

    def test_cf_by_403_and_cloudflare_server(self):
        self.assertTrue(zg.is_cf_challenge(FakeResp("blocked", 403, {"server": "cloudflare"})))

    def test_plain_403_without_cf_is_not_cf(self):
        self.assertFalse(zg.is_cf_challenge(FakeResp("forbidden", 403, {"server": "nginx"})))

    def test_normal_page_is_not_cf(self):
        self.assertFalse(zg.is_cf_challenge(FakeResp(SIGN_PAGE_SIGNED, 200)))

    def test_classify_ok(self):
        self.assertEqual(zg.classify_sign_page(FakeResp(SIGN_PAGE_SIGNED)), "ok")

    def test_classify_invalid(self):
        self.assertEqual(zg.classify_sign_page(FakeResp(LOGIN_PAGE)), "invalid")

    def test_classify_cf(self):
        self.assertEqual(zg.classify_sign_page(FakeResp(CF_CHALLENGE_PAGE, 403)), "cf")

    def test_classify_unknown(self):
        self.assertEqual(zg.classify_sign_page(FakeResp("<title>别的页面</title>")), "unknown")


# ============================ 签到 ============================

class TestSignResult(unittest.TestCase):
    def test_duplicate(self):
        r = zg.extract_sign_result(SIGN_XML_DUPLICATE)
        self.assertEqual(r["status"], "duplicate")

    def test_success_with_reward(self):
        r = zg.extract_sign_result(SIGN_XML_SUCCESS)
        self.assertEqual(r["status"], "success")
        self.assertIn("酱油", r["message"])
        self.assertIn("3", r["message"])

    def test_success_without_reward_detail(self):
        r = zg.extract_sign_result(SIGN_XML_SUCCESS_BARE)
        self.assertEqual(r["status"], "success")

    def test_failed(self):
        r = zg.extract_sign_result(SIGN_XML_FAILED)
        self.assertEqual(r["status"], "failed")
        self.assertIn("权限", r["message"])

    def test_unknown(self):
        r = zg.extract_sign_result("<root><![CDATA[<div>无关内容</div>]]></root>")
        self.assertEqual(r["status"], "unknown")

    def test_do_sign_short_circuits_when_already_signed(self):
        # 页面已含已签到特征时不应发 POST
        with mock.patch.object(zg, "http_post") as post:
            r = zg.do_sign(object(), SIGN_PAGE_SIGNED)
        post.assert_not_called()
        self.assertEqual(r["status"], "duplicate")

    def test_do_sign_missing_formhash(self):
        r = zg.do_sign(object(), "<title>每日签到 -</title>没有任何 formhash")
        self.assertEqual(r["status"], "failed")
        self.assertIn("formhash", r["message"])

    def test_do_sign_posts_and_parses(self):
        with mock.patch.object(zg, "http_post", return_value=FakeResp(SIGN_XML_SUCCESS)):
            r = zg.do_sign(object(), SIGN_PAGE_FRESH)
        self.assertEqual(r["status"], "success")


# ============================ BUX 解析 ============================

class TestBuxParse(unittest.TestCase):
    def test_no_tasks_returns_empty(self):
        self.assertEqual(zg.parse_bux_tasks(BUX_BLOCK_NO_TASK), [])

    def test_two_tasks_parsed(self):
        tasks = zg.parse_bux_tasks(BUX_BLOCK_WITH_TASKS)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["clickid"], "101")
        self.assertEqual(tasks[0]["userid"], "1000001")
        self.assertEqual(tasks[0]["formhash"], "0123abcd")
        self.assertEqual(tasks[0]["onlyhash"], "a" * 32)
        self.assertEqual(tasks[0]["seconds"], 5)
        self.assertEqual(tasks[0]["earn"], 2.0)
        self.assertEqual(tasks[1]["clickid"], "202")
        self.assertEqual(tasks[1]["seconds"], 30)
        self.assertEqual(tasks[1]["earn"], 3.0)

    def test_html_escaped_url_is_unescaped(self):
        tasks = zg.parse_bux_tasks(BUX_BLOCK_WITH_TASKS)
        # 第二个任务的 URL 在 HTML 里是 &amp; 转义,解析后必须是裸 &
        self.assertIn("&do=click&", tasks[1]["click_url"])
        self.assertNotIn("&amp;", tasks[1]["click_url"])

    def test_check_url_extracted_from_onclick(self):
        tasks = zg.parse_bux_tasks(BUX_BLOCK_WITH_TASKS)
        self.assertIn("do=check", tasks[0]["check_url"])
        self.assertIn("clickid=101", tasks[0]["check_url"])

    def test_build_update_url(self):
        task = zg.parse_bux_tasks(BUX_BLOCK_WITH_TASKS)[0]
        url = zg._build_update_url(task)
        self.assertIn("do=update", url)
        self.assertIn("clickid=101", url)
        self.assertNotIn("do=click", url)

    def test_build_check_url_constructed_when_missing(self):
        task = {"clickid": "5", "userid": "9", "formhash": "abcd1234", "check_url": None}
        url = zg._build_check_url(task)
        self.assertIn("do=check", url)
        self.assertIn("clickid=5", url)
        self.assertIn("inajax=1", url)

    def test_build_check_url_prefers_site_url(self):
        task = {"clickid": "5", "userid": "9", "formhash": "abcd1234",
                "check_url": "plugin.php?id=jnbux:jnbux&do=check&clickid=5"}
        url = zg._build_check_url(task)
        self.assertTrue(url.startswith(zg.BASE_URL + "/plugin.php"))
        self.assertIn("inajax=1", url)


class TestBuxState(unittest.TestCase):
    def test_no_task_block(self):
        st = zg.detect_bux_state("<html>没有任务列表</html>")
        self.assertFalse(st["has_task_block"])

    def test_no_task_text_detected(self):
        st = zg.detect_bux_state(BUX_BLOCK_NO_TASK)
        self.assertTrue(st["has_task_block"])
        self.assertTrue(st["no_task_text"])

    def test_not_joined_detected(self):
        st = zg.detect_bux_state(BUX_BLOCK_NOT_JOINED)
        self.assertFalse(st["joined"])

    def test_joined_detected(self):
        st = zg.detect_bux_state(BUX_BLOCK_JOINED)
        self.assertTrue(st["joined"])

    def test_structure_changed_not_treated_as_no_task(self):
        st = zg.detect_bux_state(BUX_BLOCK_STRUCTURE_CHANGED)
        self.assertTrue(st["has_task_block"])
        self.assertFalse(st["no_task_text"])

    def test_paginated_detected(self):
        block = BUX_BLOCK_WITH_TASKS.replace(
            "</table>", '<tr><td colspan="6"><div class="pg"><a href="?page=2">2</a></div></td></tr></table>')
        self.assertTrue(zg.detect_bux_state(block)["paginated"])


class TestJudgeCheckResponse(unittest.TestCase):
    def test_success(self):
        status, msg = zg.judge_check_response(CHECK_XML_SUCCESS)
        self.assertEqual(status, "success")
        self.assertIn("检查成功", msg)

    def test_failed_from_real_report(self):
        # 真实报文:失败文案藏在 <script> 里,必须仍能判定
        status, msg = zg.judge_check_response(CHECK_XML_FAILED)
        self.assertEqual(status, "failed")
        self.assertIn("检查失败", msg)

    def test_unknown(self):
        status, _ = zg.judge_check_response("<root><![CDATA[<div>无关</div>]]></root>")
        self.assertEqual(status, "unknown")


class TestWatchSeconds(unittest.TestCase):
    def test_override_env_wins(self):
        with mock.patch.dict(os.environ, {"ZODGAME_AD_SECONDS": "7"}, clear=False):
            self.assertEqual(zg._watch_seconds({"seconds": 30}, FakeResp(BUX_CLICK_PAGE)), 7)

    def test_from_click_page_countdown(self):
        with mock.patch.dict(os.environ, {"ZODGAME_AD_SECONDS": ""}, clear=False):
            self.assertEqual(zg._watch_seconds({"seconds": 30}, FakeResp(BUX_CLICK_PAGE)), 5)

    def test_from_task_row_when_click_page_has_no_count(self):
        with mock.patch.dict(os.environ, {"ZODGAME_AD_SECONDS": ""}, clear=False):
            self.assertEqual(zg._watch_seconds({"seconds": 12}, FakeResp("<html></html>")), 12)

    def test_fallback_constant(self):
        with mock.patch.dict(os.environ, {"ZODGAME_AD_SECONDS": ""}, clear=False):
            self.assertEqual(zg._watch_seconds({"seconds": None}, FakeResp("<html></html>")),
                             zg.AD_FALLBACK_SECONDS)


# ============================ BUX 流程(mock 网络) ============================

class FakeBuxSession:
    """按 URL 分派响应的假 http_get。"""

    def __init__(self, check_xml, bux_page="", click_page=BUX_CLICK_PAGE,
                 update_page=BUX_UPDATE_PAGE):
        self.check_xml = check_xml
        self.bux_page = bux_page
        self.click_page = click_page
        self.update_page = update_page
        self.calls = []

    def __call__(self, session, url, **kwargs):
        self.calls.append(url)
        if "do=click" in url:
            return FakeResp(self.click_page)
        if "do=update" in url:
            return FakeResp(self.update_page)
        if "do=check" in url:
            return FakeResp(self.check_xml)
        return FakeResp(self.bux_page)


class TestRunBuxTasks(unittest.TestCase):
    def _run(self, page_html, fake):
        with mock.patch.object(zg, "http_get", side_effect=fake), \
             mock.patch.object(zg.time, "sleep"), \
             mock.patch.object(zg, "rand_sleep"):
            return zg.run_bux_tasks(object(), page_html)

    def test_no_task_is_success_note(self):
        r = self._run(BUX_BLOCK_NO_TASK + BUX_BLOCK_JOINED, FakeBuxSession(CHECK_XML_FAILED))
        self.assertIsNone(r["error"])
        self.assertIn("无广告任务", r["note"])
        self.assertEqual(r["done"], 0)

    def test_missing_task_block_is_error(self):
        r = self._run(BUX_BLOCK_JOINED, FakeBuxSession(CHECK_XML_FAILED))
        self.assertIn("缺少", r["error"])

    def test_structure_changed_is_error(self):
        r = self._run(BUX_BLOCK_STRUCTURE_CHANGED + BUX_BLOCK_JOINED,
                      FakeBuxSession(CHECK_XML_FAILED))
        self.assertIn("疑似改版", r["error"])

    def test_task_success_path(self):
        r = self._run(BUX_BLOCK_WITH_TASKS + BUX_BLOCK_JOINED,
                      FakeBuxSession(CHECK_XML_SUCCESS, bux_page=BUX_BLOCK_WITH_TASKS + BUX_BLOCK_JOINED))
        self.assertIsNone(r["error"])
        self.assertEqual(r["done"], 2)
        self.assertEqual(r["earned"], 5.0)  # 2 + 3

    def test_task_failure_path_reports_zero_done(self):
        r = self._run(BUX_BLOCK_WITH_TASKS + BUX_BLOCK_JOINED,
                      FakeBuxSession(CHECK_XML_FAILED, bux_page=BUX_BLOCK_WITH_TASKS + BUX_BLOCK_JOINED))
        self.assertEqual(r["done"], 0)
        self.assertIsNone(r["error"])

    def test_not_joined_triggers_join_then_continues(self):
        fake = FakeBuxSession(CHECK_XML_SUCCESS,
                              bux_page=BUX_BLOCK_WITH_TASKS + BUX_BLOCK_JOINED)
        r = self._run(BUX_BLOCK_WITH_TASKS + BUX_BLOCK_NOT_JOINED, fake)
        self.assertTrue(any("do=join" in u for u in fake.calls))
        self.assertEqual(r["done"], 2)

    def test_points_extracted(self):
        r = self._run(BUX_BLOCK_NO_TASK + BUX_BLOCK_JOINED, FakeBuxSession(CHECK_XML_FAILED))
        self.assertEqual(r["total"], 8.0)


# ============================ 账号级流程 ============================

class TestRunOneAccount(unittest.TestCase):
    def _run(self, sign_resp, fake=None):
        def getter(session, url, **kwargs):
            if url == zg.SIGN_PAGE_URL:
                return sign_resp
            return fake(session, url, **kwargs) if fake else FakeResp(BUX_BLOCK_NO_TASK + BUX_BLOCK_JOINED)

        with mock.patch.object(zg, "http_get", side_effect=getter), \
             mock.patch.object(zg, "build_session", return_value=object()), \
             mock.patch.object(zg.time, "sleep"), \
             mock.patch.object(zg, "rand_sleep"):
            return zg.run_one_account("a=1", 0)

    def test_cf_blocked_classified(self):
        r = self._run(FakeResp(CF_CHALLENGE_PAGE, 403))
        self.assertEqual(r["state"], zg.CAT_CF)
        self.assertFalse(r["ok"])
        self.assertIn("Cloudflare", r["message"])

    def test_cookie_invalid_classified(self):
        r = self._run(FakeResp(LOGIN_PAGE))
        self.assertEqual(r["state"], zg.CAT_COOKIE)
        self.assertFalse(r["ok"])

    def test_unknown_page_classified_as_structure(self):
        r = self._run(FakeResp("<title>奇怪的页面</title>"))
        self.assertEqual(r["state"], zg.CAT_STRUCTURE)

    def test_ok_account_with_duplicate_sign_and_no_task(self):
        r = self._run(FakeResp(SIGN_PAGE_SIGNED))
        self.assertTrue(r["ok"])
        self.assertEqual(r["state"], zg.CAT_OK)
        self.assertEqual(r["username"], "测试用户")
        self.assertIn("测试用户", r["label"])

    def test_bux_structure_error_marks_failure(self):
        r = self._run(FakeResp(SIGN_PAGE_SIGNED),
                      FakeBuxSession(CHECK_XML_FAILED, bux_page=BUX_BLOCK_STRUCTURE_CHANGED))
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], zg.CAT_STRUCTURE)

    def test_network_error_classified(self):
        import requests as _rq

        def getter(session, url, **kwargs):
            raise _rq.ConnectionError("boom")

        with mock.patch.object(zg, "http_get", side_effect=getter), \
             mock.patch.object(zg, "build_session", return_value=object()):
            r = zg.run_one_account("a=1", 0)
        self.assertEqual(r["state"], zg.CAT_NETWORK)


# ============================ 汇总与退出码 ============================

class TestReport(unittest.TestCase):
    def test_title_all_ok(self):
        title, _ = zg.build_report([_res(True), _res(True)])
        self.assertIn("全部成功", title)

    def test_title_all_cookie_invalid(self):
        title, _ = zg.build_report([_res(False, zg.CAT_COOKIE), _res(False, zg.CAT_COOKIE)])
        self.assertIn("Cookie 全部失效", title)

    def test_title_all_failed_other(self):
        title, _ = zg.build_report([_res(False, zg.CAT_CF), _res(False, zg.CAT_NETWORK)])
        self.assertIn("全部失败", title)

    def test_title_partial(self):
        title, _ = zg.build_report([_res(True), _res(False, zg.CAT_CF)])
        self.assertIn("部分成功", title)

    def test_report_counts_failure_categories(self):
        _, body = zg.build_report([
            _res(True),
            _res(False, zg.CAT_COOKIE),
            _res(False, zg.CAT_CF),
            _res(False, zg.CAT_NETWORK),
            _res(False, zg.CAT_STRUCTURE),
        ])
        self.assertIn("Cookie 失效 1", body)
        self.assertIn("Cloudflare 拦截 1", body)
        self.assertIn("网络异常 1", body)
        self.assertIn("解析失败/疑似改版 1", body)


class TestMainExitCode(unittest.TestCase):
    def _main(self, results):
        env = {"ZODGAME_COOKIE": "a=1\nb=2", "ZODGAME_NOTIFY": "false"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(zg, "run_one_account", side_effect=results), \
             mock.patch.object(zg, "rand_sleep"):
            return zg.main([])

    def test_all_ok_exit_0(self):
        self.assertEqual(self._main([_res(True), _res(True)]), 0)

    def test_all_failed_exit_1(self):
        self.assertEqual(self._main([_res(False, zg.CAT_CF), _res(False, zg.CAT_CF)]), 1)

    def test_partial_exit_2(self):
        self.assertEqual(self._main([_res(True), _res(False, zg.CAT_CF)]), 2)

    def test_missing_cookie_exit_1(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(zg.main([]), 1)

    def test_check_flag_dispatches_to_check(self):
        env = {"ZODGAME_COOKIE": "a=1"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(zg, "run_check", return_value=0) as rc:
            self.assertEqual(zg.main(["--check"]), 0)
        rc.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
