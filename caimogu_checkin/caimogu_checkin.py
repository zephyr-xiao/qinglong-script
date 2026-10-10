#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
new Env('采蘑菇论坛自动签到');
cron: 8 8 * * *

采蘑菇论坛(caimogu.cc)自动回帖刷活跃度。Playwright 驱动真实浏览器，
AI/模板双模式生成拟人评论，每天在指定板块回复若干帖子。

环境变量：
  CAIMOGU_COOKIE            登录 Cookie 字符串（优先凭证）
  CAIMOGU_AUTH_FILE         auth_state.json 路径（回退凭证；默认脚本同目录）
  CAIMOGU_ACCOUNTS          自动登录账号，格式 用户名#密码（手机号/用户名均可）
                            兼容旧写法：CAIMOGU_USER + CAIMOGU_PASSWORD
  CAIMOGU_CIRCLE_URL        板块地址（默认 https://www.caimogu.cc/circle/308.html）
  CAIMOGU_REPLY_COUNT       每天回帖数（默认 3）
  CAIMOGU_MIN_DELAY         回帖最小间隔秒（默认 60）
  CAIMOGU_MAX_DELAY         回帖最大间隔秒（默认 180）
  CAIMOGU_DEEPSEEK_API_KEY  AI 模式 API Key（留空则用模板模式）
  CAIMOGU_DEEPSEEK_BASE_URL AI API 地址（默认 DeepSeek 官方，OpenAI 兼容）
  CAIMOGU_DEEPSEEK_MODEL    AI 模型名（默认 deepseek-chat）
  CAIMOGU_AI_TIMEOUT        AI 接口读取超时秒数（默认 120）
  CAIMOGU_NOTIFY            是否推送（默认 true）
  CAIMOGU_NOTIFY_ONLY_FAIL  仅失败时推送（默认 false，需 NOTIFY=true）
  CAIMOGU_DEBUG             调试截屏存 _caimogu_debug_*.png（默认 false）
  CAIMOGU_HEADFUL           显示真实浏览器窗口，本地调试用（默认 false）
  CAIMOGU_CHROMIUM_PATH     chromium 绝对路径（默认自动搜索系统路径）
  CAIMOGU_TIMEOUT_MS        Playwright 页面超时毫秒（默认 90000）
  CAIMOGU_PROXY             可选代理 http://host:port（默认空）
                                   未配置时自动回退青龙全局代理（HTTPS_PROXY /
                                   HTTP_PROXY / ALL_PROXY），仍为空则直连

用法：
  python caimogu_checkin.py            执行自动回帖
  python caimogu_checkin.py --test     测试评论生成效果（不启动浏览器）
  python caimogu_checkin.py --help     显示帮助
