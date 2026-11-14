# -*- coding: utf-8 -*-
"""baiduwangpan_checkin 单元测试（unittest，全部打桩，不发真实请求）。

运行：python -m unittest discover -s tests -v
"""

import hashlib
import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests

_MOD_PATH = Path(__file__).resolve().parent.parent / "baiduwangpan_checkin.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("baiduwangpan_checkin", _MOD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


# ---------------------------------------------------------------- 桩

class FakeClient(object):
    """只实现 raw()，call() 直接复用真实实现。

    自己重写一份 call 等于把被测的业务码判定换成了测试里的第二套实现，
    冲突码、非法类型这些用例就测不到真实逻辑了。
    """

    call = mod.BaiduClient.call

    def __init__(self, handlers):
        self.handlers = {path: list(items) for path, items in handlers.items()}
        self.calls = []
        self.referers = []
        self.cuid = "cuid-test"

    def raw(self, path, params=None, referer=None):
        self.calls.append((path, dict(params or {})))
        if referer is not None:
            self.referers.append((path, referer))
        queue = self.handlers.get(path)
        if not queue:
            raise AssertionError("未预期的请求 %s" % path)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):
            return item(params or {})
        if isinstance(item, Exception):
            raise item
        return item


class FakeResponse(object):
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, timeout=None, allow_redirects=True, headers=None):
        self.calls.append({"url": url, "timeout": timeout,
                           "allow_redirects": allow_redirects,
                           "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _task(**kwargs):
    base = {
        "task_id": "1001",
        "task_id_str": "1001",
        "task_from": "task_sys_daily",
        "task_type": 70,
        "task_status": 0,
        "task_name": "看广告",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------- 业务码

class BusinessCodesTest(unittest.TestCase):
    def test_collects_both_fields(self):
        self.assertEqual(mod.business_codes({"errno": 0}), ([0], False))
        self.assertEqual(
            mod.business_codes({"error_code": 421001}), ([421001], False)
        )
        self.assertEqual(
            mod.business_codes({"errno": 0, "error_code": 421001}),
            ([0, 421001], False),
        )

    def test_null_is_treated_as_missing(self):
        # null 等价于「字段不存在」，cf 的 errno ?? error_code 也是这个语义
        self.assertEqual(
            mod.business_codes({"errno": None, "error_code": 0}), ([0], False)
        )

    def test_string_and_bool_are_invalid(self):
        self.assertEqual(mod.business_codes({"errno": "0"}), ([], True))
        self.assertEqual(mod.business_codes({"errno": True}), ([], True))

    def test_missing_all(self):
        self.assertEqual(mod.business_codes({}), ([], False))

    def test_call_rejects_invalid_and_missing(self):
        for payload in ({"errno": "0", "error_code": 0}, {}):
            client = FakeClient({mod.API_GROWTH: [payload]})
            with self.assertRaises(mod.BaiduError):
                client.call(mod.API_GROWTH)

    def test_call_requires_every_code_allowed(self):
        # 两个字段冲突时按失败处理，不能信 errno
        client = FakeClient({mod.API_GROWTH: [{"errno": 0, "error_code": 8001}]})
        with self.assertRaises(mod.BaiduError):
            client.call(mod.API_GROWTH)


# ---------------------------------------------------------------- 网页签到

class SigninPcTest(unittest.TestCase):
    def test_ok(self):
        client = FakeClient({mod.API_SIGNIN: [{"errno": 0}]})
        self.assertEqual(mod.signin_pc(client), "signed")

    def test_repeat_english(self):
        client = FakeClient({
            mod.API_SIGNIN: [{"error_code": 421001, "error_msg": "repeat signin"}],
        })
        self.assertEqual(mod.signin_pc(client), "already_signed")

    def test_repeat_chinese(self):
        client = FakeClient({
            mod.API_SIGNIN: [{"error_code": 421001, "error_msg": "今日已签到"}],
        })
        self.assertEqual(mod.signin_pc(client), "already_signed")

    def test_repeat_unknown_message_raises(self):
        # 421001 配一个无法确认语义的消息时宁可报错，也不能当成已签到
        client = FakeClient({
            mod.API_SIGNIN: [{"error_code": 421001, "error_msg": "系统繁忙"}],
        })
        with self.assertRaises(mod.BaiduError):
            mod.signin_pc(client)

    def test_other_code_raises(self):
        client = FakeClient({mod.API_SIGNIN: [{"errno": 8001}]})
        with self.assertRaises(mod.BaiduError):
            mod.signin_pc(client)

    def test_repeat_code_in_error_code_only(self):
        # 百度可能同时给 errno=0 和 error_code=421001，不能因为 errno 是 0 就报成功
        client = FakeClient({
            mod.API_SIGNIN: [{"errno": 0, "error_code": 421001,
                              "error_msg": "repeat signin"}],
        })
        self.assertEqual(mod.signin_pc(client), "already_signed")

    def test_not_allow_counts_as_signed(self):
        client = FakeClient({
            mod.API_SIGNIN: [{"error_code": 421001, "error_msg": "not allow"}],
        })
        self.assertEqual(mod.signin_pc(client), "already_signed")

    def test_conflicting_codes_raise(self):
        # errno=0 但 error_code=8001：两个字段冲突时按失败处理，不能信 errno
        client = FakeClient({
            mod.API_SIGNIN: [{"errno": 0, "error_code": 8001}],
        })
        with self.assertRaises(mod.BaiduError):
            mod.signin_pc(client)


# ---------------------------------------------------------------- App 签到

class SigninAppTest(unittest.TestCase):
    def test_already_signed(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [{"errno": 0, "data": {"signed_today": 1}}],
        })
        self.assertEqual(mod.signin_app(client), "already_signed")
        self.assertEqual(len(client.calls), 1)

    def test_unknown_state_raises(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [{"errno": 0, "data": {"signed_today": "0"}}],
        })
        with self.assertRaises(mod.BaiduError):
            mod.signin_app(client)

    def test_sign_and_recheck(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [
                {"errno": 0, "data": {"signed_today": 0}},
                {"errno": 0, "data": {"signed_today": 1}},
            ],
            mod.API_APP_TASKLIST: [{
                "errno": 0,
                "result": {"list": [{"task_type": 166, "task_id": 7,
                                     "task_id_str": "7",
                                     "task_from": "task_sys_daily"}]},
            }],
            mod.API_APP_SIGNIN: [{"errno": 0}],
        })
        self.assertEqual(mod.signin_app(client), "signed")
        signin_params = [c[1] for c in client.calls if c[0] == mod.API_APP_SIGNIN][0]
        self.assertEqual(signin_params["task_id_str"], "7")
        self.assertEqual(signin_params["cuid"], "cuid-test")

    def test_recheck_failed_raises(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [
                {"errno": 0, "data": {"signed_today": 0}},
                {"errno": 0, "data": {"signed_today": 0}},
            ],
            mod.API_APP_TASKLIST: [{
                "errno": 0,
                "result": {"list": [{"task_type": 166, "task_id_str": "7",
                                     "task_from": "task_sys_daily"}]},
            }],
            mod.API_APP_SIGNIN: [{"errno": 0}],
        })
        with self.assertRaises(mod.BaiduError):
            mod.signin_app(client)

    def test_missing_task_raises(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [{"errno": 0, "data": {"signed_today": 0}}],
            mod.API_APP_TASKLIST: [{"errno": 0, "result": {"list": []}}],
        })
        with self.assertRaises(mod.BaiduError):
            mod.signin_app(client)

    def test_task_id_str_only_is_accepted(self):
        # 真实响应里可能只给 task_id_str，不能因为缺 task_id 就放弃
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [
                {"errno": 0, "data": {"signed_today": 0}},
                {"errno": 0, "data": {"signed_today": 1}},
            ],
            mod.API_APP_TASKLIST: [{
                "errno": 0,
                "result": {"list": [{"task_type": 166, "task_id_str": "7",
                                     "task_from": "task_sys_daily"}]},
            }],
            mod.API_APP_SIGNIN: [{"errno": 0}],
        })
        self.assertEqual(mod.signin_app(client), "signed")
        params = [c[1] for c in client.calls if c[0] == mod.API_APP_SIGNIN][0]
        self.assertEqual(params["task_id_str"], "7")
        self.assertNotIn("task_id", params)

    def test_missing_task_id_raises(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [{"errno": 0, "data": {"signed_today": 0}}],
            mod.API_APP_TASKLIST: [{
                "errno": 0,
                "result": {"list": [{"task_type": 166,
                                     "task_from": "task_sys_daily"}]},
            }],
        })
        with self.assertRaises(mod.BaiduError):
            mod.signin_app(client)


