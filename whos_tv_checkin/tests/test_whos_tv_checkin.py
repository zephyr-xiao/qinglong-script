# -*- coding: utf-8 -*-
"""whos_tv_checkin 纯函数单测。

覆盖本轮加固涉及的判定逻辑：Cloudflare 页识别与挑战状态、代理/cookie 解析、
脱敏、运行锁、签到响应判定，以及"localhost 探测不受 HTTP_PROXY 污染"这条回归。
不启动真实浏览器，也不访问真实站点。
"""
import http.server
import json
import os
import socket
import tempfile
import threading
import time

import pytest

import whos_tv_checkin as m

# 真实站点正常页面里 Cloudflare 也会注入 challenge-platform 脚本，
# 用它当"仍在挑战"的判据会误伤——这几段样本就是为守住这条边界写的
SITE_PAGE = (
    "<html><head><title>whos.tv</title></head><body>"
    '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
    '<button id="checkin-btn">签到</button></body></html>'
)
CHALLENGE_PAGE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
    '<div id="challenge-running">Enable JavaScript and cookies to continue</div>'
    "</body></html>"
)
BLOCK_PAGE = (
    "<html><head><title>Attention Required! | Cloudflare</title></head><body>"
    '<div class="cf-error-details">Sorry, you have been blocked</div></body></html>'
)


# ====================== 脱敏 ======================

class TestMask:
    @pytest.mark.parametrize("raw,expected", [
        ("", ""),
        ("a", "a*"),
        ("abcd", "ab*cd"),
        ("abcde", "ab*de"),
        ("abc@example.com", "ab*@example.com"),
        ("a@example.com", "a*@example.com"),
    ])
    def test_常见输入(self, raw, expected):
        assert m.mask(raw) == expected

    def test_以_at_开头的畸形用户名不再崩溃(self):
        # 服务端返回 "@domain" 这类显示名时，旧实现会在 head[0] 上抛 IndexError
        assert m.mask("@example.com") == "*@example.com"


class TestMaskProxy:
    def test_带凭据的代理隐藏密码(self):
        assert m.mask_proxy("http://user:secret@1.2.3.4:7890") == "http://user:***@1.2.3.4:7890"

    def test_无凭据代理原样返回(self):
        assert m.mask_proxy("socks5://172.17.0.1:7891") == "socks5://172.17.0.1:7891"


class TestRedactSecrets:
    def test_令牌字段被打码(self):
        text = '{"code":200000,"data":{"token":"abcdef123456"}}'
        out = m.redact_secrets(text)
        assert "abcdef123456" not in out
        assert '"token":"***"' in out

    def test_普通字段不受影响(self):
        text = '{"code":200000,"data":{"points_earned":10}}'
        assert m.redact_secrets(text) == text

    def test_空值安全(self):
        assert m.redact_secrets("") == ""


# ====================== 环境变量 ======================

class TestEnvInt:
    def test_合法值原样返回(self, monkeypatch):
        monkeypatch.setenv("WHOSTV_TEST_INT", "45")
        assert m.env_int("WHOSTV_TEST_INT", 30) == 45

    def test_非法值回退默认(self, monkeypatch):
        monkeypatch.setenv("WHOSTV_TEST_INT", "abc")
        assert m.env_int("WHOSTV_TEST_INT", 30) == 30

    def test_缺省回退默认(self, monkeypatch):
        monkeypatch.delenv("WHOSTV_TEST_INT", raising=False)
        assert m.env_int("WHOSTV_TEST_INT", 30) == 30

    def test_低于下限回退默认(self, monkeypatch):
        # 旧实现会把 WHOSTV_TIMEOUT=0 原样返回，导致请求"立即超时"
        monkeypatch.setenv("WHOSTV_TEST_INT", "0")
        assert m.env_int("WHOSTV_TEST_INT", 30, minimum=1) == 30

    def test_负数低于下限回退默认(self, monkeypatch):
        monkeypatch.setenv("WHOSTV_TEST_INT", "-5")
        assert m.env_int("WHOSTV_TEST_INT", 30, minimum=1) == 30