作者: zephyr_xiao
"""

import json
import os
import re
import random
import time
import sys
import logging
from datetime import datetime, date
from pathlib import Path

# ============================================================
#  0. 青龙环境脚手架
# ============================================================

# UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 问题）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# requests 缺失友好提示（不要直接 ImportError 崩掉）
try:
    import requests  # noqa: F401
except ImportError:
    print("❌ 缺少 requests 库，请在青龙面板「依赖管理」-「Python」中安装 requests")
    sys.exit(1)

# notify.py 兼容（青龙运行时把 notify.py 加入 sys.path；本地干跑找不到不报错）
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
        print("ℹ️ 未找到 notify.py（仅在青龙环境内可用），跳过推送")
        return False
    max_attempts, retry_delay = 3, 3
    for attempt in range(1, max_attempts + 1):
        try:
            _qinglong_send(title, content)
            print("📨 已通过 notify.py 推送")
            return True
        except Exception as e:
            print(f"⚠️ 推送第 {attempt}/{max_attempts} 次失败: {e}")
            if attempt < max_attempts:
                print(f"   等待 {retry_delay} 秒后重试推送...")
                time.sleep(retry_delay)
            else:
                print("⚠️ notify.py 推送最终失败")
    return False


def env_bool(name, default):
    """解析青龙 bool 环境变量"""
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name, default):
    """解析青龙 int 环境变量"""
    try:
        return int(os.getenv(name) or default)
    except Exception:
        return default


# 青龙面板「配置文件 / 环境变量」里配置的全局代理变量
GLOBAL_PROXY_KEYS = (
    "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)


def resolve_proxy(*specific_keys):
    """
    解析代理地址，返回 (代理地址, 来源变量名)，两者均可能为空字符串。

    优先级：脚本专属变量 > 青龙全局代理变量 > 空（直连）。
    Playwright 的 launch(proxy=...) 不读取环境变量，故必须显式取值。
    """
    for name in specific_keys:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    for name in GLOBAL_PROXY_KEYS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def parse_account(raw: str) -> tuple:
    """
    解析「用户名#密码」格式的账号配置，返回 (用户名, 密码)，解析失败返回空串。

    兼容多组写法（& 或换行分隔），但本脚本的自动登录流程按单账号设计，只取第一组。
    """
    first = next((item.strip() for item in raw.replace("\n", "&").split("&") if item.strip()), "")
    if "#" not in first:
        return "", ""
    user, pwd = first.split("#", 1)
    user, pwd = user.strip(), pwd.strip()
    return (user, pwd) if user and pwd else ("", "")


def parse_cookie_str(s):
    """'a=1; b=2' -> {'a':'1','b':'2'}"""
    jar = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar[k.strip()] = v.strip()
    return jar

# ============================================================
#  1. 路径、常量与配置
# ============================================================

SCRIPT_DIR = Path(__file__).parent.absolute()

PATHS = {
    "replied": SCRIPT_DIR / "replied_posts.json",
    "log":     SCRIPT_DIR / "signin_log.txt",
}

# 免安装版优先使用程序目录旁边的 Playwright 浏览器
_browsers_dir = SCRIPT_DIR / "playwright-browsers"
if _browsers_dir.exists():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_browsers_dir))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "circle_url": "https://www.caimogu.cc/circle/308.html",
    "reply_count": 3,
    # 间隔拉到分钟级：站点有 AI 水贴检测（踩蘑菇AI妹妹），短间隔连发更像脚本
    "min_delay": 60,
    "max_delay": 180,
    "headless": True,
    "page_timeout_ms": 90000,
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com/v1",
    "deepseek_model": "deepseek-chat",
    "ai_timeout": 120,
}

# 站点接口契约（2026-10 逆向自 postV2/detail.min.js，已用未登录探测验证）
CAIMOGU_BASE = "https://www.caimogu.cc"
REPLY_API_PATH = "/post/act/comment"        # 回帖接口，响应 {status, data, info}
POST_LIST_API_PATH = "/circle/act/post_list"  # 板块列表接口，免登录

# 回帖接口 status 语义（保守策略：非 1 一律视为失败并中止本轮）
REPLY_STATUS_OK = 1
REPLY_STATUS_NOT_LOGIN = -1001
REPLY_STATUS_WATER_SOFT = 889   # 软警告：AI 妹妹提示疑似水贴，可点「继续发布」
REPLY_STATUS_WATER_HARD = 888   # 硬拦：判定无意义水贴，发不出去
REPLY_STATUS_NEED_AUDIT = 887   # 疑似水贴，转后台审核（影响力审核通过后才发放）


def load_config():
    """从青龙环境变量读取配置，默认值与 Windows 版 DEFAULT_CONFIG 对齐"""
    proxy, proxy_source = resolve_proxy("CAIMOGU_PROXY")

    # 账号密码：优先合并式 CAIMOGU_ACCOUNTS（用户名#密码），回退旧的分开写法
    accounts_raw = os.getenv("CAIMOGU_ACCOUNTS", "").strip()
    if accounts_raw:
        login_user, login_password = parse_account(accounts_raw)
    else:
        login_user = os.getenv("CAIMOGU_USER", "").strip()
        login_password = os.getenv("CAIMOGU_PASSWORD", "").strip()

    cfg = {
        "circle_url": os.getenv("CAIMOGU_CIRCLE_URL", DEFAULT_CONFIG["circle_url"]),
        "reply_count": env_int("CAIMOGU_REPLY_COUNT", DEFAULT_CONFIG["reply_count"]),
        "min_delay": env_int("CAIMOGU_MIN_DELAY", DEFAULT_CONFIG["min_delay"]),
        "max_delay": env_int("CAIMOGU_MAX_DELAY", DEFAULT_CONFIG["max_delay"]),
        "headless": not env_bool("CAIMOGU_HEADFUL", False),
        "page_timeout_ms": env_int("CAIMOGU_TIMEOUT_MS", DEFAULT_CONFIG["page_timeout_ms"]),
        "deepseek_api_key": os.getenv("CAIMOGU_DEEPSEEK_API_KEY", ""),
        "deepseek_base_url": os.getenv("CAIMOGU_DEEPSEEK_BASE_URL", DEFAULT_CONFIG["deepseek_base_url"]),
        "deepseek_model": os.getenv("CAIMOGU_DEEPSEEK_MODEL", DEFAULT_CONFIG["deepseek_model"]),
        "ai_timeout": env_int("CAIMOGU_AI_TIMEOUT", DEFAULT_CONFIG["ai_timeout"]),
        "cookie": os.getenv("CAIMOGU_COOKIE", ""),
        "auth_file": os.getenv("CAIMOGU_AUTH_FILE", str(SCRIPT_DIR / "auth_state.json")),
        "user": login_user,
        "password": login_password,
        "notify": env_bool("CAIMOGU_NOTIFY", True),
        "notify_only_fail": env_bool("CAIMOGU_NOTIFY_ONLY_FAIL", False),
        "debug": env_bool("CAIMOGU_DEBUG", False),
        "chromium_path": os.getenv("CAIMOGU_CHROMIUM_PATH", ""),
        "proxy": proxy,
        "proxy_source": proxy_source,
    }
    return cfg


def parse_circle_id(circle_url):
    """从板块地址里取出数字板块 ID（如 .../circle/308.html -> 308）；取不到返回空串"""
    m = re.search(r'/circle/(\d+)', circle_url or "")
    return m.group(1) if m else ""


def validate_config(cfg):
    """启动即校验配置，返回错误信息列表（空列表表示通过）。

    早失败优于默默跑到 random.randint / 请求里报错——配置错在青龙日志里一眼可见。
    """
    errors = []
    url = (cfg.get("circle_url") or "").strip()
    if not url.startswith("http"):
        errors.append(f"CAIMOGU_CIRCLE_URL 非法: {url!r}（应为 http(s) 开头的板块地址）")
    elif not parse_circle_id(url):
        errors.append(f"CAIMOGU_CIRCLE_URL 中未找到板块 ID: {url!r}（形如 .../circle/308.html）")

    reply_count = cfg.get("reply_count")
    if not isinstance(reply_count, int) or reply_count < 1:
        errors.append(f"CAIMOGU_REPLY_COUNT 必须为 ≥1 的整数，当前: {reply_count!r}")

    min_delay, max_delay = cfg.get("min_delay"), cfg.get("max_delay")
    if not isinstance(min_delay, int) or min_delay < 0:
        errors.append(f"CAIMOGU_MIN_DELAY 必须为 ≥0 的整数，当前: {min_delay!r}")
    if not isinstance(max_delay, int) or max_delay < 0:
        errors.append(f"CAIMOGU_MAX_DELAY 必须为 ≥0 的整数，当前: {max_delay!r}")
    if isinstance(min_delay, int) and isinstance(max_delay, int) and min_delay > max_delay:
        errors.append(f"CAIMOGU_MIN_DELAY({min_delay}) 不能大于 CAIMOGU_MAX_DELAY({max_delay})")

    if cfg.get("ai_timeout") is not None and not isinstance(cfg.get("ai_timeout"), int):
        errors.append(f"CAIMOGU_AI_TIMEOUT 必须为整数，当前: {cfg.get('ai_timeout')!r}")
    return errors


# 全局配置与调试开关（main 里 load_config 后赋值，供页面操作层读取）
_CONFIG = DEFAULT_CONFIG.copy()
_DEBUG = False

# 页面选择器集中定义（顺序敏感，勿随意调整）
SELECTORS = {
    "post_item":  ".list-container .list .item",
    "post_title": ".title",
    "content": (
        ".post-content", ".content", ".detail-content",
        ".post-body", ".text", ".post-detail-content",
    ),
    "editor": (
        ".ql-editor",
        "#editor .ql-editor",
        ".editor .ql-editor",
        ".comment-editor .ql-editor",
        "[contenteditable='true']",
    ),
    "reply_btn": (
        ".btn-reply-root", ".btn-reply", ".reply-btn",
        'a:has-text("回复")', 'button:has-text("回复")',
    ),
    "submit": (
        ".btn-reply-root",
        'button:has-text("回复")',
        'button:has-text("发表")',
        'button:has-text("提交")',
        ".submit-btn",
        ".btn-publish",
        ".btn-send",
        'input[type="submit"]',
    ),
}

SKIP_PIN_KEYWORDS = ["圈规", "答题系统反馈", "新圈规", "不允许乱转"]

# ============================================================
#  2. 关键词提取与过滤常量（与 Windows 版一致）
# ============================================================

_MEANINGLESS_PREFIXES = [
    '大家来展示一下', '有没有人遇到', '求推荐几款', '请问一下大家',
    '大家来', '有没有人', '求推荐', '请问', '求教', '求助',
    '各位大佬', '大佬们', '各位', '有没有', '谁知道', '今天',
]

_MEANINGLESS_SUFFIXES = [
    '分享一下好运', '分享一下', '的效果吧', '效果吧', '的问题求助',
    '求助', '分享', '效果', '吧', '呢', '啊', '吗', '哦', '哈', '了',
]

_BAD_PARTS = ['一下', '目前', '有没有', '谁知道', '大家来', '展示', '这个游戏', '好玩的单']

# 强禁词：全局过滤，出现即判废
_HARD_BANNED_PARTS = [
    "感谢分享", "支持一下", "学到了", "坐等后续", "确实如此",
    "期待更新", "前排围观", "有道理", "这波可以",
    "说得好", "支持楼主", "码住", "马克", "蹲一个靠谱",
    "666", "顶一下", "水帖", "占楼", "路过", "沙发",
    "学习了", "感谢楼", "谢谢分享", "辛苦了",
]

# 弱检测词：仅在评论开头出现时才过滤，避免误杀正文正常提及
_WEAK_BANNED_PREFIXES = [
    "值得讨论", "内容质量", "参考价值", "信息量", "不错",
    "挺有意思", "有道理",
]

_SKIP_TITLE_PATTERNS = [
    r'^\s*签到\s*$', r'每日签到', r'打卡', r'水帖', r'路过',
    r'顶一下', r'冒个泡', r'有人吗', r'随便聊聊', r'无内容',
]

_DETAIL_STOP_WORDS = [
    "这个", "那个", "什么", "一下", "一个", "不是", "就是", "可以", "感觉",
    "真的", "然后", "还是", "已经", "自己", "大家", "楼主", "帖子", "内容",
    "回复", "签到", "每日", "今天", "有人", "有没有", "为什么", "怎么",
    "分享", "看看", "求助", "推荐", "问题"
]

_DETAIL_PATTERNS = [
    r'多次.{0,3}换导演', r'换导演', r'剧本调整',
    r'特效.{0,8}看不清怪', r'特效.{0,8}看不清',
    r'节奏慢', r'开放世界', r'草元素', r'新地图',
    r'雨天场景', r'反光', r'窗口.{0,4}消失', r'错误提示',
    r'最后一发', r'蓝光',
    r'登录.{0,3}闪退', r'刷图.{0,3}效果', r'抽卡.{0,3}出货',
    r'版本.{0,3}更新', r'画面.{0,3}(绝|糊|卡|舒服|清楚)',
    r'真人.{0,3}电影', r'单机.{0,3}游戏', r'取消', r'延期',
    r'报错', r'卡住', r'掉帧', r'联机', r'存档', r'补丁'
]

# 日常风格模板池：圈规要求「日常晒游戏/相关讨论」，故弱化新闻评论腔，
# 换成玩家在圈子里闲聊的口吻；每类 10 条，降低长期复用率（配合跨天去重）。
# 占位符 {d} = 从标题/正文提取的细节。落地后须落在 15-40 有效字之间。
_REPLY_TEMPLATES = {
    "help": [
        "{d}我上周也撞上过，最后是删了配置文件重进才好",
        "求一个{d}的解决办法，我这边症状一模一样",
        "{d}建议先备份存档再折腾，不然容易越修越崩",
        "我猜{d}跟驱动版本有关系，回退一版试试",
        "{d}这情况我熟，先关掉后台那几个软件再进",
        "问下{d}是每次都这样还是偶尔，偶尔的话先别管",
        "{d}大概率是兼容问题，等官方修比较省心",
        "我之前{d}折腾了两天，结果重装一遍全好了",
        "{d}先把画质降一档跑跑看，说不定就不犯了",
        "别急着重装，{d}先清一下缓存试试，我这么解决的",
    ],
    "regret": [
        "{d}消息一出我整个人都麻了，等了好久",
        "唉{d}，本来还挺期待来着，现在只能等下一作了",
        "{d}这波属实难受，之前画的饼全没了",
        "看到{d}有点意外，不过想想也不算太突然",
        "{d}这也太突然了，一点缓冲都没有，心脏受不了",
        "行吧{d}，那就先把愿望单清了，眼不见心不烦",
        "{d}这种结果说实话挺伤玩家的，投入的感情白瞎了",
        "{d}这事定了就难受，之前还盼着能有转机",
        "{d}这么一来，今年这游戏荒可更难熬了",
        "算了不想了，{d}这事就当提前止损吧",
    ],
    "update": [
        "{d}这次改动看着还行，先更了再说",
        "刚看到{d}，希望别只修表面，底层也顺一顺",
        "{d}我比较关心会不会又带出新问题",
        "等{d}实装了我再回坑，现在先观望",
        "{d}方向是对的，就看优化能不能跟上",
        "先更了{d}，有问题回来这帖子底下汇报",
        "{d}要是能顺便把老毛病修了就好了",
        "感觉{d}这次挺有诚意，不像以前敷衍",
        "{d}我先不更，等你们踩完坑再上车",
        "说真的{d}这块我更在意帧数，别又掉",
    ],
    "recommend": [
        "{d}这方向我熟，回头给你列几个能打的",
        "按{d}这口味，能选的不多但都挺稳",
        "{d}我也在找，找到记得回来吱一声",
        "说到{d}，我第一个想到的还是那几个老面孔",
        "{d}这需求好满足，就是看你接不接受老画面",
        "{d}的话建议先玩小体量的试试水",
        "我玩{d}这类比较杂，看你更看重剧情还是玩法",
        "{d}这个偏好我挺懂，就是玩久了容易腻",
        "帮你留意着{d}，有好货我直接甩给你",
        "{d}先别急着买，等个史低入手更香",
    ],
    "luck": [
        "{d}这运气可以啊，我还在保底挣扎",
        "看到{d}默默关掉了游戏，属实有点酸",
        "{d}这波属实太欧了，建议去买张彩票",
        "我{d}从来没这么顺过，非酋实名羡慕",
        "{d}差一点就反转，心都提到嗓子眼了",
        "这种{d}截图最容易劝人上头，下次我也冲",
        "{d}多少发出的，参考一下我攒不攒",
        "看{d}就知道你运气逆天，分我一点",
        "{d}我抽了半年都没见过，真是人比人",
        "恭喜{d}，不过下个池子可要小心了哈哈",
    ],
    "media": [
        "{d}这块改编难度不小，别只靠阵容撑",
        "说到{d}，我更在意剧情能不能立住",
        "{d}我觉得选角比特效重要，希望别翻车",
        "{d}这种题材拍好了是神作，拍砸就灾难",
        "我担心{d}会不会为了大众化把味道改没了",
        "{d}先观望，预告好看不代表正片行",
        "原著粉盯{d}盯得紧，改一点都要吵",
        "{d}这方向我挺期待的，就是千万别烂尾",
        "看{d}的压力挺大，改编这活儿吃力不讨好",
        "{d}要是能拍出原作那股劲就值了",
    ],
    "rumor": [
        "{d}先别急着信，这种料翻车的还少吗",
        "{d}看看就好，等官方发话再下结论",
        "{d}要是真的那可太炸了，但我不敢信",
        "据说的东西我一般当没有，{d}等实锤",
        "{d}这爆料来源模糊，八成是蹭热度",
        "先别激动，{d}这种消息我见得太多了",
        "{d}要是坐实了影响不小，先按传闻看",
        "{d}我觉得可能性一般般，先等等看吧",
        "单看{d}这料可信度不高，让子弹飞会",
        "{d}我先记下来，要是真了我回来打脸",
    ],
    "sales": [
        "{d}这成绩不算意外，前期宣发确实到位",
        "{d}能卖成这样说明质量过关，别家该学学",
        "看{d}的数据，这类型还是有市场的",
        "{d}亮眼归亮眼，长线运营才是真考验",
        "{d}这波玩家用脚投票了，口碑比营销管用",
        "{d}后续能不能稳住才是关键，别一波流",
        "说实话{d}比我预期好，看来口碑发酵了",
        "{d}这数据一出来，跟风的可就多了",
        "{d}成绩不错，但别高兴太早，后劲才难",
        "看到{d}我也想去补一份，凑个热闹",
    ],
    "normal": [
        "{d}这块我挺在意的，做扎实了体验会好不少",
        "说到{d}我也有同感，确实是个容易忽略的点",
        "{d}这角度挺新鲜，之前真没往这想",
        "{d}这个我先码一下，回头有空再细看",
        "我比较好奇{d}后面会怎么处理，先蹲",
        "{d}细想影响还挺大，不只是表面那点",
        "看到{d}我也想说两句，就是不知道从哪聊起",
        "{d}这话题有意思，评论区能聊很久",
        "{d}我先持观望态度，等更多信息再说",
        "{d}倒是提醒我了，回头我也去试试",
    ],
}

# 同义词库：仅替换描述词，不替换功能词（应该/还是/建议等会导致语义变怪）
_SYNONYMS = {
    "看着": ["感觉", "瞧着"],
    "确实": ["真的", "属实"],
    "挺": ["蛮", "相当"],
    "有点": ["略微", "稍许"],
    "不过": ["但是", "然而"],
    "其实": ["说到底", "老实说"],
    "觉得": ["感觉", "认为"],
    "不错": ["还行", "可以"],
    "挺可惜": ["蛮遗憾", "挺遗憾"],
    "说实话": ["老实讲", "说真的"],
    "属实离谱": ["太夸张", "太离谱"],
}

# ============================================================
#  3. JSON 工具函数
# ============================================================

def load_json(path, default):
    """读取 JSON 文件，出错时返回 default"""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError):
        return default


def save_json(path, data):
    """原子写入 JSON：先写临时文件再替换，避免中途崩溃损坏状态文件"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# ============================================================