# ---------------------------------------------------------------- 累计奖励

def _award_payload(days, rewards):
    return {"errno": 0, "data": {"signin_days": days, "reward_list": rewards}}


class AwardStateTest(unittest.TestCase):
    def test_sorted_and_claimable(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [_award_payload(16, [
                {"day": 30, "received": 0, "prize": [{}]},
                {"day": 7, "received": 1, "prize": [{}]},
                {"day": 15, "received": 0, "prize": [{}, {}]},
            ])],
        })
        state = mod.award_state(client)
        self.assertEqual([t["day"] for t in state["tiers"]], [7, 15, 30])
        # 7 天档已领完；15 天档 received 1/2 仍可领；30 天档未达标
        self.assertEqual([t["day"] for t in state["claimable"]], [15])
        self.assertEqual(state["signin_days"], 16)

    def test_missing_days_raises(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [{"errno": 0, "data": {"reward_list": []}}],
        })
        with self.assertRaises(mod.BaiduError):
            mod.award_state(client)

    def test_bool_received_raises(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [_award_payload(7, [
                {"day": 7, "received": True, "prize": [{}]},
            ])],
        })
        with self.assertRaises(mod.BaiduError):
            mod.award_state(client)

    def test_empty_prize_raises(self):
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [_award_payload(7, [
                {"day": 7, "received": 0, "prize": []},
            ])],
        })
        with self.assertRaises(mod.BaiduError):
            mod.award_state(client)


