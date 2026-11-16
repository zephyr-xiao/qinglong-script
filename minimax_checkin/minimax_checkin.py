#!/usr/bin/env python3
"""MiniMax Code 每日积分签到（OAuth device flow 版）

cron: 20 7 * * *
new Env('MiniMax Code 签到')

为什么走 device flow 而不是抓客户端凭据：
MiniMax 桌面端 2026-10-19 起改成 Shared MCode OAuth 架构，登录态落在
~/.minimax/auth/<env>/<region>/mcode-public/auth.json，里面是不透明的 mmoat_ token。
这个 token 由 account.minimax.cn 的标准 RFC 8628 device flow 签发，client_id 是公开的
`mcode-public`——也就是说脚本可以自己走一遍授权流程，不需要装任何客户端。

签到本身不需要订阅：每日 400 积分，连签第 4/7 天各 1000，积分 30 天过期。

refresh token 是轮换制（服务端每次刷新都换发新的，旧的立即作废），所以刷新必须
原子写回 auth.json，否则下次就是 invalid_grant。本脚本做了三层保护：
  1. 与客户端同名的 auth.lock 目录锁，避免两边并发刷新互相踢下线；
  2. 原子写（临时文件 + os.replace + fsync），不会留下半写文件；
  3. 写回失败时把新 grant 落到 auth-refresh.pending.json，下次运行优先消费，
     绝不静默丢弃——否则旧 token 已作废、新 token 又没存，等于废号。

环境变量：
  MINIMAX_AUTH_FILE        凭据文件路径，默认 <脚本目录>/.auth/auth.json
                           青龙里建议指到 /ql/data 下的持久目录
  MINIMAX_NOTIFY           true/false，默认 true，是否走青龙 notify.py 推送
  MINIMAX_NOTIFY_ONLY_FAIL true/false，默认 false，仅失败时推送
  MINIMAX_TIMEOUT          HTTP 超时秒数，默认 20，允许 5-120
  MINIMAX_DEBUG            true/false，默认 false，打印接口响应（已脱敏）
  MINIMAX_DESKTOP_VERSION  模拟的桌面端版本号，默认 3.1.6
  MINIMAX_USER_ID          签名参数 user_id，默认 0（实测服务端不严格校验）

用法：
  python minimax_checkin.py auto      查状态 -> 今日可领才领（日常定时用这个）
  python minimax_checkin.py status    仅查状态，不提交
  python minimax_checkin.py login     device flow 重新授权（需人工在浏览器确认）
  python minimax_checkin.py --check   等价 status，且默认不推送（自检用）

退出码：0 正常（含今日已签）/ 1 签到失败或登录态失效 / 2 配置或凭据缺失
"""

import base64
import errno
import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ============================ 接口常量 ============================

API_BASE = "https://agent.minimax.cn"
OAUTH_TOKEN_ENDPOINT = "https://account.minimax.cn/oauth2/token"
OAUTH_DEVICE_ENDPOINT = "https://account.minimax.cn/oauth2/device/code"
OAUTH_CLIENT_ID = "mcode-public"
OAUTH_SCOPE = "agent.default"
OAUTH_AUDIENCE = "agent-backend"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

SIGNIN_STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
SIGNIN_CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"

# 签到面板里 today.status 的语义（2026-11-16 实测 status 接口确认）：
#   3 = 已领取；1 = 未领取（未来天数实测都是 1）。
# 上游脚本假设「可领取时 status=2」，但实测未领取态是 1——若按白名单放行 2，
# 当天的 1 会被误判成「今日不可领取」而永远不提交 claim。因此这里只把 3 当已领，
# 其余状态一律交给服务端裁决（claim 是幂等的，重复领取返回 claim_result=2）。
DAY_STATUS_CLAIMED = 3

# claim_result: 1 = 本次领取成功，2 = 今日已领过（幂等）
CLAIM_RESULT_OK = 1
CLAIM_RESULT_ALREADY = 2

# 登录态失效的业务码
SESSION_INVALID_CODE = 1022100011

# 明确表示 refresh token 已失效的 OAuth 错误码（这类才需要重新授权）
REFRESH_DEAD_CODES = ("invalid_grant", "invalid_token", "unauthorized_client")

# 刷新失败的分类，用于区分「真失效」和「网络抖动」
REFRESH_OK = "OK"
REFRESH_DEAD = "DEAD"              # refresh token 被服务端拒绝，必须重新 login
REFRESH_TRANSIENT = "TRANSIENT"    # 网络/服务端临时故障，下轮重试即可
REFRESH_PERSIST_FAILED = "PERSIST_FAILED"  # 已换到新 token 但没写进主文件（已落 recovery）

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ============================ 环境配置 ============================

def env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name, default, low, high):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(low, min(high, value))


NOTIFY_ENABLED = env_bool("MINIMAX_NOTIFY", True)
NOTIFY_ONLY_FAIL = env_bool("MINIMAX_NOTIFY_ONLY_FAIL", False)
DEBUG = env_bool("MINIMAX_DEBUG", False)
REQUEST_TIMEOUT = env_int("MINIMAX_TIMEOUT", 20, 5, 120)
DESKTOP_VERSION = os.environ.get("MINIMAX_DESKTOP_VERSION", "3.1.6").strip() or "3.1.6"
USER_ID = os.environ.get("MINIMAX_USER_ID", "0").strip() or "0"

# 锁的 stale 阈值必须大于一次请求的最长耗时，否则慢刷新会被别的进程判成死锁抢走
LOCK_STALE_SEC = max(90, REQUEST_TIMEOUT * 2)


def auth_file_path():
    """凭据文件位置。默认落在脚本目录的 .auth/ 下，保证青龙重启后仍在。"""
    override = (os.environ.get("MINIMAX_AUTH_FILE")
                or os.environ.get("MINIMAX_OAUTH_AUTH_FILE") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override.strip("\"'")))
    return os.path.join(SCRIPT_DIR, ".auth", "auth.json")


