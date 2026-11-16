"""minimax_checkin 单元测试（unittest，全部打桩，不发真实请求）。

运行：python -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minimax_checkin as mod


def status_body(days):
    """构造 status 接口响应。"""
    return {"base_resp": {"status_code": 0}, "data": {"days": days}}


def day(day_no, points, status, is_today=False):
    return {"day_no": day_no, "points": points, "status": status,
            "is_today": is_today, "bonus_points": 0}


class CredentialFileMixin:
    """给每个用例准备独立的凭据目录，通过 MINIMAX_AUTH_FILE 注入。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.auth_path = os.path.join(self.tmpdir.name, ".auth", "auth.json")
        os.makedirs(os.path.dirname(self.auth_path), exist_ok=True)
        self.env = mock.patch.dict(os.environ, {"MINIMAX_AUTH_FILE": self.auth_path,
                                                "MINIMAX_NOTIFY": "false"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmpdir.cleanup()

    def write_auth(self, access="mmoat_test", refresh="mmort_test",
                   expires_at_ms=9999999999999, generation=1):
        payload = {"schemaVersion": 1, "records": {
            "com.minimax.mcode.oauth.prod.cn\0test": {
                "accessToken": access,
                "refreshToken": refresh,
                "tokenType": "Bearer",
                "clientId": "mcode-public",
                "audience": "agent-backend",
                "expiresAtMs": float(expires_at_ms),
                "generation": generation,
                "schemaVersion": 1,
            }}}
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return payload

    def read_auth(self):
        with open(self.auth_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def record(self):
        return next(iter(self.read_auth()["records"].values()))


class TestCredential(CredentialFileMixin, unittest.TestCase):

    def test_auth_file_env_override(self):
        self.assertEqual(mod.auth_file_path(), os.path.abspath(self.auth_path))

    def test_no_credential_returns_none(self):
        access, refresh, expires, path = mod.find_credential()
        self.assertIsNone(access)
        self.assertIsNone(path)

    def test_find_credential_picks_latest_expiry(self):
        payload = self.write_auth()
        payload["records"]["another"] = {
            "accessToken": "mmoat_newer", "refreshToken": "mmort_newer",
            "clientId": "mcode-public", "audience": "agent-backend",
            "expiresAtMs": 99999999999999.0, "generation": 2,
        }
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        access, _, _, _ = mod.find_credential()
        self.assertEqual(access, "mmoat_newer")

    def test_find_credential_skips_other_clients(self):
        """多记录文件里不能挑到别的客户端 token。"""
        payload = self.write_auth()
        payload["records"]["foreign"] = {
            "accessToken": "mmoat_foreign", "refreshToken": "mmort_foreign",
            "clientId": "other-client", "audience": "other-audience",
            "expiresAtMs": 99999999999999.0,
        }
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        access, _, _, _ = mod.find_credential()
        self.assertEqual(access, "mmoat_test")

    def test_string_expires_at_does_not_crash(self):
        payload = self.write_auth()
        payload["records"]["com.minimax.mcode.oauth.prod.cn\0test"]["expiresAtMs"] = "not-a-number"
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        access, _, expires, _ = mod.find_credential()
        self.assertEqual(access, "mmoat_test")
        self.assertEqual(expires, 0.0)

    def test_prefers_record_with_refresh_token(self):
        """access-only 记录过期后无法续期，不能优先于可续期记录被选中。"""
        now_ms = time.time() * 1000
        payload = self.write_auth(access="mmoat_no_refresh", expires_at_ms=now_ms + 7200_000)
        record = next(iter(payload["records"].values()))
        record.pop("refreshToken", None)
        payload["records"]["refreshable"] = {
            "accessToken": "mmoat_with_refresh", "refreshToken": "mmort_x",
            "clientId": "mcode-public", "audience": "agent-backend",
            "expiresAtMs": now_ms + 3600_000, "generation": 2,
        }
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        access, _, _, _ = mod.find_credential()
        self.assertEqual(access, "mmoat_with_refresh")

    def test_checkin_without_credential(self):
        code, result = mod.checkin(claim=True)
        self.assertEqual(code, 2)
        self.assertEqual(result["result"], "NO_AUTH")


class TestCheckinStatus(CredentialFileMixin, unittest.TestCase):
    """覆盖签到状态判定的分支。"""

    def run_checkin(self, days, claim=True, claim_body=None):
        self.write_auth()
        calls = []

        def fake_signed(path, token, method="GET", body=""):
            calls.append((path, method))
            if path == mod.SIGNIN_STATUS_PATH:
                return 200, status_body(days)
            if path == mod.SIGNIN_CLAIM_PATH:
                return 200, claim_body if claim_body is not None else {
                    "base_resp": {"status_code": 0},
                    "data": {"claim_result": 1, "points": 400, "day_no": 1}}
            return 404, {}

        with mock.patch.object(mod, "signed_request", side_effect=fake_signed):
            with redirect_stdout(StringIO()):
                code, result = mod.checkin(claim=claim)
        return code, result, calls

    def test_already_claimed_skips_claim(self):
        """status=3 表示已领取，不应再提交 claim。"""
        code, result, calls = self.run_checkin([day(1, 400, 3, True)])
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "ALREADY")
        self.assertEqual(len(calls), 1, "已领取时不应调用 claim 接口")

    def test_status_one_still_claims(self):
        """回归：实测未领取态是 status=1，必须照常提交 claim。

        上游脚本按 status==2 白名单放行，会把当天的 1 判成不可领取而永远漏签。
        """
        code, result, calls = self.run_checkin([day(1, 400, 1, True)])
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "CLAIMED")
        self.assertIn(mod.SIGNIN_CLAIM_PATH, [c[0] for c in calls])

    def test_status_two_claims(self):
        code, result, _ = self.run_checkin([day(1, 400, 2, True)])
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "CLAIMED")

    def test_claim_idempotent(self):
        """claim_result=2 表示今日已领过，按成功处理。"""
        code, result, _ = self.run_checkin(
            [day(1, 400, 1, True)],
            claim_body={"base_resp": {"status_code": 0},
                        "data": {"claim_result": 2, "points": 400, "day_no": 1}})
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "ALREADY")

    def test_status_only_does_not_claim(self):
        code, result, calls = self.run_checkin([day(1, 400, 1, True)], claim=False)
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "PENDING")
        self.assertEqual(len(calls), 1, "只查状态时不应提交 claim")

    def test_no_today_item(self):
        code, result, _ = self.run_checkin([day(1, 400, 1, False)])
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "INACTIVE")

    def test_empty_days_is_error(self):
        """空面板更可能是接口改版，不能当成「活动未开启」静默成功。"""
        code, result, _ = self.run_checkin([])
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "ERROR")

    def test_days_not_list_is_error(self):
        self.write_auth()
        with mock.patch.object(mod, "signed_request",
                               return_value=(200, {"base_resp": {"status_code": 0},
                                                   "data": {"days": "oops"}})):
            with redirect_stdout(StringIO()):
                code, result = mod.checkin(claim=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "ERROR")

    def test_non_dict_body_does_not_crash(self):
        self.write_auth()
        with mock.patch.object(mod, "signed_request", return_value=(200, [])):
            with redirect_stdout(StringIO()):
                code, result = mod.checkin(claim=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "ERROR")

    def test_session_invalid(self):
        self.write_auth()

        def fake_signed(path, token, method="GET", body=""):
            return 401, {"base_resp": {"status_code": 1022100011,
                                       "status_msg": "session invalid"}}

        with mock.patch.object(mod, "signed_request", side_effect=fake_signed):
            with redirect_stdout(StringIO()):
                code, result = mod.checkin(claim=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "NO_SESSION")

    def test_business_error(self):
        self.write_auth()

        def fake_signed(path, token, method="GET", body=""):
            return 200, {"base_resp": {"status_code": 1406010011,
                                       "status_msg": "invalid timezone_id"}}

        with mock.patch.object(mod, "signed_request", side_effect=fake_signed):
            with redirect_stdout(StringIO()):
                code, result = mod.checkin(claim=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "ERROR")
        self.assertIn("invalid timezone_id", result["report"])

    def test_network_error(self):
        self.write_auth()

        with mock.patch.object(mod, "signed_request", return_value=(-1, {"error": "timeout"})):
            with redirect_stdout(StringIO()):
                code, result = mod.checkin(claim=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "NETWORK")

    def test_claim_business_error_not_treated_as_success(self):
        """业务码非 0 时即使带了 claim_result 也不能算成功。"""
        code, result, _ = self.run_checkin(
            [day(1, 400, 1, True)],
            claim_body={"base_resp": {"status_code": 100001, "status_msg": "boom"},
                        "data": {"claim_result": 1, "points": 400}})
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "ERROR")

    def test_claim_5xx_not_treated_as_success(self):
        """5xx 响应体可能仍带 base_resp=0，不能据此判定领取成功。"""
        self.write_auth()

        def fake_signed(path, token, method="GET", body=""):
            if path == mod.SIGNIN_STATUS_PATH:
                return 200, status_body([day(1, 400, 1, True)])
            return 502, {"base_resp": {"status_code": 0},
                         "data": {"claim_result": 1, "points": 400}}

        with mock.patch.object(mod, "signed_request", side_effect=fake_signed):
            with redirect_stdout(StringIO()):
                code, result = mod.checkin(claim=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "NETWORK")