class ClaimAwardsTest(unittest.TestCase):
    def test_no_award_due(self):
        report = {"errors": []}
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [_award_payload(3, [
                {"day": 7, "received": 0, "prize": [{}]},
            ])],
        })
        self.assertEqual(mod.claim_awards(client, report), "no_award_due")
        self.assertEqual(report["errors"], [])

    def test_claim_success(self):
        report = {"errors": []}
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}]}]),
            ],
            mod.API_APP_AWARD: [{"errno": 0}],
        })
        with mock.patch.object(mod.time, "sleep"):
            self.assertEqual(mod.claim_awards(client, report), "claimed:15")
        self.assertEqual(report["errors"], [])
        self.assertEqual(
            len([c for c in client.calls if c[0] == mod.API_APP_AWARD]), 1
        )

    def test_multi_prize_tier_claims_repeatedly(self):
        # 实测每档 prize 是 2 项、received 上限 2，服务端可能一次只发一个
        report = {"errors": []}
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}, {}]}]),
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}, {}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}, {}]}]),
                _award_payload(15, [{"day": 15, "received": 2, "prize": [{}, {}]}]),
                _award_payload(15, [{"day": 15, "received": 2, "prize": [{}, {}]}]),
                _award_payload(15, [{"day": 15, "received": 2, "prize": [{}, {}]}]),
            ],
            mod.API_APP_AWARD: [{"errno": 0}],
        })
        with mock.patch.object(mod.time, "sleep"):
            self.assertEqual(mod.claim_awards(client, report), "claimed:15")
        self.assertEqual(report["errors"], [])
        self.assertEqual(
            len([c for c in client.calls if c[0] == mod.API_APP_AWARD]), 2
        )

    def test_claim_stops_when_not_advancing(self):
        # 领了一次 received 不动：立刻停手，绝不重复领同一份
        report = {"errors": []}
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}, {}]}]),
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}, {}]}]),
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}, {}]}]),
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}, {}]}]),
            ],
            mod.API_APP_AWARD: [{"errno": 0}],
        })
        with mock.patch.object(mod.time, "sleep"):
            self.assertEqual(mod.claim_awards(client, report), "award_failed")
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("未推进", report["errors"][0])
        self.assertEqual(
            len([c for c in client.calls if c[0] == mod.API_APP_AWARD]), 1
        )

    def test_all_tiers_claimed_elsewhere_is_not_failure(self):
        # 初始有可领档位，但复核时已被别处领完：这是并发不是失败
        report = {"errors": []}
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}]}]),
            ],
        })
        with mock.patch.object(mod.time, "sleep"):
            self.assertEqual(mod.claim_awards(client, report), "no_award_due")
        self.assertEqual(report["errors"], [])
        self.assertEqual([c for c in client.calls if c[0] == mod.API_APP_AWARD], [])


# ---------------------------------------------------------------- 任务完成

class FinishTaskTest(unittest.TestCase):
    def test_done_skips_requests(self):
        client = FakeClient({})
        result = mod.finish_task(client, "uk1", _task(task_status=1))
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [])

    def test_wait_claims_directly(self):
        client = FakeClient({
            mod.API_TASK_SAVE: [{"errno": 0, "result": {"taskStatus": 1}}],
        })
        result = mod.finish_task(client, "uk1", _task(task_status=6))
        self.assertTrue(result["ok"])
        self.assertTrue(result["claimed"])
        self.assertEqual(client.calls[0][1].get("action"), "receive_award")
        self.assertEqual(len(client.calls), 1)

    def test_finish_then_claim(self):
        client = FakeClient({
            mod.API_TASK_SAVE: [
                {"errno": 0, "result": {"taskStatus": 6}},
                {"errno": 0, "result": {"taskStatus": 1, "addScore": 10}},
            ],
        })
        result = mod.finish_task(client, "uk1", _task(task_status=0))
        self.assertTrue(result["ok"])
        self.assertTrue(result["claimed"])
        self.assertEqual(result["score"], 10)
        self.assertNotIn("action", client.calls[0][1])
        self.assertEqual(client.calls[1][1]["action"], "receive_award")

    def test_unfinished_reports_state(self):
        client = FakeClient({
            mod.API_TASK_SAVE: [{"errno": 0, "result": {"taskStatus": 0}}],
        })
        result = mod.finish_task(client, "uk1", _task(task_status=0))
        self.assertFalse(result["ok"])
        self.assertIn("未完成", result["error"])

    def test_business_failure_becomes_error(self):
        client = FakeClient({
            mod.API_TASK_SAVE: [{"errno": 403}],
        })
        result = mod.finish_task(client, "uk1", _task(task_status=0))
        self.assertFalse(result["ok"])
        self.assertIn("403", result["error"])


class TaskSignTest(unittest.TestCase):
    def test_token_matches_formula(self):
        params = mod.task_sign_params(_task(), "uk1")
        expected = hashlib.md5((
            "%s_%s_%s_%s_%s" % (
                "1001", "uk1", params["rand"], params["time"], mod.TASK_SIGN_SALT,
            )
        ).encode("utf-8")).hexdigest()
        self.assertEqual(params["token"], expected)
        self.assertEqual(len(params["token"]), 32)
        self.assertEqual(params["devuid"], params["rand"])

    def test_missing_task_from_raises(self):
        with self.assertRaises(mod.BaiduError):
            mod.task_sign_params(_task(task_from=""), "uk1")

    def test_missing_uk_raises(self):
        with self.assertRaises(mod.BaiduError):
            mod.task_sign_params(_task(), "")