def recovery_file_for(auth_path):
    """刷新已成功但主文件写失败时的临时落盘位置（同目录，0600）。"""
    return os.path.join(os.path.dirname(auth_path), "auth-refresh.pending.json")


# ============================ 推送 ============================

_qinglong_send = None
try:
    from notify import send as _qinglong_send  # type: ignore
except Exception:
    _scripts_dir = "/ql/data/scripts"
    if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
        try:
            from notify import send as _qinglong_send  # type: ignore
        except Exception:
            _qinglong_send = None


def send_notify(title, content):
    """通过青龙 notify.py 推送，带 3 次重试。返回是否成功。"""
    if not _qinglong_send:
        print("[INFO] 未找到 notify.py（仅青龙环境内可用），跳过推送")
        return False
    for attempt in range(1, 4):
        try:
            _qinglong_send(title, content)
            print("[PUSH] 已通过 notify.py 推送")
            return True
        except Exception as e:
            print("[WARN] 推送第 %d/3 次失败: %s" % (attempt, type(e).__name__))
            if attempt < 3:
                time.sleep(3)
    print("[WARN] notify.py 推送最终失败")
    return False


# ============================ 跨进程锁 ============================

class DirLock:
    """proper-lockfile 风格的目录锁，与桌面端的自动刷新互斥。

    客户端用 auth.lock 目录 + holder 文件 + stale 判定；这里复刻同一套语义，
    这样脚本和客户端同时在线时不会各自拿旧 refresh token 去换、把对方踢掉。
    调用方传的就是锁目录全路径（不再自动追加后缀），保证与客户端的 auth.lock 对齐。
    """

    def __init__(self, lock_dir, stale_sec=None):
        self.path = lock_dir
        self.stale_sec = stale_sec if stale_sec is not None else LOCK_STALE_SEC
        # owner 用于 release 时确认锁还是自己那把，避免删掉别人刚抢到的锁
        self.owner = "pid:%d,%s,%s" % (os.getpid(), time.time(), secrets.token_hex(6))
        self.acquired = False

    def _holder(self):
        try:
            with open(os.path.join(self.path, "holder"), "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""

    def _is_stale(self):
        """只按 mtime 判 stale（对齐 proper-lockfile）。

        不能把「同 pid」当 stale：同进程内两个锁实例会互相抢锁，互斥就失效了。
        进程异常退出留下的锁，mtime 过了阈值同样会被清理。
        """
        try:
            age = time.time() - os.stat(self.path).st_mtime
        except Exception:
            return True
        return age > self.stale_sec

    def _remove_stale(self):
        """清掉残留锁。普通文件/目录都要能处理，且不能因为删不掉就无限重试。"""
        if os.path.isdir(self.path) and not os.path.islink(self.path):
            shutil.rmtree(self.path, ignore_errors=True)
        else:
            try:
                os.unlink(self.path)
            except Exception:
                pass

    def acquire(self, timeout=15):
        # 首次部署时凭据目录可能还不存在，先建出来，否则 os.mkdir 直接 FileNotFoundError
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        except Exception:
            pass
        deadline = time.time() + timeout
        while time.time() <= deadline:
            try:
                os.mkdir(self.path, 0o700)
                try:
                    with open(os.path.join(self.path, "holder"), "w", encoding="utf-8") as f:
                        f.write(self.owner)
                except Exception:
                    pass
                self.acquired = True
                return True
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                if self._is_stale():
                    self._remove_stale()
                    time.sleep(0.1)
                    continue
                time.sleep(0.2)
        return False

    def release(self):
        if not self.acquired:
            return
        self.acquired = False
        holder = self._holder()
        # holder 读不到时无法确认锁的归属，宁可不删，交给 stale 机制清理
        if holder != self.owner:
            return
        self._remove_stale()

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("获取锁超时: %s" % self.path)
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ============================ 凭据读写 ============================

def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print("[WARN] 读取 %s 失败: %s" % (os.path.basename(path), type(e).__name__))
        return None


def _atomic_write_json(path, payload):
    """原子写 + 0600 权限 + fsync，避免断电留下半写文件。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".auth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _record_matches(rec):
    """只认本脚本/本客户端签发的记录，避免多记录文件里挑到别的客户端 token。"""
    if not isinstance(rec, dict) or not rec.get("accessToken"):
        return False
    client = rec.get("clientId")
    if client and client != OAUTH_CLIENT_ID:
        return False
    audience = rec.get("audience")
    if audience and audience != OAUTH_AUDIENCE:
        return False
    return True


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def find_credential():
    """读取凭据文件，返回 (access_token, refresh_token, expires_at_ms, path)。

    多记录时优先取「带 refreshToken 且未过期最晚」的本客户端记录：只有 access
    没有 refresh 的记录一旦过期就无法续期，选中它等于直接 DEAD。
    """
    path = auth_file_path()
    payload = read_json(path)
    if not isinstance(payload, dict):
        return None, None, 0, None
    records = payload.get("records")
    if not isinstance(records, dict):
        return None, None, 0, None
    best = None
    best_refreshable = False
    for rec in records.values():
        if not _record_matches(rec):
            continue
        expires_at = _as_number(rec.get("expiresAtMs"))
        refreshable = bool(rec.get("refreshToken"))
        better = best is None
        if not better:
            if refreshable and not best_refreshable:
                better = True
            elif refreshable == best_refreshable and expires_at > best[2]:
                better = True
        if better:
            best = (rec["accessToken"], rec.get("refreshToken"), expires_at, path)
            best_refreshable = refreshable
    if best:
        return best
    return None, None, 0, None


def _write_back(auth_path, match_refresh, new_access, new_refresh, expires_at_ms,
                new_generation=None):
    """刷新后把新凭据写回，其余字段保持不变。"""
    payload = read_json(auth_path)
    if not isinstance(payload, dict):
        return False
    records = payload.get("records")
    if not isinstance(records, dict):
        return False
    target_key = None
    for key, rec in records.items():
        if isinstance(rec, dict) and rec.get("refreshToken") == match_refresh:
            target_key = key
            break
    if target_key is None:
        return False
    rec = records[target_key]
    rec["accessToken"] = new_access
    rec["refreshToken"] = new_refresh
    rec["expiresAtMs"] = float(expires_at_ms)
    if new_generation is not None:
        rec["generation"] = int(new_generation)
    try:
        _atomic_write_json(auth_path, payload)
        return True
    except Exception as e:
        print("[WARN] 凭据写回失败: %s" % type(e).__name__)
        return False


def _state_file_for(auth_path):
    return os.path.join(os.path.dirname(auth_path), "auth-state.json")


def _commit_refresh(auth_path, state_path, match_refresh, grant, new_generation):
    """复刻客户端的提交阶段：先写 credential 再写 state。

    credential 是脚本自己用的唯一真相源，只要它落盘成功就算刷新成功；
    state 只是给桌面端读的元信息，写失败只告警，不能因此判定整轮刷新失败
    （否则凭据其实已经换了新的，脚本却继续用旧 token，白白拿到 401）。
    """
    if not _write_back(auth_path, match_refresh, grant["access_token"],
                       grant["refresh_token"], grant["expires_at_ms"], new_generation):
        return False
    state = read_json(state_path)
    if not isinstance(state, dict):
        state = {"schemaVersion": 2}
    state["status"] = "authenticated"
    state["generation"] = int(new_generation)
    state["scopes"] = [OAUTH_SCOPE]
    state["audience"] = OAUTH_AUDIENCE
    state["clientId"] = OAUTH_CLIENT_ID
    state["storeKind"] = "file"
    state.pop("refresh", None)
    try:
        _atomic_write_json(state_path, state)
    except Exception as e:
        print("[WARN] auth-state 写入失败（credential 已更新，不影响本轮）: %s"
              % type(e).__name__)
    return True


# ---- 刷新成功但主文件写不进去时的兜底落盘 ----
#
# 语义（别简化）：recovery 只有在「主文件里目标记录的 refreshToken 仍等于本次刷新
# 用的旧 RT」时才允许合并（compare-and-swap）。主文件已被别处更新（重新 login、
# 桌面端刷新、另一个进程刷新）时，recovery 就是过期数据，必须丢弃——否则会把
# 新凭据覆盖成已作废的旧凭据，等于主动废号。

def _record_from_recovery(rec):
    """把 recovery payload 转成主文件里的 record 结构。"""
    return {
        "accessToken": rec.get("accessToken"),
        "refreshToken": rec.get("refreshToken"),
        "tokenType": "Bearer",
        "clientId": OAUTH_CLIENT_ID,
        "scopes": [OAUTH_SCOPE],
        "audience": OAUTH_AUDIENCE,
        "expiresAtMs": _as_number(rec.get("expiresAtMs")),
        "generation": int(_as_number(rec.get("generation"))),
        "schemaVersion": 1,
    }


def _fallback_record_key(auth_path):
    account = base64.urlsafe_b64encode(hashlib.sha256(
        ("%s\0%s" % (os.path.dirname(auth_path), OAUTH_CLIENT_ID)).encode()).digest()
    ).decode().rstrip("=")
    return "com.minimax.mcode.oauth.prod.cn\0%s" % account


def _find_record_key(auth_path, refresh_token):
    """按 refreshToken 定位记录键，供 recovery 精确回填。"""
    payload = read_json(auth_path)
    if not isinstance(payload, dict):
        return None
    records = payload.get("records")
    if not isinstance(records, dict):
        return None
    for key, rec in records.items():
        if isinstance(rec, dict) and rec.get("refreshToken") == refresh_token:
            return key
    return None


def _state_generation(auth_path):
    state = read_json(_state_file_for(auth_path))
    if isinstance(state, dict):
        return int(_as_number(state.get("generation")))
    return 0


def _write_state(auth_path, generation):
    """写 auth-state.json。generation 只增不减，避免合并旧 recovery 时倒退。"""
    state_path = _state_file_for(auth_path)
    state = read_json(state_path)
    if not isinstance(state, dict):
        state = {"schemaVersion": 2}
    generation = max(int(generation), int(_as_number(state.get("generation"))))
    state.update({
        "status": "authenticated",
        "generation": int(generation),
        "scopes": [OAUTH_SCOPE],
        "audience": OAUTH_AUDIENCE,
        "clientId": OAUTH_CLIENT_ID,
        "storeKind": "file",
        "buildEnv": "prod",
        "region": "cn",
    })
    state.pop("refresh", None)
    try:
        _atomic_write_json(state_path, state)
    except Exception as e:
        print("[WARN] auth-state 写入失败（凭据已保存）: %s" % type(e).__name__)


def _save_recovery(auth_path, grant, generation, previous_refresh, record_key):
    """把已换到的新 grant 落盘，避免「旧 token 已作废、新 token 又丢了」直接废号。

    必须同时记下 previousRefreshToken 和 recordKey：下次合并靠它做 CAS，
    确认主文件仍停在本次刷新的起点，才允许覆盖。
    """
    path = recovery_file_for(auth_path)
    payload = {
        "schemaVersion": 1,
        "authFile": auth_path,
        "recordKey": record_key,
        "previousRefreshToken": previous_refresh,
        "accessToken": grant["access_token"],
        "refreshToken": grant["refresh_token"],
        "expiresAtMs": grant["expires_at_ms"],
        "generation": int(generation),
        "clientId": OAUTH_CLIENT_ID,
        "audience": OAUTH_AUDIENCE,
        "savedAt": time.time(),
    }
    try:
        _atomic_write_json(path, payload)
        return True
    except Exception as e:
        print("[ERROR] recovery 落盘失败: %s" % type(e).__name__)
        return False


def _locate_record(records, rec):
    """定位 recovery 要回填的记录。

    顺序：previousRefreshToken 精确匹配 → recordKey（且必须是本客户端记录）→
    本客户端记录。previous 命中但属于别的客户端时直接放弃，不能污染外来记录。
    """
    prev = rec.get("previousRefreshToken")
    if prev:
        for rec_key, candidate in records.items():
            if not isinstance(candidate, dict) or candidate.get("refreshToken") != prev:
                continue
            if _record_matches(candidate):
                return rec_key, candidate
            print("[WARN] previousRefreshToken 命中了非本客户端的记录，拒绝写入")
            return None, None
    key = rec.get("recordKey")
    if key and isinstance(records.get(key), dict) and _record_matches(records[key]):
        return key, records[key]
    for rec_key, candidate in records.items():
        if _record_matches(candidate):
            return rec_key, candidate
    return None, None


def _consume_recovery(auth_path):
    """把 recovery 里的新凭据合并回主文件。

    返回 (access, refresh, expires_at_ms) 表示已恢复；None 表示无需恢复或已丢弃。
    两个关键点：
      1. recovery 必须在**拿到锁之后**才读——锁只保证写入互斥，不保证锁外读到的那份
         还是最新的；锁外读会误删别的进程刚写入的新 recovery。
      2. 合并用 previousRefreshToken 做 CAS，缺这个字段就没有「主文件仍停在刷新起点」
         的证据，一律不合并。
    """
    path = recovery_file_for(auth_path)
    if not os.path.exists(path):
        return None

    lock = DirLock(os.path.join(os.path.dirname(auth_path), "auth.lock"))
    if not lock.acquire(timeout=15):
        # 拿不到锁说明别的进程正在动凭据，下轮再说
        return None
    try:
        # 锁内重读：别的进程可能刚写入了更新的 recovery
        rec = read_json(path)
        if rec is None:
            return None
        if not isinstance(rec, dict):
            _remove_quietly(path)
            return None

        recorded_auth = rec.get("authFile")
        if recorded_auth and os.path.abspath(recorded_auth) != os.path.abspath(auth_path):
            print("[WARN] recovery 属于其他凭据文件（%s），跳过"
                  % os.path.basename(recorded_auth))
            return None

        new_access = rec.get("accessToken")
        new_refresh = rec.get("refreshToken")
        if not new_access or not new_refresh:
            print("[WARN] recovery 文件不完整，已丢弃")
            _remove_quietly(path)
            return None

        main = read_json(auth_path)
        records = main.get("records") if isinstance(main, dict) else None
        state_generation = _state_generation(auth_path)

        if not isinstance(records, dict):
            # 主文件缺失/损坏：此时 recovery 是唯一真相源，直接重建
            generation = max(int(_as_number(rec.get("generation"))), state_generation)
            key = rec.get("recordKey") or _fallback_record_key(auth_path)
            record = _record_from_recovery(rec)
            record["generation"] = generation
            payload = {"schemaVersion": 1, "records": {key: record}}
            try:
                _atomic_write_json(auth_path, payload)
            except Exception as e:
                print("[ERROR] 用 recovery 重建主凭据失败: %s" % type(e).__name__)
                return new_access, new_refresh, _as_number(rec.get("expiresAtMs"))
            _write_state(auth_path, generation)
            _remove_quietly(path)
            print("[INFO] 主凭据缺失，已用 recovery 重建")
            return new_access, new_refresh, _as_number(rec.get("expiresAtMs"))

        _, target = _locate_record(records, rec)
        if target is None:
            print("[WARN] recovery 找不到对应记录，已丢弃（不碰其他客户端的记录）")
            _remove_quietly(path)
            return None

        current = target.get("refreshToken")
        if current == new_refresh:
            # 已经合并过：以主文件为准，不要用 recovery 里可能更旧的 access
            # 顺手把 state 的 generation 补齐，避免 credential/state 长期不一致
            _write_state(auth_path, max(int(_as_number(target.get("generation"))),
                                        state_generation))
            _remove_quietly(path)
            return (target.get("accessToken"), target.get("refreshToken"),
                    _as_number(target.get("expiresAtMs")))

        previous = rec.get("previousRefreshToken")
        if not previous:
            # 没有 previous 就无法证明主文件停在刷新起点，CAS 形同虚设
            print("[WARN] recovery 缺少 previousRefreshToken，无法校验，已丢弃")
            _remove_quietly(path)
            return None
        if current != previous:
            # 主文件已被别处更新（login / 桌面端 / 另一进程），recovery 已过期
            print("[WARN] recovery 已过期（主文件 refresh token 已被更新），忽略并丢弃")
            _remove_quietly(path)
            return None

        # CAS 通过：主文件仍停在本次刷新的起点，安全合并
        generation = max(int(_as_number(rec.get("generation"))),
                         int(_as_number(target.get("generation"))),
                         state_generation)
        target["accessToken"] = new_access
        target["refreshToken"] = new_refresh
        target["expiresAtMs"] = _as_number(rec.get("expiresAtMs"))
        target["generation"] = generation
        try:
            _atomic_write_json(auth_path, main)
        except Exception as e:
            print("[ERROR] recovery 合并写回失败: %s" % type(e).__name__)
            return new_access, new_refresh, _as_number(rec.get("expiresAtMs"))
        _write_state(auth_path, generation)
        _remove_quietly(path)
        print("[INFO] 已用 recovery 恢复凭据")
        return new_access, new_refresh, target["expiresAtMs"]
    finally:
        lock.release()


def _remove_quietly(path):
    try:
        os.unlink(path)
    except Exception:
        pass


# ============================ OAuth 刷新 ============================

def _post_form(url, values):
    payload = urllib.parse.urlencode(values).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh_oauth_token():
    """用 refreshToken 换新 accessToken。

    返回 (access, refresh, expires_at_ms, status)，status 见 REFRESH_* 常量：
      OK               刷新成功（或 token 仍有效无需刷新）
      DEAD             refresh token 被服务端拒绝，必须重新 login
      TRANSIENT        网络/服务端临时故障，下轮重试
      PERSIST_FAILED   已换到新 token 但主文件写失败（已尽力落到 recovery）
    """
    access, refresh, expires_at, path = find_credential()
    if not refresh or not path:
        return None, None, 0, REFRESH_DEAD

    lock = DirLock(os.path.join(os.path.dirname(path), "auth.lock"))
    if not lock.acquire(timeout=20):
        # 拿不到锁说明有别的进程在刷，重读一次看是否已经刷好了
        access, refresh, expires_at, path = find_credential()
        if access and expires_at > time.time() * 1000 + 60_000:
            return access, refresh, expires_at, REFRESH_OK
        return None, None, 0, REFRESH_TRANSIENT

    try:
        # 拿到锁后重读：可能已被其它进程刷新过
        access, refresh, expires_at, path = find_credential()
        if not refresh or not path:
            return None, None, 0, REFRESH_DEAD
        if expires_at > time.time() * 1000 + 60_000:
            return access, refresh, expires_at, REFRESH_OK
        state_path = _state_file_for(path)

        state = read_json(state_path) or {}
        if not isinstance(state, dict):
            state = {}
        cred_payload = read_json(path) or {}
        cred_generation = 0
        if isinstance(cred_payload, dict) and isinstance(cred_payload.get("records"), dict):
            for rec in cred_payload["records"].values():
                if isinstance(rec, dict) and rec.get("refreshToken") == refresh:
                    cred_generation = int(_as_number(rec.get("generation")))
                    break
        new_generation = max(int(_as_number(state.get("generation"))), cred_generation) + 1

        try:
            body = _post_form(OAUTH_TOKEN_ENDPOINT, {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": OAUTH_CLIENT_ID,
                "scope": OAUTH_SCOPE,
                "audience": OAUTH_AUDIENCE,
            })
        except urllib.error.HTTPError as e:
            code, desc = "", ""
            try:
                err = json.loads(e.read().decode("utf-8", "replace"))
                if isinstance(err, dict):
                    code = str(err.get("error") or "")
                    desc = str(err.get("error_description") or "")
            except Exception:
                pass
            finally:
                try:
                    e.close()
                except Exception:
                    pass
            # 只打印错误码；error 与 error_description 都可能回显 token，统一脱敏
            if code in REFRESH_DEAD_CODES:
                print("[ERROR] refresh 被拒绝: %s %s"
                      % (_redact(code, 80), _redact(desc, 120)))
                return None, None, 0, REFRESH_DEAD
            print("[WARN] refresh 请求失败: HTTP %s %s"
                  % (e.code, _redact(code or desc, 120)))
            return None, None, 0, REFRESH_TRANSIENT
        except Exception as e:
            print("[WARN] refresh 请求失败: %s" % type(e).__name__)
            return None, None, 0, REFRESH_TRANSIENT

        if not isinstance(body, dict):
            print("[WARN] refresh 响应不是 JSON 对象")
            return None, None, 0, REFRESH_TRANSIENT

        new_access = body.get("access_token")
        new_refresh = body.get("refresh_token")
        if not new_access or not new_refresh:
            # 只报缺了哪个字段 + 错误码；error_description 可能回显 token，必须脱敏
            missing = [name for name, value in (("access_token", new_access),
                                                ("refresh_token", new_refresh)) if not value]
            err = str(body.get("error") or "")
            desc = str(body.get("error_description") or "")
            extra = ""
            # error 与 error_description 都可能被服务端塞进 token，两者都要脱敏
            if err:
                extra += "（%s）" % _redact(err, 80)
            if desc:
                extra += "（%s）" % _redact(desc, 120)
            print("[WARN] refresh 响应缺字段: %s%s" % (",".join(missing), extra))
            return None, None, 0, REFRESH_DEAD if err in REFRESH_DEAD_CODES else REFRESH_TRANSIENT

        try:
            expires_in = int(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        expires_at = int(time.time() * 1000) + expires_in * 1000
        grant = {"access_token": new_access, "refresh_token": new_refresh,
                 "expires_at_ms": expires_at}

        if not _commit_refresh(path, state_path, refresh, grant, new_generation):
            # 关键：新 token 已经换到，主文件却没写进去。必须落 recovery，否则废号。
            record_key = _find_record_key(path, refresh)
            saved = _save_recovery(path, grant, new_generation, refresh, record_key)
            print("[ERROR] 新 token 已换到但主凭据写回失败%s"
                  % ("，已落 recovery 待下次合并" if saved else "，且 recovery 也失败"))
            return new_access, new_refresh, expires_at, REFRESH_PERSIST_FAILED
        # 主文件已是最新，同目录的旧 recovery 就是过期数据，清掉
        _remove_quietly(recovery_file_for(path))
        return new_access, new_refresh, expires_at, REFRESH_OK
    finally:
        lock.release()


def get_access_token():
    """取可用的 access token。返回 (token, refresh_status)。

    token 为 None 表示确实没有可用凭据。
    """
    auth_path = auth_file_path()

    # 上次刷新成功但没写回主文件的，先尝试合并（主文件缺失时会用 recovery 重建）
    recovered = _consume_recovery(auth_path)

    # 合并后以主文件为准：recovery 分支可能只回填了部分字段
    access, refresh, expires_at, _ = find_credential()
    if access and expires_at > time.time() * 1000 + 60_000:
        return access, REFRESH_OK
    # 合并写回失败时，至少用 recovery 里的 token 撑过本轮
    if recovered and recovered[2] > time.time() * 1000 + 60_000:
        return recovered[0], REFRESH_OK
    if not access:
        return None, REFRESH_DEAD

    new_access, _, new_expires, status = refresh_oauth_token()
    if new_access and new_expires > time.time() * 1000 + 60_000:
        print("[INFO] access token 已续期")
        return new_access, status
    if status == REFRESH_TRANSIENT:
        # 网络问题：不要拿已过期的旧 token 去撞 401，直接把状态抛给上层
        return None, REFRESH_TRANSIENT
    if status == REFRESH_DEAD:
        # refresh token 已被服务端拒绝，旧 access 也必然无效，别再去撞 401
        return None, REFRESH_DEAD
    return access, status


# ============================ 签到请求 ============================

# 客户端公开的签名盐，不是用户凭据；服务端若轮换需同步更新
_SIGN_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"


def signed_request(path, token, method="GET", body=""):
    """按客户端签名协议发请求，返回 (http_code, dict)。

    签名规则（逆向自前端）：
      x-timestamp = 秒级时间戳
      x-signature = md5("<秒>" + 盐 + body)    GET 时 body 为空串
      yy          = md5(quote(路径?query) + "_" + body + md5(毫秒时间戳) + "ooui")
    query 参数集合与顺序必须与前端一致，服务端会按收到的请求重算签名。
    """
    ms = int(time.time() * 1000)
    sec = ms // 1000
    params = [
        ("device_platform", "web"), ("biz_id", "3"), ("app_id", "3001"),
        ("version_code", "22201"), ("unix", str(ms)), ("timezone_offset", "28800"),
        ("is_desktop", "1"), ("desktop_version", DESKTOP_VERSION),
        ("sys_language", "zh"), ("lang", "zh"),
        ("uuid", "null"), ("device_id", "48372915"), ("os_name", "macOS"),
        ("browser_name", "Chrome"), ("browser_language", "zh-CN"),
        ("browser_platform", "MacIntel"), ("user_id", USER_ID),
        ("op_ticket", "undefined"), ("screen_width", "1512"), ("screen_height", "982"),
        ("client", "desktop"),
        ("unix", str(ms)), ("token", token),
    ]
    full = "%s?%s" % (path, urllib.parse.urlencode(params))
    yy = hashlib.md5(("%s_%s%s%s" % (urllib.parse.quote(full), body,
                                     hashlib.md5(str(ms).encode()).hexdigest(),
                                     "ooui")).encode()).hexdigest()
    xsig = hashlib.md5(("%s%s%s" % (sec, _SIGN_SALT, body)).encode()).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "x-timestamp": str(sec), "x-signature": xsig, "yy": yy,
        "Origin": API_BASE, "Referer": API_BASE + "/",
        "Accept": "application/json, text/plain, */*",
        "Authorization": "Bearer " + token,
        "User-Agent": "MiniMaxAgent Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) MiniMax/%s "
                      "Chrome/148.0.7778.280 Safari/537.36" % DESKTOP_VERSION,
    }
    data = body.encode() if method == "POST" else None
    try:
        req = urllib.request.Request(API_BASE + full, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            if DEBUG:
                print("[DEBUG] %s %s -> HTTP %s %s" % (method, path, resp.status, _redact(raw)))
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        if DEBUG:
            print("[DEBUG] %s %s -> HTTP %s %s" % (method, path, e.code, _redact(raw)))
        try:
            e.close()
        except Exception:
            pass
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": type(e).__name__}


def _redact(text, limit=400):
    """截断响应文本并把可能出现的 token 抹掉，DEBUG 也不打印明文凭据。"""
    text = text[:limit]
    for marker in ("mmoat_", "mmort_"):
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx < 0:
                break
            end = idx + len(marker)
            while end < len(text) and (text[end].isalnum() or text[end] in "-_"):
                end += 1
            text = text[:idx] + marker + "***" + text[end:]
            start = idx + len(marker) + 3
    return text


def _base_resp(body):
    return (body.get("base_resp") or {}) if isinstance(body, dict) else {}


def _is_session_invalid(http_code, body):
    base = _base_resp(body)
    return base.get("status_code") == SESSION_INVALID_CODE or http_code in (401, 403)


def checkin(claim=False):
    """查状态并按需领取。返回 (exit_code, result_dict)。"""
    token, refresh_status = get_access_token()
    if not token:
        if refresh_status == REFRESH_TRANSIENT:
            return 1, {"result": "NETWORK",
                       "report": "刷新登录态失败（网络或服务端异常），本轮跳过，下轮自动重试"}
        return 2, {"result": "NO_AUTH",
                   "report": "没有可用凭据（%s），请执行 login 授权"
                             % ("refresh token 已被服务端拒绝"
                                if refresh_status == REFRESH_DEAD else "凭据文件缺失或损坏")}

    http_code, body = signed_request(SIGNIN_STATUS_PATH, token)
    if http_code == -1:
        return 1, {"result": "NETWORK",
                   "report": "网络不可达：%s" % (body.get("error") or "")}
    if http_code >= 500:
        # 5xx 响应体可能仍带 base_resp=0，不能据此继续走签到流程
        return 1, {"result": "NETWORK",
                   "report": "状态接口 5xx（HTTP %s），下轮重试" % http_code}
    if not isinstance(body, dict):
        return 1, {"result": "ERROR", "report": "状态接口返回了非对象 JSON（HTTP %s）" % http_code}
    if _is_session_invalid(http_code, body):
        base = _base_resp(body)
        return 1, {"result": "NO_SESSION",
                   "report": "登录态已失效（HTTP %s / code %s），需要重新 login 授权"
                             % (http_code, base.get("status_code"))}
    base = _base_resp(body)
    if base.get("status_code") not in (0, None) or not isinstance(body.get("data"), dict):
        return 1, {"result": "ERROR",
                   "report": "状态接口异常：%s（HTTP %s）"
                             % (base.get("status_msg") or base.get("status_code"), http_code)}

    days = body["data"].get("days")
    if not isinstance(days, list):
        return 1, {"result": "ERROR",
                   "report": "签到面板结构异常（days 不是数组），接口可能改版"}
    if not days:
        # 空面板更可能是接口改版/服务端异常，不能当成「活动未开启」静默成功
        return 1, {"result": "ERROR",
                   "report": "签到面板为空（days 无数据），接口可能改版"}
    today = next((item for item in days
                  if isinstance(item, dict) and item.get("is_today")), None)
    if today is None:
        return 0, {"result": "INACTIVE", "report": "签到面板无今日项，活动可能未开启"}

    streak = today.get("day_no")
    streak_text = "连签第 %s 天" % streak if streak else "7 天周期"
    today_status = today.get("status")

    if today_status == DAY_STATUS_CLAIMED:
        return 0, {"result": "ALREADY", "report": "今日已签到（%s）" % streak_text,
                   "day_no": streak, "points": today.get("points")}
    if not claim:
        return 0, {"result": "PENDING",
                   "report": "今日可领 %s 积分（%s，status=%s）"
                             % (today.get("points"), streak_text, today_status),
                   "day_no": streak, "points": today.get("points")}

    http_code, body = signed_request(SIGNIN_CLAIM_PATH, token, method="POST", body="{}")
    if http_code == -1:
        return 1, {"result": "NETWORK", "report": "领取请求未送达，下次自动重试"}
    if http_code >= 500:
        # 5xx 响应体可能仍带 base_resp=0，不能据此判定领取成功
        return 1, {"result": "NETWORK",
                   "report": "领取接口 5xx（HTTP %s），下次自动重试" % http_code}
    if not isinstance(body, dict):
        return 1, {"result": "ERROR", "report": "领取接口返回了非对象 JSON（HTTP %s）" % http_code}
    if _is_session_invalid(http_code, body):
        return 1, {"result": "NO_SESSION",
                   "report": "领取时登录态失效（HTTP %s），需要重新 login 授权" % http_code}
    if not 200 <= http_code < 300:
        # 4xx 且响应体带 base_resp=0/claim_result=1 时不能当成功
        return 1, {"result": "ERROR",
                   "report": "领取接口返回 HTTP %s，按失败处理" % http_code}
    base = _base_resp(body)
    if base.get("status_code") not in (0, None):
        return 1, {"result": "ERROR",
                   "report": "领取失败：%s（HTTP %s）"
                             % (base.get("status_msg") or base.get("status_code"), http_code)}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    claim_result = data.get("claim_result")
    if claim_result in (CLAIM_RESULT_OK, CLAIM_RESULT_ALREADY):
        gained = claim_result == CLAIM_RESULT_OK
        return 0, {
            "result": "CLAIMED" if gained else "ALREADY",
            "report": "%s %s 积分（%s）" % ("成功领取" if gained else "今日已领过",
                                            data.get("points"), streak_text),
            "day_no": data.get("day_no") or streak,
            "points": data.get("points"),
        }
    return 1, {"result": "ERROR",
               "report": "领取失败：claim_result=%s（HTTP %s）" % (claim_result, http_code)}


# ============================ device flow 授权 ============================

def _store_new_credential(access, refresh, expires_in_ms):
    """把新凭据写入凭据文件（含 auth-state.json 的 generation 同步）。

    全程持同一把 auth.lock，避免与正在跑的 auto 刷新交错写坏 generation。
    拿不到锁时直接失败——此时写进去可能覆盖另一个进程刚刷好的新凭据。
    """
    path = auth_file_path()
    lock = DirLock(os.path.join(os.path.dirname(path), "auth.lock"))
    if not lock.acquire(timeout=20):
        print("[ERROR] 授权写入没拿到 auth.lock（可能有其他刷新在进行），本次放弃写入")
        return False
    try:
        saved = _store_new_credential_locked(path, access, refresh, expires_in_ms)
        if saved:
            # 新凭据已落主文件，同目录的旧 recovery 是过期数据
            _remove_quietly(recovery_file_for(path))
        return saved
    finally:
        lock.release()


def _store_new_credential_locked(path, access, refresh, expires_in_ms):
    state_path = _state_file_for(path)
    auth_home = os.path.dirname(path)

    payload = read_json(path)
    if not isinstance(payload, dict):
        payload = {"schemaVersion": 1, "records": {}}
    records = payload.get("records")
    if not isinstance(records, dict):
        records = {}
        payload["records"] = records

    # 优先复用本客户端的记录键，避免覆盖别的客户端凭据
    key = None
    for existing_key, rec in records.items():
        if isinstance(rec, dict) and rec.get("clientId") == OAUTH_CLIENT_ID:
            key = existing_key
            break
    if key is None:
        account = base64.urlsafe_b64encode(hashlib.sha256(
            ("%s\0%s" % (auth_home, OAUTH_CLIENT_ID)).encode()).digest()
        ).decode().rstrip("=")
        key = "com.minimax.mcode.oauth.prod.cn\0%s" % account

    state = read_json(state_path)
    if not isinstance(state, dict):
        state = {}
    new_generation = int(_as_number(state.get("generation"))) + 1
    for rec in records.values():
        if isinstance(rec, dict):
            new_generation = max(new_generation, int(_as_number(rec.get("generation"))) + 1)

    records[key] = {
        "accessToken": access,
        "refreshToken": refresh,
        "tokenType": "Bearer",
        "clientId": OAUTH_CLIENT_ID,
        "scopes": [OAUTH_SCOPE],
        "audience": OAUTH_AUDIENCE,
        "expiresAtMs": float(time.time() * 1000 + expires_in_ms),
        "generation": new_generation,
        "schemaVersion": 1,
    }
    try:
        _atomic_write_json(path, payload)
    except Exception as e:
        print("[ERROR] 凭据写入失败: %s" % type(e).__name__)
        return False

    _write_state(path, new_generation)
    return True


def device_login():
    """device flow 授权：打印链接与 user_code，轮询直到用户在浏览器确认。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    try:
        body = _post_form(OAUTH_DEVICE_ENDPOINT, {
            "client_id": OAUTH_CLIENT_ID,
            "scope": OAUTH_SCOPE,
            "audience": OAUTH_AUDIENCE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
    except Exception as e:
        return 1, {"result": "ERROR", "report": "device code 申请失败：%s" % type(e).__name__}
    if not isinstance(body, dict):
        return 1, {"result": "ERROR", "report": "device code 响应不是 JSON 对象"}

    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verify_uri = body.get("verification_uri_complete") or body.get("verification_uri")
    if not device_code or not user_code or not verify_uri:
        return 1, {"result": "ERROR",
                   "report": "device code 响应缺字段（device_code/user_code/verification_uri）"}
    try:
        expires_in = int(body.get("expires_in") or 300)
        interval = int(body.get("interval") or 5)
    except (TypeError, ValueError):
        expires_in, interval = 300, 5
    # 服务端给异常值时不能让 time.sleep 抛错
    expires_in = max(1, expires_in)
    interval = max(1, interval)

    print("请在浏览器打开以下链接完成授权：\n  %s\n（user_code: %s，%s 秒内有效）"
          % (verify_uri, user_code, expires_in), file=sys.stderr, flush=True)

    deadline = time.time() + expires_in
    while time.time() < deadline:
        try:
            token_body = _post_form(OAUTH_TOKEN_ENDPOINT, {
                "grant_type": DEVICE_GRANT_TYPE,
                "device_code": device_code,
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": verifier,
            })
        except urllib.error.HTTPError as e:
            code = ""
            try:
                err = json.loads(e.read().decode("utf-8", "replace"))
                if isinstance(err, dict):
                    code = str(err.get("error") or "")
            except Exception:
                pass
            finally:
                try:
                    e.close()
                except Exception:
                    pass
            if code == "authorization_pending":
                time.sleep(interval)
                continue
            if code == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
            return 1, {"result": "ERROR",
                       "report": "授权失败：%s（HTTP %s）"
                                 % (_redact(code, 80) or "unknown", e.code)}
        except Exception as e:
            return 1, {"result": "ERROR", "report": "轮询异常：%s" % type(e).__name__}

        if not isinstance(token_body, dict):
            time.sleep(interval)
            continue
        access = token_body.get("access_token")
        refresh = token_body.get("refresh_token")
        if not access or not refresh:
            time.sleep(interval)
            continue
        try:
            expires_in_token = int(token_body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in_token = 3600
        saved = _store_new_credential(access, refresh, expires_in_token * 1000)
        return (0 if saved else 1), {
            "result": "AUTHED" if saved else "ERROR",
            "report": ("授权成功，凭据已写入 %s" % auth_file_path()) if saved
                      else "授权成功但凭据写入失败",
        }
    return 1, {"result": "ERROR", "report": "授权超时（%ss 内未确认）" % expires_in}


# ============================ 报告与主流程 ============================

RESULT_ICON = {
    "CLAIMED": "✅", "ALREADY": "ℹ️", "PENDING": "🕒",
    "INACTIVE": "😴", "NO_SESSION": "🔒", "NO_AUTH": "🔑",
    "NETWORK": "🌐", "ERROR": "❌",
}


def build_report(exit_code, result):
    icon = RESULT_ICON.get(result.get("result"), "❓")
    lines = [
        "# MiniMax Code 签到 - 执行报告",
        "",
        "⏰ 执行时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "📊 结果: %s %s" % (icon, result.get("report") or ""),
    ]
    if result.get("points") is not None:
        lines.append("💎 本次积分: %s（连签第 %s 天）"
                     % (result.get("points"), result.get("day_no")))
    if exit_code == 1 and result.get("result") in ("NO_SESSION", "NO_AUTH"):
        lines.append("")
        lines.append("⚠️ 需要处理：执行 `python minimax_checkin.py login` 重新授权，"
                     "凭据会写入 %s。" % auth_file_path())
    return "\n".join(lines)


def _setup_stdio():
    """青龙容器是 UTF-8，但本机 Windows 管道可能是 GBK，emoji 会让 print 直接抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _setup_stdio()
    action = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if action in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    check_only = action == "--check"
    if check_only:
        action = "status"

    try:
        if action == "auto":
            exit_code, result = checkin(claim=True)
        elif action == "status":
            exit_code, result = checkin(claim=False)
        elif action == "login":
            exit_code, result = device_login()
            print(json.dumps(result, ensure_ascii=False))
            return exit_code
        else:
            exit_code, result = 2, {"result": "ERROR",
                                    "report": "未知命令：%s（可用：auto / status / login / --check）"
                                              % action}
    except Exception as e:
        # 未预期异常按「运行失败」处理；退出码 2 留给明确的配置/凭据缺失
        exit_code, result = 1, {"result": "ERROR",
                                "report": "脚本异常（%s）" % type(e).__name__}

    report = build_report(exit_code, result)
    print("\n" + report)
    print("\n[结果] %s" % json.dumps(result, ensure_ascii=False))

    # --check 是自检，默认不推送，避免频繁跑把通知刷满
    notify_enabled = NOTIFY_ENABLED and not check_only
    if notify_enabled:
        if NOTIFY_ONLY_FAIL and exit_code == 0:
            print("[INFO] MINIMAX_NOTIFY_ONLY_FAIL=true 且本次正常，跳过推送")
        else:
            title = "MiniMax Code 签到" if exit_code == 0 else "MiniMax Code 签到失败"
            send_notify(title, report)
    elif check_only:
        print("[INFO] --check 自检模式，不推送")
    else:
        print("[INFO] MINIMAX_NOTIFY=false，已禁用推送")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