#  4. 日志
# ============================================================

def setup_logging():
    """配置日志（输出到控制台 + 脚本同目录文件，UTF-8）"""
    logger = logging.getLogger("caimogu_qinglong")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        fh = logging.FileHandler(str(PATHS["log"]), encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger

# ============================================================
#  5. 防重复执行记录
# ============================================================

def get_today_reply_count():
    """获取今天已成功回复的数量"""
    data = load_json(PATHS["replied"], {})
    today = date.today().isoformat()
    if data.get("last_run_date") == today:
        return int(data.get("last_run_posts", 0) or 0)
    return 0


def get_today_replied_ids():
    """获取今天已回复的帖子ID列表，防止中断后重复回复"""
    data = load_json(PATHS["replied"], {})
    today = date.today().isoformat()
    if data.get("last_run_date") == today:
        return data.get("today_post_ids", [])
    return []


def mark_today_progress(post_count, reply_count, post_id=None):
    """记录进度，避免中断后重复回复；同时保存帖子ID"""
    data = load_json(PATHS["replied"], {})
    today = date.today().isoformat()
    same_day = data.get("last_run_date") == today
    data["last_run_date"] = today
    data["last_run_posts"] = post_count
    data["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["status"] = "running" if post_count < reply_count else "done"

    if post_id:
        # 跨天要清空，否则历史帖 ID 会累积，导致误跳过当天其实没回过的帖
        ids = data.get("today_post_ids", []) if same_day else []
        if post_id not in ids:
            ids.append(post_id)
        data["today_post_ids"] = ids

    save_json(PATHS["replied"], data)


def mark_done_today(post_count):
    """标记今天签到完成"""
    data = load_json(PATHS["replied"], {})
    data["last_run_date"] = date.today().isoformat()
    data["last_run_posts"] = post_count
    data["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["status"] = "done"
    save_json(PATHS["replied"], data)


def load_recent_comments(days=30, limit=100):
    """读取最近 N 天用过的评论文本（跨天去重用），按时间保留、截断到 limit 条"""
    data = load_json(PATHS["replied"], {})
    cutoff = date.today().toordinal() - days
    out = []
    for item in data.get("recent_comments", []):
        try:
            if date.fromisoformat(item.get("date", "")).toordinal() >= cutoff:
                text = item.get("text", "")
                if text:
                    out.append(text)
        except (ValueError, TypeError, AttributeError):
            continue
    return out[-limit:]


def save_recent_comment(text, days=30, limit=100):
    """记录一条成功发出的评论，供后续跨天去重（与进度同存于 replied_posts.json）"""
    if not text:
        return
    data = load_json(PATHS["replied"], {})
    items = data.get("recent_comments", [])
    items.append({"text": text, "date": date.today().isoformat()})
    cutoff = date.today().toordinal() - days
    kept = []
    for item in items:
        try:
            if date.fromisoformat(item.get("date", "")).toordinal() >= cutoff:
                kept.append(item)
        except (ValueError, TypeError, AttributeError):
            continue
    data["recent_comments"] = kept[-limit:]
    save_json(PATHS["replied"], data)

# ============================================================
#  6. 评论生成（纯逻辑，不涉及页面操作）
# ============================================================

def _comment_len(text):
    """计算评论有效字数"""
    return len(re.sub(r'[^一-龥a-zA-Z0-9]', '', text))


def _normalize_comment(text):
    """清理评论，避免过度模板化和超长"""
    text = re.sub(r'\s+', '', text)
    text = text.strip('，。！？!?、；; ')
    return text


def detect_title_type(title):
    """粗略判断帖子类型，用于生成更贴合标题的回复。

    判定顺序敏感：传闻类（未实锤）必须排在结果类之前，否则
    「爆料：XX 可能延期」会先被「延期」命中而被当成已确认的坏消息去惋惜。
    """
    # 1) 明确在问人/求助的帖子：优先于传闻与结果类，避免「求助：疑似驱动问题闪退」被判成传闻
    if re.search(r'求助|求教|请教|请问|急求|求解|有没有人', title):
        return "help"
    # 2) 传闻/爆料：尚未实锤，优先于结果类
    if re.search(r'爆料|传闻|泄露|消息人士|据说|内部消息|疑似|可能延期|或将|有望|恐将|被曝', title):
        return "rumor"
    # 3) 已确认的坏消息
    if re.search(r'取消|砍|延期|跳票|停服|下架|暴死|失败|崩|凉', title):
        return "regret"
    # 4) 求助/故障：含常见故障词，正文里的抱怨也据此归到 help，避免把 bug 当亮点夸
    if re.search(r'怎么办|怎么回事|为啥|为什么|闪退|报错|问题|卡住'
                 r'|看不清|卡顿|掉帧|黑屏|花屏|卡死|打不开|进不去|登录不上', title):
        return "help"
    # 5) 销量/成绩
    if re.search(r'销量|销售额|突破|万套|百万|销量榜|成绩|首周|月销|出货量', title):
        return "sales"
    # 6) 更新/版本
    if re.search(r'更新|版本|补丁|改动|上线|发布|公布|官宣|新增', title):
        return "update"
    # 7) 推荐/安利
    if re.search(r'推荐|安利|好玩|入坑|值得买吗|买不买', title):
        return "recommend"
    # 8) 抽卡/运气
    if re.search(r'抽卡|出货|晒|欧|非|运气|掉落', title):
        return "luck"
    # 9) 影视/动漫
    if re.search(r'电影|剧|漫威|动画|漫画|主创|演员', title):
        return "media"
    return "normal"


# 常见游戏/作品名词库：优先匹配，避免硬截前4字产生无意义关键词
_KNOWN_GAME_NAMES = [
    "黑神话悟空", "黑神话", "原神", "崩坏星穹铁道", "星穹铁道", "崩坏",
    "艾尔登法环", "老头环", "刺客信条", "GTA", "侠盗猎车手",
    "塞尔达", "王国之泪", "旷野之息", "最终幻想", "勇者斗恶龙",
    "怪物猎人", "怪猎", "荒野大镖客", "使命召唤", "战神",
    "对马岛之魂", "赛博朋克", "巫师", "霍格沃茨", "帕鲁",
    "幻兽帕鲁", "绝区零", "鸣潮", "明日方舟", "王者荣耀",
    "和平精英", "永劫无间", "双人成行", "糖豆人", "光遇",
    "原神", "崩坏三", "崩坏3", "蔚蓝档案", "妮姬",
    "胜利女神", "无主之地", "生化危机", "寂静岭", "龙之信条",
    "死亡搁浅", "往日不再", "地平线", "极限竞速",
    "刀锋战士", "超人", "蝙蝠侠", "蜘蛛侠", "复仇者联盟",
    "明日之子", "沙丘", "三体",
]


def extract_keyword(title):
    """从帖子标题中提取关键词，优先匹配游戏名/专有名词"""
    title = re.sub(r'^【.*?】\s*', '', title)
    title = re.sub(r'^\[.*?\]\s*', '', title)

    # 优先级1：书名号《》内的内容（通常是游戏名或作品名）
    match = re.search(r'《(.+?)》', title)
    if match and len(match.group(1)) >= 2:
        return match.group(1)[:6]

    # 优先级1.5：引号内的内容
    match = re.search(r'[“”"「」『』](.+?)[“”"「」『』]', title)
    if match and len(match.group(1)) >= 2:
        return match.group(1)[:6]

    # 优先级2：已知游戏/作品名词库
    for name in _KNOWN_GAME_NAMES:
        if name in title:
            return name

    # 优先级3：已知的复合关键词模式
    keyword_patterns = [
        r'单机游戏', r'登录闪退', r'闪退问题', r'游戏画面', r'刷图效果',
        r'版本更新', r'更新内容', r'抽卡出货', r'真人电影', r'档期原因',
        r'主创澄清', r'合约已签', r'突然砍剧', r'推荐.*游戏',
    ]
    for pattern in keyword_patterns:
        match = re.search(pattern, title)
        if match:
            found = match.group(0).replace('推荐几款', '').replace('推荐', '')
            if 2 <= len(found) <= 6:
                return found

    # 优先级4：清理后提取，取较长的中文连续片段
    clean = re.sub(r'[^一-龥a-zA-Z0-9]', '', title)

    for prefix in _MEANINGLESS_PREFIXES:
        if clean.startswith(prefix) and len(clean) > len(prefix) + 2:
            clean = clean[len(prefix):]
            break

    for suffix in _MEANINGLESS_SUFFIXES:
        if clean.endswith(suffix) and len(clean) > len(suffix) + 2:
            clean = clean[:-len(suffix)]
            break

    # 从清理后的文本中提取2-6字的中文片段，取最长的
    segments = re.findall(r'[一-龥]{2,6}', clean)
    if segments:
        best = max(segments, key=len)
        if 2 <= len(best) <= 6:
            return best

    # 兜底：如果以上都没匹配到，取前2-4字
    if len(clean) >= 2:
        return clean[:min(4, len(clean))]
    return ""


def _strip_html_and_noise(text):
    """清理正文噪音，保留适合判断和回复的文本"""
    text = text or ""
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'\[[^\]]{1,20}\]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _meaningful_text_len(text):
    """统计中文、字母和数字的有效长度"""
    return len(re.sub(r'[^一-龥a-zA-Z0-9]', '', text or ""))


def _is_generic_or_empty(title, content):
    """判断帖子是否太空泛"""
    title = title or ""
    content = _strip_html_and_noise(content)
    compact_title = re.sub(r'\s+', '', title)
    compact_content = re.sub(r'\s+', '', content)

    if any(re.search(pattern, compact_title) for pattern in _SKIP_TITLE_PATTERNS):
        if _meaningful_text_len(compact_content) < 30:
            return True

    if _meaningful_text_len(compact_title + compact_content) < 10:
        return True

    generic_phrases = ["如题", "RT", "rt", "看看", "分享一下", "随便说说", "占个楼"]
    if compact_content in generic_phrases:
        return True

    if not content and not any(re.search(p, compact_title) for p in _DETAIL_PATTERNS):
        keyword = extract_keyword(title)
        if not keyword or len(keyword) < 2:
            return True

    return False


def judge_replyability(title, content):
    """判断是否有明确可回应点，返回 REPLY 或 SKIP"""
    title = title or ""
    content = _strip_html_and_noise(content)
    combined = title + " " + content

    if _is_generic_or_empty(title, content):
        return "SKIP"

    if any(bad in combined for bad in ["灌水", "纯水", "无意义", "占楼"]):
        return "SKIP"

    signal_patterns = [
        r'取消|延期|下架|停服|砍了|跳票',
        r'求助|请问|闪退|报错|卡住|失败|问题|怎么|为什么',
        r'爆料|传闻|泄露|消息人士|据说|内部消息|疑似',
        r'销量|突破|万套|百万|销量榜|成绩|首周|出货量',
        r'更新|版本|补丁|改动|新增|上线|发布',
        r'推荐|安利|入坑|好玩|单机|联机',
        r'抽卡|出货|掉落|运气|晒',
        r'电影|动画|漫画|真人|演员|主创',
        r'画面|刷图|存档|掉帧|优化|手感|剧情|玩法',
    ]
    if any(re.search(pattern, combined) for pattern in signal_patterns):
        return "REPLY"

    if _meaningful_text_len(content) >= 30:
        return "REPLY"

    return "SKIP"


# 细节片段当主语要读着顺，故排除：中英数混排（如「已突破1000万」）、
# 动词/副词/连词开头、残缺结尾（如「内部开发进度不及」）
_DETAIL_BAD_LEAD = set("已将把被也就都还又很真太好是有没能会要想让给对从向跟和与及而但却则才只仅再其此该本那名动据可看使")
_DETAIL_BAD_TAIL = ("不及", "没有", "不能", "不会", "不到", "不了", "的话", "之后",
                    "之前", "以来", "还是", "就是", "但是", "不过", "而且", "以及", "同时")


def _detail_usable(chunk):
    """判断候选细节是否适合直接当评论主语"""
    if not chunk or len(chunk) < 2:
        return False
    has_digit = bool(re.search(r'\d', chunk))
    has_cjk = bool(re.search(r'[一-龥]', chunk))
    if has_digit and has_cjk:            # 中英数混排（「已突破1000万」）当主语别扭
        return False
    if chunk[0] in _DETAIL_BAD_LEAD:     # 动词/副词/连词开头，句子不完整
        return False
    if chunk.endswith(_DETAIL_BAD_TAIL):  # 残缺结尾
        return False
    return True


def _extract_detail(title, content):
    """提取一个确实出现在标题或正文里的细节"""
    title = title or ""
    content = _strip_html_and_noise(content)

    def clean_detail(raw):
        detail = re.sub(r'[^一-龥A-Za-z0-9]', '', raw or "")
        if "特效" in detail and "看不清" in detail:
            return "特效看不清怪" if "怪" in detail else "特效看不清"
        if "窗口" in detail and "消失" in detail:
            return "窗口直接消失"
        if "多次" in detail and "换导演" in detail:
            return "多次换导演"
        return detail[:10]

    compact_content = re.sub(r'\s+', '', content)
    for pattern in _DETAIL_PATTERNS:
        m = re.search(pattern, compact_content)
        if m:
            return clean_detail(m.group(0))

    sentences = re.split(r'[。！？!?；;\n\r]+', content)
    chunks = []
    for sentence in sentences:
        sentence = re.sub(r'\s+', '', sentence)
        if 4 <= _meaningful_text_len(sentence) <= 40:
            chunks.extend(re.findall(r'[一-龥A-Za-z0-9]{2,8}', sentence))

    scored = []
    for chunk in chunks:
        if any(stop in chunk for stop in _DETAIL_STOP_WORDS):
            continue
        if any(bad in chunk for bad in _HARD_BANNED_PARTS):
            continue
        if not _detail_usable(chunk):
            continue
        score = len(chunk)
        if content and chunk in content:
            score += 3
        if chunk in title:
            score += 1
        scored.append((score, chunk))

    if scored:
        scored.sort(reverse=True)
        return scored[0][1][:8]

    compact_title = re.sub(r'\s+', '', title)
    for pattern in _DETAIL_PATTERNS:
        m = re.search(pattern, compact_title)
        if m:
            return clean_detail(m.group(0))

    chunks = re.findall(r'[一-龥A-Za-z0-9]{2,8}', title)
    for chunk in chunks:
        if any(stop in chunk for stop in _DETAIL_STOP_WORDS):
            continue
        if not _detail_usable(chunk):
            continue
        return chunk[:8]

    keyword = extract_keyword(title)
    return keyword[:8] if keyword else ""


def _normalize_generated_comment(text):
    """清理模型或模板生成的回复"""
    text = (text or "").strip()
    text = text.strip('"‘’“”\'')
    text = re.sub(r'^(REPLY|回复|评论)[:：\s-]*', '', text, flags=re.I)
    text = re.sub(r'\s+', '', text)
    text = text.strip('，。！？!?、；; ')
    return text


def _is_reply_valid(comment, title="", content=""):
    """检查回复是否符合规则"""
    comment = _normalize_generated_comment(comment)
    if not comment or comment.upper() == "SKIP":
        return False
    # 强禁词：全局过滤
    if any(part in comment for part in _HARD_BANNED_PARTS):
        return False
    # 弱检测词：仅在开头出现时过滤，避免误杀正文正常提及
    if any(comment.startswith(prefix) for prefix in _WEAK_BANNED_PREFIXES):
        return False
    length = _comment_len(comment)
    if not (15 <= length <= 40):
        return False
    compact_title = re.sub(r'\s+', '', title or "")
    if compact_title and comment == compact_title:
        return False
    if "我也" in comment and not re.search(r'求助|请问|有没有|问题|推荐', title or ""):
        return False
    return True


def _apply_synonyms(text):
    """随机替换同义词，增加回复多样性"""
    for word, subs in _SYNONYMS.items():
        if word in text and random.random() < 0.4:
            text = text.replace(word, random.choice(subs), 1)
    return text


def _comment_key(text):
    """评论指纹：去掉标点与空白后的正文"""
    return re.sub(r'[^一-龥A-Za-z0-9]', '', text or "")


def _ngrams(text, n=4):
    key = _comment_key(text)
    return {key[i:i + n] for i in range(max(0, len(key) - n + 1))}


def _comment_similarity(a, b):
    """两条评论的 4-gram Jaccard 相似度，用于判断是否过于雷同"""
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _is_duplicate_comment(comment, recent, threshold=0.6):
    """判断评论是否与最近用过的某条雷同（完全相同或相似度超阈值）"""
    key = _comment_key(comment)
    if not key:
        return False
    for old in recent:
        if key == _comment_key(old):
            return True
        if _comment_similarity(comment, old) >= threshold:
            return True
    return False


def _pick_fresh(candidates, recent):
    """从候选中优先挑没和最近评论撞车的；若全撞车，退回与最近最不相似的一条"""
    if not candidates:
        return None
    if not recent:
        return random.choice(candidates)
    fresh = [c for c in candidates if not _is_duplicate_comment(c, recent)]
    if fresh:
        return random.choice(fresh)
    return min(candidates, key=lambda c: max((_comment_similarity(c, o) for o in recent), default=0.0))


def generate_comment_template(title, content="", recent=None):
    """模板模式：先判断 REPLY/SKIP，再生成短回复（避开最近用过的评论）"""
    decision = judge_replyability(title, content)
    if decision == "SKIP":
        return "SKIP"

    detail = _extract_detail(title, content)
    if not detail:
        return "SKIP"

    if recent is None:
        recent = load_recent_comments()

    keyword = extract_keyword(title) or ""
    title_type = detect_title_type((title or "") + " " + _strip_html_and_noise(content or "")[:120])
    templates = _REPLY_TEMPLATES.get(title_type, _REPLY_TEMPLATES["normal"])

    # 打乱模板顺序，填充插槽并应用同义词随机化
    candidates = []
    for tpl in random.sample(templates, len(templates)):
        filled = tpl.replace("{d}", detail).replace("{kw}", keyword)
        filled = _apply_synonyms(filled)
        candidates.append(filled)

    valid = [c for c in candidates if _is_reply_valid(c, title, content)]
    picked = _pick_fresh(valid, recent)
    return picked if picked else "SKIP"


def _call_deepseek_api(url, headers, data, logger, timeout):
    """发起一次 API 请求，返回 (raw_content, returned_model)。
    timeout 为读取超时秒数；连接超时固定 10s，网关不可达时秒级失败。"""
    resp = requests.post(url, headers=headers, json=data, timeout=(10, timeout))
    resp.raise_for_status()
    result = resp.json()
    returned_model = result.get("model", "未知")
    logger.info("AI 返回模型: %s", returned_model)
    raw_content = (result["choices"][0]["message"]["content"] or "").strip()
    return raw_content, returned_model


def _ai_retry_delay(attempt):
    """重试退避间隔：第 1 次失败后 3s，第 2 次失败后 6s"""
    return 3 * attempt


def generate_comment_ai(title, content, api_key, base_url, model, recent=None):
    """AI 模式：让 AI 直接生成评论或 SKIP，含空返回重试和429重试"""
    logger = logging.getLogger("caimogu_qinglong")
    if recent is None:
        recent = load_recent_comments()
    try:
        base_url = (base_url or "https://api.deepseek.com/v1").rstrip("/")
        model = model or "deepseek-chat"
        url = base_url + "/chat/completions"
        headers = {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json"
        }

        title_clean = (title or "").strip()
        if len(title_clean) > 200:
            title_clean = title_clean[:200]
            logger.info("标题过长(>%d字)，已截断", len(title or ""))

        content_summary = _strip_html_and_noise(content)[:700] if content else ""
        prompt = (
            "你是采蘑菇游戏论坛（游戏圈）的普通玩家，刷圈子时看到这个帖子，随手回一句。\n\n"
            "要求：\n"
            "- 像在游戏群里跟人闲聊，口语、随意，可以带「哈哈」「唉」「啊」「吧」这类语气词\n"
            "- 结合帖子里一个具体细节说一句自己的看法或感受，不要复述标题、不要总结内容\n"
            "- 站在玩家角度，可以吐槽、附和、提问、补充，但不要像在给新闻写评论\n"
            "- 不要假装亲身经历过\n"
            "- 严禁任何套话：感谢分享、支持一下、学到了、坐等后续、确实如此、期待更新、前排围观、"
            "有道理、这波可以、说得好、支持楼主、码住、马克\n"
            "- 长度 15 到 40 个字，绝对不要超过 40 个字\n"
            "- 直接输出回复正文，不要任何前缀、编号、引号、解释\n\n"
            "如果帖子太水或没法自然接话，只回复两个字母：SKIP\n"
        )
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "标题：" + title_clean + "\n正文：" + content_summary}
            ],
            "max_tokens": 400,
            "temperature": 0.9
        }
        # 空返回/太短时重试用的缩短输入（实测缩短输入能提高推理模型成功率）
        retry_data = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "标题：" + title_clean[:80] + "\n正文：" + content_summary[:300]}
            ],
            "max_tokens": 400,
            "temperature": 0.9
        }

        logger.info("AI 请求: base_url=%s, model=%s", base_url, model)
        timeout = _CONFIG.get("ai_timeout", 120)
        total_attempts = 3  # 首试 + 2 次重试
        raw_content = ""
        use_short = False   # 空返回后改用缩短输入重试
        for attempt in range(1, total_attempts + 1):
            req_data = retry_data if use_short else data
            try:
                raw_content, _ = _call_deepseek_api(url, headers, req_data, logger, timeout)
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status != 429 and status < 500:
                    raise  # 参数/key 等 4xx 错误，重试无意义
                logger.warning("第 %d/%d 次 HTTP %s", attempt, total_attempts, status)
                if attempt < total_attempts:
                    time.sleep(_ai_retry_delay(attempt))
                else:
                    raw_content = ""
                continue
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning("第 %d/%d 次网络错误: %s", attempt, total_attempts, e)
                if attempt < total_attempts:
                    time.sleep(_ai_retry_delay(attempt))
                else:
                    raw_content = ""
                continue

            logger.info("AI 第 %d/%d 次返回: %s", attempt, total_attempts, raw_content)
            if raw_content.strip():
                break
            # 返回空：标记缩短输入并进入下一轮重试
            logger.warning("第 %d/%d 次返回空", attempt, total_attempts)
            if attempt < total_attempts:
                use_short = True
                time.sleep(_ai_retry_delay(attempt))
            else:
                raw_content = ""

        if not raw_content.strip():
            logger.warning("AI %d 次尝试均未返回内容，回退模板", total_attempts)
            return generate_comment_template(title, content, recent=recent)

        comment = _normalize_generated_comment(raw_content)
        logger.info("AI 清洗后: %s (字数=%d)", comment, _comment_len(comment))

        if comment.upper() == "SKIP":
            return "SKIP"

        if _comment_len(comment) < 5:
            logger.warning("AI 回复太短，回退模板: %s", comment)
            return generate_comment_template(title, content, recent=recent)

        if _comment_len(comment) > 40:
            logger.warning("AI 回复超过 40 字，回退模板: %s", comment)
            return generate_comment_template(title, content, recent=recent)

        if any(part in comment for part in _HARD_BANNED_PARTS):
            logger.warning("AI 回复含套话，回退模板: %s", comment)
            return generate_comment_template(title, content, recent=recent)

        if _is_duplicate_comment(comment, recent):
            logger.warning("AI 回复与最近评论雷同，回退模板: %s", comment)
            return generate_comment_template(title, content, recent=recent)

        return comment
    except Exception as e:
        logging.getLogger("caimogu_qinglong").warning("AI生成评论失败，回退到模板模式: %s", e)
        return generate_comment_template(title, content, recent=recent)