class CountMaxTest(unittest.TestCase):
    def test_json_string(self):
        self.assertEqual(mod.count_max({"task_mhz": '{"count_max": 15}'}), 15)

    def test_dict(self):
        self.assertEqual(mod.count_max({"task_mhz": {"count_max": 3}}), 3)

    def test_fallback_field(self):
        self.assertEqual(mod.count_max({"task_count_max": 5}), 5)

    def test_default(self):
        self.assertEqual(mod.count_max({}), 1)

    def test_broken_json(self):
        self.assertEqual(mod.count_max({"task_mhz": "not json"}), 1)


# ---------------------------------------------------------------- 补签广告

class MakeupTest(unittest.TestCase):
    def _patch(self, tasks_fn, complete_fn):
        return [
            mock.patch.object(mod, "get_makeup_tasks", tasks_fn),
            mock.patch.object(mod, "complete_task", complete_fn),
            mock.patch.object(mod.time, "sleep"),
        ]

    def test_nothing_to_do(self):
        state = {"finish": 3}
        task = {"task_id_str": "m1", "task_type": 70, "task_status": 0,
                "task_mhz": '{"count_max": 3}'}
        patches = self._patch(
            lambda client: [dict(task, task_finish_count=state["finish"])],
            lambda *args, **kwargs: {"errno": 0},
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        result = mod.run_makeup(FakeClient({}), "uk1")
        self.assertEqual(result["state"], "nothing_to_do")

    def test_loop_to_limit(self):
        state = {"finish": 0}
        task = {"task_id_str": "m1", "task_type": 70, "task_status": 0,
                "task_mhz": '{"count_max": 2}'}

        def tasks(_client):
            return [dict(task, task_finish_count=state["finish"])]

        def complete(_client, _uk, _task, action=""):
            state["finish"] += 1
            return {"errno": 0}

        patches = self._patch(tasks, complete)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        result = mod.run_makeup(FakeClient({}), "uk1")
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["completed"], 2)

    def test_local_cap_does_not_report_success(self):
        # 服务端要 100 次、本地上限 5 次时不能报 done
        state = {"finish": 0}
        task = {"task_id_str": "m1", "task_type": 70, "task_status": 0,
                "task_mhz": '{"count_max": 100}'}

        def tasks(_client):
            return [dict(task, task_finish_count=state["finish"])]

        def complete(_client, _uk, _task, action=""):
            state["finish"] += 1
            return {"errno": 0}

        with mock.patch.object(mod, "get_makeup_tasks", tasks), \
                mock.patch.object(mod, "complete_task", complete), \
                mock.patch.object(mod, "MAX_MAKEUP_STEPS", 5), \
                mock.patch.object(mod.time, "sleep"):
            result = mod.run_makeup(FakeClient({}), "uk1")
        self.assertEqual(result["state"], "failed")
        self.assertIn("超过本地上限", result["message"])


# ---------------------------------------------------------------- HTTP 层

class HttpTest(unittest.TestCase):
    def test_redirect_rejected(self):
        session = FakeSession([FakeResponse(302, {"errno": 0})])
        with self.assertRaises(mod.BaiduHTTPError):
            mod.http_json(session, "/x", None, 5, retries=1)
        self.assertFalse(session.calls[0]["allow_redirects"])

    def test_non_json(self):
        session = FakeSession([FakeResponse(200, None)])
        with self.assertRaises(mod.BaiduError):
            mod.http_json(session, "/x", None, 5, retries=1)

    def test_retry_on_network_error(self):
        session = FakeSession([
            requests.RequestException("boom"),
            FakeResponse(200, {"errno": 0}),
        ])
        with mock.patch.object(mod.time, "sleep"):
            data = mod.http_json(session, "/x", None, 5, retries=2)
        self.assertEqual(data["errno"], 0)
        self.assertEqual(len(session.calls), 2)

    def test_params_encoded_with_plus_for_space(self):
        session = FakeSession([FakeResponse(200, {"errno": 0})])
        mod.http_json(session, "/x", {"task_from": "a b"}, 5, retries=1)
        self.assertIn("task_from=a+b", session.calls[0]["url"])

    def test_network_error_message_has_no_query(self):
        # requests 的异常原文会带完整 URL（含 uk/token），不能进日志和通知
        session = FakeSession([
            requests.ConnectionError(
                "boom /api/taskscore/tasksave?uk=123456&token=deadbeef"
            ),
        ])
        with self.assertRaises(mod.BaiduError) as ctx:
            mod.http_json(
                session, "/api/taskscore/tasksave",
                {"uk": "123456", "token": "deadbeef"}, 5, retries=1,
            )
        message = str(ctx.exception)
        self.assertNotIn("uk=123456", message)
        self.assertNotIn("token=", message)
        self.assertIn("ConnectionError", message)

    def test_non_dict_json_rejected(self):
        session = FakeSession([FakeResponse(200, [1, 2, 3])])
        with self.assertRaises(mod.BaiduError):
            mod.http_json(session, "/x", None, 5, retries=1)

    def test_referer_forwarded(self):
        # /act/* 这组接口校验 Referer，必须能按调用覆盖
        session = FakeSession([FakeResponse(200, {"errno": 0})])
        mod.http_json(session, "/x", None, 5, retries=1,
                      referer="https://example.com/page")
        self.assertEqual(
            session.calls[0]["headers"]["Referer"], "https://example.com/page"
        )

    def test_no_headers_when_no_referer(self):
        session = FakeSession([FakeResponse(200, {"errno": 0})])
        mod.http_json(session, "/x", None, 5, retries=1)
        self.assertIsNone(session.calls[0]["headers"])