class TestSplitEnvList:
    def test_按_and_切分(self):
        assert m.split_env_list("a&b&c") == ["a", "b", "c"]

    def test_按换行切分(self):
        assert m.split_env_list("a\nb") == ["a", "b"]

    def test_混合分隔符与空白(self):
        assert m.split_env_list(" a & b\n\n c ") == ["a", "b", "c"]

    def test_空值返回空列表(self):
        assert m.split_env_list("") == []


# ====================== 代理与 Cookie 解析 ======================

class TestParseProxyAddr:
    @pytest.mark.parametrize("proxy,expected", [
        ("http://172.17.0.1:7890", ("172.17.0.1", 7890)),
        ("socks5://172.17.0.1:7891", ("172.17.0.1", 7891)),
        ("socks5h://proxy.example.com:1080", ("proxy.example.com", 1080)),
        ("http://user:pass@1.2.3.4:7890", ("1.2.3.4", 7890)),
        ("http://[::1]:7890", ("::1", 7890)),
        ("172.17.0.1:7890", ("172.17.0.1", 7890)),
    ])
    def test_各种写法都能解析出_host_port(self, proxy, expected):
        # 旧正则对 socks5://（scheme 含数字）与带凭据代理一律 NO MATCH，
        # 于是"代理不通"这种最需要诊断的场景反而静默跳过探测
        assert m.parse_proxy_addr(proxy) == expected

    @pytest.mark.parametrize("proxy", ["", "   ", "not-a-proxy", "http://host-without-port"])
    def test_无法解析时返回_None(self, proxy):
        assert m.parse_proxy_addr(proxy) is None


class TestCfCookieFilter:
    @pytest.mark.parametrize("name", ["cf_clearance", "__cf_bm", "cf_chl_2", "__cfduid"])
    def test_cloudflare_自管_cookie_命中(self, name):
        assert m._is_cf_cookie(name) is True

    @pytest.mark.parametrize("name", ["HYPERF_SESSION_ID", "token", "cfbusiness", ""])
    def test_业务_cookie_不命中(self, name):
        assert m._is_cf_cookie(name) is False


class TestParseCookieStr:
    def test_解析基本_cookie_串(self):
        assert m.parse_cookie_str("a=1; b=2; c=3") == {"a": "1", "b": "2", "c": "3"}

    def test_值里含等号不被截断(self):
        assert m.parse_cookie_str("token=abc=def") == {"token": "abc=def"}

    def test_忽略无等号的片段(self):
        assert m.parse_cookie_str("a=1; junk; b=2") == {"a": "1", "b": "2"}


class TestParseCredentials:
    def test_and_分隔多账号(self):
        assert m.parse_credentials("u1#p1&u2#p2") == [("u1", "p1"), ("u2", "p2")]

    def test_换行分隔多账号(self):
        assert m.parse_credentials("u1#p1\nu2#p2") == [("u1", "p1"), ("u2", "p2")]

    def test_跳过缺分隔符或空值的项(self):
        assert m.parse_credentials("u1#p1&junk&u2#") == [("u1", "p1")]


# ====================== Cloudflare 判据 ======================

class TestLooksLikeCfPage:
    def test_挑战页命中(self):
        assert m.looks_like_cf_page(CHALLENGE_PAGE) is True

    def test_硬拦截页命中(self):
        assert m.looks_like_cf_page(BLOCK_PAGE) is True

    def test_json_业务响应不误判(self):
        assert m.looks_like_cf_page('{"code":200000,"data":{}}') is False

    def test_正常站点页面不误判(self):
        # 页面里有 /cdn-cgi/challenge-platform/ 脚本，但那是正常注入，不是挑战
        assert m.looks_like_cf_page(SITE_PAGE) is False

    def test_空值不误判(self):
        assert m.looks_like_cf_page("") is False


