#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
cron: 0 9 * * *
new Env('百度网盘签到')
"""

import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

try:
    import requests
except ImportError:  # 青龙缺依赖时给出可执行的修复提示，而不是抛一堆栈
    print("❌ 缺少 requests 依赖，请在青龙「依赖管理」里安装：pip3 install requests")
    sys.exit(1)

# ---------------- 统一通知模块加载 ----------------
# ---------------- 统一通知模块加载 ----------------
def load_notify():
    """青龙的 notify.py 在 scripts 根目录，脚本在子目录里时不在 sys.path 上。

    所以先试原样导入，失败了再把脚本目录、父目录、青龙默认目录逐个补进
    sys.path 重试——不写死单一路径，本机/容器/自定义目录都能用。
    """
    script_path = os.path.abspath(__file__)
    candidates = [
        os.path.dirname(script_path),
        os.path.dirname(os.path.dirname(script_path)),
        "/ql/data/scripts",
        "/ql/scripts",
    ]
    for path in candidates:
        if not os.path.isdir(path):
            continue
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            from notify import send as notify_func

            return notify_func
        except Exception:
            continue
    return None


send = load_notify()
hadsend = send is not None
if hadsend:
    print("✅ 已加载notify.py通知模块")
else:
    print("⚠️  未加载通知模块，通知只写日志")


# ============================================================ 常量

BASE = "https://pan.baidu.com"
REFERER = "https://pan.baidu.com/wap/svip/growth/task"

# 成长值接口按移动端走，PC 版 UA 拿不到移动端任务
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/90.0.4430.91 Mobile Safari/537.36"
)

# --- A. 网页端会员成长值体系
API_LOGIN = "/api/loginstatus?app_id=250528&web=5&clienttype=1&channel=chunlei"
API_GROWTH = "/rest/2.0/membership/user?app_id=250528&web=5&method=query"
API_SIGNIN = "/rest/2.0/membership/level?app_id=250528&web=5&method=signin"
API_QUESTION = "/act/v2/membergrowv2/getdailyquestion?app_id=250528&web=5"
API_ANSWER = (
    "/act/v2/membergrowv2/answerquestion?app_id=250528&web=5"
    "&ask_id={ask_id}&answer={answer}"
)

# --- B. App 端金币任务中心体系
API_APP_SIGNINLIST = "/coins/taskcenter/signinlist"
API_APP_TASKLIST = "/coins/taskcenter/tasklist"
API_APP_SIGNIN = "/coins/taskcenter/signin"
API_APP_AWARD = "/coins/taskcenter/signinaward"

# --- C/D/E. 任务中心与积分中心
API_TASK_LIST = "/api/taskscore/tasklist"
API_TASK_SAVE = "/api/taskscore/tasksave"
API_POINTS_TASK_LIST = "/coins/center/tasklist"
API_POINTS_BALANCE = "/coins/points/balance"

# --- App「赚积分」页：与上面的 task_sys_* 是两套不同接口
API_ACT_TASKLIST = "/act/task/tasklist"
ACT_TASK_FROM = "netdisk_activity"
# 活动 ID 来自 App 页面 URL 的 id 参数，可由 BAIDU_ACT_ID 覆盖
DEFAULT_ACT_ID = "987141565000"
ACT_VERSION = "10.0.154"
# /act/* 这组接口会校验 Referer，必须伪装成活动页请求
ACT_REFERER_TEMPLATE = (
    "https://pan.baidu.com/lifeactivity/task/mainpage?id={act_id}&from=wodeicon"
)

# App 任务中心的任务来源未知，探测时逐个试
API_TASKCENTER_RECORD = "/act/v2/taskcenter/recordlist"
ACT_TASK_FROM_CANDIDATES = (
    "task_sys_ai",
    "task_sys_ai_new",
    "task_sys_ai_task",
    "task_sys_svip",
    "task_sys_growth_task",
    "svip_task",
    "netdisk_activity",
    "svipday2606",
)


def act_referer(config):
    return ACT_REFERER_TEMPLATE.format(act_id=config["act_id"])

# App 任务列表用的分类（沿用 cf-netdisk-signin 实测通过的 6 个）
APP_TASK_FROM = (
    "task_sys_beginner task_sys_daily task_sys_space "
    "task_sys_ai task_sys_function task_sys_growth"
)
# 网页任务中心多一个 badge 分类
TASK_CENTER_FROM = (
    "task_sys_beginner task_sys_daily task_sys_space task_sys_badge "
    "task_sys_ai task_sys_function task_sys_growth"
)
GROWTH_TASK_FROM = "task_sys_task_growth"
MAKEUP_TASK_FROM = "egg_checkin_makeup_task"
# 补签广告的本地硬上限：服务端 count_max 理论上可控，不能让它决定循环次数
MAX_MAKEUP_STEPS = 60

# tasksave 的请求签名盐；百度改算法时本模块会整体失效，不影响签到主流程
TASK_SIGN_SALT = "ae82c240578eb391de93c2f4c3dfc3ba"

TASK_TYPE_DAILY_QUESTION = 169
TASK_TYPE_AD = 70
TASK_TYPE_APP_SIGNIN = 166
TASK_TYPE_PROGRESS = 141

TASK_STATUS_DONE = 1   # 已完成
TASK_STATUS_WAIT = 6   # 待领奖

# 只执行已验证可自动完成的类型，其余（需真实打开 App/付费/群共享）跳过并记录
GROWTH_TASK_TYPES = {13, 70}
TASK_CENTER_TASK_TYPES = {13, 70, 91, 141, 158}
POINTS_TASK_TYPES = {13, 70, 93, 158}

POINTS_BALANCE_PTYPE = 8      # 积分中心余额
TASKCENTER_BALANCE_PTYPE = 10  # 任务中心余额

CODE_OK = 0
CODE_REPEAT_SIGNIN = 421001    # 今日已签到
CODE_INVALID_BDUSS = -6        # cookie 失效

REPEAT_WORDS = ("repeat", "already", "not allow", "已签", "重复")

LOG_PREFIX = "  "


# ============================================================ 异常

class BaiduError(Exception):
    """业务层失败：响应码不符合预期、字段缺失、状态无法确认。"""


class BaiduHTTPError(BaiduError):
    """HTTP 层失败：非 200，或重定向被拒绝跟随。"""

    def __init__(self, status, detail=""):
        self.status = status
        text = "百度接口 HTTP %s" % status
        if detail:
            text += "：%s" % detail
        super(BaiduHTTPError, self).__init__(text)


# ============================================================ 配置

def env_bool(name, default=True):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off")


def env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print("⚠️  %s=%r 不是整数，改用默认值 %s" % (name, raw, default))
        return default


def type_set(raw, default):
    """解析逗号分隔的 task_type 白名单。"""
    values = set()
    for part in (raw or "").replace(" ", ",").split(","):
        part = part.strip()
        if part.isdigit():
            values.add(int(part))
    return values or set(default)


def load_config():
    """从环境变量读取全部配置；青龙只需配 BAIDU_COOKIE 一项即可跑。"""
    timeout = env_int("BAIDU_TIMEOUT", 15)
    if timeout < 5 or timeout > 120:
        print("⚠️  BAIDU_TIMEOUT 超出 5-120，改用 15")
        timeout = 15
    return {
        "cookie": (os.environ.get("BAIDU_COOKIE") or "").strip(),
        "cuid": (os.environ.get("BAIDU_CUID") or "").strip(),
        "timeout": timeout,
        "ua": (os.environ.get("BAIDU_UA") or "").strip() or DEFAULT_UA,
        "privacy": env_bool("PRIVACY_MODE", True),
        "random_signin": env_bool("RANDOM_SIGNIN", True),
        "max_delay": max(0, env_int("MAX_RANDOM_DELAY", 3600)),
        # 青龙「运行」按钮不转发命令行参数，所以诊断开关也支持环境变量
        "try_type": (os.environ.get("BAIDU_TRY_TYPE") or "").strip(),
        "try_source": (os.environ.get("BAIDU_TRY_SOURCE") or "").strip(),
        "act_id": (os.environ.get("BAIDU_ACT_ID") or "").strip() or DEFAULT_ACT_ID,
        "act_version": (os.environ.get("BAIDU_ACT_VERSION") or "").strip() or ACT_VERSION,
        "act_types": type_set(os.environ.get("BAIDU_ACT_TYPES"), (13, 70)),
        "act_devuid": (os.environ.get("BAIDU_ACT_DEVID") or "").strip(),
        "probe_act": env_bool("BAIDU_PROBE_ACT", False),
        "modules": {
            "pc": env_bool("BAIDU_ENABLE_PC", True),
            "question": env_bool("BAIDU_ENABLE_QUESTION", True),
            "app": env_bool("BAIDU_ENABLE_APP", True),
            "awards": env_bool("BAIDU_ENABLE_AWARDS", True),
            "ads": env_bool("BAIDU_ENABLE_ADS", True),
            "taskcenter": env_bool("BAIDU_ENABLE_TASKCENTER", True),
            "points": env_bool("BAIDU_ENABLE_POINTS", True),
            "act": env_bool("BAIDU_ENABLE_ACT", False),
            "makeup": env_bool("BAIDU_ENABLE_MAKEUP", True),
        },
    }


def cookie_field(cookie, name):
    """从整条 cookie 里取指定字段，取不到返回空串。"""
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return ""


def resolve_cuid(config, cookie):
    """设备号：环境变量 > cookie 的 BAIDUCUID > 按账号持久化的随机值。

    每个账号按 cookie 摘要分文件，避免多账号共用同一个设备号导致任务状态串味。
    """
    if config["cuid"]:
        return config["cuid"]
    from_cookie = cookie_field(cookie, "BAIDUCUID")
    if from_cookie:
        return from_cookie
    digest = hashlib.md5(cookie.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".cuid-" + digest
    )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            saved = handle.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    generated = hashlib.md5(os.urandom(16)).hexdigest()
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(generated)
    except OSError as error:
        # 只读目录下每次都会换设备号，至少要让人知道，别默默漂移
        print("⚠️  无法写入设备号缓存 %s（%s），本次使用临时设备号" % (path, error))
    return generated


# ============================================================ 工具

def as_int(value):
    """宽容转 int：None、空串、非法字符串都归 0。"""
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def business_codes(data):
    """取出响应里所有业务码字段。

    百度不同接口分别用 errno / error_code，个别响应会同时给两个且可能冲突，
    两个参考实现的优先级还相反。这里保守处理：返回全部码 + 是否存在非法类型，
    由调用方要求「所有码都必须通过」。

    值为 null 视为「字段不存在」——cf 的 `errno ?? error_code` 也是这个语义，
    只有字符串、布尔这类明确的类型错误才判格式异常。
    """
    codes = []
    invalid = False
    for key in ("errno", "error_code"):
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            invalid = True
            continue
        codes.append(value)
    return codes, invalid


def as_dict(value):
    """嵌套字段可能是 list/str/null，统一收敛成 dict，避免 AttributeError 逸出。"""
    return value if isinstance(value, dict) else {}


def error_text(error):
    """异常转一行文本；BaiduError 已经是可读中文，其它补上类型名。"""
    if isinstance(error, BaiduError):
        return str(error)
    return "%s: %s" % (type(error).__name__, error)


def brief_msg(data):
    """从响应里挑一条可读消息，用于报错；绝不回显 cookie。"""
    for key in ("show_msg", "error_msg", "error", "msg"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return ""


def task_id_of(task):
    return str(task.get("task_id_str") or task.get("task_id") or "")


def task_status(task):
    return as_int(task.get("task_status"))


def device_params(cuid):
    """积分/任务中心接口要求的设备参数。"""
    return {
        "rand": str(random.randint(10 ** 9, 10 ** 10 - 1)),
        "time": str(int(time.time() * 1000)),
        "cuid": cuid,
        "devuid": cuid,
    }


def task_sign_params(task, uk):
    """tasksave 的签名参数，token 与网页端 taskSystem 算法一致。"""
    task_id = task_id_of(task)
    task_from = str(task.get("task_from") or "")
    if not task_id or not task_from or not uk:
        raise BaiduError("任务完成参数不完整（task_id/task_from/uk）")
    rand = str(random.randint(10 ** 9, 10 ** 10 - 1))
    stamp = str(int(time.time() * 1000))
    token = hashlib.md5(
        ("%s_%s_%s_%s_%s" % (task_id, uk, rand, stamp, TASK_SIGN_SALT)).encode("utf-8")
    ).hexdigest()
    return {
        "app_id": "250528",
        "web": "1",
        "clienttype": "0",
        "channel": "chunlei",
        "uk": uk,
        "task_id": task_id,
        "task_from": task_from,
        "rand": rand,
        "time": stamp,
        "devuid": rand,
        "token": token,
    }


def mask_name(name, enabled):
    if not enabled or not name:
        return name or "未知用户"
    if len(name) > 2:
        return name[0] + "***" + name[-1]
    return "***"


def sleep_between(low, high):
    time.sleep(random.uniform(low, high))


# ============================================================ HTTP 层

def http_json(session, path, params=None, timeout=15, retries=3, referer=None):
    """发 GET 并解析 JSON。

    三处刻意的设计：
    1. allow_redirects=False —— cookie 失效时百度会 302 到登录页，跟过去会拿到
       200 的 HTML，被误当成成功。
    2. 只有网络异常才重试；HTTP 4xx/5xx 直接抛，避免对风控做无意义重试。
    3. 非 200 时尽量把响应体里的业务消息带出来，方便定位是鉴权还是风控。
    """
    url = BASE + path
    if params:
        sep = "&" if "?" in path else "?"
        # urlencode 把空格编成 '+'，与网页端/App 端的实际请求保持一致
        url += sep + urlencode(params)
    last_error = None
    safe_path = path.split("?")[0]
    for attempt in range(retries):
        try:
            response = session.get(
                url, timeout=timeout, allow_redirects=False,
                headers={"Referer": referer} if referer else None,
            )
        except requests.RequestException as error:
            # 只留异常类型和接口路径：requests 的异常原文会带含 uk/token 的完整 URL
            last_error = "%s @ %s" % (type(error).__name__, safe_path)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1) + random.random())
            continue
        if response.status_code != 200:
            raise BaiduHTTPError(response.status_code, _body_hint(response))
        try:
            data = response.json()
        except ValueError:
            raise BaiduError("百度返回的不是 JSON（可能是登录页或风控页）")
        if not isinstance(data, dict):
            raise BaiduError("百度返回的 JSON 结构异常（不是对象）")
        return data
    raise BaiduError("网络请求失败（%s）" % last_error)


def _body_hint(response):
    """从错误响应体里捞业务消息；捞不到就不带，避免把整页 HTML 塞进日志。"""
    try:
        data = response.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        return brief_msg(data)
    return ""


class BaiduClient(object):
    def __init__(self, cookie, cuid, timeout=15, ua=None):
        self.cuid = cuid
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ua or DEFAULT_UA,
            "Cookie": cookie,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "Connection": "keep-alive",
        })

    def raw(self, path, params=None, referer=None):
        """不做业务码校验，给 --probe 用。"""
        return http_json(self.session, path, params, self.timeout, referer=referer)

    def call(self, path, params=None, allowed=(CODE_OK,), referer=None):
        data = self.raw(path, params, referer=referer)
        codes, invalid = business_codes(data)
        if invalid or not codes:
            raise BaiduError("百度响应缺少业务状态码或格式异常")
        bad = [code for code in codes if code not in allowed]
        if bad:
            if CODE_INVALID_BDUSS in codes:
                raise BaiduError("cookie 已失效（BDUSS 无效），请重新导出")
            hint = brief_msg(data)
            raise BaiduError(
                "百度业务失败（%s）%s" % (bad[0], "：" + hint if hint else "")
            )
        return data


# ============================================================ 成长值测量

class GrowthMeter(object):
    """按步测量成长值。

    只用于如实展示：签到本身可能 +0，所以 delta 不能当成功判据。
    缓存上一次的读数，N 个步骤只花 N+1 次请求。
    """

    def __init__(self, client):
        self.client = client
        self._cached = None

    def seed(self, value):
        """复用别处已经查到的成长值，避免同一接口连查两次。"""
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            self._cached = value

    def value(self):
        if self._cached is None:
            self._cached = self._read()
        return self._cached

    def _read(self):
        data = self.client.call(API_GROWTH)
        info = as_dict(data.get("level_info"))
        value = info.get("current_value")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BaiduError("成长值响应缺少有效数值")
        return value

    def step(self, action):
        """执行 action 并返回 {state, before, after, delta, message, warnings}。

        测量失败绝不能挡住操作，也不参与成败判定：签到本身不依赖成长值读数，
        读不到就只记一条 warning。action 抛错后仍会回读余额——请求可能已经
        到达服务端。
        """
        warnings = []
        message = ""
        before = None
        try:
            before = self.value()
        except Exception as error:
            warnings.append("成长值读取失败：%s" % error_text(error))

        state = None
        try:
            state = action()
        except Exception as error:
            message = error_text(error)

        self._cached = None
        after = None
        try:
            after = self.value()
        except Exception as error:
            warnings.append("成长值回读失败：%s" % error_text(error))
        delta = (after - before) if (after is not None and before is not None) else None
        return {
            "state": state,
            "before": before,
            "after": after,
            "delta": delta,
            "message": message,
            "warnings": warnings,
        }


# ============================================================ A. 网页端

def get_login_info(client):
    data = client.call(API_LOGIN)
    info = as_dict(data.get("login_info"))
    if not info.get("uk"):
        raise BaiduError("cookie 已失效或未登录，请重新导出 pan.baidu.com 的 cookie")
    return info


def get_level(client):
    data = client.call(API_GROWTH)
    info = as_dict(data.get("level_info"))
    product = as_dict(data.get("current_product_v2"))
    return {
        "level": info.get("current_level"),
        "value": info.get("current_value"),
        "product": product.get("product_type") or "",
    }


def signin_pc(client):
    """网页端会员成长值签到。

    百度可能同时返回 errno=0 与 error_code=421001，所以两个字段都要看；
    421001 必须同时确认消息语义，语义不明宁可报错也不当成已签。
    """
    data = client.call(API_SIGNIN, allowed=(CODE_OK, CODE_REPEAT_SIGNIN))
    if CODE_REPEAT_SIGNIN in (data.get("errno"), data.get("error_code")):
        hint = brief_msg(data).lower()
        if not any(word in hint for word in REPEAT_WORDS):
            raise BaiduError("PC 重复签到状态无法确认：%s" % (hint or "无消息"))
        return "already_signed"
    return "signed"


def answer_question(client):
    """每日一题；答题后任务可能停在 WAIT，需要补一次领奖。"""
    data = client.call(API_QUESTION)
    payload = as_dict(data.get("data"))
    if payload.get("answer_status") == 1:
        return {"state": "already_answered", "score": 0, "question": ""}
    ask_id = payload.get("ask_id")
    answer = payload.get("answer")
    if ask_id is None or answer is None:
        raise BaiduError("没有拿到题目")
    result = client.call(API_ANSWER.format(ask_id=ask_id, answer=answer))
    score = as_int(as_dict(result.get("data")).get("score"))
    return {
        "state": "answered",
        "score": score,
        "question": payload.get("question") or "",
    }


def get_growth_tasks(client):
    """成长值页的每日任务列表（只取 task_sys_task_growth）。"""
    data = client.call(API_TASK_LIST, {
        "app_id": "250528",
        "web": "1",
        "clienttype": "0",
        "channel": "chunlei",
        "task_from": GROWTH_TASK_FROM,
    })
    items = as_dict(data.get("result")).get("list") or []
    return [item for item in items if isinstance(item, dict)]


# ============================================================ B. App 端

def app_signin_state(client):
    data = client.call(API_APP_SIGNINLIST, {"cuid": client.cuid})
    return as_dict(data.get("data"))


def signin_app(client):
    """App 金币任务中心签到：先查状态，签完再复核一次。"""
    state = app_signin_state(client)
    signed = state.get("signed_today")
    if signed == 1:
        return "already_signed"
    if signed != 0:
        raise BaiduError("App 签到状态无法确认（signed_today=%r）" % (signed,))

    data = client.call(API_APP_TASKLIST, {
        "cuid": client.cuid,
        "task_from": APP_TASK_FROM,
    })
    items = as_dict(data.get("result")).get("list") or []
    task = next(
        (item for item in items
         if isinstance(item, dict)
         and as_int(item.get("task_type")) == TASK_TYPE_APP_SIGNIN),
        None,
    )
    if not task:
        raise BaiduError("未找到 App 签到任务（task_type=166）")

    # 任务 ID 两种形态都可能出现，有则都带上；至少要有一个才发请求
    params = {"cuid": client.cuid}
    for key in ("task_id", "task_id_str"):
        value = task.get(key)
        if value is not None and value != "":
            params[key] = value
    task_from = task.get("task_from")
    if task_from is None or task_from == "":
        raise BaiduError("App 签到任务缺少字段 task_from")
    if "task_id" not in params and "task_id_str" not in params:
        raise BaiduError("App 签到任务缺少任务 ID")
    params["task_from"] = task_from
    resp = client.call(
        API_APP_SIGNIN, params, allowed=(CODE_OK, CODE_REPEAT_SIGNIN)
    )

    if app_signin_state(client).get("signed_today") != 1:
        raise BaiduError("App 签到请求成功，但签到状态未确认")
    # 服务端回「已签到」码时按已签处理，避免把重复签到报成异常
    codes, _ = business_codes(resp)
    return "already_signed" if CODE_REPEAT_SIGNIN in codes else "signed"


def award_state(client):
    """累计签到奖励档位；档位全部来自服务端，不写死 7/15/30。"""
    state = app_signin_state(client)
    days = state.get("signin_days")
    rewards = state.get("reward_list")
    if isinstance(days, bool) or not isinstance(days, int):
        raise BaiduError("累计奖励状态缺少 signin_days")
    if not isinstance(rewards, list):
        raise BaiduError("累计奖励状态缺少 reward_list")

    tiers = []
    for item in rewards:
        if not isinstance(item, dict):
            raise BaiduError("累计奖励档位格式错误")
        day = item.get("day")
        received = item.get("received")
        prize = item.get("prize")
        if isinstance(day, bool) or not isinstance(day, int) or day < 1:
            raise BaiduError("累计奖励档位 day 格式错误")
        if isinstance(received, bool) or not isinstance(received, int) or received < 0:
            raise BaiduError("累计奖励档位 received 格式错误")
        if not isinstance(prize, list) or not prize:
            raise BaiduError("累计奖励档位 prize 格式错误")
        tiers.append({"day": day, "received": received, "count": len(prize)})
    tiers.sort(key=lambda item: item["day"])

    claimable = [
        tier for tier in tiers
        if days >= tier["day"] and tier["received"] < tier["count"]
    ]
    return {
        "signin_days": days,
        "signed_today": state.get("signed_today"),
        "tiers": tiers,
        "claimable": claimable,
    }


def claim_awards(client, report):
    """逐档领取累计奖励。

    真实响应里每档 prize 是一个数组（实测 2 项），received 上限等于数组长度，
    说明服务端可能一次只发一个奖品。所以按「received 在推进就继续领」循环：
    领一次不动就立刻停手报错，既不会漏领，也不会重复领同一份。
    单档失败不影响后续档位。
    """
    state = award_state(client)
    report["signin_days"] = state["signin_days"]
    if not state["claimable"]:
        return "no_award_due"

    claimed = []
    for tier in state["claimable"]:
        day = tier["day"]
        progress = None
        issued = 0
        try:
            # 上限 = 奖品数 + 2，防止服务端计数异常导致无限领取
            for _ in range(tier["count"] + 2):
                latest = award_state(client)
                matched = next(
                    (t for t in latest["tiers"] if t["day"] == day), None
                )
                if not matched:
                    raise BaiduError("累计 %s 天档位消失" % day)
                if matched["received"] >= matched["count"]:
                    break
                if progress is not None and matched["received"] <= progress:
                    raise BaiduError(
                        "累计 %s 天奖励领取后未推进（%s/%s）"
                        % (day, matched["received"], matched["count"])
                    )
                progress = matched["received"]
                client.call(API_APP_AWARD, {"cuid": client.cuid, "day": day})
                issued += 1
                sleep_between(0.4, 0.9)
            after = award_state(client)
            matched = next(
                (t for t in after["tiers"] if t["day"] == day), None
            )
            if not matched or matched["received"] < matched["count"]:
                raise BaiduError("累计 %s 天奖励未确认全部领取" % day)
            if issued:
                claimed.append(day)
        except BaiduError as error:
            report["errors"].append("累计 %s 天奖励：%s" % (day, error))

    report["signin_days"] = award_state(client)["signin_days"]
    if claimed:
        return "claimed:" + ",".join(str(day) for day in claimed)
    # 全部档位在复核时已被领完（并发或上一轮已领）不算失败
    return "award_failed" if report["errors"] else "no_award_due"


# ============================================================ 任务完成

def complete_task(client, uk, task, action=""):
    params = task_sign_params(task, uk)
    if action:
        params["action"] = action
    return client.call(API_TASK_SAVE, params)


def finish_task(client, uk, task):
    """把单个任务推进到 DONE，必要时补领奖。

    状态机：非 WAIT 先调完成，变成 WAIT 再调领奖；已经是 WAIT 就直接领。
    """
    result = {
        "task_id": task_id_of(task),
        "task_type": as_int(task.get("task_type")),
        "task_name": str(task.get("task_name") or ""),
        "status_before": task_status(task),
        "status_after": task_status(task),
        "ok": False,
        "claimed": False,
        "grow_score": 0,
        "score": 0,
        "error": "",
    }
    if not result["task_id"]:
        result["error"] = "任务参数不完整"
        return result
    if result["status_before"] == TASK_STATUS_DONE:
        result["ok"] = True
        return result

    status = result["status_before"]
    finish_resp = None
    claim_resp = None
    try:
        if status != TASK_STATUS_WAIT:
            finish_resp = complete_task(client, uk, task)
            status = as_int(as_dict(finish_resp.get("result")).get("taskStatus"))
            result["status_after"] = status
        if status == TASK_STATUS_WAIT:
            claim_resp = complete_task(client, uk, task, action="receive_award")
            status = as_int(as_dict(claim_resp.get("result")).get("taskStatus"))
            result["status_after"] = status
            result["claimed"] = status == TASK_STATUS_DONE
        result["status_after"] = status
        if status == TASK_STATUS_DONE:
            result["ok"] = True
        else:
            result["error"] = "任务状态=%s，未完成" % status
        # 完成响应和领奖响应都会回传奖励，取较大值避免重复相加
        for resp in (finish_resp, claim_resp):
            if not resp:
                continue
            payload = as_dict(resp.get("result"))
            result["grow_score"] = max(
                result["grow_score"], as_int(payload.get("addGrowScore"))
            )
            result["score"] = max(result["score"], as_int(payload.get("addScore")))
    except BaiduError as error:
        result["error"] = str(error)
    return result


def finish_progress_task(client, uk, task, refresh, max_steps=20):
    """处理 task_need_num 型进度任务（例如砍树 3 次），逐步推进后领奖。"""
    result = {
        "task_id": task_id_of(task),
        "task_type": as_int(task.get("task_type")),
        "task_name": str(task.get("task_name") or ""),
        "status_before": task_status(task),
        "status_after": task_status(task),
        "ok": False,
        "claimed": False,
        "grow_score": 0,
        "score": 0,
        "error": "",
    }
    if not result["task_id"]:
        result["error"] = "任务参数不完整"
        return result

    extra = task.get("task_extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    need = as_int(as_dict(extra).get("task_need_num"))
    if need <= 0:
        return finish_task(client, uk, task)

    try:
        for _ in range(max_steps):
            fresh = next(
                (item for item in refresh()
                 if task_id_of(item) == result["task_id"]),
                None,
            )
            if not fresh:
                result["error"] = "刷新后找不到任务"
                break
            status = task_status(fresh)
            current = as_int(fresh.get("task_has_finish_num"))
            result["status_after"] = status
            if status == TASK_STATUS_DONE:
                result["ok"] = True
                break
            if status == TASK_STATUS_WAIT or current >= need:
                merged = finish_task(client, uk, fresh)
                result.update({
                    "ok": merged["ok"],
                    "claimed": merged["claimed"],
                    "status_after": merged["status_after"],
                    "grow_score": merged["grow_score"],
                    "score": merged["score"],
                    "error": merged["error"],
                })
                break
            resp = complete_task(client, uk, fresh)
            payload = as_dict(resp.get("result"))
            result["score"] = max(result["score"], as_int(payload.get("addScore")))
            sleep_between(0.25, 0.45)
        else:
            result["error"] = "任务进度超过最大尝试次数"
    except BaiduError as error:
        result["error"] = str(error)
    return result


# ============================================================ C. 任务中心

def get_taskcenter_tasks(client):
    params = {
        "app_id": "250528",
        "web": "1",
        "clienttype": "0",
        "channel": "chunlei",
        "task_from": TASK_CENTER_FROM,
    }
    params.update(device_params(client.cuid))
    data = client.call(API_TASK_LIST, params)
    items = as_dict(data.get("result")).get("list") or []
    return [item for item in items if isinstance(item, dict)]


def points_balance(client, ptype):
    data = client.call(API_POINTS_BALANCE, {
        "app_id": "250528",
        "web": "1",
        "clienttype": "0",
        "channel": "chunlei",
        "ptype": str(ptype),
    })
    return as_int(data.get("balance"))


def run_task_batch(client, uk, tasks, allowed_types, refresh=None, report=None):
    """按白名单批量做任务，返回统计；不在白名单的类型跳过并计数。"""
    stats = {"found": len(tasks), "done": 0, "completed": 0, "skipped": 0,
             "failed": 0, "score": 0, "details": []}
    for task in tasks:
        status = task_status(task)
        score = as_int(task.get("task_score"))
        reward = as_int(task.get("task_reward_num"))
        if status == TASK_STATUS_DONE:
            stats["done"] += 1
            continue
        if score <= 0 and reward <= 0:
            continue
        task_type = as_int(task.get("task_type"))
        if task_type not in allowed_types:
            stats["skipped"] += 1
            continue
        if task_type == TASK_TYPE_PROGRESS and refresh is not None:
            result = finish_progress_task(client, uk, task, refresh)
        else:
            result = finish_task(client, uk, task)
        if result["ok"]:
            stats["completed"] += 1
            stats["score"] += result["score"]
        else:
            stats["failed"] += 1
            if report is not None and result["error"]:
                report["errors"].append(
                    "%s：%s" % (result["task_name"] or task_type, result["error"])
                )
        stats["details"].append(result)
        sleep_between(1.0, 2.5)
    return stats


def run_taskcenter(client, uk, report):
    tasks = get_taskcenter_tasks(client)
    return run_task_batch(
        client, uk, tasks, TASK_CENTER_TASK_TYPES,
        refresh=lambda: get_taskcenter_tasks(client), report=report,
    )


def get_points_tasks(client):
    params = {
        "app_id": "250528",
        "web": "1",
        "clienttype": "0",
        "channel": "chunlei",
    }
    params.update(device_params(client.cuid))
    data = client.call(API_POINTS_TASK_LIST, params)
    items = as_dict(data.get("result")).get("list") or []
    return [item for item in items if isinstance(item, dict)]


def run_points(client, uk, report):
    tasks = get_points_tasks(client)
    return run_task_batch(client, uk, tasks, POINTS_TASK_TYPES, report=report)


# ============================================================ App「赚积分」任务

def act_task_type(task):
    """App 任务把 task_type 藏在 task_extra 的 JSON 字符串里，顶层没有。"""
    extra = task.get("task_extra")
    if isinstance(extra, dict):
        return as_int(extra.get("task_type"))
    if isinstance(extra, str):
        try:
            return as_int(as_dict(json.loads(extra)).get("task_type"))
        except ValueError:
            return 0
    return as_int(task.get("task_type"))


def act_task_extra(task):
    extra = task.get("task_extra")
    if isinstance(extra, dict):
        return extra
    if isinstance(extra, str):
        try:
            return as_dict(json.loads(extra))
        except ValueError:
            return {}
    return {}


def act_list_params(client, config):
    """App 任务列表的完整参数。

    JS 里给每个请求都注入了 clienttype/channel/rand/devuid/time/version/_t，
    少任何一个都会返回 errno=1011。
    """
    return {
        "id": config["act_id"],
        "task_from": ACT_TASK_FROM,
        "clienttype": "1",
        "channel": "android",
        "version": config["act_version"],
        "rand": str(random.randint(10 ** 9, 10 ** 10 - 1)),
        "devuid": config["act_devuid"] or client.cuid,
        "time": str(int(time.time() * 1000)),
        "_t": str(int(time.time())),
    }


def get_act_task_groups(client, config):
    """App 任务列表，按 new_all / fresh / daily 分组返回。"""
    data = client.call(
        API_ACT_TASKLIST, act_list_params(client, config),
        referer=act_referer(config),
    )
    payload = as_dict(data.get("data"))
    groups = {}
    for key in ("new_all", "fresh", "daily"):
        value = payload.get(key)
        if isinstance(value, list):
            groups[key] = [item for item in value if isinstance(item, dict)]
    return groups


def flatten_act_tasks(groups):
    """三个分组可能重复，按 task_id 去重后返回。"""
    seen = {}
    for key in ("new_all", "fresh", "daily"):
        for task in groups.get(key) or []:
            task_id = task_id_of(task)
            if task_id and task_id not in seen:
                seen[task_id] = task
    return list(seen.values())


def act_task_params(client, uk, task, config):
    """App 端 tasksave 签名：md5(task_id_uk_rand_time)，**不带盐**。

    与 PC 端 task_sign_params 的差别就是少一个 SALT，clienttype 也要写成 110。
    """
    task_id = str(task.get("task_id_str") or task.get("task_id") or "")
    if not task_id or not uk:
        raise BaiduError("App 任务参数不完整（task_id/uk）")
    rand = str(random.randint(10 ** 9, 10 ** 10 - 1))
    stamp = str(int(time.time() * 1000))
    token = hashlib.md5(
        ("%s_%s_%s_%s" % (task_id, uk, rand, stamp)).encode("utf-8")
    ).hexdigest()
    return {
        "uk": uk,
        "task_id": task_id,
        "rand": rand,
        "time": stamp,
        "token": token,
        "task_from": ACT_TASK_FROM,
        "clienttype": "110",
        "version": config["act_version"],
        "channel": "android",
    }


def finish_act_task(client, uk, task, config):
    result = {
        "task_id": task_id_of(task),
        "task_type": act_task_type(task),
        "task_name": str(task.get("task_name") or ""),
        "status_before": task_status(task),
        "status_after": task_status(task),
        "ok": False,
        "score": 0,
        "error": "",
    }
    if result["status_before"] == TASK_STATUS_DONE:
        result["ok"] = True
        return result
    try:
        resp = client.call(
            API_TASK_SAVE, act_task_params(client, uk, task, config),
            referer=act_referer(config),
        )
        payload = as_dict(resp.get("result"))
        result["score"] = as_int(payload.get("addScore"))
        result["status_after"] = as_int(payload.get("taskStatus")) or \
            result["status_before"]
        result["ok"] = True
    except BaiduError as error:
        result["error"] = str(error)
    return result


def run_act_tasks(client, uk, report, config):
    """App「赚积分」页任务：只做白名单类型，其余跳过。"""
    stats = {"found": 0, "done": 0, "completed": 0, "skipped": 0,
             "failed": 0, "score": 0}
    groups = get_act_task_groups(client, config)
    tasks = flatten_act_tasks(groups)
    stats["found"] = len(tasks)
    allowed = config["act_types"]
    for task in tasks:
        if task_status(task) == TASK_STATUS_DONE:
            stats["done"] += 1
            continue
        task_type = act_task_type(task)
        if task_type not in allowed:
            stats["skipped"] += 1
            continue
        result = finish_act_task(client, uk, task, config)
        if result["ok"]:
            stats["completed"] += 1
            stats["score"] += result["score"]
        else:
            stats["failed"] += 1
            if result["error"]:
                report["errors"].append(
                    "%s：%s" % (result["task_name"] or task_type, result["error"])
                )
        sleep_between(1.0, 2.5)
    return stats


def fetch_task_list(client, source, config):
    """按来源拉任务列表，供 --try-type 使用。"""
    if source == "points":
        return get_points_tasks(client)
    if source == "taskcenter":
        return get_taskcenter_tasks(client)
    if source == "app":
        data = client.call(API_APP_TASKLIST, {
            "cuid": client.cuid, "task_from": APP_TASK_FROM,
        })
        items = as_dict(data.get("result")).get("list") or []
        return [item for item in items if isinstance(item, dict)]
    if source == "act":
        return flatten_act_tasks(get_act_task_groups(client, config))
    raise BaiduError(
        "未知来源 %s（可选 points / taskcenter / app / act）" % source
    )


def try_task_type(cookie, index, config, task_type, source):
    """--try-type：对指定类型的单个任务真发一次 tasksave，看服务端认不认。

    用来验证某个 task_type 能否自动完成。只打一个任务、不重试，返回 WAIT
    时才补一次领奖。注意这是写操作，会消耗掉这个任务。
    """
    client = BaiduClient(
        cookie, resolve_cuid(config, cookie), config["timeout"], config["ua"]
    )
    info = get_login_info(client)
    uk = str(info.get("uk") or "")
    print("👤 账号%d：%s  uk=%s" % (
        index,
        mask_name(str(info.get("username") or ""), config["privacy"]),
        mask_name(uk, True),
    ))

    balance_before = None
    if source == "points":
        balance_before = points_balance(client, POINTS_BALANCE_PTYPE)
        print("💰 积分余额（操作前）：%s" % balance_before)

    tasks = fetch_task_list(client, source, config)

    def type_of(item):
        # App 任务类型在 task_extra 里，其它来源在顶层
        return act_task_type(item) if source == "act" else as_int(item.get("task_type"))

    def sign(target):
        if source == "act":
            return act_task_params(client, uk, target, config)
        return task_sign_params(target, uk)

    same_type = [item for item in tasks if type_of(item) == task_type]
    candidates = [
        item for item in same_type
        if task_status(item) != TASK_STATUS_DONE
    ]
    print("📋 %s 共 %d 条任务，task_type=%s 有 %d 条，其中未完成 %d 条" % (
        source, len(tasks), task_type, len(same_type), len(candidates),
    ))
    if not candidates:
        print("（没有可试探的该类型任务）")
        return 0

    target = candidates[0]
    task_id = task_id_of(target)
    print("🎯 目标任务：%s（id=%s status=%s score=%s）" % (
        target.get("task_name"), task_id, task_status(target),
        target.get("task_score"),
    ))

    print("⏳ 发送 tasksave（单次，不重试；来源=%s）..." % source)
    raw = client.raw(API_TASK_SAVE, sign(target))
    print("📨 原始响应：%s" % json.dumps(raw, ensure_ascii=False)[:600])
    reported = as_int(as_dict(raw.get("result")).get("taskStatus"))
    print("📊 响应自报 taskStatus=%s（1=已完成 6=待领奖）" % reported)

    if reported == TASK_STATUS_WAIT and source != "act":
        # App 端一条请求就该完成，不补领奖
        print("🎁 返回待领奖，补一次 receive_award ...")
        claim = client.raw(API_TASK_SAVE, dict(sign(target), action="receive_award"))
        print("📨 领奖响应：%s" % json.dumps(claim, ensure_ascii=False)[:600])

    # 以重新拉取的列表为准，不信响应自报
    matched = next(
        (item for item in fetch_task_list(client, source, config)
         if task_id_of(item) == task_id),
        None,
    )
    if matched is None:
        print("⚠️  重新拉列表后找不到该任务")
    else:
        print("📊 复核：status %s → %s，finish=%s" % (
            task_status(target), task_status(matched),
            matched.get("task_finish_count"),
        ))
    if source == "points" and balance_before is not None:
        balance_after = points_balance(client, POINTS_BALANCE_PTYPE)
        print("💰 积分余额（操作后）：%s（变化 %+d）" % (
            balance_after, balance_after - balance_before,
        ))
    return 0


# ============================================================ E. 补签广告

def get_makeup_tasks(client):
    params = {
        "app_id": "250528",
        "web": "1",
        "clienttype": "0",
        "channel": "chunlei",
        "task_from": MAKEUP_TASK_FROM,
    }
    params.update(device_params(client.cuid))
    data = client.call(API_TASK_LIST, params)
    items = as_dict(data.get("result")).get("list") or []
    return [item for item in items if isinstance(item, dict)]


def count_max(task):
    """任务上限次数，藏在 task_mhz（JSON 字符串或对象）里。"""
    mhz = task.get("task_mhz")
    if isinstance(mhz, str):
        try:
            mhz = json.loads(mhz)
        except ValueError:
            mhz = {}
    value = mhz.get("count_max") if isinstance(mhz, dict) else None
    return as_int(value) or as_int(task.get("task_count_max")) or 1


def run_makeup(client, uk):
    """补签广告：把 task_finish_count 推到 count_max。"""
    tasks = get_makeup_tasks(client)
    targets = []
    for task in tasks:
        if as_int(task.get("task_type")) != TASK_TYPE_AD:
            continue
        required = count_max(task)
        done = as_int(task.get("task_finish_count"))
        if done < required and task_status(task) != TASK_STATUS_DONE:
            targets.append((task, required, done))
    if not targets:
        return {"found": len(tasks), "completed": 0, "state": "nothing_to_do"}

    completed = 0
    errors = []
    for task, required, start in targets:
        task_id = task_id_of(task)
        # 本地硬上限只约束「一轮最多打多少请求」，不能把没做完当成功
        limit = min(required, MAX_MAKEUP_STEPS)
        last_seen = None
        stalled = 0
        try:
            for _ in range(limit * 2 + 4):
                fresh = next(
                    (item for item in get_makeup_tasks(client)
                     if task_id_of(item) == task_id),
                    None,
                )
                if not fresh:
                    raise BaiduError("刷新后找不到补签广告任务")
                done = as_int(fresh.get("task_finish_count"))
                if done >= required or task_status(fresh) == TASK_STATUS_DONE:
                    break
                if done >= limit:
                    raise BaiduError(
                        "服务端要求 %s 次，超过本地上限 %s 次" % (required, limit)
                    )
                if last_seen is not None and done <= last_seen:
                    # 计数连续不推进说明接口没生效，继续刷只是白打请求
                    stalled += 1
                    if stalled >= 3:
                        raise BaiduError("补签广告计数连续 3 次未推进")
                else:
                    stalled = 0
                last_seen = done
                complete_task(client, uk, fresh)
                sleep_between(0.25, 0.5)
            final = next(
                (item for item in get_makeup_tasks(client)
                 if task_id_of(item) == task_id),
                None,
            )
            if not final:
                raise BaiduError("补签广告任务消失")
            final_done = as_int(final.get("task_finish_count"))
            if final_done < required and task_status(final) != TASK_STATUS_DONE:
                raise BaiduError("补签广告只到 %s/%s" % (final_done, required))
            completed += max(0, final_done - start)
        except BaiduError as error:
            # 单个任务失败不影响其它补签任务
            errors.append(str(error))
    if errors:
        return {"found": len(tasks), "completed": completed,
                "state": "failed", "message": "；".join(errors)}
    return {"found": len(tasks), "completed": completed, "state": "done"}


# ============================================================ 单账号执行

STEP_LABELS = {
    "pc": "网页签到",
    "question": "每日答题",
    "app": "App签到",
    "awards": "累计奖励",
    "ads": "成长值任务",
    "taskcenter": "任务中心",
    "points": "积分中心",
    "act": "App任务",
    "makeup": "补签广告",
}

STATE_TEXT = {
    "signed": "已完成",
    "already_signed": "今日已签到",
    "answered": "已答对",
    "already_answered": "今日已答过",
    "claimed": "已领取",
    "no_award_due": "无达标档位",
    "award_failed": "领取失败",
    "nothing_to_do": "今日已完成",
    "no_task": "页面未提供",
    "unavailable": "接口不可用",
    "done": "已完成",
    "failed": "失败",
}


def run_account(cookie, index, config):
    """跑一个账号的全部模块；单模块失败不影响其它模块。"""
    client = BaiduClient(
        cookie, resolve_cuid(config, cookie), config["timeout"], config["ua"]
    )
    report = {"index": index, "steps": [], "errors": [], "warnings": [], "ok": True}

    info = get_login_info(client)
    report["uk"] = str(info.get("uk") or "")
    report["username"] = mask_name(
        str(info.get("username") or ""), config["privacy"]
    )
    report["vip_level"] = info.get("vip_level")
    report["cuid"] = client.cuid

    meter = GrowthMeter(client)
    report["level"] = None
    report["product"] = ""
    try:
        level = get_level(client)
        report["level"] = level.get("level")
        report["product"] = level.get("product")
        meter.seed(level.get("value"))
    except Exception as error:
        report["warnings"].append("等级查询失败：%s" % error_text(error))
    try:
        report["growth_before"] = meter.value()
    except Exception as error:
        report["growth_before"] = None
        report["warnings"].append("成长值读取失败：%s" % error_text(error))

    modules = config["modules"]

    def record(key, outcome):
        # 测量类问题单独收进 warnings，不参与成败判定
        warnings = outcome.pop("warnings", [])
        step = {"key": key, "label": STEP_LABELS[key]}
        step.update(outcome)
        report["steps"].append(step)
        report["warnings"].extend(
            "%s：%s" % (step["label"], item) for item in warnings
        )
        return step

    # --- 网页签到
    if modules["pc"]:
        record("pc", meter.step(lambda: signin_pc(client)))

    # --- 每日答题（任务停在 WAIT 时补一次领奖，无论本轮是否新答题）
    if modules["question"]:
        def do_question():
            result = answer_question(client)
            try:
                tasks = get_growth_tasks(client)
            except BaiduError:
                return result["state"]  # 读不到任务列表不影响答题本身
            daily = next(
                (t for t in tasks
                 if as_int(t.get("task_type")) == TASK_TYPE_DAILY_QUESTION),
                None,
            )
            # 上一轮答题成功但领奖失败/延迟时，任务会一直停在 WAIT
            if daily and task_status(daily) == TASK_STATUS_WAIT:
                claim = finish_task(client, report["uk"], daily)
                if not claim["ok"]:
                    report["errors"].append(
                        "答题领奖：%s" % (claim["error"] or "未确认")
                    )
            return result["state"]

        record("question", meter.step(do_question))

    # --- App 签到
    if modules["app"]:
        record("app", meter.step(lambda: signin_app(client)))

    # --- 累计奖励
    if modules["awards"]:
        step = record("awards", meter.step(lambda: claim_awards(client, report)))
        step["signin_days"] = report.get("signin_days")

    # --- 成长值页任务（广告 + 已可自动完成的浏览类）
    if modules["ads"]:
        holder = {"stats": None}

        def do_ads():
            tasks = get_growth_tasks(client)
            holder["stats"] = run_task_batch(
                client, report["uk"], tasks, GROWTH_TASK_TYPES, report=report
            )
            return "done" if holder["stats"]["failed"] == 0 else "failed"

        step = record("ads", meter.step(do_ads))
        step["stats"] = holder["stats"]

    # --- 任务中心
    if modules["taskcenter"]:
        holder = {"stats": None}

        def do_taskcenter():
            holder["stats"] = run_taskcenter(client, report["uk"], report)
            return "done" if holder["stats"]["failed"] == 0 else "failed"

        step = record("taskcenter", meter.step(do_taskcenter))
        if holder["stats"]:
            step["stats"] = holder["stats"]

    # --- 积分中心
    if modules["points"]:
        holder = {"stats": None, "before": None, "after": None}

        def do_points():
            try:
                holder["before"] = points_balance(client, POINTS_BALANCE_PTYPE)
            except BaiduError:
                holder["before"] = None
            holder["stats"] = run_points(client, report["uk"], report)
            try:
                holder["after"] = points_balance(client, POINTS_BALANCE_PTYPE)
            except BaiduError:
                holder["after"] = None
            return "done" if holder["stats"]["failed"] == 0 else "failed"

        step = record("points", meter.step(do_points))
        step["stats"] = holder["stats"]
        step["points_before"] = holder["before"]
        step["points_after"] = holder["after"]

    # --- App「赚积分」任务（netdisk_activity，另一套接口）
    if modules["act"]:
        holder = {"stats": None, "error": ""}

        def do_act():
            try:
                holder["stats"] = run_act_tasks(
                    client, report["uk"], report, config
                )
            except BaiduError as error:
                # 这套接口还在校准期，拉不到列表只记警告，不污染每日报告
                holder["error"] = str(error)
                report["warnings"].append("App任务列表读取失败：%s" % error)
                return "unavailable"
            return "done" if holder["stats"]["failed"] == 0 else "failed"

        step = record("act", meter.step(do_act))
        step["stats"] = holder["stats"]
        if holder["error"]:
            step["message"] = holder["error"]

    # --- 补签广告
    if modules["makeup"]:
        holder = {"result": None}

        def do_makeup():
            holder["result"] = run_makeup(client, report["uk"])
            if holder["result"]["state"] == "failed":
                raise BaiduError(holder["result"].get("message") or "补签广告未完成")
            return holder["result"]["state"]

        step = record("makeup", meter.step(do_makeup))
        step["makeup"] = holder["result"]

    try:
        report["growth_after"] = meter.value()
    except Exception as error:
        report["growth_after"] = None
        report["warnings"].append("成长值回读失败：%s" % error_text(error))
    if report["growth_after"] is not None and report["growth_before"] is not None:
        report["growth_delta"] = report["growth_after"] - report["growth_before"]
    else:
        report["growth_delta"] = None
    report["ok"] = not report["errors"] and all(
        step["state"] not in (None, "failed", "award_failed") for step in report["steps"]
    )
    return report


# ============================================================ 报告

def step_line(step):
    state = step["state"] or ""
    if state.startswith("claimed:"):
        text = "已领取（%s 天档）" % state.split(":", 1)[1]
    else:
        text = STATE_TEXT.get(state, state or "未确认")
    if step.get("delta") is not None:
        text += "（成长值 %+d）" % step["delta"]
    stats = step.get("stats")
    if stats:
        text += "，完成 %d/跳过 %d/失败 %d" % (
            stats["completed"], stats["skipped"], stats["failed"]
        )
    if step.get("points_before") is not None and step.get("points_after") is not None:
        text += "，积分 %d→%d" % (step["points_before"], step["points_after"])
    if step.get("signin_days") is not None:
        text += "，累计签到 %d 天" % step["signin_days"]
    if step.get("makeup"):
        text += "，完成 %d 次" % step["makeup"]["completed"]
    return ("✅ " if step["state"] not in (None, "failed", "award_failed") else "❌ ") \
        + step["label"] + "：" + text


def build_message(report):
    before = report.get("growth_before")
    after = report.get("growth_after")
    delta = report.get("growth_delta")
    lines = [
        "👤 账号%d：%s" % (report["index"], report["username"]),
        "🏆 等级 Lv%s（%s），成长值 %s" % (
            report.get("level"), report.get("product") or "普通用户",
            "?" if before is None else before,
        ),
    ]
    for step in report["steps"]:
        lines.append(step_line(step))
        if step["message"]:
            lines.append("   原因：%s" % step["message"])
    if before is None or after is None:
        lines.append("📈 本轮成长值：%s → %s（未完整测量）" % (
            "?" if before is None else before,
            "?" if after is None else after,
        ))
    else:
        lines.append("📈 本轮成长值 %s → %s（%+d）" % (before, after, delta or 0))
    for error in report["errors"]:
        lines.append("❌ %s" % error)
    for warning in report.get("warnings") or []:
        lines.append("⚠️ %s" % warning)
    lines.append("⏰ %s" % datetime.now().strftime("%m-%d %H:%M"))
    return ("百度网盘账号%d签到%s" % (
        report["index"], "成功" if report["ok"] else "异常"
    ), "\n".join(lines))


def notify_user(title, content):
    if hadsend:
        try:
            send(title, content)
            print("✅ 通知发送完成: %s" % title)
        except Exception as error:  # 通知失败不能影响主流程
            print("❌ 通知发送失败: %s" % error)
    else:
        print("📢 %s\n📄 %s" % (title, content))


# ============================================================ 探测模式

def summarize_task_list(items):
    if not items:
        return ["（空）"]
    lines = []
    for item in items:
        lines.append(
            "  task_type=%s task_status=%s name=%s from=%s score=%s reward=%s "
            "id=%s mhz=%s finish=%s" % (
                item.get("task_type"), item.get("task_status"),
                item.get("task_name"), item.get("task_from"),
                item.get("task_score"), item.get("task_reward_num"),
                task_id_of(item), item.get("task_mhz"),
                item.get("task_finish_count"),
            )
        )
    return lines


def probe_account(cookie, index, config):
    """只读探测：不签到、不领奖、不做任务，只把接口真实结构打出来。"""
    client = BaiduClient(
        cookie, resolve_cuid(config, cookie), config["timeout"], config["ua"]
    )
    out = ["", "=" * 62, "账号%d 只读探测" % index, "=" * 62]
    uk = ""
    failures = 0

    def section(title, func):
        nonlocal failures
        out.append("")
        out.append("=== %s ===" % title)
        try:
            func()
        except Exception as error:  # 探测模式要把任何异常都变成一行输出，不能中断
            failures += 1
            out.append("  [失败] %s: %s" % (type(error).__name__, error))

    def do_login():
        nonlocal uk
        data = client.raw(API_LOGIN)
        info = as_dict(data.get("login_info"))
        uk = str(info.get("uk") or "")
        out.append("  errno=%s uk=%s username=%s vip_level=%s" % (
            data.get("errno"),
            mask_name(uk, True),
            mask_name(str(info.get("username") or ""), True),
            info.get("vip_level"),
        ))

    def do_growth():
        data = client.raw(API_GROWTH)
        info = as_dict(data.get("level_info"))
        product = as_dict(data.get("current_product_v2"))
        out.append("  errno=%s error_code=%s level=%s value=%s product=%s" % (
            data.get("errno"), data.get("error_code"),
            info.get("current_level"), info.get("current_value"),
            product.get("product_type"),
        ))

    def do_question():
        data = client.raw(API_QUESTION)
        payload = as_dict(data.get("data"))
        out.append("  errno=%s answer_status=%s ask_id=%s answer=%s question=%s" % (
            data.get("errno"), payload.get("answer_status"),
            payload.get("ask_id"), payload.get("answer"),
            str(payload.get("question") or "")[:40],
        ))

    def do_app_state():
        data = client.raw(API_APP_SIGNINLIST, {"cuid": client.cuid})
        payload = as_dict(data.get("data"))
        out.append("  errno=%s signed_today=%s signin_days=%s" % (
            data.get("errno"), payload.get("signed_today"),
            payload.get("signin_days"),
        ))
        rewards = payload.get("reward_list") or []
        out.append("  reward_list(%d):" % len(rewards))
        for item in rewards:
            prize = item.get("prize") or []
            out.append("    day=%s received=%s prize=%d 项" % (
                item.get("day"), item.get("received"), len(prize),
            ))

    def do_app_tasks():
        data = client.raw(API_APP_TASKLIST, {
            "cuid": client.cuid, "task_from": APP_TASK_FROM,
        })
        items = as_dict(data.get("result")).get("list") or []
        out.append("  errno=%s result.list(%d)" % (data.get("errno"), len(items)))
        out.extend(summarize_task_list(items))

    def do_growth_tasks():
        data = client.raw(API_TASK_LIST, {
            "app_id": "250528", "web": "1", "clienttype": "0",
            "channel": "chunlei", "task_from": GROWTH_TASK_FROM,
        })
        items = as_dict(data.get("result")).get("list") or []
        out.append("  errno=%s result.list(%d)" % (data.get("errno"), len(items)))
        out.extend(summarize_task_list(items))

    def do_center_tasks():
        params = {
            "app_id": "250528", "web": "1", "clienttype": "0",
            "channel": "chunlei", "task_from": TASK_CENTER_FROM,
        }
        params.update(device_params(client.cuid))
        data = client.raw(API_TASK_LIST, params)
        items = as_dict(data.get("result")).get("list") or []
        out.append("  errno=%s result.list(%d)" % (data.get("errno"), len(items)))
        out.extend(summarize_task_list(items))

    def do_makeup_tasks():
        params = {
            "app_id": "250528", "web": "1", "clienttype": "0",
            "channel": "chunlei", "task_from": MAKEUP_TASK_FROM,
        }
        params.update(device_params(client.cuid))
        data = client.raw(API_TASK_LIST, params)
        items = as_dict(data.get("result")).get("list") or []
        out.append("  errno=%s result.list(%d)" % (data.get("errno"), len(items)))
        out.extend(summarize_task_list(items))

    def do_points_tasks():
        params = {
            "app_id": "250528", "web": "1", "clienttype": "0",
            "channel": "chunlei",
        }
        params.update(device_params(client.cuid))
        data = client.raw(API_POINTS_TASK_LIST, params)
        items = as_dict(data.get("result")).get("list") or []
        out.append("  errno=%s result.list(%d)" % (data.get("errno"), len(items)))
        out.extend(summarize_task_list(items))

    def do_balance():
        for ptype in (POINTS_BALANCE_PTYPE, TASKCENTER_BALANCE_PTYPE):
            data = client.raw(API_POINTS_BALANCE, {
                "app_id": "250528", "web": "1", "clienttype": "0",
                "channel": "chunlei", "ptype": str(ptype),
            })
            out.append("  ptype=%s errno=%s balance=%s" % (
                ptype, data.get("errno"), data.get("balance"),
            ))

    def do_act_tasks():
        """App「赚积分」页的任务列表：与 task_sys_* 是另一套接口。"""
        data = client.raw(
            API_ACT_TASKLIST, act_list_params(client, config),
            referer=act_referer(config),
        )
        out.append("  errno=%s errmsg=%s" % (
            data.get("errno"),
            data.get("errmsg") or data.get("show_msg") or "",
        ))
        payload = as_dict(data.get("data"))
        if not payload:
            out.append("  data=%r" % (data.get("data"),))
            return
        out.append("  isQuotaReach=%s" % payload.get("isQuotaReach"))
        for key in ("new_all", "fresh", "daily"):
            if key not in payload:
                continue
            items = payload.get(key) or []
            out.append("  %s(%d):" % (key, len(items)))
            for item in items:
                if not isinstance(item, dict):
                    continue
                parsed = {}
                extra = item.get("task_extra")
                if isinstance(extra, str):
                    try:
                        parsed = as_dict(json.loads(extra))
                    except ValueError:
                        parsed = {}
                out.append(
                    "    type=%s status=%s finish=%s score=%s id=%s "
                    "name=%s need=%s url=%s" % (
                        parsed.get("task_type"), item.get("task_status"),
                        item.get("task_finish_count"), item.get("task_score"),
                        task_id_of(item), item.get("task_name"),
                        parsed.get("task_need_num"),
                        str(parsed.get("task_start_url") or "")[:36],
                    )
                )
            if items and isinstance(items[0], dict):
                out.append("    字段: %s" % ",".join(sorted(items[0].keys())))

    section("1. 登录态 " + API_LOGIN.split("?")[0], do_login)
    section("2. 成长值 " + API_GROWTH.split("?")[0], do_growth)
    section("3. 每日答题 " + API_QUESTION.split("?")[0], do_question)
    section("4. App签到状态 " + API_APP_SIGNINLIST, do_app_state)
    section("5. App任务列表 " + API_APP_TASKLIST, do_app_tasks)
    section("6. 成长值页任务 " + API_TASK_LIST + "?task_from=" + GROWTH_TASK_FROM,
            do_growth_tasks)
    section("7. 任务中心 " + API_TASK_LIST + "?task_from=<7 分类>", do_center_tasks)
    section("8. 补签广告 " + API_TASK_LIST + "?task_from=" + MAKEUP_TASK_FROM,
            do_makeup_tasks)
    section("9. 积分中心任务 " + API_POINTS_TASK_LIST, do_points_tasks)
    section("10. 积分余额 " + API_POINTS_BALANCE, do_balance)
    section("11. App赚积分任务 " + API_ACT_TASKLIST
            + "?task_from=" + ACT_TASK_FROM, do_act_tasks)

    out.append("")
    out.append("cuid=%s（来自%s）" % (
        mask_name(client.cuid, True),
        "BAIDU_CUID" if config["cuid"] else "cookie/env 派生",
    ))
    # -6 = invalid bduss：HTTP 层成功但业务失败，也必须算进自检结果
    invalid_cookie = sum(1 for line in out if "errno=-6" in line)
    if invalid_cookie:
        out.append("")
        out.append("提示：errno=-6 表示 cookie 无效或已过期，请重新导出")
    return "\n".join(out), failures + invalid_cookie


def probe_act_sources(cookie, index, config):
    """--probe-act：找 App 任务中心的真实数据来源。

    两路都是只读 GET：
    A. 用多个候选 task_from 打 /api/taskscore/tasklist，看哪组能带出 AI/功能任务
    B. 打 /act/v2/taskcenter/recordlist，从完成记录里反查真实 task_from
    """
    client = BaiduClient(
        cookie, resolve_cuid(config, cookie), config["timeout"], config["ua"]
    )
    info = get_login_info(client)
    uk = str(info.get("uk") or "")
    print("👤 账号%d：%s  uk=%s" % (
        index,
        mask_name(str(info.get("username") or ""), config["privacy"]),
        mask_name(uk, True),
    ))

    print("\n=== A. task_from 候选（%s）===" % API_TASK_LIST)
    print("%-26s %-10s %s" % ("task_from", "结果", "前几条任务名"))
    print("-" * 74)
    for candidate in ACT_TASK_FROM_CANDIDATES:
        params = {
            "app_id": "250528",
            "web": "1",
            "clienttype": "0",
            "channel": "chunlei",
            "task_from": candidate,
        }
        params.update(device_params(client.cuid))
        try:
            data = client.raw(API_TASK_LIST, params)
            items = as_dict(data.get("result")).get("list") or []
            names = "、".join(
                str(item.get("task_name"))
                for item in items[:4] if isinstance(item, dict)
            )
            print("%-26s errno=%-5s %s" % (
                candidate, data.get("errno"), names or "（空）",
            ))
        except BaiduError as error:
            print("%-26s ERR       %s" % (candidate, str(error)[:48]))

    print("\n=== B. 任务中心记录 %s ===" % API_TASKCENTER_RECORD)
    for label, params in (("无参数", {}), ("带uk", {"uk": uk})):
        try:
            data = client.raw(API_TASKCENTER_RECORD, params)
            print("  %-4s errno=%s  %s" % (
                label, data.get("errno"),
                json.dumps(data, ensure_ascii=False)[:400],
            ))
        except BaiduError as error:
            print("  %-4s ERR %s" % (label, str(error)[:60]))

    # task_from 是服务端枚举校验的，已知 7 个就是全集；
    # 剩下唯一变量是 clienttype / web / channel（App 专属任务可能按端过滤）
    print("\n=== C. clienttype/web/channel 变体 ===")
    variants = [
        ("ai  ct0 web1", "task_sys_ai", "0", "1", "chunlei"),
        ("ai  ct1 web5", "task_sys_ai", "1", "5", "chunlei"),
        ("ai  ct110 web5", "task_sys_ai", "110", "5", "chunlei"),
        ("ai  ct1 web5 android", "task_sys_ai", "1", "5", "android"),
        ("7类 ct1 web5", TASK_CENTER_FROM, "1", "5", "chunlei"),
        ("7类 ct110 web5", TASK_CENTER_FROM, "110", "5", "chunlei"),
    ]
    for label, task_from, clienttype, web, channel in variants:
        params = {
            "app_id": "250528",
            "web": web,
            "clienttype": clienttype,
            "channel": channel,
            "task_from": task_from,
        }
        params.update(device_params(client.cuid))
        try:
            data = client.raw(API_TASK_LIST, params)
            items = as_dict(data.get("result")).get("list") or []
            names = "、".join(
                str(item.get("task_name"))
                for item in items[:6] if isinstance(item, dict)
            )
            print("%-20s errno=%-5s list=%-3d %s" % (
                label, data.get("errno"), len(items), names or "（空）",
            ))
        except BaiduError as error:
            print("%-20s ERR %s" % (label, str(error)[:48]))
    return 0


# ============================================================ 入口

def check_account(cookie, index, config):
    """--check：只查状态，不签到、不领奖、不做任务。"""
    client = BaiduClient(
        cookie, resolve_cuid(config, cookie), config["timeout"], config["ua"]
    )
    info = get_login_info(client)
    username = mask_name(str(info.get("username") or ""), config["privacy"])
    level = get_level(client)
    print("👤 账号%d：%s  uk=%s" % (
        index, username, mask_name(str(info.get("uk") or ""), True),
    ))
    print("🏆 Lv%s 成长值 %s %s" % (
        level.get("level"), level.get("value"),
        level.get("product") or "普通用户",
    ))
    ok = True
    try:
        state = award_state(client)
        print("📅 App签到：signed_today=%s，累计 %s 天" % (
            state["signed_today"], state["signin_days"],
        ))
        tiers = "、".join(
            "%s天(%s/%s)" % (tier["day"], tier["received"], tier["count"])
            for tier in state["tiers"]
        )
        print("🎁 累计档位：%s" % (tiers or "无"))
    except BaiduError as error:
        ok = False
        print("⚠️  App签到/累计奖励状态读取失败：%s" % error)
    for ptype, label in ((POINTS_BALANCE_PTYPE, "积分中心"),
                         (TASKCENTER_BALANCE_PTYPE, "任务中心")):
        try:
            print("💰 %s余额：%s" % (label, points_balance(client, ptype)))
        except BaiduError as error:
            ok = False
            print("⚠️  %s余额读取失败：%s" % (label, error))
    return ok


def split_cookies(raw):
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def format_remaining(seconds):
    if seconds <= 0:
        return "立即执行"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d小时%d分%d秒" % (hours, minutes, secs)
    if minutes:
        return "%d分%d秒" % (minutes, secs)
    return "%d秒" % secs


def wait_with_countdown(seconds):
    if seconds <= 0:
        return
    print("🎲 随机延迟 %s" % format_remaining(seconds))
    remaining = seconds
    while remaining > 0:
        chunk = 1 if remaining <= 10 else min(10, remaining)
        time.sleep(chunk)
        remaining -= chunk
        if remaining and (remaining <= 10 or remaining % 60 == 0):
            print("   倒计时 %s" % format_remaining(remaining))


def parse_args(argv):
    """手写参数解析：只认几个开关，避免 argparse 在青龙日志里多出噪音。"""
    args = argv[1:]
    probe = "--probe" in args
    check = "--check" in args
    probe_act = "--probe-act" in args
    try_type = None
    source = None
    for position, arg in enumerate(args):
        if arg == "--try-type" and position + 1 < len(args):
            try_type = args[position + 1]
        elif arg.startswith("--try-type="):
            try_type = arg.split("=", 1)[1]
        elif arg == "--source" and position + 1 < len(args):
            source = args[position + 1]
        elif arg.startswith("--source="):
            source = arg.split("=", 1)[1]
    return probe, check, try_type, source, probe_act


def main():
    probe, check_only, try_type, source, probe_act = parse_args(sys.argv)

    print("==== 百度网盘签到开始 - %s ====" %
          datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    config = load_config()
    # 青龙「运行」按钮不会转发命令行参数，所以环境变量也能开启诊断模式
    try_type_from_env = False
    if try_type is None and config["try_type"]:
        try_type = config["try_type"]
        try_type_from_env = True
    if source is None:
        source = config["try_source"] or "points"
    probe_act = probe_act or config["probe_act"]
    if not config["cookie"]:
        message = """❌ 未找到 BAIDU_COOKIE 环境变量