# ---------------------------------------------------------------- 杂项

class MiscTest(unittest.TestCase):
    def test_split_cookies(self):
        self.assertEqual(
            mod.split_cookies(" BDUSS=a;x=1 \n\n BDUSS=b;y=2 \n"),
            ["BDUSS=a;x=1", "BDUSS=b;y=2"],
        )

    def test_mask_name(self):
        self.assertEqual(mod.mask_name("张三丰", True), "张***丰")
        self.assertEqual(mod.mask_name("李四", True), "***")
        self.assertEqual(mod.mask_name("王五", False), "王五")
        self.assertEqual(mod.mask_name("", True), "未知用户")

    def test_cookie_field(self):
        cookie = "BDUSS=abc; BAIDUCUID=dev-123; STOKEN=x"
        self.assertEqual(mod.cookie_field(cookie, "BAIDUCUID"), "dev-123")
        self.assertEqual(mod.cookie_field(cookie, "MISSING"), "")

    def test_growth_meter_reads_after_failure(self):
        client = FakeClient({
            mod.API_GROWTH: [
                {"errno": 0, "level_info": {"current_value": 100}},
                {"errno": 0, "level_info": {"current_value": 105}},
            ],
        })
        meter = mod.GrowthMeter(client)

        def boom():
            raise mod.BaiduError("响应丢失")

        outcome = meter.step(boom)
        self.assertEqual(outcome["before"], 100)
        self.assertEqual(outcome["after"], 105)
        self.assertEqual(outcome["delta"], 5)
        self.assertIn("响应丢失", outcome["message"])
        self.assertIsNone(outcome["state"])

    def test_growth_meter_rejects_bad_value(self):
        client = FakeClient({
            mod.API_GROWTH: [{"errno": 0, "level_info": {"current_value": "100"}}],
        })
        with self.assertRaises(mod.BaiduError):
            mod.GrowthMeter(client).value()

    def test_growth_meter_before_failure_still_runs_action(self):
        # 读不到基准值时也要照常签到，只是标记「未测量」
        client = FakeClient({
            mod.API_GROWTH: [
                {"errno": 8001},
                {"errno": 0, "level_info": {"current_value": 105}},
            ],
        })
        called = []

        def action():
            called.append(1)
            return "signed"

        outcome = mod.GrowthMeter(client).step(action)
        self.assertEqual(called, [1])
        self.assertEqual(outcome["state"], "signed")
        self.assertIsNone(outcome["before"])
        self.assertEqual(outcome["after"], 105)
        self.assertIsNone(outcome["delta"])
        self.assertEqual(outcome["message"], "")
        self.assertIn("成长值读取失败", " ".join(outcome["warnings"]))

    def test_brief_msg_never_leaks_cookie(self):
        data = {"show_msg": "账户已过期", "Cookie": "BDUSS=secret"}
        self.assertEqual(mod.brief_msg(data), "账户已过期")


# ---------------------------------------------------------------- 多模块隔离

class RunAccountIsolationTest(unittest.TestCase):
    def test_one_module_failure_does_not_stop_others(self):
        config = mod.load_config()
        config["cookie"] = "BDUSS=test; BAIDUCUID=dev"
        config["modules"] = {key: False for key in config["modules"]}
        config["modules"]["pc"] = True
        config["modules"]["app"] = True

        growth = [{"errno": 0, "level_info": {"current_value": 100}},
                  {"errno": 0, "level_info": {"current_value": 104}},
                  {"errno": 0, "level_info": {"current_value": 104}}]
        client = FakeClient({
            mod.API_LOGIN: [{"errno": 0, "login_info": {"uk": 42, "username": "张三"}}],
            mod.API_GROWTH: growth,
            mod.API_SIGNIN: [{"errno": 8001}],          # 网页签到失败
            mod.API_APP_SIGNINLIST: [
                {"errno": 0, "data": {"signed_today": 0}},
                {"errno": 0, "data": {"signed_today": 1}},
            ],
            mod.API_APP_TASKLIST: [{
                "errno": 0,
                "result": {"list": [{"task_type": 166, "task_id_str": "7",
                                     "task_from": "task_sys_daily"}]},
            }],
            mod.API_APP_SIGNIN: [{"errno": 0}],
        })

        with mock.patch.object(mod, "BaiduClient", return_value=client), \
                mock.patch.object(mod, "resolve_cuid", return_value="dev"):
            report = mod.run_account("BDUSS=test", 1, config)

        keys = [step["key"] for step in report["steps"]]
        self.assertEqual(keys, ["pc", "app"])
        self.assertIsNone(report["steps"][0]["state"])
        self.assertEqual(report["steps"][1]["state"], "signed")
        self.assertFalse(report["ok"])