class TestClassifyChallengeState:
    @pytest.mark.parametrize("title", ["请稍候…", "Just a moment...", "正在验证您是否是真人"])
    def test_挑战标题判为_challenge(self, title):
        assert m.classify_challenge_state(title, "") == "challenge"

    def test_空标题判为_challenge(self):
        # 标题还没渲染出来，不能当成"已通过"
        assert m.classify_challenge_state("", "") == "challenge"

    @pytest.mark.parametrize("title", [
        "Attention Required! | Cloudflare",
        "Access denied",
        "Error 1020",
    ])
    def test_硬拦截标题判为_blocked(self, title):
        # 旧实现只看"标题非空且不含请稍候"就判通过，这些页会被当成挑战已通过
        assert m.classify_challenge_state(title, "") == "blocked"

    def test_真实标题加干净内容判为_ok(self):
        assert m.classify_challenge_state("whos.tv", SITE_PAGE) == "ok"

    def test_真实标题但内容仍是挑战页判为_challenge(self):
        assert m.classify_challenge_state("whos.tv", CHALLENGE_PAGE) == "challenge"

    def test_真实标题但内容是被拦截页判为_blocked(self):
        assert m.classify_challenge_state("whos.tv", BLOCK_PAGE) == "blocked"


# ====================== 签到响应判定 ======================

class TestIsSuccessResponse:
    def test_v2_签到成功(self):
        ok, msg = m.is_success_response(
            '{"code":200000,"data":{"points_earned":10,"streak_bonus":5,"consecutive_days":3}}',
            200,
        )
        assert ok is True
        assert "15" in msg and "连续签到 3 天" in msg

    def test_v2_今日已签到幂等算成功(self):
        ok, msg = m.is_success_response('{"code":406008,"message":"今日已签到"}', 200)
        assert ok is True and "已签到" in msg

    def test_http_401_判为_cookie_失效(self):
        ok, msg = m.is_success_response('{"code":401,"message":"Unauthorized"}', 401)
        assert ok is False and "Cookie 失效" in msg

    def test_非_2xx_带_code_200000_不得判成功(self):
        # 网关自定义错误页可能恰好带 code=200000，必须以状态码拦住
        ok, _ = m.is_success_response('{"code":200000,"data":{}}', 403)
        assert ok is False

    def test_文案里的已签到算成功(self):
        ok, msg = m.is_success_response('{"message":"您今天已经签到过了"}', 200)
        assert ok is True and "已签到" in msg

    def test_cloudflare_页面不算成功(self):
        ok, _ = m.is_success_response(BLOCK_PAGE, 403)
        assert ok is False


# ====================== localhost 探测不受 HTTP_PROXY 污染 ======================

class _JsonHandler(http.server.BaseHTTPRequestHandler):
    payload = b"[]"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):  # 静音测试输出
        pass


def _serve_json(payload: bytes):
    """起一个只服务一次探测的本地 HTTP 服务，返回 (port, shutdown)。"""
    handler = type("_H", (_JsonHandler,), {"payload": payload})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server.server_address[1], server.shutdown


class TestLocalhostProbeIgnoresProxyEnv:
    def test_设置了_HTTP_PROXY_仍能连上本地调试端口(self, monkeypatch):
        # 回归：旧实现用 urllib，配了 HTTP_PROXY 后会把 127.0.0.1 的请求也交给代理
        # （proxy_bypass('127.0.0.1') 为 False），探测必然失败
        monkeypatch.setenv("HTTP_PROXY", "http://172.17.0.1:7890")
        monkeypatch.setenv("http_proxy", "http://172.17.0.1:7890")
        port, shutdown = _serve_json(b'[{"type":"page","url":"about:blank"}]')
        try:
            assert m._http_json(port, "/json", timeout=3) == [
                {"type": "page", "url": "about:blank"}
            ]
            assert m.browser_port_ready(port, timeout=3) is True
        finally:
            shutdown()

    def test_端口无人监听时返回_None(self):
        # 取一个刚被释放的端口，确保没有服务在听
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
        sock.close()
        assert m._http_json(free_port, "/json", timeout=1) is None


class TestDechunk:
    def test_解开_chunked_传输编码(self):
        body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        assert m._dechunk(body) == b"hello world"


# ====================== 运行锁 ======================