🔧 获取方法：
1. 浏览器打开 https://pan.baidu.com/ 并登录
2. F12 → Network → 刷新页面
3. 找到任意请求的 Request Headers，复制完整 Cookie
4. 青龙面板 → 环境变量 → 新增 BAIDU_COOKIE
"""
        print(message)
        notify_user("百度网盘签到失败", message)
        return 2

    cookies = split_cookies(config["cookie"])
    print("📝 共发现 %d 个账号" % len(cookies))
    if not probe and not check_only and try_type is None and not probe_act:
        enabled = [name for name, on in config["modules"].items() if on]
        if not enabled:
            message = "❌ 所有模块都已关闭，请检查 BAIDU_ENABLE_* 环境变量"
            print(message)
            notify_user("百度网盘签到未执行", message)
            return 2
        print("🔧 已启用模块：%s" % "、".join(STEP_LABELS[name] for name in enabled))
        if config["random_signin"] and config["max_delay"] > 0:
            wait_with_countdown(random.randint(0, config["max_delay"]))

    if probe:
        failures = 0
        for index, cookie in enumerate(cookies):
            try:
                text, count = probe_account(cookie, index + 1, config)
                print(text)
                failures += count
            except Exception as error:
                failures += 1
                print("账号%d 探测异常：%s" % (index + 1, error))
        if failures:
            print("\n⚠️  共 %d 个接口/账号探测失败，见上方 [失败] 行" % failures)
        print("\n（--probe 只读，未向百度写入任何数据）")
        return 1 if failures else 0

    if check_only:
        failures = 0
        for index, cookie in enumerate(cookies):
            try:
                if not check_account(cookie, index + 1, config):
                    failures += 1
            except Exception as error:
                failures += 1
                print("❌ 账号%d 查询失败：%s" % (index + 1, error))
        print("\n（--check 只读，未向百度写入任何数据）")
        return 1 if failures else 0

    if try_type is not None:
        print("\n⚠️  --try-type 是写操作：只对 1 个任务发 1 次请求，会消耗该任务")
        failures = 0
        for index, cookie in enumerate(cookies):
            try:
                try_task_type(cookie, index + 1, config, as_int(try_type), source)
            except Exception as error:
                failures += 1
                print("❌ 账号%d 试探失败：%s" % (index + 1, error_text(error)))
        if try_type_from_env:
            print("\n⚠️  诊断模式来自环境变量，请立刻到青龙删除 "
                  "BAIDU_TRY_TYPE / BAIDU_TRY_SOURCE，否则每天只会做这次试探、不签到")
        return 1 if failures else 0

    if probe_act:
        failures = 0
        for index, cookie in enumerate(cookies):
            try:
                probe_act_sources(cookie, index + 1, config)
            except Exception as error:
                failures += 1
                print("❌ 账号%d 来源探测失败：%s" % (index + 1, error_text(error)))
        return 1 if failures else 0

    reports = []
    aborted = []
    for index, cookie in enumerate(cookies):
        if index > 0:
            sleep_between(10, 20)
        print("\n==== 账号%d 开始 ====" % (index + 1))
        try:
            report = run_account(cookie, index + 1, config)
        except Exception as error:
            aborted.append(index + 1)
            print("❌ 账号%d 中止：%s" % (index + 1, error))
            notify_user(
                "百度网盘账号%d签到失败" % (index + 1), error_text(error)
            )
            continue
        reports.append(report)

        for step in report["steps"]:
            print(LOG_PREFIX + step_line(step))
            if step["message"]:
                print(LOG_PREFIX + "  原因：%s" % step["message"])
        print(LOG_PREFIX + "成长值 %s → %s" % (
            "?" if report["growth_before"] is None else report["growth_before"],
            "?" if report["growth_after"] is None else report["growth_after"],
        ))
        for warning in report.get("warnings") or []:
            print(LOG_PREFIX + "⚠️ %s" % warning)

        title, content = build_message(report)
        notify_user(title, content)

    total = len(cookies)
    success = sum(1 for item in reports if item["ok"])
    failed_count = total - success

    if total > 1:
        lines = [
            "📊 百度网盘签到汇总",
            "📈 总计 %d 个账号" % total,
            "✅ 成功 %d，❌ 异常 %d" % (success, failed_count),
        ]
        for item in reports:
            lines.append("%s 账号%d %s" % (
                "✅" if item["ok"] else "❌", item["index"], item["username"],
            ))
        for number in aborted:
            lines.append("❌ 账号%d 执行中止" % number)
        notify_user("百度网盘签到汇总", "\n".join(lines))

    print("\n==== 完成 - 成功 %d/%d ====" % (success, total))
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
