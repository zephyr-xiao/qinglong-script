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
  CAIMOGU_MIN_DELAY         回帖最小间隔秒（默认 8）
  CAIMOGU_MAX_DELAY         回帖最大间隔秒（默认 20）
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
  python caimogu_qinglong.py            执行自动回帖
  python caimogu_qinglong.py --test     测试评论生成效果（不启动浏览器）
  python caimogu_qinglong.py --help     显示帮助
作者: 箫遥风
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
    "min_delay": 8,
    "max_delay": 20,
    "headless": True,
    "page_timeout_ms": 90000,
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com/v1",
    "deepseek_model": "deepseek-chat",
    "ai_timeout": 120,
}


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
    "error": ".error, .alert, .toast-error, .msg-error",
    "login_link": 'a[href*="login"]',
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

# 兼容旧引用
_BANNED_COMMENT_PARTS = _HARD_BANNED_PARTS

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

_REPLY_TEMPLATES = {
    "help": [
        "{d}这步最怕没提示，我之前也卡在这，最后靠回档才过去",
        "{d}看着像兼容性问题，等个补丁估计就好了，先别折腾",
        "说到{d}，我遇到过一模一样的，清缓存没用，重装才解决",
        "{d}要是能稳定复现就好办了，就怕随机触发排查到崩溃",
        "{d}这种问题最恶心，没报错没日志，只能盲猜",
        "我猜{d}是源头，楼主试试把这步跳过看会不会好",
        "{d}这情况我熟，先别急着重装，试试点修复看看",
        "{d}能每次都触发吗？如果随机出现的话大概率是内存泄漏",
    ],
    "regret": [
        "{d}可惜了，等了这么久就这结局，感觉之前的期待全白费了",
        "看到{d}被砍说实话挺难受的，毕竟关注了那么长时间",
        "{d}这种结局最搞心态，投入的精力直接打水漂",
        "又是{d}这一刀，怎么感觉最近好项目都活不下来",
        "{d}到这步戛然而止，就问之前预热的意义在哪",
        "说实话{d}这消息一点缓冲都没有，太突然了",
        "{d}这种收场方式真的让人不想再关注新项目了",
        "卡在{d}收尾，怎么说呢，期待越大失望越大吧",
    ],
    "update": [
        "{d}方向没问题，就是别最后又缩水，不然期待全落空",
        "{d}这块如果能影响玩法就好了，别只换皮不换骨",
        "说实话{d}这改动我挺期待的，就怕实装之后又是另一回事",
        "{d}看着有诚意，但执行力度才是关键，别光说不练",
        "{d}这个改动要是真能落地就舒服了，就怕砍一半",
        "我比较担心{d}会不会影响平衡，到时候又是一波调整",
        "{d}方向是对的，就怕优化跟不上，先观望吧",
        "光看{d}描述还行，等实机出来再判断，现在说啥都早",
    ],
    "recommend": [
        "{d}这个偏好挺明确，我玩过几个对口的，回头整理给你",
        "按{d}这个方向找准没错，能少踩不少坑",
        "{d}要是再耐玩一点就好了，不然选择面确实窄",
        "说到{d}，我第一个想到的就是那几个老牌作品，稳",
        "{d}这个需求其实挺好满足的，就是看你想不想接受老画面",
        "{d}按这个标准筛的话选择面会窄不少，但质量有保障",
        "{d}这个方向我还真玩过几个，主要看你能不能接受肝度",
        "单看{d}这个要求，能排掉一大批了，剩下的都还行",
    ],
    "luck": [
        "{d}这运气没谁了，我抽了八十发才出，人比人气死人",
        "看到{d}这种结果默默关掉了游戏，差距太大了",
        "{d}这波属实离谱，我十连全是保底，太酸了",
        "这种{d}截图最容易劝人手痒，下次我也想试试",
        "{d}比玄学还刺激，差一点就反转了，运气这东西真没道理",
        "看到{d}我突然不想玩这游戏了，非酋不配拥有快乐",
        "{d}这波操作妥妥的欧皇附体，建议去买彩票",
        "单看{d}就知道这运气逆天，我连续保底三个月了都",
    ],
    "media": [
        "{d}这块改编好了是神作，改砸了就是灾难，风险太大",
        "说到{d}，我觉得选角比剧情更决定成败，别只靠阵容",
        "{d}这种设定搬到银幕上观众接不接受是个大问题",
        "{d}改编的难点就在这，原著粉肯定会盯着不放",
        "我比较担心{d}会不会为了大众化把核心改没了",
        "{d}这个点处理不好整部就散了，不能只靠噱头",
        "说实话{d}看着就压力大，改编这种东西吃力不讨好",
        "{d}方向比噱头重要，别到时候光有阵容没有内容",
    ],
    "rumor": [
        "{d}如果消息属实后续影响应该不小，但先等官方回应吧",
        "{d}这种爆料看看就好，别太早下结论，之前翻车的还少吗",
        "说实话{d}现在信息还有限，等正式消息比较稳",
        "{d}如果是真的那确实炸裂，就怕最后又辟谣",
        "{d}这种传闻我持观望态度，毕竟消息来源太模糊了",
        "看到{d}先别激动，等实锤再说，假爆料太多了",
        "{d}要是真延了那影响可太大了，但我赌大概率是误传",
        "单看{d}这爆料可信度一般，等个官方公告比较靠谱",
    ],
    "sales": [
        "{d}这成绩放在同类里已经不错了，说明玩家反馈还可以",
        "{d}销量能起来说明确实有竞争力，后续能不能保持才是关键",
        "说实话{d}这数据比预期好，看来口碑发酵起作用了",
        "{d}这成绩不算意外，毕竟前期宣发到位了",
        "{d}后续热度能不能稳住才重要，别又是一波流",
        "看到{d}我觉得这个类型还是有市场的，别家可以跟进了",
        "{d}这数据说明玩家用脚投票了，质量说话比营销管用",
        "单看{d}确实亮眼，但长线运营才是考验，别高兴太早",
    ],
    "normal": [
        "{d}这块我比较在意，处理好了体验会好不少",
        "说实话{d}这方向挺有意思的，之前没往这方面想过",
        "{d}如果能落地的话影响会很明显，先观望吧",
        "我比较担心{d}会不会有隐藏问题，等实测再说",
        "{d}这个角度挺新颖的，细想的话确实值得关注",
        "看到{d}我觉得可以期待一下，就怕最后虎头蛇尾",
        "{d}细想的话影响挺深远的，不只是表面上那么简单",
        "说到{d}我也有同感，这确实是个容易被忽略的点",
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
    "关键": ["重要", "核心"],
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
    """写入 JSON 文件"""
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
    data["last_run_date"] = today
    data["last_run_posts"] = post_count
    data["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["status"] = "running" if post_count < reply_count else "done"

    if post_id:
        ids = data.get("today_post_ids", [])
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
    """粗略判断帖子类型，用于生成更贴合标题的回复"""
    if re.search(r'取消|砍|延期|跳票|停服|下架|暴死|失败|崩|凉', title):
        return "regret"
    if re.search(r'求助|请问|有没有|怎么|如何|为啥|为什么|闪退|报错|问题|卡住', title):
        return "help"
    if re.search(r'爆料|传闻|泄露|消息人士|据说|内部消息|疑似|可能延期|或将于', title):
        return "rumor"
    if re.search(r'销量|销售额|突破|万套|百万|销量榜|成绩|首周|月销|出货量', title):
        return "sales"
    if re.search(r'更新|版本|补丁|改动|上线|发布|公布|官宣|新增', title):
        return "update"
    if re.search(r'推荐|安利|好玩|入坑|值得买吗|买不买', title):
        return "recommend"
    if re.search(r'抽卡|出货|晒|欧|非|运气|掉落', title):
        return "luck"
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
        if not any(stop in chunk for stop in _DETAIL_STOP_WORDS):
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


def generate_comment_template(title, content=""):
    """模板模式：先判断 REPLY/SKIP，再生成短回复"""
    decision = judge_replyability(title, content)
    if decision == "SKIP":
        return "SKIP"

    detail = _extract_detail(title, content)
    if not detail:
        return "SKIP"

    keyword = extract_keyword(title) or ""
    title_type = detect_title_type((title or "") + " " + (content or "")[:120])
    templates = _REPLY_TEMPLATES.get(title_type, _REPLY_TEMPLATES["normal"])

    # 打乱模板顺序，填充插槽并应用同义词随机化
    candidates = []
    for tpl in random.sample(templates, len(templates)):
        filled = tpl.replace("{d}", detail).replace("{kw}", keyword)
        filled = _apply_synonyms(filled)
        candidates.append(filled)

    valid = [c for c in candidates if _is_reply_valid(c, title, content)]
    if valid:
        return random.choice(valid)
    return "SKIP"


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


def generate_comment_ai(title, content, api_key, base_url, model):
    """AI 模式：让 AI 直接生成评论或 SKIP，含空返回重试和429重试"""
    logger = logging.getLogger("caimogu_qinglong")
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
            "你是一个游戏论坛用户，正在浏览帖子。请根据标题和正文，写一条真实的回复。\n\n"
            "规则：\n"
            "- 从玩家立场出发回复，表达个人态度（担忧、期待、吐槽、对比、怀疑），不要像在评价新闻\n"
            "- 抓住帖子里一个具体细节来回复，可以推测影响、表达预期\n"
            "- 语气口语化，像真人在闲聊，可以吐槽、提问、补充\n"
            "- 回复必须控制在 15 到 40 个字之间，绝对不要超过 40 个字；直接输出正文，不要任何前缀、编号、解释\n"
            "- 绝对不要用这些套话：感谢分享、支持一下、学到了、坐等后续、确实如此、期待更新、前排围观、有道理、这波可以、说得好、支持楼主、码住、马克\n"
            "- 不要假装亲身经历过\n"
            "- 不要总结帖子内容或复述标题\n"
            "- 不要用\"这个细节\"\"这个改动\"\"这个消息\"开头\n\n"
            "如果帖子内容太少、没法自然接话，只回复两个字母：SKIP\n"
            "能回复就直接输出回复内容，不要加任何前缀、编号、解释\n"
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
            return generate_comment_template(title, content)

        comment = _normalize_generated_comment(raw_content)
        logger.info("AI 清洗后: %s (字数=%d)", comment, _comment_len(comment))

        if comment.upper() == "SKIP":
            return "SKIP"

        if _comment_len(comment) < 5:
            logger.warning("AI 回复太短，回退模板: %s", comment)
            return generate_comment_template(title, content)

        if _comment_len(comment) > 40:
            logger.warning("AI 回复超过 40 字，回退模板: %s", comment)
            return generate_comment_template(title, content)

        if any(part in comment for part in _HARD_BANNED_PARTS):
            logger.warning("AI 回复含套话，回退模板: %s", comment)
            return generate_comment_template(title, content)

        return comment
    except Exception as e:
        logging.getLogger("caimogu_qinglong").warning("AI生成评论失败，回退到模板模式: %s", e)
        return generate_comment_template(title, content)


def generate_comment(title, content, config):
    """根据配置选择 AI 模式或模板模式生成评论；可能返回 SKIP"""
    api_key = config.get("deepseek_api_key", "")
    if api_key:
        base_url = config.get("deepseek_base_url", "https://api.deepseek.com/v1")
        model = config.get("deepseek_model", "deepseek-chat")
        return generate_comment_ai(title, content, api_key, base_url, model)
    return generate_comment_template(title, content)

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
    }
    if storage_state:
        context_kwargs["storage_state"] = storage_state
    context = browser.new_context(**context_kwargs)

    # 反检测：抹掉 webdriver 标志
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
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


def get_post_list(page, config, count, logger):
    """从板块页面获取帖子列表，自动跳过置顶帖"""
    circle_url = config["circle_url"]
    logger.info("正在获取帖子列表: %s", circle_url)
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
    max_candidates = max(count * 8, count + 10)

    for item in items[:max_candidates]:
        try:
            title_el = item.query_selector(SELECTORS["post_title"])
            if not title_el:
                continue
            href = title_el.get_attribute("href")
            title = title_el.inner_text().strip()
            if not (href and title):
                continue

            item_class = item.get_attribute("class") or ""
            item_html = item.inner_html()[:500] if hasattr(item, "inner_html") else ""
            is_pinned = (
                "sticky" in item_class.lower()
                or "pin" in item_class.lower()
                or "top" in item_class.lower()
                or "置顶" in item_html
                or "精华" in item_html
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
    """输入评论到编辑器，依次尝试 fill → evaluate → keyboard.type"""
    editor.click()
    page.wait_for_timeout(300)

    # 方式一：fill
    try:
        editor.fill(comment)
        page.wait_for_timeout(500)
        logger.info("评论已输入编辑器(fill)")
        return True
    except Exception:
        pass

    # 方式二：直接设置 Quill 编辑器内容
    try:
        page.evaluate(
            '(text) => { var ed = document.querySelector(".ql-editor"); '
            'if(ed) { ed.innerHTML = "<p>" + text + "</p>"; '
            'ed.dispatchEvent(new Event("input", {bubbles: true})); } }',
            comment
        )
        page.wait_for_timeout(500)
        logger.info("评论已输入编辑器(evaluate)")
        return True
    except Exception:
        pass

    # 方式三：键盘逐字输入
    try:
        editor.click()
        page.wait_for_timeout(200)
        page.keyboard.type(comment, delay=50)
        page.wait_for_timeout(500)
        logger.info("评论已输入编辑器(keyboard)")
        return True
    except Exception as e:
        logger.error("输入评论失败: %s", e)
        return False


class LoginExpiredError(Exception):
    """登录态失效（如 cmg_token 过期）时抛出，用于中断整个回帖流程。"""


def submit_reply(page, logger):
    """查找并点击提交按钮，返回是否成功。
    若被 swal2「未登录/登录」弹窗拦截则抛 LoginExpiredError 中断流程。"""
    btn, sel = first_element(page, SELECTORS["submit"])
    if btn:
        try:
            btn.click(timeout=10000)
            logger.info("点击提交按钮: %s", sel)
            return True
        except Exception as e:
            # 区分「登录失效」与「其他点击问题」：swal2 弹窗拦截时明确提示
            swal_text = ""
            try:
                el = page.query_selector(".swal2-popup")
                swal_text = el.inner_text() if el else ""
            except Exception:
                pass
            if "登录" in swal_text or "未登录" in swal_text:
                # 登录失效：关闭弹窗，抛专用异常让上层中断后续回帖
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                swal_clean = swal_text.replace("\n", " ").strip()
                logger.error(
                    "提交被「%s」弹窗拦截，登录态可能已失效！"
                    "请更换有效的 CAIMOGU_COOKIE 后重试",
                    swal_clean,
                )
                raise LoginExpiredError(swal_clean)
            logger.error("点击提交按钮失败: %s", e)
            return False

    # 备选：Ctrl+Enter
    try:
        page.keyboard.press("Control+Enter")
        logger.info("通过 Ctrl+Enter 提交")
        return True
    except Exception:
        logger.error("未找到提交按钮")
        return False


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

        # 输入评论
        if not input_comment(page, editor, comment, logger):
            _debug_shot(page, "input_fail")
            return False

        # 提交回复
        if not submit_reply(page, logger):
            _debug_shot(page, "submit_fail")
            return False

        # 等待提交完成
        page.wait_for_timeout(3000)

        # 检查错误提示
        try:
            error_el = page.query_selector(SELECTORS["error"])
            if error_el:
                error_text = error_el.inner_text()
                if error_text and len(error_text) > 2:
                    logger.warning("页面提示: %s", error_text)
                    _debug_shot(page, "page_error")
        except Exception:
            pass

        logger.info("回复提交完成")
        _debug_shot(page, "reply_ok")
        return True

    except LoginExpiredError:
        # 登录失效：原样向上抛出，由 run_signin 中断整个回帖流程
        raise
    except Exception as e:
        logger.error("回复帖子时出错: %s", e)
        _debug_shot(page, "exception")
        return False


def check_login_status(page, logger):
    """检查登录状态是否有效。

    实测：caimogu 对「token 过期」的页面渲染与正常登录几乎无差别
    （回复框、回复按钮、头部菜单照常显示），真正校验发生在点击提交时，
    由 swal2「您还未登录」弹窗提示。因此这里只做「彻底未登录」的拦截，
    无法靠页面 DOM 判断 token 是否过期；更可靠的失效检测在 submit_reply
    提交环节（见 LoginExpiredError）。
    """
    try:
        page.goto("https://www.caimogu.cc/", timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # 彻底未登录：页面存在「登录/注册」字样或 /login 跳转入口
        has_login_entry = page.query_selector(
            'a[href*="/login"], a[href*="login.html"], '
            'a:has-text("登录"), button:has-text("登录"), '
            'a:has-text("注册"), button:has-text("注册")'
        )
        if has_login_entry:
            logger.warning("检测到未登录（页面存在登录/注册入口）")
            return False

        # 没有明确未登录标志：放行。token 是否过期由提交环节最终校验，
        # 若失效会抛 LoginExpiredError 并中断流程，不会误回帖。
        logger.info("未检测到明确的未登录标志，继续（最终以提交校验为准）")
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
                post_id = re.search(r'/(\d+)\.html', post["url"])
                post_id = post_id.group(1) if post_id else post["url"]
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
    print("  python caimogu_qinglong.py            执行自动回帖")
    print("  python caimogu_qinglong.py --test     测试评论生成效果（不启动浏览器）")
    print("  python caimogu_qinglong.py --help     显示帮助")
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

    # 标题按成败分档
    if ok and done >= target:
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