class TestRefresh(CredentialFileMixin, unittest.TestCase):
    """refresh token 轮换是唯一「写错就废号」的路径，必须覆盖。"""

    def test_refresh_rotates_generation_and_token(self):
        self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=1)
        grant = {"access_token": "mmoat_new", "refresh_token": "mmort_new",
                 "expires_in": 3600}

        with mock.patch.object(mod, "_post_form", return_value=grant):
            with redirect_stdout(StringIO()):
                access, refresh, expires, status = mod.refresh_oauth_token()

        self.assertEqual(status, mod.REFRESH_OK)
        self.assertEqual(access, "mmoat_new")
        self.assertEqual(refresh, "mmort_new")
        self.assertGreater(expires, 0)

        record = self.record()
        self.assertEqual(record["accessToken"], "mmoat_new")
        self.assertEqual(record["refreshToken"], "mmort_new")
        self.assertEqual(record["generation"], 2, "generation 必须递增")

        with open(os.path.join(os.path.dirname(self.auth_path), "auth-state.json"),
                  encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["generation"], 2, "state 与 credential 的 generation 必须一致")
        self.assertEqual(state["status"], "authenticated")

    def test_refresh_skipped_when_still_valid(self):
        """token 还有效时不该发起刷新（避免白白轮换掉 refresh token）。"""
        self.write_auth(expires_at_ms=9999999999999)
        with mock.patch.object(mod, "_post_form") as fake_post:
            with redirect_stdout(StringIO()):
                access, _, _, status = mod.refresh_oauth_token()
        fake_post.assert_not_called()
        self.assertEqual(status, mod.REFRESH_OK)
        self.assertEqual(access, "mmoat_test")

    def test_refresh_request_failure_keeps_old_credential(self):
        """刷新请求本身失败时不能把原凭据写坏。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=1)
        with mock.patch.object(mod, "_post_form", side_effect=RuntimeError("boom")):
            with redirect_stdout(StringIO()):
                access, refresh, _, status = mod.refresh_oauth_token()
        self.assertIsNone(access)
        self.assertEqual(status, mod.REFRESH_TRANSIENT)
        record = self.record()
        self.assertEqual(record["refreshToken"], "mmort_old", "失败时原凭据必须保持不变")
        self.assertEqual(record["generation"], 1)

    def test_refresh_network_error_is_transient(self):
        """网络抖动不能报成 refresh token 失效。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0)
        with mock.patch.object(mod, "_post_form",
                               side_effect=urllib.error.URLError("timed out")):
            with redirect_stdout(StringIO()):
                _, _, _, status = mod.refresh_oauth_token()
        self.assertEqual(status, mod.REFRESH_TRANSIENT)

    def test_refresh_invalid_grant_is_dead(self):
        """服务端明确拒绝 refresh token 才算失效。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0)
        err = urllib.error.HTTPError(mod.OAUTH_TOKEN_ENDPOINT, 400, "bad", {},
                                     io.BytesIO(b'{"error":"invalid_grant"}'))
        with mock.patch.object(mod, "_post_form", side_effect=err):
            with redirect_stdout(StringIO()):
                _, _, _, status = mod.refresh_oauth_token()
        self.assertEqual(status, mod.REFRESH_DEAD)

    def test_refresh_missing_field_does_not_leak_token(self):
        """P0：refresh 响应缺字段时绝不能把响应体（含 token）打进日志。

        缺字段且无明确错误码时按「临时异常」处理（下轮重试），不提示用户重新授权；
        关键是日志里不能出现响应体原文。
        """
        self.write_auth(refresh="mmort_old", expires_at_ms=0)
        bad = {"access_token": "mmoat_LEAKED_SECRET", "expires_in": 3600}
        buf = StringIO()
        with mock.patch.object(mod, "_post_form", return_value=bad):
            with redirect_stdout(buf):
                _, _, _, status = mod.refresh_oauth_token()
        output = buf.getvalue()
        self.assertNotIn("mmoat_LEAKED_SECRET", output, "日志里出现了明文 token")
        self.assertIn("refresh_token", output, "应只报告缺失的字段名")
        self.assertEqual(status, mod.REFRESH_TRANSIENT)

    def test_refresh_persist_failure_saves_recovery(self):
        """P0：已换到新 token 但主文件写不进去时，必须落 recovery 而不是丢弃。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=1)
        grant = {"access_token": "mmoat_new", "refresh_token": "mmort_new",
                 "expires_in": 3600}
        with mock.patch.object(mod, "_post_form", return_value=grant):
            with mock.patch.object(mod, "_write_back", return_value=False):
                with redirect_stdout(StringIO()):
                    access, refresh, _, status = mod.refresh_oauth_token()

        self.assertEqual(status, mod.REFRESH_PERSIST_FAILED)
        self.assertEqual(access, "mmoat_new", "新 token 至少要在内存里可用")
        recovery_path = mod.recovery_file_for(self.auth_path)
        self.assertTrue(os.path.exists(recovery_path), "必须落 recovery 文件")
        with open(recovery_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["refreshToken"], "mmort_new")

    def make_recovery(self, **overrides):
        rec = {
            "schemaVersion": 1,
            "authFile": self.auth_path,
            "recordKey": "com.minimax.mcode.oauth.prod.cn\0test",
            "previousRefreshToken": "mmort_old",
            "accessToken": "mmoat_recovered",
            "refreshToken": "mmort_recovered",
            "expiresAtMs": time.time() * 1000 + 3600_000,
            "generation": 2,
            "clientId": "mcode-public",
            "audience": "agent-backend",
        }
        rec.update(overrides)
        with open(mod.recovery_file_for(self.auth_path), "w", encoding="utf-8") as f:
            json.dump(rec, f)
        return rec

    def test_recovery_is_consumed_next_run(self):
        """主文件仍停在旧 RT 时，recovery 应合并回去。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=1)
        self.make_recovery()
        token, status = mod.get_access_token()
        self.assertEqual(status, mod.REFRESH_OK)
        self.assertEqual(token, "mmoat_recovered")
        self.assertEqual(self.record()["refreshToken"], "mmort_recovered")
        self.assertEqual(self.record()["generation"], 2)
        self.assertFalse(os.path.exists(mod.recovery_file_for(self.auth_path)),
                         "合并成功后应删除 recovery")

    def test_stale_recovery_does_not_overwrite_newer_credential(self):
        """P0 回归：主文件已被别处更新时，recovery 必须丢弃，绝不能覆盖。

        覆盖的后果是把已作废的旧 refresh token 写回去，等于主动废号。
        """
        self.write_auth(access="mmoat_main_new", refresh="mmort_main_new",
                        expires_at_ms=time.time() * 1000 + 7200_000, generation=5)
        self.make_recovery(previousRefreshToken="mmort_old",
                           refreshToken="mmort_recovered", generation=2)

        token, status = mod.get_access_token()

        record = self.record()
        self.assertEqual(record["refreshToken"], "mmort_main_new", "主文件不能被旧 recovery 覆盖")
        self.assertEqual(record["accessToken"], "mmoat_main_new")
        self.assertEqual(record["generation"], 5, "generation 不能被降级")
        self.assertEqual(token, "mmoat_main_new")
        self.assertEqual(status, mod.REFRESH_OK)
        self.assertFalse(os.path.exists(mod.recovery_file_for(self.auth_path)),
                         "过期 recovery 应被丢弃")

    def test_recovery_rebuilds_missing_main_file(self):
        """主文件缺失时用 recovery 重建，而不是返回 NO_AUTH。"""
        self.assertFalse(os.path.exists(self.auth_path), "本用例前提：主文件不存在")
        self.make_recovery()

        token, status = mod.get_access_token()

        self.assertEqual(status, mod.REFRESH_OK)
        self.assertEqual(token, "mmoat_recovered")
        self.assertTrue(os.path.exists(self.auth_path), "应重建主凭据文件")
        self.assertEqual(self.record()["refreshToken"], "mmort_recovered")
        self.assertFalse(os.path.exists(mod.recovery_file_for(self.auth_path)))

    def test_recovery_does_not_touch_foreign_record(self):
        """主文件第一条是别的客户端时，recovery 只能写自己的记录。"""
        payload = self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=1)
        mine_key = next(iter(payload["records"]))
        payload["records"] = {
            "foreign": {
                "accessToken": "mmoat_foreign", "refreshToken": "mmort_foreign",
                "clientId": "other-client", "audience": "other-audience",
                "expiresAtMs": 99999999999999.0, "generation": 9,
            },
            mine_key: payload["records"][mine_key],
        }
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        self.make_recovery()
        mod.get_access_token()

        records = self.read_auth()["records"]
        self.assertEqual(records["foreign"]["refreshToken"], "mmort_foreign",
                         "外来客户端的记录不能被改")
        self.assertEqual(records["foreign"]["generation"], 9)
        self.assertEqual(records[mine_key]["refreshToken"], "mmort_recovered")

    def test_error_description_does_not_leak_token(self):
        """P1：error_description 也可能回显 token，必须脱敏。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0)
        bad = {"error": "server_error", "error_description": "mmort_LEAKED_DESC"}
        buf = StringIO()
        with mock.patch.object(mod, "_post_form", return_value=bad):
            with redirect_stdout(buf):
                mod.refresh_oauth_token()
        self.assertNotIn("mmort_LEAKED_DESC", buf.getvalue())

    def test_error_field_does_not_leak_token(self):
        """P1：error 字段同样可能回显 token。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0)
        bad = {"error": "mmort_LEAK_IN_ERROR"}
        buf = StringIO()
        with mock.patch.object(mod, "_post_form", return_value=bad):
            with redirect_stdout(buf):
                mod.refresh_oauth_token()
        self.assertNotIn("mmort_LEAK_IN_ERROR", buf.getvalue())

    def test_recovery_is_reread_inside_lock(self):
        """P1 回归：recovery 必须在锁内重读。

        场景：本进程启动时 recovery 是 R1；另一个进程持锁合并 R1、刷新到 R2、
        写入新 recovery R2；本进程这时才拿到锁。若沿用锁外读到的 R1，会把 R2 当成
        「已合并」删掉，主文件停在已作废的 R1。
        """
        self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=1)
        self.make_recovery()          # R1: previous=mmort_old, new=mmort_recovered
        auth_dir = os.path.dirname(self.auth_path)

        real_acquire = mod.DirLock.acquire
        state = {"swapped": False}

        def acquire_with_swap(lock_self, timeout=15):
            ok = real_acquire(lock_self, timeout=timeout)
            if ok and not state["swapped"]:
                state["swapped"] = True
                # 模拟另一个进程：先把 R1 合并进主文件
                payload = self.read_auth()
                record = next(iter(payload["records"].values()))
                record["accessToken"] = "mmoat_recovered"
                record["refreshToken"] = "mmort_recovered"
                record["generation"] = 2
                with open(self.auth_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                # 再刷新到 R2 并写入新 recovery
                with open(mod.recovery_file_for(self.auth_path), "w", encoding="utf-8") as f:
                    json.dump({
                        "schemaVersion": 1,
                        "authFile": self.auth_path,
                        "recordKey": "com.minimax.mcode.oauth.prod.cn\0test",
                        "previousRefreshToken": "mmort_recovered",
                        "accessToken": "mmoat_r2",
                        "refreshToken": "mmort_r2",
                        "expiresAtMs": time.time() * 1000 + 3600_000,
                        "generation": 3,
                        "clientId": "mcode-public",
                        "audience": "agent-backend",
                    }, f)
            return ok

        with mock.patch.object(mod.DirLock, "acquire", acquire_with_swap):
            token, status = mod.get_access_token()

        self.assertTrue(state["swapped"], "用例前提：锁内应已替换 recovery")
        self.assertEqual(status, mod.REFRESH_OK)
        self.assertEqual(token, "mmoat_r2", "应消费锁内读到的最新 recovery R2")
        self.assertEqual(self.record()["refreshToken"], "mmort_r2")
        self.assertFalse(os.path.exists(mod.recovery_file_for(self.auth_path)))
        self.assertTrue(os.path.isdir(auth_dir))

    def test_recovery_does_not_downgrade_generation(self):
        """P1 回归：用旧 recovery 重建主文件时 generation 不能倒退。"""
        auth_dir = os.path.dirname(self.auth_path)
        with open(os.path.join(auth_dir, "auth-state.json"), "w", encoding="utf-8") as f:
            json.dump({"schemaVersion": 2, "generation": 9, "status": "authenticated"}, f)
        self.make_recovery(generation=2)

        mod.get_access_token()

        self.assertEqual(self.record()["generation"], 9, "generation 不能从 9 倒退到 2")
        with open(os.path.join(auth_dir, "auth-state.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["generation"], 9)

    def test_cas_merge_keeps_higher_generation(self):
        """CAS 合并同样不能把主文件的 generation 降低。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=7)
        self.make_recovery(generation=2)

        mod.get_access_token()

        self.assertEqual(self.record()["refreshToken"], "mmort_recovered")
        self.assertEqual(self.record()["generation"], 7)

    def test_recovery_without_previous_is_not_merged(self):
        """P1 回归：缺 previousRefreshToken 时 CAS 没有依据，不能合并。"""
        payload = self.write_auth(access="mmoat_only", expires_at_ms=0)
        record = next(iter(payload["records"].values()))
        record.pop("refreshToken", None)      # 模拟 access-only 记录
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        self.make_recovery(previousRefreshToken=None)

        mod.get_access_token()

        record = self.record()
        self.assertNotIn("refreshToken", record, "access-only 记录不应被写入 refresh token")
        self.assertEqual(record["accessToken"], "mmoat_only")
        self.assertFalse(os.path.exists(mod.recovery_file_for(self.auth_path)),
                         "无法校验的 recovery 应被丢弃")

    def test_record_key_pointing_to_wrong_record_falls_back(self):
        """recordKey 指错记录时，应按 previousRefreshToken 回填到正确记录。"""
        payload = self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=1)
        mine_key = next(iter(payload["records"]))
        payload["records"]["wrong"] = {
            "accessToken": "mmoat_wrong", "refreshToken": "mmort_wrong",
            "clientId": "mcode-public", "audience": "agent-backend",
            "expiresAtMs": 99999999999999.0, "generation": 1,
        }
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        self.make_recovery(recordKey="wrong", previousRefreshToken="mmort_old")
        mod.get_access_token()

        records = self.read_auth()["records"]
        self.assertEqual(records[mine_key]["refreshToken"], "mmort_recovered",
                         "应按 previous 回填到正确记录")
        self.assertEqual(records["wrong"]["refreshToken"], "mmort_wrong",
                         "recordKey 指错的记录不能被改")

    def test_state_write_failure_still_counts_as_success(self):
        """P1：credential 写成功、state 写失败时，整轮刷新必须算成功。

        否则凭据其实已换新，脚本却继续用旧 token，白白拿到 401 并被误报成登录态失效。
        """
        self.write_auth(refresh="mmort_old", expires_at_ms=0, generation=1)
        grant = {"access_token": "mmoat_new", "refresh_token": "mmort_new",
                 "expires_in": 3600}
        real_atomic = mod._atomic_write_json
        calls = {"n": 0}

        def flaky(path, payload):
            calls["n"] += 1
            if calls["n"] >= 2:      # 第二次是 auth-state.json
                raise OSError("disk full")
            return real_atomic(path, payload)

        with mock.patch.object(mod, "_post_form", return_value=grant):
            with mock.patch.object(mod, "_atomic_write_json", side_effect=flaky):
                with redirect_stdout(StringIO()):
                    access, _, _, status = mod.refresh_oauth_token()

        self.assertEqual(status, mod.REFRESH_OK)
        self.assertEqual(access, "mmoat_new")
        self.assertEqual(self.record()["refreshToken"], "mmort_new")

    def test_network_failure_not_reported_as_session_lost(self):
        """P1：刷新遇网络问题时不能提示用户重新授权。"""
        self.write_auth(refresh="mmort_old", expires_at_ms=0)
        with mock.patch.object(mod, "_post_form",
                               side_effect=urllib.error.URLError("timed out")):
            with redirect_stdout(StringIO()):
                code, result = mod.checkin(claim=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "NETWORK")
        self.assertNotIn("login", result["report"])