class MeasurementWarningTest(unittest.TestCase):
    def test_measurement_failure_is_warning_not_error(self):
        # 测量只用于展示，读不到不能让账号被判异常
        config = mod.load_config()
        config["cookie"] = "BDUSS=test"
        config["modules"] = {key: False for key in config["modules"]}
        config["modules"]["pc"] = True
        client = FakeClient({
            mod.API_LOGIN: [{"errno": 0,
                             "login_info": {"uk": 42, "username": "张三"}}],
            mod.API_GROWTH: [{"errno": 8001}],
            mod.API_SIGNIN: [{"errno": 0}],
        })
        with mock.patch.object(mod, "BaiduClient", return_value=client), \
                mock.patch.object(mod, "resolve_cuid", return_value="dev"):
            report = mod.run_account("BDUSS=test", 1, config)

        self.assertEqual(report["steps"][0]["state"], "signed")
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["warnings"])
        self.assertTrue(report["ok"], "测量失败不应把账号判成异常")

    def test_claim_awards_ignores_unrelated_warnings(self):
        # 前面有测量告警时，「档位已在别处领完」仍应判为 no_award_due
        report = {"errors": [], "warnings": ["成长值读取失败：boom"]}
        client = FakeClient({
            mod.API_APP_SIGNINLIST: [
                _award_payload(15, [{"day": 15, "received": 0, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}]}]),
                _award_payload(15, [{"day": 15, "received": 1, "prize": [{}]}]),
            ],
        })
        self.assertEqual(mod.claim_awards(client, report), "no_award_due")


class CliArgsTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(mod.parse_args(["prog"]), (False, False, None, None, False))

    def test_probe_and_check(self):
        self.assertTrue(mod.parse_args(["prog", "--probe"])[0])
        self.assertTrue(mod.parse_args(["prog", "--check"])[1])
        self.assertTrue(mod.parse_args(["prog", "--probe-act"])[4])

    def test_try_type_space_and_equals(self):
        self.assertEqual(
            mod.parse_args(["prog", "--try-type", "84"]),
            (False, False, "84", None, False),
        )
        self.assertEqual(
            mod.parse_args(["prog", "--try-type=84", "--source=taskcenter"]),
            (False, False, "84", "taskcenter", False),
        )

    def test_try_type_from_env(self):
        # 青龙「运行」按钮不转发参数，所以诊断开关也要能从环境变量来
        with mock.patch.dict(mod.os.environ, {
            "BAIDU_TRY_TYPE": "84", "BAIDU_TRY_SOURCE": "taskcenter",
        }):
            config = mod.load_config()
        self.assertEqual(config["try_type"], "84")
        self.assertEqual(config["try_source"], "taskcenter")


class TryTaskTypeTest(unittest.TestCase):
    def _config(self):
        config = mod.load_config()
        config["cookie"] = "BDUSS=test"
        return config

    def test_single_attempt_and_recheck(self):
        task = {"task_type": 84, "task_id_str": "t84",
                "task_from": "coin_center_activity", "task_status": 0,
                "task_name": "去百度地图金币商城", "task_score": 800}
        client = FakeClient({
            mod.API_LOGIN: [{"errno": 0,
                             "login_info": {"uk": 42, "username": "张三"}}],
            mod.API_POINTS_BALANCE: [
                {"errno": 0, "balance": 100},
                {"errno": 0, "balance": 900},
            ],
            mod.API_POINTS_TASK_LIST: [
                {"errno": 0, "result": {"list": [task]}},
                {"errno": 0, "result": {"list": [dict(task, task_status=1)]}},
            ],
            mod.API_TASK_SAVE: [{"errno": 0, "result": {"taskStatus": 1}}],
        })
        with mock.patch.object(mod, "BaiduClient", return_value=client), \
                mock.patch.object(mod, "resolve_cuid", return_value="dev"), \
                redirect_stdout(io.StringIO()) as buffer:
            code = mod.try_task_type("BDUSS=test", 1, self._config(), 84, "points")

        self.assertEqual(code, 0)
        saves = [call for call in client.calls if call[0] == mod.API_TASK_SAVE]
        self.assertEqual(len(saves), 1, "试探只允许发一次 tasksave")
        output = buffer.getvalue()
        self.assertIn("status 0 → 1", output)
        self.assertIn("变化 +800", output)

    def test_no_candidate_does_not_write(self):
        task = {"task_type": 84, "task_id_str": "t84",
                "task_from": "coin_center_activity", "task_status": 1,
                "task_name": "去百度地图金币商城", "task_score": 800}
        client = FakeClient({
            mod.API_LOGIN: [{"errno": 0,
                             "login_info": {"uk": 42, "username": "张三"}}],
            mod.API_POINTS_BALANCE: [{"errno": 0, "balance": 100}],
            mod.API_POINTS_TASK_LIST: [{"errno": 0, "result": {"list": [task]}}],
        })
        with mock.patch.object(mod, "BaiduClient", return_value=client), \
                mock.patch.object(mod, "resolve_cuid", return_value="dev"), \
                redirect_stdout(io.StringIO()) as buffer:
            code = mod.try_task_type("BDUSS=test", 1, self._config(), 84, "points")

        self.assertEqual(code, 0)
        self.assertIn("没有可试探", buffer.getvalue())
        self.assertEqual(
            [call for call in client.calls if call[0] == mod.API_TASK_SAVE], []
        )


