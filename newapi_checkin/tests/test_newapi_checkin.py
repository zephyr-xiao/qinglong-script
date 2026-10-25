# -*- coding: utf-8 -*-
"""newapi_checkin 单元测试（不发真实请求）。

覆盖：会话构建（trust_env / TLS 默认校验 / SSL 握手失败一次性降级）。
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests as _real_requests
except ImportError:
    _real_requests = None

try:
    import newapi_checkin as nap
except SystemExit:
    # 本机无 requests 时脚本会 print 提示并 sys.exit；打桩后重新加载。
    # stub 只在 requests 确实缺失时创建，绝不覆盖已存在的真实模块。
    if _real_requests is None:
        stub = types.ModuleType("requests")
        stub.Session = object
        stub.exceptions = types.SimpleNamespace(RequestException=Exception, SSLError=Exception)
        sys.modules["requests"] = stub
        import newapi_checkin as nap
    else:
        raise


class TestNewSession(unittest.TestCase):
    def test_defaults_verify_on_trust_env_off(self):
        s = nap._new_session()
        self.assertTrue(s.verify)
        self.assertFalse(s.trust_env)
        self.assertIn("User-Agent", s.headers)

    @unittest.skipIf(_real_requests is None, "需要真实 requests 库")
    def test_ssl_error_downgrades_once_and_retries(self):
        """SSL 握手失败 → 降级为不校验 + 立即重试一次成功。"""
        fake_resp = mock.MagicMock(status_code=200)
        ssl_err = _real_requests.exceptions.SSLError("handshake failed")
        with mock.patch.object(_real_requests.Session, "request",
                               side_effect=[ssl_err, fake_resp]) as m:
            s = nap._new_session()
            resp = s.get("https://example.com/api")
        self.assertIs(resp, fake_resp)
        self.assertFalse(s.verify)
        self.assertEqual(m.call_count, 2)

    @unittest.skipIf(_real_requests is None, "需要真实 requests 库")
    def test_persistent_ssl_error_still_raises(self):
        """降级后仍持续 SSL 失败时异常照常上抛（由上层网络重试逻辑处理）。"""
        ssl_err = _real_requests.exceptions.SSLError("handshake failed")
        with mock.patch.object(_real_requests.Session, "request",
                               side_effect=ssl_err):
            s = nap._new_session()
            with self.assertRaises(_real_requests.exceptions.SSLError):
                s.get("https://example.com/api")
        self.assertFalse(s.verify)


if __name__ == "__main__":
    unittest.main(verbosity=2)