class TestDirLock(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.lock_path = os.path.join(self.tmpdir.name, "auth.lock")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_lock_path_is_auth_lock(self):
        """P1：锁目录必须是 auth.lock，与客户端同名才能互斥。"""
        lock = mod.DirLock(self.lock_path)
        self.assertEqual(lock.path, self.lock_path)
        self.assertNotIn("auth.lock.lock", lock.path)

    def test_acquire_and_release(self):
        lock = mod.DirLock(self.lock_path)
        self.assertTrue(lock.acquire(timeout=2))
        self.assertTrue(os.path.isdir(self.lock_path))
        lock.release()
        self.assertFalse(os.path.exists(self.lock_path))

    def test_stale_regular_file_does_not_hang(self):
        """P1：锁路径被普通文件占用时不能死循环。"""
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write("stale")
        os.utime(self.lock_path, (0, 0))
        lock = mod.DirLock(self.lock_path, stale_sec=1)
        start = time.time()
        ok = lock.acquire(timeout=3)
        elapsed = time.time() - start
        self.assertLess(elapsed, 8, "stale 清理不应无限重试")
        self.assertTrue(ok, "清掉残留文件后应能拿到锁")
        lock.release()

    def test_release_does_not_remove_foreign_lock(self):
        """P1：锁被别的进程接管后，release 不能删掉对方的锁。"""
        lock = mod.DirLock(self.lock_path)
        self.assertTrue(lock.acquire(timeout=2))
        with open(os.path.join(self.lock_path, "holder"), "w", encoding="utf-8") as f:
            f.write("pid:999999,0,someone-else")
        lock.release()
        self.assertTrue(os.path.isdir(self.lock_path), "别人的锁不能被删掉")

    def test_lock_timeout_returns_false(self):
        lock = mod.DirLock(self.lock_path)
        self.assertTrue(lock.acquire(timeout=2))
        other = mod.DirLock(self.lock_path)
        self.assertFalse(other.acquire(timeout=1), "锁被占用时应超时返回 False")
        lock.release()


class TestSignature(unittest.TestCase):

    def test_signature_headers_present(self):
        captured = {}

        class FakeResp:
            status = 200

            def read(self):
                return b'{"ok":1}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["headers"] = req.headers
            captured["url"] = req.full_url
            return FakeResp()

        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            code, _ = mod.signed_request(mod.SIGNIN_STATUS_PATH, "mmoat_x")

        self.assertEqual(code, 200)
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        for name in ("x-timestamp", "x-signature", "yy", "authorization"):
            self.assertIn(name, headers)
        self.assertEqual(headers["authorization"], "Bearer mmoat_x")
        self.assertIn("timezone_offset=28800", captured["url"])

    def test_redact_masks_tokens(self):
        """DEBUG 输出必须脱敏，不能把 token 打进日志。"""
        text = '{"accessToken":"mmoat_abcdef123456","refreshToken":"mmort_xyz789"}'
        masked = mod._redact(text)
        self.assertNotIn("abcdef123456", masked)
        self.assertNotIn("xyz789", masked)
        self.assertIn("mmoat_***", masked)


class TestDeviceLogin(CredentialFileMixin, unittest.TestCase):

    def test_device_login_success(self):
        code_body = {"device_code": "dc", "user_code": "UC-123",
                     "verification_uri": "https://example.com/auth",
                     "expires_in": 30, "interval": 1}
        token_body = {"access_token": "mmoat_a", "refresh_token": "mmort_b",
                      "expires_in": 3600}
        with mock.patch.object(mod, "_post_form", side_effect=[code_body, token_body]):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code, result = mod.device_login()
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "AUTHED")
        record = self.record()
        self.assertEqual(record["accessToken"], "mmoat_a")
        self.assertEqual(record["refreshToken"], "mmort_b")

    def test_device_login_missing_fields(self):
        with mock.patch.object(mod, "_post_form", return_value={"device_code": "dc"}):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code, result = mod.device_login()
        self.assertEqual(code, 1)
        self.assertEqual(result["result"], "ERROR")

    def test_device_login_bad_expires_value(self):
        code_body = {"device_code": "dc", "user_code": "UC-123",
                     "verification_uri": "https://example.com/auth",
                     "expires_in": "abc", "interval": None}
        token_body = {"access_token": "mmoat_a", "refresh_token": "mmort_b"}
        with mock.patch.object(mod, "_post_form", side_effect=[code_body, token_body]):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code, _ = mod.device_login()
        self.assertEqual(code, 0, "非法 expires_in 应回退默认值而不是崩溃")

    def test_device_login_creates_missing_auth_dir(self):
        """P1 回归：首次部署时 .auth 目录还不存在，login 也必须能成功。"""
        shutil.rmtree(os.path.dirname(self.auth_path))
        self.assertFalse(os.path.exists(os.path.dirname(self.auth_path)))

        code_body = {"device_code": "dc", "user_code": "UC-123",
                     "verification_uri": "https://example.com/auth",
                     "expires_in": 30, "interval": 1}
        token_body = {"access_token": "mmoat_a", "refresh_token": "mmort_b",
                      "expires_in": 3600}
        with mock.patch.object(mod, "_post_form", side_effect=[code_body, token_body]):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code, result = mod.device_login()

        self.assertEqual(code, 0, "目录缺失不应导致 login 失败")
        self.assertEqual(result["result"], "AUTHED")
        self.assertTrue(os.path.exists(self.auth_path), "应自动建目录并写入凭据")

    def test_device_login_gives_up_when_lock_busy(self):
        """拿不到锁时必须放弃写入，不能覆盖别的进程刚刷好的凭据。"""
        with mock.patch.object(mod.DirLock, "acquire", return_value=False):
            saved = mod._store_new_credential("mmoat_a", "mmort_b", 3600_000)
        self.assertFalse(saved)
        self.assertFalse(os.path.exists(self.auth_path), "锁冲突时不应写文件")