class GrowthPageTaskTest(unittest.TestCase):
    def test_type_13_and_70_done_86_skipped(self):
        # 成长值页除了广告(70)，浏览类(13)也能自动完成；填表类(86)必须跳过
        config = mod.load_config()
        config["cookie"] = "BDUSS=test"
        config["modules"] = {key: False for key in config["modules"]}
        config["modules"]["ads"] = True
        tasks = [
            {"task_type": 13, "task_id_str": "g13",
             "task_from": "task_sys_task_growth", "task_status": 0,
             "task_score": 5, "task_reward_num": 1},
            {"task_type": 70, "task_id_str": "g70",
             "task_from": "task_sys_task_growth", "task_status": 0,
             "task_score": 6, "task_reward_num": 1},
            {"task_type": 86, "task_id_str": "g86",
             "task_from": "task_sys_task_growth", "task_status": 0,
             "task_score": 5, "task_reward_num": 1},
        ]
        client = FakeClient({
            mod.API_LOGIN: [{"errno": 0,
                             "login_info": {"uk": 42, "username": "张三"}}],
            mod.API_GROWTH: [{"errno": 0, "level_info": {"current_value": 100}}],
            mod.API_TASK_LIST: [{"errno": 0, "result": {"list": tasks}}],
            mod.API_TASK_SAVE: [{"errno": 0, "result": {"taskStatus": 1}}],
        })
        with mock.patch.object(mod, "BaiduClient", return_value=client), \
                mock.patch.object(mod, "resolve_cuid", return_value="dev"), \
                mock.patch.object(mod.time, "sleep"):
            report = mod.run_account("BDUSS=test", 1, config)

        step = report["steps"][0]
        self.assertEqual(step["key"], "ads")
        self.assertEqual(step["stats"]["completed"], 2)
        self.assertEqual(step["stats"]["skipped"], 1)
        saved = [call[1]["task_id"] for call in client.calls
                 if call[0] == mod.API_TASK_SAVE]
        self.assertEqual(saved, ["g13", "g70"])


class ActTaskTest(unittest.TestCase):
    def _config(self):
        config = mod.load_config()
        config["cookie"] = "BDUSS=test"
        return config

    def test_task_type_from_extra(self):
        self.assertEqual(
            mod.act_task_type({"task_extra": '{"task_type":13}'}), 13
        )
        self.assertEqual(
            mod.act_task_type({"task_extra": {"task_type": 70}}), 70
        )
        self.assertEqual(mod.act_task_type({"task_extra": "not json"}), 0)
        # 顶层有 task_type 时兜底
        self.assertEqual(mod.act_task_type({"task_type": 13}), 13)

    def test_sign_token_has_no_salt(self):
        # App 端签名是 md5(task_id_uk_rand_time)，与 PC 端相比少一个 SALT
        params = mod.act_task_params(
            None, "uk1", {"task_id_str": "a1"}, self._config()
        )
        expected = hashlib.md5((
            "a1_uk1_%s_%s" % (params["rand"], params["time"])
        ).encode("utf-8")).hexdigest()
        self.assertEqual(params["token"], expected)
        self.assertEqual(params["task_from"], "netdisk_activity")
        self.assertEqual(params["clienttype"], "110")
        self.assertNotIn("app_id", params)

    def test_missing_task_id_raises(self):
        with self.assertRaises(mod.BaiduError):
            mod.act_task_params(None, "uk1", {}, self._config())

    def test_flatten_dedupes(self):
        groups = {
            "new_all": [{"task_id_str": "a"}, {"task_id_str": "b"}],
            "daily": [{"task_id_str": "b"}, {"task_id_str": "c"}],
        }
        ids = [mod.task_id_of(task) for task in mod.flatten_act_tasks(groups)]
        self.assertEqual(ids, ["a", "b", "c"])

    def test_type_set(self):
        self.assertEqual(mod.type_set("13,70", (1,)), {13, 70})
        self.assertEqual(mod.type_set("13 70", (1,)), {13, 70})
        self.assertEqual(mod.type_set("", (13, 70)), {13, 70})
        self.assertEqual(mod.type_set("abc", (13,)), {13})

    def test_run_completes_whitelisted_only(self):
        config = self._config()
        config["modules"] = {key: False for key in config["modules"]}
        config["modules"]["act"] = True
        groups = {
            "new_all": [
                {"task_id_str": "a13", "task_name": "浏览", "task_status": 0,
                 "task_extra": '{"task_type":13}', "task_score": 4},
                {"task_id_str": "a84", "task_name": "去美团", "task_status": 0,
                 "task_extra": '{"task_type":84}', "task_score": 20},
            ],
            "daily": [
                {"task_id_str": "a70", "task_name": "看广告", "task_status": 0,
                 "task_extra": '{"task_type":70}', "task_score": 5},
                {"task_id_str": "adone", "task_name": "已做", "task_status": 1,
                 "task_extra": '{"task_type":13}', "task_score": 4},
            ],
        }
        client = FakeClient({
            mod.API_LOGIN: [{"errno": 0,
                             "login_info": {"uk": 42, "username": "张三"}}],
            mod.API_GROWTH: [{"errno": 0, "level_info": {"current_value": 100}}],
            mod.API_ACT_TASKLIST: [
                {"errno": 0, "data": {"isQuotaReach": False, **groups}},
            ],
            mod.API_TASK_SAVE: [
                {"errno": 0, "result": {"taskStatus": 1, "addScore": 4}},
            ],
        })
        with mock.patch.object(mod, "BaiduClient", return_value=client), \
                mock.patch.object(mod, "resolve_cuid", return_value="dev"), \
                mock.patch.object(mod.time, "sleep"):
            report = mod.run_account("BDUSS=test", 1, config)

        step = report["steps"][0]
        self.assertEqual(step["key"], "act")
        self.assertEqual(step["stats"]["found"], 4)
        self.assertEqual(step["stats"]["done"], 1)
        self.assertEqual(step["stats"]["completed"], 2)
        self.assertEqual(step["stats"]["skipped"], 1)
        saved = [call[1]["task_id"] for call in client.calls
                 if call[0] == mod.API_TASK_SAVE]
        self.assertEqual(saved, ["a13", "a70"])
        self.assertTrue(report["ok"])

    def test_list_failure_is_warning_not_failure(self):
        # 接口还没校准时拉不到列表，不能把账号判成异常
        config = self._config()
        config["modules"] = {key: False for key in config["modules"]}
        config["modules"]["act"] = True
        client = FakeClient({
            mod.API_LOGIN: [{"errno": 0,
                             "login_info": {"uk": 42, "username": "张三"}}],
            mod.API_GROWTH: [{"errno": 0, "level_info": {"current_value": 100}}],
            mod.API_ACT_TASKLIST: [{"errno": 1011}],
        })
        with mock.patch.object(mod, "BaiduClient", return_value=client), \
                mock.patch.object(mod, "resolve_cuid", return_value="dev"), \
                mock.patch.object(mod.time, "sleep"):
            report = mod.run_account("BDUSS=test", 1, config)

        step = report["steps"][0]
        self.assertEqual(step["state"], "unavailable")
        self.assertEqual(report["errors"], [])
        self.assertTrue(any("App任务列表读取失败" in w for w in report["warnings"]))
        self.assertTrue(report["ok"])