class TestRunLock:
    def test_抢到锁后可重复释放(self, tmp_path):
        d = str(tmp_path)
        assert m.acquire_lock(d) is True
        m.release_lock(d)
        assert m.acquire_lock(d) is True
        m.release_lock(d)

    def test_同一进程重复抢锁被拒(self, tmp_path):
        d = str(tmp_path)
        assert m.acquire_lock(d) is True
        # 锁文件存在且持有者（本进程）存活 → 第二次抢不到
        assert m.acquire_lock(d) is False
        m.release_lock(d)

    def test_持有者进程已死的残留锁可接管(self, tmp_path):
        d = str(tmp_path)
        lock_path = os.path.join(d, m.LOCK_FILENAME)
        os.makedirs(d, exist_ok=True)
        with open(lock_path, "w", encoding="ascii") as fp:
            fp.write("0 9999999999\n")  # pid 0 恒为"已死"
        assert m.acquire_lock(d) is True
        m.release_lock(d)

    def test_时间戳过期的锁可接管(self, tmp_path):
        d = str(tmp_path)
        lock_path = os.path.join(d, m.LOCK_FILENAME)
        os.makedirs(d, exist_ok=True)
        stale_ts = int(time.time()) - m.LOCK_STALE_SECONDS - 60
        with open(lock_path, "w", encoding="ascii") as fp:
            fp.write(f"{os.getpid()} {stale_ts}\n")
        assert m.acquire_lock(d) is True
        m.release_lock(d)

    def test_释放时不删他人的锁(self, tmp_path):
        d = str(tmp_path)
        lock_path = os.path.join(d, m.LOCK_FILENAME)
        os.makedirs(d, exist_ok=True)
        with open(lock_path, "w", encoding="ascii") as fp:
            fp.write("1 9999999999\n")  # 不是本进程创建的锁
        m.release_lock(d)
        assert os.path.exists(lock_path) is True

    def test_内容损坏的锁按残留处理(self, tmp_path):
        d = str(tmp_path)
        lock_path = os.path.join(d, m.LOCK_FILENAME)
        os.makedirs(d, exist_ok=True)
        with open(lock_path, "w", encoding="ascii") as fp:
            fp.write("garbage\n")
        assert m.acquire_lock(d) is True
        m.release_lock(d)


# ====================== CF 自愈重试 ======================

class TestRunWithCfRecovery:
    def test_非_cf_失败不重试(self, monkeypatch):
        calls = []

        def runner():
            calls.append(1)
            return False, "Cookie 失效", False

        monkeypatch.setattr(m, "ensure_challenge", lambda *a, **k: pytest.fail("不应重过挑战"))
        ok, msg = m.run_with_cf_recovery(runner, page=None, debug=False)
        assert ok is False and msg == "Cookie 失效" and len(calls) == 1

    def test_cf_拦截后重过挑战并重试成功(self, monkeypatch):
        results = iter([(False, "被 CF 拦截", True), (True, "签到成功", False)])

        def runner():
            return next(results)

        monkeypatch.setattr(m, "ensure_challenge", lambda *a, **k: "ok")
        ok, msg = m.run_with_cf_recovery(runner, page=None, debug=False)
        assert ok is True and msg == "签到成功"

    def test_重过挑战失败时保留原失败信息(self, monkeypatch):
        monkeypatch.setattr(m, "ensure_challenge", lambda *a, **k: "challenge")
        ok, msg = m.run_with_cf_recovery(
            lambda: (False, "被 CF 拦截", True), page=None, debug=False
        )
        assert ok is False
        assert "被 CF 拦截" in msg and "重新过挑战仍未通过" in msg


# ====================== 签到流程短路（假 page） ======================

class _FakePage:
    """只实现 _do_signin 用得到的接口：签到 POST 与任务页 GET 都由 post/get 注入。"""

    def __init__(self, responses):
        self._responses = responses

    def evaluate(self, _script):
        return self._responses.pop(0)