class TestReport(unittest.TestCase):

    def test_report_contains_result(self):
        text = mod.build_report(0, {"result": "CLAIMED", "report": "成功领取 400 积分",
                                    "points": 400, "day_no": 1})
        self.assertIn("成功领取 400 积分", text)
        self.assertIn("400", text)

    def test_failure_report_has_hint(self):
        text = mod.build_report(1, {"result": "NO_SESSION", "report": "登录态已失效"})
        self.assertIn("login", text)


class TestMain(CredentialFileMixin, unittest.TestCase):

    def run_main(self, argv, checkin_result=None, checkin_side_effect=None,
                 notify_enabled=False):
        """返回 (exit_code, send_notify_mock)。默认关推送，避免测试真的发通知。"""
        args = ["minimax_checkin.py"] + argv
        with mock.patch.object(mod, "NOTIFY_ENABLED", notify_enabled), \
                mock.patch.object(mod, "send_notify") as fake_notify:
            with mock.patch.object(sys, "argv", args):
                if checkin_side_effect is not None:
                    with mock.patch.object(mod, "checkin", side_effect=checkin_side_effect):
                        with redirect_stdout(StringIO()):
                            return mod.main(), fake_notify
                with mock.patch.object(mod, "checkin", return_value=checkin_result):
                    with redirect_stdout(StringIO()):
                        return mod.main(), fake_notify

    def test_unexpected_exception_returns_1(self):
        """P2：未预期异常按运行失败处理，退出码 2 只留给配置/凭据缺失。"""
        code, _ = self.run_main(["auto"], checkin_side_effect=RuntimeError("boom"))
        self.assertEqual(code, 1)

    def test_check_mode_does_not_push(self):
        code, fake_notify = self.run_main(
            ["--check"], checkin_result=(0, {"result": "ALREADY", "report": "已签"}),
            notify_enabled=True)
        self.assertEqual(code, 0)
        fake_notify.assert_not_called()

    def test_auto_mode_pushes_when_enabled(self):
        """对照组：证明上面那条不是因为推送总开关关掉才「没推送」。"""
        code, fake_notify = self.run_main(
            ["auto"], checkin_result=(0, {"result": "ALREADY", "report": "已签"}),
            notify_enabled=True)
        self.assertEqual(code, 0)
        fake_notify.assert_called_once()

    def test_unknown_command_returns_2(self):
        code, _ = self.run_main(["nonsense"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