class QuestionWaitTest(unittest.TestCase):
    def test_already_answered_still_claims_wait_task(self):
        # 上一轮答题成功但领奖失败时，任务会一直停在 WAIT，不能因为「已答过」就永久跳过
        config = mod.load_config()
        config["cookie"] = "BDUSS=test"
        config["modules"] = {key: False for key in config["modules"]}
        config["modules"]["question"] = True
        client = FakeClient({
            mod.API_LOGIN: [{"errno": 0,
                             "login_info": {"uk": 42, "username": "张三"}}],
            mod.API_GROWTH: [{"errno": 0, "level_info": {"current_value": 100}}],
            mod.API_QUESTION: [{"errno": 0, "data": {"answer_status": 1}}],
            mod.API_TASK_LIST: [{"errno": 0, "result": {"list": [
                {"task_type": 169, "task_id_str": "9",
                 "task_from": "task_sys_task_growth", "task_status": 6},
            ]}}],
            mod.API_TASK_SAVE: [{"errno": 0, "result": {"taskStatus": 1}}],
        })
        with mock.patch.object(mod, "BaiduClient", return_value=client), \
                mock.patch.object(mod, "resolve_cuid", return_value="dev"):
            report = mod.run_account("BDUSS=test", 1, config)

        saves = [call for call in client.calls if call[0] == mod.API_TASK_SAVE]
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0][1]["action"], "receive_award")
        self.assertEqual(report["steps"][0]["state"], "already_answered")


class MainAccountingTest(unittest.TestCase):
    def test_aborted_account_counts_as_failure(self):
        # 中途抛异常的账号也必须计入总数和退出码，不能只发单账号通知就消失
        config = mod.load_config()
        config["cookie"] = "a\nb\nc"
        config["modules"] = {key: False for key in config["modules"]}
        config["modules"]["pc"] = True
        config["random_signin"] = False

        good = {"index": 1, "username": "甲", "ok": True, "steps": [],
                "errors": [], "growth_before": 1, "growth_after": 2,
                "growth_delta": 1, "level": 1, "product": ""}

        def fake_run(_cookie, index, _config):
            if index == 2:
                raise mod.BaiduError("boom")
            return dict(good, index=index)

        titles = []
        with mock.patch.object(mod, "load_config", return_value=config), \
                mock.patch.object(mod, "run_account", side_effect=fake_run), \
                mock.patch.object(
                    mod, "notify_user",
                    side_effect=lambda title, _content: titles.append(title),
                ), \
                mock.patch.object(mod, "sleep_between"), \
                mock.patch.object(mod.sys, "argv", ["prog"]):
            with redirect_stdout(io.StringIO()):
                code = mod.main()

        self.assertEqual(code, 1)
        self.assertIn("百度网盘签到汇总", titles)


if __name__ == "__main__":
    unittest.main()