class TestDoSigninCfShortCircuit:
    def test_签到请求被_cf_拦截时标记_cf_blocked(self):
        page = _FakePage([
            {"ok": True, "status": 403, "text": BLOCK_PAGE},
        ])
        ok, msg, cf = m._do_signin(page, "账号 1", timeout=5, debug=False)
        assert ok is False and cf is True and "Cloudflare" in msg

    def test_签到成功时不标记_cf_blocked(self):
        page = _FakePage([
            {"ok": True, "status": 200, "text": '{"code":200000,"data":{"points_earned":3}}'},
        ])
        ok, msg, cf = m._do_signin(page, "账号 1", timeout=5, debug=False)
        assert ok is True and cf is False and "签到成功" in msg

    def test_任务页被_cf_拦截时短路不再探测候选(self):
        page = _FakePage([
            {"ok": True, "status": 500, "text": '{"code":1,"message":"boom"}'},
            {"ok": True, "status": 200, "text": BLOCK_PAGE},
        ])
        ok, msg, cf = m._do_signin(page, "账号 1", timeout=5, debug=False)
        assert ok is False and cf is True and "任务页被 Cloudflare 拦截" in msg


class TestLoginCfDetection:
    """登录路径也要能识别 CF 拦截页，否则自愈只在签到阶段生效。"""

    def test_登录响应是_cf_页时标记_cf_blocked(self):
        page = _FakePage([{"ok": True, "status": 403, "text": BLOCK_PAGE}])
        ok, msg, cf = m.login_one_account(
            page, "u", "p", timeout=5, debug=False, label="账号 1"
        )
        assert ok is False and cf is True and "Cloudflare" in msg

    def test_登录成功返回三态(self):
        page = _FakePage([{
            "ok": True, "status": 200,
            "text": '{"code":200000,"data":{"username":"someone@example.com"}}',
        }])
        ok, msg, cf = m.login_one_account(
            page, "u", "p", timeout=5, debug=False, label="账号 1"
        )
        assert ok is True and cf is False and "登录成功" in msg

    def test_密码错误不标记_cf(self):
        page = _FakePage([{
            "ok": True, "status": 200,
            "text": '{"code":400001,"message":"密码错误"}',
        }])
        ok, msg, cf = m.login_one_account(
            page, "u", "p", timeout=5, debug=False, label="账号 1"
        )
        assert ok is False and cf is False and "密码错误" in msg


class TestFindCheckinState:
    def test_识别_html_里的_data_signed_in(self):
        assert m.find_checkin_state('<button id="checkin-btn" data-signed-in="true">') == "true"

    def test_属性顺序颠倒也能识别(self):
        assert m.find_checkin_state('<button data-signed-in="false" id="checkin-btn">') == "false"

    def test_v2_页面按需要签到处理(self):
        html = '<button id="checkin-btn" class="insufficient-points-checkin-btn">签到</button>'
        assert m.find_checkin_state(html) == "false"

    def test_无相关元素时返回_None(self):
        assert m.find_checkin_state("<html><body>nothing</body></html>") is None


class TestExtractApiCandidates:
    def test_抽取去重且保持顺序(self):
        text = 'fetch("/api/user/tasks/signin"); fetch("/api/user/checkin"); fetch("/api/user/tasks/signin")'
        assert m.extract_api_candidates(text) == [
            "/api/user/tasks/signin",
            "/api/user/checkin",
        ]

    def test_无关路径不抽取(self):
        assert m.extract_api_candidates('"/api/user/profile"') == []


class TestAbsolutize:
    @pytest.mark.parametrize("raw,expected", [
        ("https://other.example/x", "https://other.example/x"),
        ("//cdn.example/x", "https://cdn.example/x"),
        ("/api/x", "https://whos.tv/api/x"),
        ("api/x", "https://whos.tv/api/x"),
    ])
    def test_补全为绝对地址(self, raw, expected):
        assert m.absolutize(raw) == expected


def test_profile_dir_随端口隔离():
    a = m.profile_dir_for(9222)
    b = m.profile_dir_for(9223)
    assert a != b and a.endswith(os.path.join("whostv_profile", "9222"))
