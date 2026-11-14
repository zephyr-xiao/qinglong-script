# -*- coding: utf-8 -*-
"""sophnet_checkin 单元测试（不发真实请求）。

覆盖：会话构建（trust_env / TLS 默认校验 / 默认 timeout 注入 / SSL 握手失败一次性降级）。
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
    import sophnet_checkin as sp
except SystemExit:
    # 本机无 requests 时脚本会 print 提示并 sys.exit；打桩后重新加载。
    # stub 只在 requests 确实缺失时创建，绝不覆盖已存在的真实模块。
    if _real_requests is None:
        stub = types.ModuleType("requests")
        stub.Session = object
        stub.exceptions = types.SimpleNamespace(RequestException=Exception, SSLError=Exception)
        sys.modules["requests"] = stub
        import sophnet_checkin as sp
    else:
        raise


class TestBuildSession(unittest.TestCase):
    def test_defaults_verify_on_trust_env_off(self):
        s = sp._build_session(30)
        self.assertTrue(s.verify)
        self.assertFalse(s.trust_env)
        self.assertIn("User-Agent", s.headers)

    def test_proxy_applied(self):
        with mock.patch.dict(os.environ, {"SOPHNET_PROXY": "http://127.0.0.1:7890"},
                             clear=False):
            s = sp._build_session(30)
        self.assertEqual(s.proxies["https"], "http://127.0.0.1:7890")

    @unittest.skipIf(_real_requests is None, "需要真实 requests 库")
    def test_default_timeout_injected(self):
        fake_resp = mock.MagicMock(status_code=200)
        with mock.patch.object(_real_requests.Session, "request",
                               return_value=fake_resp) as m:
            s = sp._build_session(25)
            s.get("https://example.com/x")
        self.assertEqual(m.call_args.kwargs["timeout"], 25)

    @unittest.skipIf(_real_requests is None, "需要真实 requests 库")
    def test_ssl_error_downgrades_once_and_retries(self):
        """SSL 握手失败 → 降级为不校验 + 立即重试一次成功。"""
        fake_resp = mock.MagicMock(status_code=200)
        ssl_err = _real_requests.exceptions.SSLError("handshake failed")
        with mock.patch.object(_real_requests.Session, "request",
                               side_effect=[ssl_err, fake_resp]) as m:
            s = sp._build_session(30)
            resp = s.get("https://example.com/x")
        self.assertIs(resp, fake_resp)
        self.assertFalse(s.verify)
        self.assertEqual(m.call_count, 2)


def _welfare(tasks, earned=0):
    """构造 _get_welfare 返回的 result 结构（只含 _fill_interact 用到的字段）。"""
    return {"dailyTasks": {"tasks": tasks},
            "tokenSummary": {"totalEarned": earned}}


def _task(key, threshold, progress, completed=False, rewarded=False):
    return {"taskKey": key, "taskType": "INTERACT", "threshold": threshold,
            "currentProgress": progress, "completed": completed,
            "rewarded": rewarded, "rewardTokens": 1000}


class TestFillInteract(unittest.TestCase):
    """交互任务上报逻辑：幂等、分片、异常不抛出、兜底领奖范围。"""

    def test_all_rewarded_skips_without_any_request(self):
        tasks = [_task("interact_3min", 180, 180, True, True),
                 _task("interact_10min", 600, 600, True, True),
                 _task("interact_30min", 1800, 1800, True, True)]
        with mock.patch.object(sp, "_get_welfare", return_value=(_welfare(tasks), "")), \
                mock.patch.object(sp, "_report_interact") as rep, \
                mock.patch.object(sp.time, "sleep") as slp:
            msg = sp._fill_interact(object(), "tk", False)
        self.assertIn("已全部领取", msg)
        rep.assert_not_called()
        slp.assert_not_called()

    def test_reports_only_up_to_top_threshold(self):
        before = [_task("interact_3min", 180, 0), _task("interact_10min", 600, 0),
                  _task("interact_30min", 1800, 0)]
        after = [_task("interact_3min", 180, 180, True, True),
                 _task("interact_10min", 600, 600, True, True),
                 _task("interact_30min", 1800, 1800, True, True)]
        report_ret = ({"accepted": 1800, "todayTotal": 1800, "dailyLimit": 7200}, "")
        with mock.patch.object(sp, "_get_welfare",
                               side_effect=[(_welfare(before, 0), ""),
                                            (_welfare(after, 780000), "")]), \
                mock.patch.object(sp, "_report_interact", return_value=report_ret) as rep, \
                mock.patch.object(sp, "_claim_task") as claim, \
                mock.patch.object(sp.time, "sleep") as slp:
            msg = sp._fill_interact(object(), "tk", False)
        self.assertEqual(rep.call_count, 1)
        self.assertEqual(rep.call_args[0][2], sp.INTERACT_CHUNK_MAX)
        slp.assert_called_once_with(sp.INTERACT_MIN_INTERVAL)
        claim.assert_not_called()
        self.assertIn("780k", msg)

    def test_partial_progress_reports_remainder(self):
        before = [_task("interact_3min", 180, 300, True, True),
                  _task("interact_10min", 600, 300),
                  _task("interact_30min", 1800, 300)]
        after = [_task("interact_3min", 180, 180, True, True),
                 _task("interact_10min", 600, 600, True, True),
                 _task("interact_30min", 1800, 1800, True, True)]
        report_ret = ({"accepted": 1500, "todayTotal": 1800, "dailyLimit": 7200}, "")
        with mock.patch.object(sp, "_get_welfare",
                               side_effect=[(_welfare(before, 0), ""),
                                            (_welfare(after, 150000), "")]), \
                mock.patch.object(sp, "_report_interact", return_value=report_ret) as rep, \
                mock.patch.object(sp, "_claim_task"), \
                mock.patch.object(sp.time, "sleep"):
            sp._fill_interact(object(), "tk", False)
        self.assertEqual(rep.call_args[0][2], 1500)

    def test_report_failure_returns_warning_without_raising(self):
        before = [_task("interact_3min", 180, 0)]
        with mock.patch.object(sp, "_get_welfare", return_value=(_welfare(before), "")), \
                mock.patch.object(sp, "_report_interact",
                                  return_value=({}, "上报过于频繁，请10秒后再试")), \
                mock.patch.object(sp.time, "sleep"):
            msg = sp._fill_interact(object(), "tk", False)
        self.assertIn("上报失败", msg)
        self.assertIn("频繁", msg)

    def test_claim_only_for_completed_unrewarded(self):
        """进度已达标但未发奖的任务才走兜底领取，领完要重新拉状态。"""
        before = [_task("interact_3min", 180, 180, True, False),
                  _task("interact_30min", 1800, 1800, False, False)]
        need_claim = [_task("interact_3min", 180, 180, True, False),
                      _task("interact_30min", 1800, 1800, True, False)]
        final = [_task("interact_3min", 180, 180, True, True),
                 _task("interact_30min", 1800, 1800, True, True)]
        with mock.patch.object(sp, "_get_welfare",
                               side_effect=[(_welfare(before), ""),
                                            (_welfare(need_claim), ""),
                                            (_welfare(final, 630000), "")]), \
                mock.patch.object(sp, "_report_interact") as rep, \
                mock.patch.object(sp, "_claim_task", return_value=(True, "")) as claim, \
                mock.patch.object(sp.time, "sleep"):
            msg = sp._fill_interact(object(), "tk", False)
        rep.assert_not_called()
        self.assertEqual(claim.call_count, 2)
        self.assertEqual([c[0][2] for c in claim.call_args_list],
                         ["interact_3min", "interact_30min"])
        self.assertIn("630k", msg)


class TestMaskSecrets(unittest.TestCase):
    """DEBUG 日志脱敏：凭证字段必须被替换，非凭证字段保持原样。"""

    def test_masks_token_fields_but_keeps_others(self):
        raw = ('{"status":0,"result":{"token":"eyJhbGciOiJIUzI1NiJ9abcdefgh",'
               '"refreshToken":"ZYEcvO1234567890abcdef","expires":"2026-11-14T12:00:00"}}')
        masked = sp.mask_secrets_in_json(raw)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9abcdefgh", masked)
        self.assertNotIn("ZYEcvO1234567890abcdef", masked)
        self.assertIn("2026-11-14T12:00:00", masked)
        self.assertIn('"status":0', masked)

    def test_masks_nested_lists(self):
        raw = '{"data":[{"accessToken":"aaaaaaaaaaaaaaa"},{"idToken":"bbbbbbbbbbbb"}]}'
        masked = sp.mask_secrets_in_json(raw)
        self.assertNotIn("aaaaaaaaaaaaaaa", masked)
        self.assertNotIn("bbbbbbbbbbbb", masked)

    def test_non_json_returned_as_is(self):
        raw = "<html>502 Bad Gateway</html>"
        self.assertEqual(sp.mask_secrets_in_json(raw), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