def generate_comment(title, content, config):
    """根据配置选择 AI 模式或模板模式生成评论；可能返回 SKIP"""
    recent = load_recent_comments()
    api_key = config.get("deepseek_api_key", "")
    if api_key:
        base_url = config.get("deepseek_base_url", "https://api.deepseek.com/v1")
        model = config.get("deepseek_model", "deepseek-chat")
        return generate_comment_ai(title, content, api_key, base_url, model, recent=recent)
    return generate_comment_template(title, content, recent=recent)

# ============================================================
#  7. Playwright 工具函数
# ============================================================

def _ensure_browser_deps():
    """浏览器路线依赖检测（青龙容器缺 chromium 时给出安装指引）"""
    missing = []
    try:
        import playwright  # noqa: F401
    except ImportError:
        missing.append("playwright")
    if missing:
        print("❌ 浏览器路线缺少依赖：", " ".join(missing))
        print("   青龙面板「依赖管理」-「Python」安装：playwright")
        print("   并在容器内执行：apt update && apt install -y chromium libxss1 libnss3 libgbm1 libasound2")
        sys.exit(1)


def _find_chromium_path():
    """自动搜索系统 chromium 路径；未找到返回 None 交给 playwright 自带"""
    path = os.getenv("CAIMOGU_CHROMIUM_PATH", "").strip()
    if path:
        if os.path.isfile(path):
            return path
        print(f"⚠️ CAIMOGU_CHROMIUM_PATH 指定文件不存在: {path}，改自动搜索")
        path = ""

    candidates = [
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/snap/bin/chromium", "/usr/bin/chromium-headless-shell",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p

    import shutil
    for name in ("chromium", "chromium-browser", "google-chrome", "chromium-headless-shell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def first_element(page, selectors, *, wait=False, timeout=5000):
    """按顺序查找第一个存在的元素，返回 (element, selector) 或 (None, None)"""
    for selector in selectors:
        try:
            if wait:
                element = page.wait_for_selector(selector, timeout=timeout)
            else:
                element = page.query_selector(selector)
            if element:
                return element, selector
        except Exception:
            continue
    return None, None


def get_text(page, selectors, limit=500):
    """从多个选择器中提取第一个匹配元素的文本"""
    element, _ = first_element(page, selectors)
    if not element:
        return ""
    try:
        return element.inner_text()[:limit].strip()
    except Exception:
        return ""


def create_browser_context(p, cookies=None, storage_state=None):
    """启动 chromium 并创建上下文，返回 (browser, context)。
    cookies: dict，走 add_cookies；storage_state: 文件路径，走 new_context。"""
    launch_kwargs = {
        "headless": _CONFIG.get("headless", True),
        "args": [
            "--no-sandbox",                      # 容器内必加
            "--disable-dev-shm-usage",           # 容器 /dev/shm 太小会崩
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
        ],
    }
    if _CONFIG.get("proxy"):
        launch_kwargs["proxy"] = {"server": _CONFIG["proxy"]}
    chromium_path = _find_chromium_path()
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    try:
        browser = p.chromium.launch(**launch_kwargs)
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            print("❌ 未找到 chromium 浏览器，容器内请执行：")
            print("   apt update && apt install -y chromium libxss1 libnss3 libgbm1 libasound2")
            print("   或设置 CAIMOGU_CHROMIUM_PATH 指向浏览器绝对路径")
        raise

    context_kwargs = {
        "viewport": {"width": 1280, "height": 800},
        "user_agent": USER_AGENT,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
    }
    if storage_state:
        context_kwargs["storage_state"] = storage_state
    context = browser.new_context(**context_kwargs)

    # 反检测：抹平基础自动化指纹（webdriver / plugins / chrome / WebGL vendor）
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                {name: 'PDF Viewer'}, {name: 'Chrome PDF Viewer'},
                {name: 'Chromium PDF Viewer'}, {name: 'WebKit built-in PDF'},
            ],
        });
        if (!window.chrome) { window.chrome = {runtime: {}}; }
        try {
            const _getParam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function (p) {
                if (p === 37445) { return 'Intel Inc.'; }
                if (p === 37446) { return 'Intel Iris OpenGL Engine'; }
                return _getParam.call(this, p);
            };
        } catch (e) {}
        """
    )

    if cookies:
        context.add_cookies([
            {"name": k, "value": v, "domain": ".caimogu.cc", "path": "/"}
            for k, v in cookies.items()
        ])
    return browser, context


def _load_auth(config, logger):
    """加载登录凭证。Cookie 字符串优先，auth_state.json 文件回退。
    返回 (cookies_dict, storage_state_path)，两者都无则返回 (None, None)。"""
    cookies = parse_cookie_str(config.get("cookie", "")) if config.get("cookie") else {}
    if cookies:
        logger.info("使用环境变量 CAIMOGU_COOKIE（%d 个 cookie）", len(cookies))
        return cookies, None

    auth_file = config.get("auth_file", "")
    if auth_file and os.path.isfile(auth_file):
        logger.info("使用登录状态文件: %s", auth_file)
        return None, auth_file

    return None, None

# ============================================================
#  8. 页面操作
# ============================================================

def _debug_shot(page, tag):
    """DEBUG 模式截屏存到脚本同目录，方便排查页面结构变化"""
    if not _DEBUG:
        return
    try:
        path = SCRIPT_DIR / f"_caimogu_debug_{tag}_{int(time.time())}.png"
        page.screenshot(path=str(path))
        print(f"🔍 调试截屏: {path}")
    except Exception:
        pass


def _post_list_api(circle_id, cookie_str, logger, timeout=20):
    """调用 /circle/act/post_list 取板块帖子（免登录，返回精确的 is_top/is_lock/tags）。

    返回 (posts, error)。posts 元素形如
    {"id","url","title","content","tags","is_top","is_boutique","is_lock"}。
    """
    headers = {
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{CAIMOGU_BASE}/circle/{circle_id}.html",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str
    try:
        resp = requests.get(
            CAIMOGU_BASE + POST_LIST_API_PATH,
            params={"id": circle_id, "kwType": "all", "kw": "", "page": 1},
            headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [], f"接口请求失败: {e}"

    if data.get("status") != 1:
        return [], f"接口 status={data.get('status')} info={data.get('info')}"

    posts = []
    for item in (data.get("data") or {}).get("list") or []:
        pid = item.get("id")
        title = (item.get("title") or "").strip()
        if not pid or not title:
            continue
        posts.append({
            "id": str(pid),
            "url": f"{CAIMOGU_BASE}/post/{pid}.html",
            "title": title,
            "content": item.get("content") or "",
            "tags": [t.get("name") for t in (item.get("tags") or []) if t.get("name")],
            "is_top": item.get("is_top"),
            "is_boutique": item.get("is_boutique"),
            "is_lock": item.get("is_lock"),
        })
    return posts, ""


def get_post_list(page, config, count, logger):
    """获取候选帖子：优先走接口（字段精确），失败回退 DOM 解析。"""
    circle_url = config["circle_url"]
    circle_id = parse_circle_id(circle_url)
    max_candidates = max(count * 8, count + 10)

    posts = []
    if circle_id:
        posts, err = _post_list_api(circle_id, config.get("cookie", ""), logger)
        if err:
            logger.warning("帖子列表接口不可用（%s），回退 DOM 解析", err)
            posts = []

    if posts:
        filtered, skipped = [], 0
        for p in posts:
            if p.get("is_top") or p.get("is_lock") or any(kw in p["title"] for kw in SKIP_PIN_KEYWORDS):
                skipped += 1
                logger.info("跳过置顶/锁定/圈规帖: %s", p["title"])
                continue
            filtered.append(p)
            if len(filtered) >= max_candidates:
                break
        logger.info("接口取到 %d 帖，跳过 %d 个置顶/锁定/圈规帖，剩 %d 个候选",
                    len(posts), skipped, len(filtered))
        if filtered:
            return filtered
        logger.warning("接口返回的帖子全被过滤，回退 DOM 解析")

    # 回退：DOM 解析
    logger.info("正在通过页面获取帖子列表: %s", circle_url)
    try:
        page.goto(circle_url, timeout=config.get("page_timeout_ms", 90000),
                  wait_until="domcontentloaded")
    except Exception as e:
        logger.warning("板块页面加载超时，尝试继续解析已加载内容: %s", e)
    page.wait_for_timeout(3000)

    try:
        page.wait_for_selector(SELECTORS["post_item"], timeout=10000)
    except Exception:
        logger.error("帖子列表未加载，可能页面结构有变化")
        return []

    items = page.query_selector_all(SELECTORS["post_item"])
    posts = []
    skipped_pinned = 0

    for item in items[:max_candidates]:
        try:
            title_el = item.query_selector(SELECTORS["post_title"])
            if not title_el:
                continue
            href = title_el.get_attribute("href")
            title = title_el.inner_text().strip()
            if not (href and title):
                continue

            item_class = (item.get_attribute("class") or "").lower()
            item_html = item.inner_html()[:500] if hasattr(item, "inner_html") else ""
            # 用词边界匹配 top，避免 class 含 topic 时被误杀
            is_pinned = (
                "sticky" in item_class
                or "pin" in item_class
                or re.search(r'\btop\b', item_class)
                or "置顶" in item_html
            )
            title_matches_skip = any(kw in title for kw in SKIP_PIN_KEYWORDS)

            if is_pinned or title_matches_skip:
                skipped_pinned += 1
                logger.info("跳过置顶帖: %s", title)
                continue

            if not href.startswith("http"):
                href = "https://www.caimogu.cc" + href
            posts.append({"url": href, "title": title})
            if len(posts) >= max_candidates:
                break
        except Exception:
            continue

    logger.info("跳过 %d 个置顶帖，获取到 %d 个普通帖子", skipped_pinned, len(posts))
    return posts


def extract_post_info(page):
    """从当前帖子页面提取标题和内容"""
    page_title = page.title()
    title = re.sub(r'\s*-\s*.*$', '', page_title).strip()
    content = get_text(page, SELECTORS["content"], limit=500)
    return title, content


def find_editor(page, logger):
    """查找回复编辑器，必要时先点击回复按钮"""
    editor, sel = first_element(page, SELECTORS["editor"], wait=True, timeout=5000)
    if editor:
        logger.info("找到回复编辑器: %s", sel)
        return editor

    # 尝试点击回复按钮后再查找
    btn, _ = first_element(page, SELECTORS["reply_btn"])
    if btn:
        try:
            btn.click()
            page.wait_for_timeout(2000)
        except Exception:
            pass

    editor, sel = first_element(page, SELECTORS["editor"], wait=True, timeout=5000)
    if editor:
        logger.info("找到回复编辑器(点击回复后): %s", sel)
    return editor


def input_comment(page, editor, comment, logger):
    """输入评论：主路径逐字键入（产生真实按键事件），失败再退回 fill / 直接写 DOM"""
    try:
        editor.click()
        page.wait_for_timeout(random.randint(300, 900))          # 像人一样先停一下
        page.keyboard.type(comment, delay=random.randint(45, 130))
        page.wait_for_timeout(random.randint(400, 900))
        typed = page.evaluate(
            '() => { var ed = document.querySelector(".ql-editor"); '
            'return ed ? (ed.innerText || "").trim() : ""; }'
        )
        if typed and _comment_key(typed) == _comment_key(comment):
            logger.info("评论已逐字键入编辑器")
            return True
        logger.warning("逐字键入后编辑器内容不符（得到 %r），尝试其它方式", (typed or "")[:40])
    except Exception as e:
        logger.warning("逐字键入失败: %s", e)

    # 方式二：fill
    try:
        editor.fill(comment)
        page.wait_for_timeout(500)
        logger.info("评论已输入编辑器(fill)")
        return True
    except Exception:
        pass

    # 方式三：直接设置 Quill 编辑器内容
    try:
        safe = comment.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        page.evaluate(
            '(text) => { var ed = document.querySelector(".ql-editor"); '
            'if(ed) { ed.innerHTML = "<p>" + text + "</p>"; '
            'ed.dispatchEvent(new Event("input", {bubbles: true})); } }',
            safe
        )
        page.wait_for_timeout(500)
        logger.info("评论已输入编辑器(evaluate)")
        return True
    except Exception as e:
        logger.error("输入评论失败: %s", e)
        return False


class LoginExpiredError(Exception):
    """登录态失效（接口返回 status=-1001，或前端拦截提交）时抛出，中断整个回帖流程。"""


class WaterPostBlockedError(Exception):
    """被站点判定为水贴（status 887/888/889）时抛出，保守中止本轮并告警。

    站点有「踩蘑菇AI妹妹」水贴识别，且圈规明确「被删帖会扣影响力」，
    故此处不自动重试、不点「继续发布」，直接停下避免把账号刷成负影响力。
    """

    def __init__(self, status, info=""):
        super().__init__(info or f"status={status}")
        self.status = status
        self.info = info


def _read_swal_text(page):
    """读取当前 swal2 弹窗文本（没有则返回空串）"""
    try:
        el = page.query_selector(".swal2-popup")
        return el.inner_text() if el else ""
    except Exception:
        return ""


def _dismiss_swal(page):
    """关闭可能存在的 swal2 弹窗，避免阻塞后续操作"""
    try:
        if page.query_selector(".swal2-popup"):
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
    except Exception:
        pass


def submit_reply(page, logger, timeout_ms=20000):
    """提交回复，并拦截 /post/act/comment 的响应按 status 判定成败。

    返回 {"ok", "status", "info", "raw"}。
    - status=1        → ok=True
    - status=-1001    → 抛 LoginExpiredError
    - 887/888/889     → 抛 WaterPostBlockedError（保守中止）
    - 其他非 1 / 无响应 → ok=False

    前端在未登录时直接弹窗拦截、根本不发请求，故「超时未捕获响应 + 弹窗含登录字样」
    也判为登录失效。
    """
    from playwright.sync_api import TimeoutError as PWTimeoutError

    btn, sel = first_element(page, SELECTORS["submit"])
    if not btn:
        logger.error("未找到提交按钮")
        return {"ok": False, "status": None, "info": "未找到提交按钮", "raw": ""}

    try:
        with page.expect_response(
            lambda r: REPLY_API_PATH in r.url and r.request.method == "POST",
            timeout=timeout_ms,
        ) as resp_info:
            btn.click(timeout=10000)
            logger.info("已点击提交按钮: %s", sel)
        resp = resp_info.value
    except PWTimeoutError:
        swal_text = _read_swal_text(page)
        if "登录" in swal_text:
            _dismiss_swal(page)
            logger.error("提交被「%s」弹窗拦截，登录态已失效",
                         swal_text.replace("\n", " ").strip())
            raise LoginExpiredError(swal_text.strip())
        logger.error("提交后 %dms 内未捕获到回帖接口响应，判定失败", timeout_ms)
        return {"ok": False, "status": None, "info": "未捕获到接口响应", "raw": ""}
    except Exception as e:
        logger.error("点击提交按钮失败: %s", e)
        return {"ok": False, "status": None, "info": str(e), "raw": ""}

    raw, status, info = "", None, ""
    try:
        raw = resp.text()
        data = json.loads(raw)
        status = data.get("status")
        info = str(data.get("info") or "")
    except Exception as e:
        logger.warning("回帖响应解析失败: %s | body=%s", e, raw[:200])
        return {"ok": False, "status": None, "info": "响应解析失败", "raw": raw[:200]}

    logger.info("回帖接口返回: status=%s info=%s", status, info)

    if status == REPLY_STATUS_OK:
        return {"ok": True, "status": status, "info": info, "raw": raw}
    if status == REPLY_STATUS_NOT_LOGIN:
        _dismiss_swal(page)
        raise LoginExpiredError(info or "请先登录")
    if status in (REPLY_STATUS_WATER_SOFT, REPLY_STATUS_WATER_HARD, REPLY_STATUS_NEED_AUDIT):
        _dismiss_swal(page)
        raise WaterPostBlockedError(status, info)
    return {"ok": False, "status": status, "info": info, "raw": raw}


def reply_to_post(page, post_url, config, logger):
    """打开帖子并回复（页面交互层，评论生成委托给 generate_comment）"""
    logger.info("正在打开帖子: %s", post_url)
    try:
        try:
            page.goto(post_url, timeout=config.get("page_timeout_ms", 90000),
                      wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("帖子页面加载超时，尝试继续: %s", e)
        page.wait_for_timeout(3000)

        # 提取帖子信息
        title, content = extract_post_info(page)
        logger.info("帖子标题: %s", title)

        # 生成评论（纯逻辑，不涉及页面操作）
        comment = generate_comment(title, content, config)
        if comment == "SKIP":
            logger.info("判断结果: SKIP，跳过此帖")
            return False
        logger.info("生成评论: %s", comment)

        # 滚动到底部
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

        # 查找编辑器
        editor = find_editor(page, logger)
        if not editor:
            logger.error("未找到回复输入框，跳过此帖子")
            _debug_shot(page, "no_editor")
            return False

        # 未登录时站点把编辑器置为 contenteditable=false，点它只会弹「您还未登录」，
        # 提前识别可省掉一次提交超时，并让上层直接走自动登录逻辑
        editable = page.evaluate(
            '() => { const ed = document.querySelector(".ql-editor");'
            ' return ed ? ed.getAttribute("contenteditable") : null; }'
        )
        if editable == "false":
            _dismiss_swal(page)
            logger.error("回复编辑器不可编辑（contenteditable=false），登录态可能已失效")
            raise LoginExpiredError("编辑器不可编辑，登录态可能已失效")

        # 输入评论
        if not input_comment(page, editor, comment, logger):
            _debug_shot(page, "input_fail")
            return False

        # 提交回复：按接口 status 判定；登录失效/水贴会抛异常向上传播
        result = submit_reply(page, logger)
        if not result["ok"]:
            logger.warning("提交失败: status=%s info=%s", result["status"], result["info"])
            _debug_shot(page, "submit_fail")
            return False

        # 成功：等待前端把评论插入列表，并记录评论文本用于跨天去重
        page.wait_for_timeout(2000)
        save_recent_comment(comment)
        logger.info("回复提交成功（status=1）")
        _debug_shot(page, "reply_ok")
        return True

    except (LoginExpiredError, WaterPostBlockedError):
        # 登录失效 / 被判定水贴：原样向上抛出，由 run_signin 决定中断
        raise
    except Exception as e:
        logger.error("回复帖子时出错: %s", e)
        _debug_shot(page, "exception")
        return False


def check_login_status(page, logger):
    """检查登录状态。

    可靠信号：出现「退出」入口=已登录；出现**可见的**「登录/注册」入口=彻底未登录。
    caimogu 对「token 过期」的渲染与正常登录几乎无差别（回复框照常显示），
    故两者都没有时无法从 DOM 判断，此时放行，最终由提交环节的接口
    status=-1001 判定（见 submit_reply），不会误回帖。
    """
    try:
        page.goto(CAIMOGU_BASE + "/", timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        _VIS = ("const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); "
                "return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }; ")

        # 正向信号：登录后头部必有「退出」入口
        if page.evaluate(
            "() => { " + _VIS +
            "return [...document.querySelectorAll('a,button')].some(e => vis(e) && (e.innerText || '').trim() === '退出'); }"
        ):
            logger.info("检测到「退出」入口，登录状态有效")
            return True

        # 负向信号：可见的「登录/注册」入口（按可见性判断，避免命中隐藏元素误判）
        if page.evaluate(
            "() => { " + _VIS +
            "const nodes = [...document.querySelectorAll('a,button')].filter(vis); "
            "const byHref = nodes.some(e => /login/i.test(e.getAttribute('href') || '')); "
            "const byText = nodes.some(e => ['登录', '注册', '登录/注册'].includes((e.innerText || '').trim())); "
            "return byHref || byText; }"
        ):
            logger.warning("检测到未登录（页面存在可见的登录/注册入口）")
            return False

        logger.info("未检测到明确的登录/未登录标志，继续（最终以提交时接口 status 为准）")
        return True
    except Exception as e:
        logger.warning("检查登录状态时出错: %s", e)
        return True


def auto_login(page, config, logger):
    """用账号密码自动登录 caimogu，登录成功返回 True。

    实测：caimogu 登录面板默认是微信/Apple 快速登录，账号密码表单藏在
    .switch-login（div，「手机号 / 账号 登录」）里，需点击它切换；且该面板在
    headless 下原生 click/fill 时序不稳定，故全程用 JS 注入交互。
    账号密码登录无图形/滑块验证码（.verify-code 的「获取验证码」属短信登录）。
    """
    user, pwd = config.get("user", ""), config.get("password", "")
    if not (user and pwd):
        logger.error("未配置 CAIMOGU_ACCOUNTS（格式：用户名#密码），无法自动登录")
        return False
    try:
        page.goto("https://www.caimogu.cc/login.html", timeout=60000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # 点击 .switch-login 切换到账号密码面板
        page.evaluate("""() => {
            const el = document.querySelector('.switch-login');
            if (el) el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        }""")
        page.wait_for_timeout(1200)

        # 填账号密码（仅操作可见输入框，避开注册/找回等隐藏表单的同名元素）
        filled = page.evaluate("""(a) => {
            const vis = [...document.querySelectorAll('input')]
                .filter(i => i.offsetParent !== null);
            let u = false, p = false;
            const acc = vis.find(i => i.name === 'account');
            const pw = vis.find(i => i.name === 'password');
            if (acc) { acc.value = a.account; acc.dispatchEvent(new Event('input', {bubbles:true})); u = true; }
            if (pw)  { pw.value  = a.password; pw.dispatchEvent(new Event('input', {bubbles:true})); p = true; }
            return {u, p};
        }""", {"account": user, "password": pwd})
        if not (filled.get("u") and filled.get("p")):
            logger.error("自动登录：未找到可见的账号/密码输入框")
            return False
        page.wait_for_timeout(300)

        # 点登录按钮
        page.evaluate("""() => {
            const b = document.querySelector('.btn-login');
            if (b) b.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        }""")
        page.wait_for_timeout(4000)  # 等待登录跳转

        # 验证登录成功：优先看「退出」菜单（登录后必有，最可靠），再看 URL 离开登录页
        ok = page.evaluate("""() => {
            if ([...document.querySelectorAll('a,button')]
                    .some(e => e.innerText && e.innerText.trim() === '退出')) {
                return true;
            }
            return !location.pathname.includes('login')
                && !location.href.includes('/login');
        }""")
        if ok:
            logger.info("自动登录成功")
            return True
        logger.error("自动登录：登录后仍停留在登录页，可能密码错误或账号异常")
        return False
    except Exception as e:
        logger.error("自动登录出错: %s", e)
        return False


# ============================================================
#  9. 回帖主流程
# ============================================================

def run_signin(config, logger):
    """执行自动回帖主流程，返回结果 dict。"""
    logger.info("=" * 50)
    logger.info("采蘑菇论坛自动回帖开始")
    logger.info("时间: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 50)

    reply_count = config.get("reply_count", 3)
    already_count = get_today_reply_count()
    if already_count >= reply_count:
        logger.info("今天已经成功回复 %d 条，已达到目标，自动跳过。", already_count)
        return {"success": True, "done": already_count, "target": reply_count,
                "message": f"今日已回帖 {already_count}/{reply_count}，跳过"}
    remaining_count = reply_count - already_count
    logger.info("今天已记录成功回复 %d 条，本次还需要回复 %d 条。", already_count, remaining_count)

    cookies, storage_state = _load_auth(config, logger)
    has_account = bool(config.get("user") and config.get("password"))
    if not cookies and not storage_state and not has_account:
        msg = "未找到登录凭证！请设置 CAIMOGU_COOKIE 环境变量，或把 auth_state.json 放到脚本同目录，"
        msg += "或配置 CAIMOGU_ACCOUNTS（用户名#密码）以便自动登录"
        logger.error(msg)
        return {"success": False, "done": already_count, "target": reply_count, "message": msg}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        msg = "未安装 Playwright，请先安装依赖"
        logger.error(msg)
        return {"success": False, "done": already_count, "target": reply_count, "message": msg}

    with sync_playwright() as p:
        browser, context = create_browser_context(p, cookies=cookies, storage_state=storage_state)
        try:
            page = context.new_page()

            if not check_login_status(page, logger):
                if has_account:
                    logger.warning("检测到未登录，尝试用账号密码自动登录...")
                    if not auto_login(page, config, logger):
                        msg = "自动登录失败，请检查 CAIMOGU_ACCOUNTS 是否正确，或更新 CAIMOGU_COOKIE"
                        logger.error(msg)
                        return {"success": False, "done": already_count,
                                "target": reply_count, "message": msg}
                    # 登录成功后持久化（供后续直接复用，减少重复登录）
                    try:
                        context.storage_state(path=config.get("auth_file"))
                        logger.info("已持久化登录态到 %s", config.get("auth_file"))
                    except Exception as e:
                        logger.warning("持久化登录态失败: %s", e)
                else:
                    msg = "登录状态已失效，且未配置自动登录账号（CAIMOGU_ACCOUNTS）"
                    logger.error(msg)
                    return {"success": False, "done": already_count,
                            "target": reply_count, "message": msg}

            logger.info("登录状态有效")

            posts = get_post_list(page, config, remaining_count, logger)
            if not posts:
                msg = "未获取到帖子列表，回帖失败"
                logger.error(msg)
                return {"success": False, "done": already_count, "target": reply_count, "message": msg}

            success_count = already_count
            replied_ids = get_today_replied_ids()
            for i, post in enumerate(posts):
                if success_count >= reply_count:
                    break

                # 跳过今天已回复过的帖子（防止中断后重复回复）
                _m = re.search(r'/([0-9]+)[.]html', post["url"])
                post_id = post.get("id") or (_m.group(1) if _m else post["url"])
                if post_id in replied_ids:
                    logger.info("跳过今天已回复的帖子: %s", post["title"])
                    continue

                logger.info("--- 本次第 %d/%d 个帖子，总进度 %d/%d ---",
                            i + 1, remaining_count, success_count, reply_count)
                logger.info("标题: %s", post["title"])

                # 提交时可能触发登录失效：若配了账号密码则自动登录后重试一次
                retried = False
                while True:
                    try:
                        replied = reply_to_post(page, post["url"], config, logger)
                        break
                    except WaterPostBlockedError as e:
                        # 保守策略：一旦被判水贴立即中止本轮，不换帖重试、不点「继续发布」
                        logger.error("站点判定为水贴（status=%s：%s），保守中止本轮回帖", e.status, e.info)
                        return {"success": False, "done": success_count, "target": reply_count,
                                "water_blocked": True, "water_status": e.status,
                                "message": f"被站点判定水贴（status={e.status}），已中止本轮：{e.info}"}
                    except LoginExpiredError as e:
                        if not has_account or retried:
                            # 无账号密码（或已重试过）→ 立即终止整个流程
                            logger.error("登录态失效且无法自动恢复，终止回帖流程")
                            return {"success": False, "done": success_count,
                                    "target": reply_count,
                                    "message": "登录态已失效（token 过期），请更换有效的 CAIMOGU_COOKIE"
                                               " 或配置 CAIMOGU_ACCOUNTS（用户名#密码）"}
                        # 有账号密码：自动登录后重试当前帖子一次
                        logger.warning("提交时发现登录失效（%s），尝试自动登录后重试该帖...", e)
                        retried = True
                        if not auto_login(page, config, logger):
                            return {"success": False, "done": success_count,
                                    "target": reply_count,
                                    "message": "自动登录失败，请检查账号密码或更新 CAIMOGU_COOKIE"}
                        try:
                            context.storage_state(path=config.get("auth_file"))
                            logger.info("已持久化登录态到 %s", config.get("auth_file"))
                        except Exception as se:
                            logger.warning("持久化登录态失败: %s", se)

                if replied:
                    success_count += 1
                    logger.info("回复成功 (%d/%d)", success_count, reply_count)
                    mark_today_progress(success_count, reply_count, post_id)
                    if success_count < reply_count:
                        delay = random.randint(config["min_delay"], config["max_delay"])
                        logger.info("等待 %d 秒...", delay)
                        time.sleep(delay)
                else:
                    logger.warning("回复失败，尝试下一个帖子")
                    time.sleep(3)

            logger.info("=" * 50)
            logger.info("回帖完成！今天累计成功回复 %d/%d 个帖子", success_count, reply_count)
            logger.info("=" * 50)
            if success_count >= reply_count:
                mark_done_today(success_count)

            if success_count > already_count:
                return {"success": True, "done": success_count, "target": reply_count,
                        "message": f"今日成功回帖 {success_count}/{reply_count}"}
            return {"success": False, "done": success_count, "target": reply_count,
                    "message": f"未新增回帖（{success_count}/{reply_count}），可能帖子都不适合回复"}
        except Exception as e:
            logger.error("回帖过程出错: %s", e)
            import traceback
            traceback.print_exc()
            return {"success": False, "done": already_count, "target": reply_count,
                    "message": f"回帖过程异常: {e}"}
        finally:
            context.close()
            browser.close()

# ============================================================
#  10. 命令行入口
# ============================================================

def show_help():
    """显示帮助信息"""
    print("采蘑菇论坛自动回帖（青龙版）")
    print()
    print("用法:")
    print("  python caimogu_checkin.py            执行自动回帖")
    print("  python caimogu_checkin.py --test     测试评论生成效果（不启动浏览器）")
    print("  python caimogu_checkin.py --help     显示帮助")
    print()
    print("环境变量见脚本头部 docstring（CAIMOGU_* 前缀）")


def show_test_comments():
    """测试模式：预览评论生成效果（不启动浏览器，无登录态也可跑）"""
    config = load_config()
    logger = setup_logging()
    logger.info("=" * 50)
    logger.info("评论生成测试（不会实际回复帖子）")
    logger.info("=" * 50)

    test_posts = [
        ("历经七年制作波折 《刀锋战士》真人电影正式宣告取消", "项目经历多次换导演和剧本调整，最后还是被取消了。"),
        ("大家来展示一下目前刷图的效果吧", "我这边刷图速度还行，就是特效一多会有点看不清怪。"),
        ("求推荐几款好玩的单机游戏", "想找节奏慢一点的，不太想玩特别肝的开放世界。"),
        ("《原神》3.0版本更新内容汇总分享", "这次主要加了新地图和草元素相关机制，任务线也比较长。"),
        ("这个游戏画面真的绝了分享给大家看看", "雨天场景的反光做得很明显，截图看着比白天舒服。"),
        ("有没有人遇到登录闪退的问题求助", "点登录后窗口直接消失，没有弹错误提示。"),
        ("今天抽卡出货了分享一下好运", "十连最后一发才出的，前面全是蓝光。"),
        ("爆料：《GTA6》可能延期至2026年发售", "据业内人士透露，Rockstar内部开发进度不及预期。"),
        ("《黑神话悟空》海外销量突破千万套", "发售首月海外销量已突破1000万套，成绩远超预期。"),
        ("每日签到", "如题"),
    ]

    for i, (title, content) in enumerate(test_posts):
        keyword = extract_keyword(title)
        decision = judge_replyability(title, content)
        logger.info("-" * 40)
        logger.info("标题: %s", title)
        logger.info("正文: %s", content)
        comment = generate_comment(title, content, config)
        char_count = _comment_len(comment)
        logger.info("判断: %s", decision)
        logger.info("关键词: %s", keyword)
        if comment == "SKIP":
            logger.info("结果: SKIP")
        else:
            logger.info("评论: %s (%d字)", comment, char_count)
        if config.get("deepseek_api_key") and i < len(test_posts) - 1:
            time.sleep(2)
    logger.info("-" * 40)
    logger.info("测试完成")


def main():
    """青龙入口：--test/--help 不启动浏览器，否则走回帖主流程 + 推送"""
    global _CONFIG, _DEBUG

    args = sys.argv[1:]
    if "--test" in args:
        show_test_comments()
        return
    if "--help" in args or "-h" in args:
        show_help()
        return

    _CONFIG = load_config()
    _DEBUG = _CONFIG.get("debug", False)

    # 启动即校验配置：错在日志里一眼可见，别等跑到 random.randint / 请求里才炸
    config_errors = validate_config(_CONFIG)
    if config_errors:
        print("❌ 配置校验未通过，已退出：")
        for err in config_errors:
            print("   - " + err)
        sys.exit(2)

    _proxy = _CONFIG.get("proxy", "")
    _proxy_src = _CONFIG.get("proxy_source", "")
    if not _proxy:
        print("ℹ️ 未配置 CAIMOGU_PROXY 且未检测到青龙全局代理，直连访问")
    elif _proxy_src == "CAIMOGU_PROXY":
        print(f"🌐 使用代理: {_proxy}")
    else:
        print(f"🌐 使用青龙全局代理（{_proxy_src}）: {_proxy}")

    _ensure_browser_deps()
    logger = setup_logging()

    title = "采蘑菇论坛自动回帖"
    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    result = run_signin(_CONFIG, logger)
    done, target = result.get("done", 0), result.get("target", 0)
    ok = result.get("success", False)

    # 标题按成败分档（被判定水贴单列，便于一眼区分"没刷到"和"被拦"）
    if result.get("water_blocked"):
        push_title = f"🚫 {title} 被判定水贴已中止（{done}/{target}）"
    elif ok and done >= target:
        push_title = f"✅ {title} 完成（{done}/{target}）"
    elif ok:
        push_title = f"⚠️ {title} 部分完成（{done}/{target}）"
    else:
        push_title = f"❌ {title} 失败（{done}/{target}）"

    report = "\n".join([
        f"# {title} - 执行报告",
        "",
        f"⏰ 执行时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"📊 目标 {target} 条，✅ 成功 {done} 条",
        "",
        f"- {result.get('message', '')}",
    ]).strip()
    print("\n" + report)

    notify = _CONFIG.get("notify", True)
    if notify:
        if _CONFIG.get("notify_only_fail", False) and ok and done >= target:
            print("ℹ️ CAIMOGU_NOTIFY_ONLY_FAIL=true 且本次全部成功，跳过推送")
        else:
            send_notify(push_title, report)
    else:
        print("ℹ️ CAIMOGU_NOTIFY=false，已禁用推送")


if __name__ == "__main__":
    main()
