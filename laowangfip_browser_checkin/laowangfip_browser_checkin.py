# -*- coding: utf-8 -*-
"""
new Env('老王FIP签到(浏览器版)');
cron: 30 8 * * *

适配青龙面板 - 老王FIP论坛签到（Playwright 浏览器驱动版）
站点：https://laowangfip372.vip/forum.php   （被墙，必须挂代理）

为什么用浏览器版：
  tncode 滑块校验涉及前端 JS 计算的 sign/track 字段（自定义 XOR 滚动哈希 +
  base64 编码），纯 requests 无法复现。让 Chromium 自己跑站点 JS，脚本
  只负责"OpenCV 识别滑块位置 + 模拟人类拖动"。

环境变量：
  LWFIP_ACCOUNTS              必填 用户名#密码，多账号用 & 或换行分隔
  LWFIP_PROXY                 可选 HTTP 代理（站点被墙，建议配置）
                                   例：http://172.17.0.1:7890
                                   未配置时自动回退青龙全局代理（HTTPS_PROXY /
                                   HTTP_PROXY / ALL_PROXY），仍为空则直连尝试
  LWFIP_PROXY_REQUIRED        可选 默认 false，true 时缺代理直接报错退出
  LWFIP_BASE_URL              可选 默认 https://laowangfip372.vip
  LWFIP_NOTIFY                可选 默认 true
  LWFIP_TIMEOUT               可选 默认 30000（毫秒）
  LWFIP_DEBUG                 可选 默认 false，每步截屏到 _browser_debug_*.png
  LWFIP_HEADFUL               可选 默认 false，true 时显示浏览器窗口（本地调试用）
  LWFIP_CHROMIUM_PATH         可选 系统 Chromium 路径（青龙容器装系统包后填 /usr/bin/chromium）
  LWFIP_MAX_CAPTCHA_RETRY     可选 默认 5，单次拼图失败重试次数
  LWFIP_NOTIFY_ONLY_FAIL      可选 默认 false，true 时仅在有失败时推送
  LWFIP_MAX_RETRY             可选 默认 5，账号级任务重试次数
  LWFIP_RETRY_INTERVAL        可选 默认 60（秒），账号级重试间隔
  LWFIP_COOKIE_CACHE          可选 默认 true，登录成功后会话 Cookie 落盘复用，
                                    后续任务/重试直通签到跳过滑块；false 关闭

依赖：
  playwright              （浏览器驱动）
  opencv-python-headless  （滑块识别）
  numpy

作者: 箫遥风
"""

import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import traceback
from datetime import datetime
from urllib.parse import urlparse

# ====================== 1. 编码 / 依赖 / 通知 脚手架 ======================

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Playwright
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("❌ 缺少 playwright，请安装：")
    print("   pip install playwright")
    print("   playwright install chromium    # 下载浏览器（在青龙里改用系统 chromium 时跳过）")
    print()
    print("   青龙容器内推荐用系统 chromium：")
    print("     apt update && apt install -y chromium")
    print("     export LWFIP_CHROMIUM_PATH=/usr/bin/chromium")
    sys.exit(1)

# OpenCV + numpy
try:
    import cv2
    import numpy as np
except ImportError as e:
    print("❌ 缺少 opencv-python-headless 或 numpy")
    print("   pip install opencv-python-headless numpy")
    print(f"   原始错误: {e}")
    sys.exit(1)

# 青龙 notify.py 兼容
_qinglong_send = None
try:
    from notify import send as _qinglong_send  # type: ignore
except Exception:
    # 青龙容器内 notify.py 在 /ql/data/scripts/，脚本运行时 cwd 可能不在该目录
    _scripts_dir = "/ql/data/scripts"
    if os.path.isdir(_scripts_dir) and _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
        try:
            from notify import send as _qinglong_send  # type: ignore
        except Exception:
            _qinglong_send = None


# ====================== 2. 顶部常量 ======================

DEFAULT_BASE_URL = "https://laowangfip372.vip"
# 登录页（直接进 Discuz 登录页，不要绕道 forum.php）
LOGIN_PAGE_PATH = "/member.php?mod=logging&action=login"
SIGN_PAGE_PATH = "/plugin.php?id=k_misign:sign"

# 关键 DOM 选择器（已根据真实 HTML 抓取对齐）
SEL_USERNAME_INPUT = "input[name='username']"
SEL_PASSWORD_INPUT = "input[name='password']"
SEL_LOGIN_SUBMIT = "button[name='loginsubmit'], #captcha_submit"

# tncode 验证码
SEL_TNCODE_TRIGGER = "#tncode"
# 弹窗容器真实 DOM：<div class="tncode_div is-ready" id="tncode_box">（探针实测），
# id 是 tncode_box 而 class 才是 tncode_div；此处必须用单一简单选择器，
# 因为 _wait_loading_gone 等处会拼 `{SEL_TNCODE_DIV} .loading` 后代选择器
SEL_TNCODE_DIV = "#tncode_box"
SEL_TNCODE_CANVAS_BG = ".tncode_canvas_bg"
SEL_TNCODE_CANVAS_MARK = ".tncode_canvas_mark"
SEL_SLIDE_BLOCK = ".slide_block"
SEL_SLIDE_TRACK = ".slide"
SEL_TNCODE_REFRESH = ".tncode_refresh"
SEL_TNCODE_MSG_OK = ".tncode_msg_ok"
SEL_TNCODE_MSG_ERROR = ".tncode_msg_error"

# 滑块识别参数
TEMPLATE_THRESHOLD = 0.30
# 低置信度仍可尝试拖动的下限：>= 此值且 < TEMPLATE_THRESHOLD 时仍拖过去，靠服务端校验兜底
MIN_DRAG_CONFIDENCE = 0.20
SLIDE_DURATION_MS = (700, 1300)
OVERSHOOT_PX = (5, 15)

# 账号级重试参数默认值（可由环境变量 LWFIP_MAX_RETRY / LWFIP_RETRY_INTERVAL 覆盖）
MAX_RETRY = 5
RETRY_INTERVAL_SEC = 60  # 1 分钟

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ====================== 3. 通用工具 ======================

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
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


def resolve_proxy(*specific_keys: str) -> tuple:
    """
    解析代理地址，返回 (代理地址, 来源变量名)，两者均可能为空字符串。

    优先级：脚本专属变量 > 青龙全局代理变量 > 空（直连）。

    这样脚本在未配置专属变量时也能复用青龙面板的全局代理；代理是否必须
    交由调用方判断，缺失代理本身不视为错误。
    Playwright 的 launch(proxy=...) 不会读取环境变量，因此必须在这里显式取值。
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


def is_retryable_error(err_text: str) -> bool:
    """
    判断错误是否可重试。

    可重试：网络/超时/代理类、滑块验证码类（识别与过检是概率性失败，
    重开浏览器可能通过）、签到流程类（页面加载不完整/跳转结果未知）。
    不可重试：密码错误、用户名不存在等业务错误（重试无意义）。
    """
    retry_keywords = [
        # 网络 / 超时 / 代理类
        "timeout", "timed out", "超时",
        "proxy", "代理",
        "connection", "connect", "连接",
        "network", "网络",
        "socket", "dns", "resolve",
        "net::", "err_",
        # 导航被中断 / Chromium 错误页（chrome-error://）＝网络/代理加载失败
        "navigation", "interrupted", "chromewebdata",
        "浏览器启动失败",
        "browser", "chromium",
        # 滑块验证码类
        "验证码", "滑块", "captcha", "tncode", "v2_captcha",
        # 签到流程类
        "未到达验证页", "提交后仍在验证页", "签到结果未知",
        "j_chkitot", "找不到签到",
    ]
    lower = err_text.lower()
    return any(kw in lower for kw in retry_keywords)


def compact_error(err: BaseException, limit: int = 120) -> str:
    """
    压缩异常文本为单行摘要。

    Playwright 错误的 call log 是多行缩进格式，直接 str(e)[:80] 往往
    截在 'waiting for loca' 这种半句上，把最关键的 locator 名切掉；
    先折叠全部空白再截断，locator 信息就能保留到摘要里。
    """
    text = re.sub(r"\s+", " ", str(err)).strip()
    return text[:limit]


def parse_credentials(env_value: str):
    if not env_value:
        return []
    parts = re.split(r"[&\n]+", env_value.strip())
    accounts = []
    for raw in parts:
        raw = raw.strip()
        if not raw or "#" not in raw:
            if raw:
                print(f"  ⚠️ 跳过格式错误: {raw[:30]}")
            continue
        u, p = raw.split("#", 1)
        u, p = u.strip(), p.strip()
        if u and p:
            accounts.append((u, p))
    return accounts


def mask_account(s: str) -> str:
    if not s:
        return ""
    if "@" in s:
        head, tail = s.split("@", 1)
        if len(head) <= 2:
            return head[0] + "*@" + tail
        return head[:2] + "*" * (len(head) - 2) + "@" + tail
    if len(s) <= 2:
        return s[0] + "*"
    return s[:2] + "*" * max(1, len(s) - 4) + s[-2:]


def extract_cookie_domain(base_url: str) -> str:
    """从站点 URL 提取 Cookie domain（hostname，不含端口），支持换域名。"""
    try:
        host = urlparse(base_url).hostname
        if host:
            return host
    except Exception:
        pass
    return urlparse(DEFAULT_BASE_URL).hostname or DEFAULT_BASE_URL


def cookie_cache_path(username: str) -> str:
    """会话 Cookie 缓存文件路径：脚本同目录，文件名用用户名 hash（防特殊字符/敏感信息泄漏）。"""
    digest = hashlib.sha1(username.encode("utf-8")).hexdigest()[:12]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f"cookie_{digest}.json")


async def save_cookies_to_cache(context, username: str) -> bool:
    """登录成功后把会话 Cookie 落盘，供后续任务/重试直通签到（跳过滑块）。"""
    try:
        cookies = await context.cookies()
        # 会话级 Cookie（expires<0）删除 expires 字段，否则 add_cookies 会拒绝
        cleaned = []
        for c in cookies:
            c = dict(c)
            if c.get("expires", -1) < 0:
                c.pop("expires", None)
            cleaned.append(c)
        with open(cookie_cache_path(username), "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False)
        print(f"🍪 已保存会话 Cookie 缓存（{len(cleaned)} 条）")
        return True
    except Exception as e:
        print(f"⚠️ 保存 Cookie 缓存失败: {e}")
        return False


def load_cookies_from_cache(username: str):
    """读取会话 Cookie 缓存；文件缺失或损坏返回 None。"""
    path = cookie_cache_path(username)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return None


def remove_cookie_cache(username: str) -> None:
    """删除失效的 Cookie 缓存（登录态过期时调用，避免反复复用坏 Cookie）。"""
    path = cookie_cache_path(username)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


async def is_logged_in(page) -> bool:
    """
    判断当前页面是否处于 Discuz 已登录状态。

    保守判定：强登录标志（退出链接 / 欢迎您 / user_notice）任一命中才视为已登录，
    未命中一律视为未登录（宁可重新登录，不可误判直通导致签到失败）。
    """
    try:
        logout_link = page.locator("a[href*='action=logout'], a[href*='logout']").first
        if await logout_link.count() > 0:
            return True
        page_text = await page.content()
        if "退出</a>" in page_text or "欢迎您" in page_text or "user_notice" in page_text:
            return True
    except Exception:
        return False
    return False


async def is_chromium_error_page(page) -> bool:
    """
    检测页面是否已导航到 Chromium 错误页（chrome-error://）。

    网络/代理加载失败时 Chromium 会把页面切到内置错误页，此时页面已不可用，
    应尽快返回明确的网络错误并触发重试，而不是继续走「等输入框」等
    误导性长失败路径。
    """
    try:
        return page.url.startswith("chrome-error://")
    except Exception:
        return False


async def send_notify_with_retry(title: str, content: str) -> bool:
    """使用青龙 notify.py 推送，失败时固定重试 3 次。"""
    if not _qinglong_send:
        print("ℹ️ 未找到 notify.py，跳过推送")
        return False

    max_attempts = 3
    retry_delay = 3

    for attempt in range(1, max_attempts + 1):
        try:
            _qinglong_send(title, content)
            print("📨 已通过 notify.py 推送")
            return True
        except Exception as e:
            print(f"⚠️ 推送第 {attempt}/{max_attempts} 次失败: {e}")
            if attempt < max_attempts:
                print(f"   等待 {retry_delay} 秒后重试推送...")
                await asyncio.sleep(retry_delay)
            else:
                print("⚠️ notify.py 推送最终失败")

    return False


def _format_countqian_value(label: str, value: str) -> str:
    """格式化 countqian 统计值，按白名单字段附加固定单位。"""
    value = str(value or "").strip()
    if not value:
        return ""
    # 白名单字段 → 固定单位映射
    label_units = {
        "连续签到": "天",
        "总天数": "天",
        "签到等级": "级",
        "积分奖励": "分",
    }
    expected_unit = label_units.get(label)
    if expected_unit:
        return f"{value} {expected_unit}"
    return value


def parse_countqian_stats(items: list[str]) -> dict:
    """解析 k_misign 签到统计文本兜底，只保留指定白名单字段。"""
    wanted_labels = ("连续签到", "签到等级", "积分奖励", "总天数")
    stats = {}

    for raw in items:
        text = re.sub(r"\s+", " ", raw or "").strip()
        if not text:
            continue

        for label in wanted_labels:
            if label in text and label not in stats:
                value = text.replace(label, "", 1).strip(" ：:　")
                match = re.search(r"\d+", value)
                if match:
                    stats[label] = _format_countqian_value(label, match.group(0))
                break

    return stats


# ====================== 4. OpenCV 滑块识别（适配浏览器截屏场景） ======================

def _captcha_log(debug: bool, msg: str):
    if debug:
        print(f"   [captcha] {msg}")


def solve_slider_from_screenshot(png_bytes: bytes, debug: bool,
                                 image_bottom_px: int | None = None):
    """
    从浏览器截屏（按下滑块后）的 #tncode_box 中找出"还需要移动的像素数"。

    实测 laowangfip372.vip 的真实布局（单张图，非三联）：
      - 上部：单张完整背景图，按下滑块后左上角出现绿色边框的拼图碎片
      - 下部：滑块条 + 取消/刷新按钮
      - 缺口：在背景图某处用黑边阴影绘制（多个时叠加更深）

    算法：
      1) 切掉下部（滑块条 + 按钮区域）
      2) 在剩余图像中找绿色边框的拼图碎片 → 得到 piece_x（碎片当前 x）
      3) 提取拼图碎片的内容（不含绿边）作为模板
      4) 把碎片所在区域涂黑（避免自匹配）
      5) 在剩余图像里 matchTemplate → 找到目标缺口 target_x
      6) 返回 (piece_x, target_x, confidence)，调用方算 remaining = target_x - piece_x

    参数：
      png_bytes - bytes 类型的图片
      image_bottom_px - 图像区下边界（相对截图顶部，像素）。
                       由调用方按 .slide 元素上沿算出传入；
                       None 时回退到 h*0.78 旧逻辑（兼容）。
    返回：(piece_x, target_x, confidence)，失败时返回 (-1, -1, 0.0)
    """
    try:
        arr = np.frombuffer(png_bytes, np.uint8)
        full = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if full is None:
            _captcha_log(debug, "解码图片失败")
            return -1, -1, 0.0

        h, w = full.shape[:2]

        # === 步骤 1：切掉底部滑块条区域 ===
        # 优先用调用方传入的 .slide 上沿位置（动态），回退到固定 0.78
        if image_bottom_px is not None and 0 < image_bottom_px < h:
            cut_h = image_bottom_px
        else:
            cut_h = int(h * 0.78)
        image_area = full[:cut_h, :]
        ih, iw = image_area.shape[:2]
        _captcha_log(debug, f"切图区: 总图 {h}x{w} → 图像区 {ih}x{iw} (cut_bottom={image_bottom_px})")

        # === 步骤 2：在图像区找绿色边框拼图碎片 ===
        b, g, r = cv2.split(image_area)
        # 绿色判定：G 通道明显高于 R 和 B
        green_mask = (
            (g > 100)
            & (g.astype(np.int16) > r.astype(np.int16) + 30)
            & (g.astype(np.int16) > b.astype(np.int16) + 30)
        ).astype(np.uint8) * 255

        green_pixels = int(green_mask.sum() / 255)
        if green_pixels < 50:
            _captcha_log(debug, f"未找到绿色边框（绿色像素 {green_pixels}px）")
            return -1, -1, 0.0

        contours, _ = cv2.findContours(green_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            _captcha_log(debug, "未找到绿色轮廓")
            return -1, -1, 0.0

        # 过滤合理的拼图候选（不能太小、不能太扁、宽高比接近正方形）
        # 拼图碎片一般 30~80px 见方，宽高比 0.6~1.6
        candidates_pieces = []
        for c in contours:
            cx, cy, cw, ch = cv2.boundingRect(c)
            if cw < 25 or ch < 25 or cw > 120 or ch > 120:
                continue
            ratio = cw / max(1, ch)
            if ratio < 0.6 or ratio > 1.6:
                continue
            area = cv2.contourArea(c)
            if area < 200:  # 太小的轮廓多半是噪声
                continue
            candidates_pieces.append((c, cx, cy, cw, ch, area))

        if not candidates_pieces:
            # 退化：直接取最大轮廓试试（避免完全失败）
            biggest = max(contours, key=cv2.contourArea)
            cx, cy, cw, ch = cv2.boundingRect(biggest)
            if cw < 20 or ch < 20:
                _captcha_log(debug, f"无合格拼图轮廓（最大候选 {cw}x{ch}）")
                return -1, -1, 0.0
            candidates_pieces.append((biggest, cx, cy, cw, ch, cv2.contourArea(biggest)))

        # 优先取尺寸最大的合格候选
        candidates_pieces.sort(key=lambda t: t[5], reverse=True)
        biggest, px, py, pw, ph = candidates_pieces[0][:5]

        _captcha_log(
            debug,
            f"找到拼图碎片: ({px},{py},{pw}x{ph}), 候选={len(candidates_pieces)}个, "
            f"绿色像素={green_pixels}px"
        )

        piece_x = px  # 碎片 bbox 当前 x 坐标（含绿边）

        # === 步骤 3：提取碎片内容作模板（去掉绿边，留中央实际图案） ===
        # 收缩 4 像素去掉绿边
        inset = 4
        tx = px + inset
        ty = py + inset
        tw = max(1, pw - 2 * inset)
        th = max(1, ph - 2 * inset)
        if tw < 10 or th < 10:
            _captcha_log(debug, f"模板太小 {tw}x{th}")
            return -1, -1, 0.0

        template_bgr = image_area[ty : ty + th, tx : tx + tw]
        template_gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)

        # === 步骤 4：把碎片所在区域涂黑，避免自匹配 ===
        search_area = image_area.copy()
        # 涂黑范围比 bbox 多 10px，确保碎片完全被遮蔽
        mask_pad = 10
        x0 = max(0, px - mask_pad)
        y0 = max(0, py - mask_pad)
        x1 = min(iw, px + pw + mask_pad)
        y1 = min(ih, py + ph + mask_pad)
        cv2.rectangle(search_area, (x0, y0), (x1, y1), (0, 0, 0), -1)
        search_gray = cv2.cvtColor(search_area, cv2.COLOR_BGR2GRAY)

        # === 步骤 5：模板匹配，找目标缺口位置 ===
        # 灰度版主匹配
        result_g = cv2.matchTemplate(search_gray, template_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val_g, _, max_loc_g = cv2.minMaxLoc(result_g)
        target_x_g = max_loc_g[0]
        target_y_g = max_loc_g[1]

        # 边缘版（处理缺口是黑边阴影绘制的情况）
        search_edge = cv2.Canny(search_gray, 50, 150)
        template_edge = cv2.Canny(template_gray, 50, 150)
        result_e = cv2.matchTemplate(search_edge, template_edge, cv2.TM_CCOEFF_NORMED)
        _, max_val_e, _, max_loc_e = cv2.minMaxLoc(result_e)
        target_x_e = max_loc_e[0]

        _captcha_log(
            debug,
            f"匹配: 灰度 ({target_x_g},{target_y_g}) score={max_val_g:.3f} | "
            f"边缘 x={target_x_e} score={max_val_e:.3f}"
        )

        # 决策：避开退化分数（>= 0.999），优先取分数高且 y 接近 piece_y 的
        # 置信度分三档——
        #   score >= TEMPLATE_THRESHOLD(0.30)         正常命中
        #   MIN_DRAG_CONFIDENCE(0.20) <= score < 0.30 把握低但仍拖过去（服务端校验兜底）
        #   score < 0.20                              真没命中，返回失败
        SCORE_DEGENERATE = 0.999

        candidates = []
        if max_val_g < SCORE_DEGENERATE and max_val_g >= MIN_DRAG_CONFIDENCE:
            # y 也要接近碎片 y（缺口和碎片应在同一水平线，容差 ±20px）
            if abs(target_y_g - py) <= max(20, ph):
                candidates.append((target_x_g, max_val_g, "灰度"))
        if max_val_e < SCORE_DEGENERATE and max_val_e >= MIN_DRAG_CONFIDENCE:
            if abs(max_loc_e[1] - py) <= max(20, ph):
                candidates.append((target_x_e, max_val_e, "边缘"))

        if not candidates:
            _captcha_log(debug,
                         f"无有效匹配（最低置信度 {MIN_DRAG_CONFIDENCE}，"
                         f"灰度 {max_val_g:.3f} / 边缘 {max_val_e:.3f}）")
            return -1, -1, 0.0

        # 取分数最高的
        candidates.sort(key=lambda c: c[1], reverse=True)
        target_x, best_score, best_name = candidates[0]

        # piece 与 target 统一到"去绿边后的内容左沿"坐标，消除 inset 系统性偏差
        piece_anchor = piece_x + inset
        target_anchor = target_x + inset

        # 把握低提醒
        if best_score < TEMPLATE_THRESHOLD:
            _captcha_log(debug,
                         f"⚠️ 匹配置信度低（{best_score:.3f} < {TEMPLATE_THRESHOLD}），"
                         f"仍尝试拖动由服务端校验兜底")
        _captcha_log(
            debug,
            f"采用 {best_name}: piece_x={piece_anchor}, target_x={target_anchor}, "
            f"distance={target_anchor - piece_anchor}, score={best_score:.3f}"
        )

        return piece_anchor, int(target_anchor), float(best_score)
    except Exception as e:
        _captcha_log(debug, f"识别异常: {e}")
        if debug:
            traceback.print_exc()
        return -1, -1, 0.0


# ====================== 5. 拖动轨迹生成 ======================

def generate_trajectory(distance: int):
    """生成"加速-减速 + 过冲回拉 + 微抖动"轨迹，返回 [(x, y, t_ms), ...]。"""
    import math
    if distance <= 0:
        return []

    duration_ms = random.randint(*SLIDE_DURATION_MS)
    overshoot = random.randint(*OVERSHOOT_PX)
    overshoot = min(overshoot, max(2, distance // 4))

    forward = distance + overshoot
    forward_ms = int(duration_ms * 0.75)
    backward_ms = duration_ms - forward_ms

    points = []
    steps_fwd = max(20, forward_ms // 30)
    for i in range(1, steps_fwd + 1):
        ratio = i / steps_fwd
        eased = 0.5 * (1 - math.cos(math.pi * ratio))
        x = int(forward * eased)
        y_jit = random.randint(-2, 2)
        t = int(forward_ms * ratio + random.randint(-10, 10))
        points.append((x, y_jit, max(t, 1)))

    pause_ms = random.randint(50, 120)
    base_t = points[-1][2] + pause_ms
    points.append((forward, random.randint(-2, 2), base_t))

    steps_back = max(8, backward_ms // 40)
    for i in range(1, steps_back + 1):
        ratio = i / steps_back
        eased = 0.5 * (1 - math.cos(math.pi * ratio))
        x = forward - int(overshoot * eased)
        y_jit = random.randint(-2, 2)
        t = base_t + int(backward_ms * ratio + random.randint(-8, 8))
        points.append((x, y_jit, max(t, base_t + 1)))

    points[-1] = (distance, points[-1][1], points[-1][2])
    return points


# ====================== 6. Playwright 浏览器封装 ======================

async def _shot(page, name: str, debug: bool):
    """DEBUG 模式下截屏存到当前目录。"""
    if not debug:
        return
    try:
        ts = int(time.time() * 1000)
        fn = f"_browser_debug_{ts}_{name}.png"
        await page.screenshot(path=fn, full_page=False)
        print(f"   [shot] 已截屏 {fn}")
    except Exception as e:
        print(f"   [shot] 截屏失败 {name}: {e}")


async def pass_tncode_in_browser(page, debug: bool, max_retry: int = 3) -> tuple[bool, str]:
    """
    在浏览器里完成一次 tncode 滑块验证。

    返回 (是否通过, 失败原因摘要)。通过时原因参数为 ""。

    ⚠️ 关键流程（老王FIP 实测）：
       缺口拼图碎片必须先按住滑块滑动后才会显示（防爬）。
       未按下时弹窗显示的只有完整背景图，看不到拼图。
       按下后碎片显示在滑块当前位置（屏幕左上角附近），
       缺口位置在背景图某处用黑边阴影绘制。

    流程：
      1) 等 #tncode_box 弹出（点击无效时自动重新点击触发，自愈）并加载完（.loading 消失）
      2) 按下滑块（mouse.down）
      3) 微动 3px → 截屏前测出 .slide 上沿，作为切图基准
      4) 截屏 → OpenCV 识别 (piece_x, target_x, confidence)
      5) 算还需走的距离 = target_x - piece_x
      6) 继续移动到目标 x（带轨迹模拟，含低置信度兜底尝试）
      7) 松手（mouse.up）
      8) 等 .tncode_msg_ok 显示成功 / .tncode_msg_error 失败
      9) 失败时点 .tncode_refresh 刷新重试
    """
    failure_reasons = []  # 累积每次失败原因，最终透出到推送

    # 入口预检：如果 #tncode 已显示"验证成功"，直接返回（避免重复验证）
    if await _check_tncode_already_passed(page, debug):
        return True, ""

    for attempt in range(1, max_retry + 1):
        _captcha_log(debug, f"=== 尝试 {attempt}/{max_retry} ===")
        # 每轮预检：拖动结果判定误判时本轮再次确认
        if await _check_tncode_already_passed(page, debug):
            return True, ""
        try:
            # 等弹窗真正弹出来（点击无效/资源失败时自动重新点击触发）
            if not await _ensure_tncode_popup(page, debug):
                reason = "验证码弹窗未出现（点击触发无效，已自动重试点击）"
                _captcha_log(debug, reason)
                failure_reasons.append(reason)
                await _shot(page, f"tncode_popup_missing_a{attempt}", debug)
                await _click_refresh(page, debug)
                await asyncio.sleep(2)
                continue

            # 动态等图片真正加载（替代固定 sleep(5)）
            await _wait_loading_gone(page, debug)

            # 找到滑块和验证码弹窗
            slide_block = page.locator(SEL_SLIDE_BLOCK).first
            tncode_div = page.locator(SEL_TNCODE_DIV).first

            block_box = await slide_block.bounding_box()
            div_box = await tncode_div.bounding_box()
            if not block_box or not div_box:
                _captcha_log(debug, "找不到滑块或弹窗位置")
                failure_reasons.append("找不到滑块/弹窗位置")
                return False, "找不到滑块或弹窗位置"

            # 按下前截屏（DEBUG 用）
            if debug:
                png_before = await tncode_div.screenshot()
                ts = int(time.time() * 1000)
                fn_before = f"_browser_debug_{ts}_captcha_a{attempt}_before.png"
                with open(fn_before, "wb") as f:
                    f.write(png_before)
                _captcha_log(debug, f"按下前截屏 {fn_before}")

            # ★ 关键：先按下并微动，触发拼图碎片显示
            start_x = block_box['x'] + block_box['width'] / 2
            start_y = block_box['y'] + block_box['height'] / 2

            await page.mouse.move(start_x, start_y, steps=5)
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.15))

            # 微动 3 像素，确保 mousemove 事件真的触发了
            await page.mouse.move(start_x + 1, start_y, steps=1)
            await asyncio.sleep(0.2)
            await page.mouse.move(start_x + 2, start_y, steps=1)
            await asyncio.sleep(0.2)
            await page.mouse.move(start_x + 3, start_y, steps=1)
            await asyncio.sleep(0.4)  # 让拼图碎片完全渲染

            # 按下后测 .slide 上沿相对 div 的 y，作为切图下边界（动态基准）
            cut_bottom = None
            sbox = None
            try:
                slide_track = page.locator(SEL_SLIDE_TRACK).first
                sbox = await slide_track.bounding_box()
                if sbox and div_box:
                    cut_bottom = int(sbox['y'] - div_box['y'])
                    if cut_bottom <= 0:
                        cut_bottom = None
            except Exception:
                cut_bottom = None
            if cut_bottom is not None:
                _captcha_log(
                    debug,
                    f"切图基准 cut_bottom={cut_bottom} "
                    f"(div y={div_box['y']:.0f}, slide y={sbox['y']:.0f})"
                )
            else:
                _captcha_log(debug, "切图基准 cut_bottom=None（回退 0.78）")

            # 截屏（此时拼图碎片应已显示在弹窗左上角附近）
            png = await tncode_div.screenshot()
            if debug:
                ts = int(time.time() * 1000)
                fn = f"_browser_debug_{ts}_captcha_a{attempt}_pressed.png"
                with open(fn, "wb") as f:
                    f.write(png)
                _captcha_log(debug, f"按下后截屏 {fn}")

            # OpenCV 识别（现在返回三元组）
            piece_x, target_x, conf = solve_slider_from_screenshot(
                png, debug, image_bottom_px=cut_bottom
            )
            if piece_x < 0 or target_x < 0:
                reason = (f"识别失败（置信度 {conf:.2f}）" if conf > 0
                          else "识别失败（未找到拼图/缺口）")
                _captcha_log(debug, reason)
                failure_reasons.append(reason)
                await page.mouse.up()
                await asyncio.sleep(0.3)
                await _click_refresh(page, debug)
                await asyncio.sleep(2)
                continue

            # 计算"鼠标还需要走的距离"
            #   piece_x     碎片内容左沿在弹窗里的 x（已去绿边）
            #   target_x   缺口内容左沿在弹窗里的 x（已去绿边）
            #   鼠标需要继续走的像素数 = target_x - piece_x
            remaining = target_x - piece_x

            _captcha_log(
                debug,
                f"piece_x={piece_x}, target_x={target_x}, "
                f"还需走 {remaining}px (conf={conf:.3f})"
            )

            if remaining <= 0:
                reason = f"distance 异常 ({remaining}, conf={conf:.2f})"
                _captcha_log(debug, f"{reason}，刷新重试")
                failure_reasons.append(reason)
                await page.mouse.up()
                await asyncio.sleep(0.3)
                await _click_refresh(page, debug)
                await asyncio.sleep(2)
                continue

            # 低置信度也统一拖过去（conf < TEMPLATE_THRESHOLD 时靠服务端校验兜底）
            # confidence 已记进日志与 failure_reasons 备用

            # 当前鼠标位置（已经走了 3px）
            current_x = start_x + 3
            current_y = start_y

            # 继续拖动 remaining 像素
            ok = await drag_slider_continue(page, current_x, current_y, remaining, debug)
            if not ok:
                failure_reasons.append("拖动操作异常")
                _captcha_log(debug, "拖动操作异常")
                await page.mouse.up()
                continue

            # 松手前停顿
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.mouse.up()

            # 等结果
            await asyncio.sleep(0.5)
            success = await _wait_captcha_result(page, debug)
            if success:
                _captcha_log(debug, f"✅ 验证码通过 (尝试 {attempt}, conf={conf:.2f})")
                return True, ""

            reason = f"服务端判定未通过 (尝试{attempt}, conf={conf:.2f})"
            failure_reasons.append(reason)
            _captcha_log(debug, f"❌ {reason}，刷新重试")
            await _click_refresh(page, debug)
            await asyncio.sleep(1.5)

        except PWTimeout as e:
            failure_reasons.append(f"超时: {compact_error(e)}")
            _captcha_log(debug, f"超时: {compact_error(e)}")
            try:
                await page.mouse.up()
            except Exception:
                pass
            # 超时多为弹窗/资源未就绪，不重新触发的话后续轮次会白等同一个死弹窗
            await _click_refresh(page, debug)
            await asyncio.sleep(2)
        except Exception as e:
            failure_reasons.append(f"异常: {compact_error(e)}")
            _captcha_log(debug, f"异常: {compact_error(e)}")
            try:
                await page.mouse.up()
            except Exception:
                pass
            if debug:
                traceback.print_exc()

    summary = "；".join(failure_reasons[-3:]) if failure_reasons else "未知原因"
    return False, summary


async def drag_slider_continue(page, current_x: float, current_y: float,
                               remaining_distance: int, debug: bool) -> bool:
    """
    在已经按下滑块的状态下，继续拖动 remaining_distance 像素到目标。
    用人类轨迹（加速-减速 + 过冲回拉 + 微抖动）。
    """
    try:
        if remaining_distance <= 0:
            return True

        trajectory = generate_trajectory(remaining_distance)
        prev_t = 0
        for px, py, t_ms in trajectory:
            await page.mouse.move(current_x + px, current_y + py, steps=1)
            sleep_ms = max(1, t_ms - prev_t)
            await asyncio.sleep(sleep_ms / 1000)
            prev_t = t_ms
        return True
    except Exception as e:
        _captcha_log(debug, f"拖动异常: {e}")
        return False


async def _check_tncode_already_passed(page, debug: bool) -> bool:
    """
    检查 #tncode 触发按钮是否已经显示"验证成功"。
    服务端校验通过后，#tncode 的文字会被替换为"验证成功"，原 ripple 子元素消失。
    """
    try:
        tn = page.locator(SEL_TNCODE_TRIGGER).first
        if await tn.count() == 0:
            return False
        text = (await tn.text_content() or "").strip()
        if "验证成功" in text or "已通过" in text:
            _captcha_log(debug, f"检测到 #tncode 已显示『{text}』，跳过本次验证")
            return True
    except Exception:
        pass
    return False


async def _wait_loading_gone(page, debug: bool, max_wait: float = 15.0) -> bool:
    """
    等 #tncode_box 内的 .loading 元素真正消失（图片加载完成）。
    替代原固定的 5 秒等待——代理慢时最多等 15 秒，
    代理快时 .loading 一消失立即返回，节省多账号累计耗时。
    返回 True 表示加载完成；False 表示超时。
    """
    deadline = time.time() + max_wait
    last_log = 0
    while time.time() < deadline:
        try:
            loading_el = page.locator(f"{SEL_TNCODE_DIV} .loading").first
            if await loading_el.count() == 0:
                return True
            try:
                style = await loading_el.get_attribute("style") or ""
                if "display: none" in style or "display:none" in style:
                    return True
                visible = await loading_el.is_visible(timeout=300)
                if not visible:
                    return True
            except Exception:
                return True
        except Exception:
            return True
        if debug and time.time() - last_log > 3:
            elapsed = time.time() - (deadline - max_wait)
            _captcha_log(debug, f"等图片加载中... ({elapsed:.1f}s)")
            last_log = time.time()
        await asyncio.sleep(0.4)
    return False


async def _wait_tncode_div_visible(page, debug: bool, timeout_ms: int = 3000) -> bool:
    """等 #tncode_box 真正显示（可见），未显示时按 attached 状态分类记日志。"""
    try:
        await page.wait_for_selector(SEL_TNCODE_DIV, timeout=timeout_ms, state="visible")
        return True
    except PWTimeout:
        div = page.locator(SEL_TNCODE_DIV).first
        try:
            if await div.count() > 0:
                _captcha_log(debug, "弹窗 DOM 已存在但未显示（疑似验证码图片/资源加载失败）")
            else:
                _captcha_log(debug, "弹窗 DOM 完全不存在（疑似 tncode JS 未加载或点击无效）")
        except Exception:
            pass
        return False


async def _ensure_tncode_popup(page, debug: bool, max_triggers: int = 3) -> bool:
    """
    确保 tncode 弹窗真正弹出来，返回弹窗是否就绪。

    只点一次 #tncode 就傻等是最大隐患：点击时机过早（JS 未绑完事件）、
    验证码图片资源在代理下加载失败，都会让弹窗永不出现，后续重试轮
    全部白等同一个不存在的弹窗。这里主动等 + 未出现就重新点，自愈。
    """
    if await _check_tncode_already_passed(page, debug):
        return True

    for trigger_round in range(1, max_triggers + 1):
        # 弹窗可能已被前一轮点击拉起，先探再点
        if await _wait_tncode_div_visible(page, debug, timeout_ms=3000):
            return True
        if trigger_round < max_triggers:
            _captcha_log(debug, f"弹窗未出现（第 {trigger_round} 次点击无效），重新点击 #tncode")
            try:
                trigger = page.locator(SEL_TNCODE_TRIGGER).first
                # 短超时：弹窗盖住触发按钮时 click 会干等，久等不如交回外层刷新/重试
                if await trigger.count() > 0 and await trigger.is_visible(timeout=1000):
                    await trigger.click(timeout=5000)
                else:
                    # 触发按钮不可见时退化为刷新路径（内部含重新触发逻辑）
                    await _click_refresh(page, debug)
            except Exception as e:
                _captcha_log(debug, f"重新点击 #tncode 失败: {compact_error(e)}")

    return False


async def _click_refresh(page, debug: bool):
    """点击验证码刷新按钮。"""
    try:
        refresh = page.locator(SEL_TNCODE_REFRESH).first
        if await refresh.count() > 0:
            await refresh.click()
            _captcha_log(debug, "已点击刷新")
        else:
            # 找不到刷新按钮：关闭弹窗 → 重新触发
            close = page.locator(".tncode_close").first
            if await close.count() > 0:
                await close.click()
                await asyncio.sleep(0.5)
            trigger = page.locator(SEL_TNCODE_TRIGGER).first
            if await trigger.count() > 0:
                await trigger.click()
                _captcha_log(debug, "已重新触发验证码")
    except Exception as e:
        _captcha_log(debug, f"刷新失败: {e}")


async def _wait_captcha_result(page, debug: bool, timeout_ms: int = 4000) -> bool:
    """等待验证码结果（成功或失败）。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            # 强信号 1：#tncode 触发按钮文字变成"验证成功"
            tn = page.locator(SEL_TNCODE_TRIGGER).first
            if await tn.count() > 0:
                text = (await tn.text_content() or "").strip()
                if "验证成功" in text or "已通过" in text:
                    return True

            # 强信号 2：#tncode_box 已隐藏（通过即关闭）
            div = page.locator(SEL_TNCODE_DIV).first
            if await div.count() > 0:
                style = await div.get_attribute("style") or ""
                if "display: none" in style:
                    return True

            # 一般信号：tncode_msg_ok 显示
            ok = page.locator(SEL_TNCODE_MSG_OK).first
            if await ok.count() > 0:
                style = await ok.get_attribute("style") or ""
                if "opacity: 0" not in style and "display: none" not in style:
                    inner = (await ok.text_content() or "").strip()
                    if inner or "opacity: 1" in style:
                        return True

            # 失败提示
            err = page.locator(SEL_TNCODE_MSG_ERROR).first
            if await err.count() > 0:
                style = await err.get_attribute("style") or ""
                if "opacity: 0" not in style:
                    err_text = (await err.text_content() or "").strip()
                    if err_text:
                        _captcha_log(debug, f"验证失败提示: {err_text}")
                        return False
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False


# ====================== 7. 业务流程 ======================

async def fetch_sign_stats(page, base_url: str, debug: bool) -> tuple[dict, str]:
    """读取签到页 countqian 统计，失败不影响主签到结果。"""
    base = base_url.rstrip("/")
    wanted_labels = ("连续签到", "签到等级", "积分奖励", "总天数")
    try:
        await page.goto(base + SIGN_PAGE_PATH, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        await dismiss_intro_popups(page, debug)

        ul = page.locator("ul.countqian.cl").first
        await ul.wait_for(state="attached", timeout=8000)

        stats = {}
        lis = page.locator("ul.countqian.cl li")
        count = await lis.count()
        for idx in range(count):
            li = lis.nth(idx)
            try:
                label = await li.locator("h4").first.inner_text(timeout=1000)
                label = re.sub(r"\s+", "", label or "")
                if label not in wanted_labels:
                    continue

                value = await li.locator("input.hidnum").first.get_attribute("value", timeout=1000)
                formatted = _format_countqian_value(label, value or "")
                if formatted:
                    stats[label] = formatted
            except Exception as e:
                if debug:
                    _captcha_log(debug, f"解析第 {idx + 1} 个签到统计项失败: {e}")

        if debug:
            _captcha_log(debug, f"签到统计 DOM 解析结果: {stats}")

        if not stats:
            items = await page.locator("ul.countqian.cl li").all_inner_texts()
            stats = parse_countqian_stats(items)
            if debug:
                _captcha_log(debug, f"签到统计文本兜底原始项: {items}")
                _captcha_log(debug, f"签到统计文本兜底解析结果: {stats}")

        if not stats:
            return {}, "未解析到 countqian 统计字段"
        return stats, ""
    except PWTimeout:
        return {}, "读取签到统计超时"
    except Exception as e:
        if debug:
            traceback.print_exc()
        return {}, f"读取签到统计异常: {compact_error(e, 200)}"


async def dismiss_intro_popups(page, debug: bool):
    """
    关闭老王FIP进站时的两层弹窗：
      1) 语言选择弹窗：#zh-cvt-cn（简体中文）/ #zh-cvt-tw / #zh-cvt-skip
      2) 重要提示弹窗：.btn_c（一天内不再提醒）/ .btn（关闭）

    两层都按"先点掉简体中文 → 等 → 点掉一天内不再提醒"顺序处理。
    弹窗可能不出现（cookie 已记忆），找不到就跳过，不报错。
    """
    # 第一层：语言选择
    for sel in ("#zh-cvt-cn", "#zh-cvt-skip"):
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible(timeout=1000):
                await btn.click()
                if debug:
                    print(f"   [popup] 关闭语言选择弹窗 ({sel})")
                await asyncio.sleep(0.5)
                break
        except Exception:
            continue

    # 第二层：重要提示
    for sel in (".btn_c", "button[onclick='clos_no_day()']",
                "button[onclick='close_ls()']", ".modal-footer .btn"):
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible(timeout=1000):
                await btn.click()
                if debug:
                    print(f"   [popup] 关闭重要提示弹窗 ({sel})")
                await asyncio.sleep(0.5)
                break
        except Exception:
            continue

    # 兜底：年龄确认弹窗（部分页面模板仍会弹出）
    try:
        agree = page.locator(
            "button:has-text('我已满'), button:has-text('同意'), .agree-btn"
        ).first
        if await agree.count() > 0 and await agree.is_visible(timeout=500):
            await agree.click()
            if debug:
                print(f"   [popup] 关闭年龄确认弹窗")
            await asyncio.sleep(0.5)
    except Exception:
        pass


async def login_with_browser(page, base_url: str, username: str, password: str,
                             debug: bool, max_captcha_retry: int) -> tuple:
    """
    浏览器内完成登录。
    返回 (success: bool, msg: str)。
    """
    base = base_url.rstrip("/")

    try:
        # 直接进 Discuz 登录页
        await page.goto(base + LOGIN_PAGE_PATH, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        await _shot(page, "after_goto", debug)

        # 导航失败：页面已切到 Chromium 错误页（网络/代理加载失败）。
        # 立即返回明确的网络错误以触发重试，避免继续走「等输入框」的误导性长失败路径。
        if await is_chromium_error_page(page):
            return False, "页面加载失败（连接中断，Chromium 错误页 chrome-error://）"

        # 关闭进站弹窗（语言选择 + 重要提示）
        await dismiss_intro_popups(page, debug)
        await _shot(page, "popups_dismissed", debug)

        # 等用户名输入框出现（最多 15 秒）
        try:
            await page.wait_for_selector(SEL_USERNAME_INPUT, timeout=15000, state="visible")
        except PWTimeout:
            await _shot(page, "no_username_input", debug)
            return False, "找不到用户名输入框（页面可能未加载完，或弹窗未关掉）"

        # 填账号密码（用 type 模拟人类输入，比 fill 更不容易被检测）
        username_input = page.locator(SEL_USERNAME_INPUT).first
        await username_input.click()
        await username_input.fill("")  # 先清空
        await username_input.type(username, delay=random.randint(50, 120))
        await asyncio.sleep(random.uniform(0.2, 0.5))

        password_input = page.locator(SEL_PASSWORD_INPUT).first
        await password_input.click()
        await password_input.fill("")
        await password_input.type(password, delay=random.randint(50, 120))
        await asyncio.sleep(random.uniform(0.3, 0.7))
        await _shot(page, "filled_creds", debug)

        # 点 tncode 触发按钮
        tncode_trigger = page.locator(SEL_TNCODE_TRIGGER).first
        try:
            await tncode_trigger.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            return False, "找不到 #tncode 触发按钮（页面可能不完整）"
        await tncode_trigger.click()
        await asyncio.sleep(0.5)

        # 过验证码
        captcha_ok, captcha_reason = await pass_tncode_in_browser(
            page, debug, max_retry=max_captcha_retry
        )
        if not captcha_ok:
            await _shot(page, "captcha_failed", debug)
            return False, f"验证码识别失败（{captcha_reason}）"

        await _shot(page, "captcha_passed", debug)

        # 点登录提交按钮
        submit = page.locator(SEL_LOGIN_SUBMIT).first
        try:
            await submit.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            # 退化：找包含"登录"文字的按钮
            submit = page.locator("button:has-text('登 录'), button:has-text('登录')").first
            try:
                await submit.wait_for(state="visible", timeout=3000)
            except PWTimeout:
                return False, "找不到登录提交按钮"
        await submit.click()

        # 等跳转或错误提示（最多 8 秒）
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except PWTimeout:
            pass
        await asyncio.sleep(2)
        await _shot(page, "after_submit", debug)

        # 跳转后可能再次弹出进站弹窗，再关一遍
        await dismiss_intro_popups(page, debug)

        # 判断登录成功（收紧判定：Discuz 登录失败跳回 forum.php 时不得误判成功）
        page_text = await page.content()
        cur_url = page.url

        # 仍在登录相关页面 → 失败（无论是否提示错误，未离开登录页即视为失败）
        is_on_login_page = (
            "logging" in cur_url or "login" in cur_url or "member.php" in cur_url
        )

        # 强登录态标志
        strong_logged_in = (
            "欢迎您" in page_text
            or "退出</a>" in page_text
            or "user_notice" in page_text
        )

        # 1) 强标志命中 → 成功
        if strong_logged_in:
            return True, "登录成功"

        # 2) 仍在登录页 → 一定失败
        if is_on_login_page:
            # 失败信息抓取
            err_match = re.search(r'errorhandle_login\([^)]*[\'"]([^\'"]+)[\'"]', page_text)
            if err_match:
                return False, f"登录失败: {err_match.group(1)}"
            if "密码错误" in page_text:
                return False, "登录失败: 密码错误"
            if "用户名" in page_text and "不存在" in page_text:
                return False, "登录失败: 用户名不存在"
            if "验证码" in page_text and ("错误" in page_text or "失败" in page_text):
                return False, "登录失败: 验证码未通过"
            if "登录失败" in page_text:
                m = re.search(r"登录失败[，,。:：]?\s*([^<\n]{1,80})", page_text)
                if m:
                    return False, f"登录失败: {m.group(1).strip()}"
                return False, "登录失败（未提取到具体原因）"
            return False, f"登录可能失败（仍在登录页 {cur_url[:80]}）"

        # 3) 已离开登录页但无强标志 → 检查是否跳到了带错误提示的论坛页
        #    （Discuz 失败重定向也可能落 forum.php，需排除）
        fail_hints = ("登录失败", "密码错误", "密码不正确", "用户不存在", "验证码")
        if any(h in page_text for h in fail_hints):
            err_match = re.search(r'errorhandle_login\([^)]*[\'"]([^\'"]+)[\'"]', page_text)
            if err_match:
                return False, f"登录失败: {err_match.group(1)}"
            m = re.search(r"登录失败[，,。:：]?\s*([^<\n]{1,80})", page_text)
            if m:
                return False, f"登录失败: {m.group(1).strip()}"
            return False, "登录失败（跳转后页面含失败提示）"

        # 4) 已离开登录页且无失败提示 → 视为成功
        return True, f"登录成功（跳转到 {cur_url[:60]}）"
    except PWTimeout as e:
        return False, f"登录超时: {compact_error(e, 200)}"
    except Exception as e:
        if debug:
            traceback.print_exc()
        return False, f"登录异常: {compact_error(e, 200)}"


async def sign_with_browser(page, base_url: str, debug: bool,
                             max_captcha_retry: int) -> dict:
    """
    浏览器内完成签到。

    实测流程（laowangfip372.vip k_misign 插件）：
      1) 进入签到页 /k_misign-sign.html
      2) 找签到按钮 <a class="J_chkitot" href="plugin.php?id=k_misign:sign&...qiandao...">
      3) 已签到按钮通常 class 含 disabled
      4) 点击签到按钮 → 跳转到 plugin.php 验证码独立页面
         （表单 #v2_captcha_form，含 #tncode + 隐藏字段 + 提交按钮 #submit-btn）
      5) 点 #tncode → 弹滑块 → 过滑块（沿用 pass_tncode_in_browser）
      6) 点 #submit-btn 提交表单
      7) 服务端验证后跳回签到页或论坛，解析结果文字
    """
    base = base_url.rstrip("/")

    try:
        # 1) 进入签到首页 /plugin.php?id=k_misign:sign
        await page.goto(base + SIGN_PAGE_PATH, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

        # 导航失败：页面已切到 Chromium 错误页（网络/代理加载失败），返回网络错误以触发重试
        if await is_chromium_error_page(page):
            return {"success": False, "error": "页面加载失败（连接中断，Chromium 错误页 chrome-error://）"}

        # 签到页也可能弹"重要提示"，先关掉
        await dismiss_intro_popups(page, debug)
        await _shot(page, "sign_page", debug)

        # 2) 优先检测"已签到"标识：<span class="btn btnvisted"> 表示今日已签
        btnvisted = page.locator("span.btnvisted").first
        if await btnvisted.count() > 0:
            _captcha_log(debug, "检测到 span.btn.btnvisted，今日已签到")
            return {"success": True, "message": "今日已签到（btnvisted）"}

        # 3) 找 J_chkitot 签到按钮 —— 必须严格匹配 href 含 operation=qiandao&formhash=
        #    （formhash 是随机的，仅做存在性判断；不用全文 "已签到" 字符串匹配，避免误判排行榜文案）
        sign_btn = page.locator("a.J_chkitot[href*='operation=qiandao']").first
        if await sign_btn.count() == 0:
            # 退化：放宽到任何包含 qiandao 的链接
            sign_btn = page.locator("a[href*='operation=qiandao']").first
        if await sign_btn.count() == 0:
            # 仍找不到 → 检查页面是否已是"已签到"状态
            page_text = await page.content()
            if any(kw in page_text for kw in ("您今日已签到", "今日已经签到", "今天已经签到")):
                return {"success": True, "message": "今日已签到（页面状态）"}
            return {"success": False, "error": "找不到 J_chkitot 签到按钮（可能页面结构变化）"}

        # 已签到时按钮通常含 disabled / 不可点击
        cls = await sign_btn.get_attribute("class") or ""
        if "disabled" in cls.lower():
            return {"success": True, "message": "今日已签到（按钮禁用）"}

        # 取出按钮的真实 href，用于：① 验证跳转目标 ② 提取 formhash
        sign_href = await sign_btn.get_attribute("href") or ""
        formhash_match = re.search(r"formhash=([a-fA-F0-9]+)", sign_href)
        formhash = formhash_match.group(1) if formhash_match else ""
        _captcha_log(debug, f"签到按钮 href={sign_href[:120]}, formhash={formhash}")

        # 3) 点击签到按钮 → 等导航到验证码独立页
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                await sign_btn.click()
        except PWTimeout:
            # 部分主题点击后是 ajax 不导航，宽容处理
            _captcha_log(debug, "签到点击后未发生整页导航，继续按当前页面处理")
        await asyncio.sleep(1.5)
        await _shot(page, "after_sign_click", debug)

        cur_url = page.url
        _captcha_log(debug, f"签到点击后到达 {cur_url}")

        # 4) 优先确认 #v2_captcha_form 表单存在 —— 这是真正的验证页标志
        captcha_form = page.locator("#v2_captcha_form").first
        tncode_trigger = page.locator(SEL_TNCODE_TRIGGER).first
        submit_btn = page.locator("#submit-btn").first

        try:
            await captcha_form.wait_for(state="attached", timeout=8000)
        except PWTimeout:
            # 没看到验证表单 → 可能直接成功，或弹了错误页
            _captcha_log(debug, "签到后未出现 v2_captcha_form，检查是否已成功或报错")
            result_text = await page.content()
            for kw in ("签到成功", "签到完成", "连续签到天数", "您今日已签到"):
                if kw in result_text:
                    return {"success": True, "message": kw}
            # 抓错误信息
            err_match = re.search(r"(操作失败|提示|错误)[：:][^<\n]{0,80}", result_text)
            if err_match:
                return {"success": False, "error": f"签到失败: {err_match.group(0)[:120]}"}
            return {"success": False, "error": f"签到后未到达验证页（当前 {cur_url[:80]}）"}

        # 5) 确认验证码触发按钮可点
        try:
            await tncode_trigger.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            return {"success": False, "error": "验证页加载但 #tncode 触发按钮未显示"}

        # 点 #tncode 触发滑块弹窗
        await tncode_trigger.click()
        await asyncio.sleep(0.5)

        # 过滑块（沿用现有函数）
        captcha_ok, captcha_reason = await pass_tncode_in_browser(
            page, debug, max_retry=max_captcha_retry
        )
        if not captcha_ok:
            await _shot(page, "sign_captcha_failed", debug)
            return {"success": False, "error": f"签到验证码识别失败（{captcha_reason}）"}

        await asyncio.sleep(0.8)
        await _shot(page, "sign_captcha_passed", debug)

        # 验证码通过后点击"提交"按钮
        if await submit_btn.count() == 0:
            # 退化：找含"提交"文字的 button
            submit_btn = page.locator("button:has-text('提交'), button[type='submit']").first
        if await submit_btn.count() == 0:
            return {"success": False, "error": "找不到签到提交按钮"}

        try:
            await submit_btn.wait_for(state="visible", timeout=3000)
        except PWTimeout:
            pass

        await submit_btn.click()

        # 等服务端处理 + 跳转
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except PWTimeout:
            pass
        await asyncio.sleep(2)
        await _shot(page, "after_sign_submit", debug)

        result_text = await page.content()
        result_url = page.url
        _captcha_log(debug, f"签到提交后跳转到 {result_url}")

        # 强成功标志：跳回 k_misign 签到页 / forum.php，且页面出现已签到字样
        for kw in ("签到成功", "签到完成", "连续签到", "今日已签到", "已签到", "已经签到", "您今日已"):
            if kw in result_text:
                return {"success": True, "message": kw}

        # 积分增加提示
        if "积分" in result_text and re.search(r"\+\d+", result_text):
            return {"success": True, "message": "签到完成（积分已增加）"}

        # 仍在 plugin.php 验证页 = 验证码没过
        if "v2_captcha_form" in result_text or "captcha" in result_url:
            return {"success": False, "error": "提交后仍在验证页（验证码可能未真正通过）"}

        # 错误信息提取
        err_match = re.search(r"(失败|错误|请重试)[^<\n]{0,40}", result_text)
        if err_match:
            return {"success": False, "error": f"签到失败: {err_match.group(0)[:100]}"}

        # 跳回签到页或论坛但没明确文字 → 多半是成功了
        if "k_misign" in result_url or result_url.endswith("/forum.php"):
            return {"success": True, "message": f"签到完成（跳转到 {result_url[:60]}）"}

        return {"success": False, "error": f"签到结果未知（最终 URL: {result_url[:80]}）"}
    except PWTimeout as e:
        return {"success": False, "error": f"签到超时: {compact_error(e, 200)}"}
    except Exception as e:
        if debug:
            traceback.print_exc()
        return {"success": False, "error": f"签到异常: {compact_error(e, 200)}"}


# ====================== 8. 单账号编排 ======================

async def run_one_account(username: str, password: str, base_url: str, proxy: str,
                          headless: bool, timeout_ms: int, debug: bool,
                          max_captcha_retry: int, chromium_path: str,
                          use_cookie_cache: bool = True) -> dict:
    """登录 + 签到一条龙。已存会话 Cookie 时直通签到（跳过登录与滑块）。"""
    async with async_playwright() as p:
        try:
            launch_kwargs = {
                "headless": headless,
                "proxy": {"server": proxy} if proxy else None,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            }
            if chromium_path:
                launch_kwargs["executable_path"] = chromium_path
            # 过滤 None 值
            launch_kwargs = {k: v for k, v in launch_kwargs.items() if v is not None}

            browser = await p.chromium.launch(**launch_kwargs)
        except Exception as e:
            err_msg = str(e)[:300]
            hint = ""
            if "Executable doesn't exist" in err_msg or "doesn't exist" in err_msg:
                hint = (
                    "\n\n💡 解决方案（在青龙容器内执行）："
                    "\n  docker exec -it qinglong bash"
                    "\n  apt update && apt install -y chromium"
                    "\n  或设置环境变量 LWFIP_CHROMIUM_PATH=/usr/bin/chromium"
                )
            return {"success": False, "error": f"浏览器启动失败: {err_msg}{hint}"}

        try:
            context = await browser.new_context(
                user_agent=DEFAULT_UA,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            # 反检测
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)

            # 同意弹窗 cookie 预设（避免每次首跑被弹"年龄确认"挡住；域名动态提取，换域名仍生效）
            cookie_domain = extract_cookie_domain(base_url)
            await context.add_cookies([
                {"name": "is_agree", "value": "1", "domain": cookie_domain, "path": "/"},
                {"name": "yd_is_check", "value": "verified", "domain": cookie_domain, "path": "/"},
            ])

            # 会话 Cookie 复用：命中且登录态有效则直通签到，跳过登录 + 滑块
            # （滑块是最大失败源，复用登录态可大幅提升成功率并降低登录频率防风控）
            logged_in = False
            if use_cookie_cache:
                cached_cookies = load_cookies_from_cache(username)
                if cached_cookies:
                    try:
                        await context.add_cookies(cached_cookies)
                        await page.goto(
                            base_url + SIGN_PAGE_PATH,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await asyncio.sleep(1)
                        await dismiss_intro_popups(page, debug)
                        logged_in = await is_logged_in(page)
                        if logged_in:
                            print(f"🍪 {mask_account(username)} 命中会话 Cookie，直通签到")
                        else:
                            # 登录态已失效：清空并删除缓存，走完整登录
                            print(f"🍪 {mask_account(username)} Cookie 已失效，重新登录")
                            await context.clear_cookies()
                            remove_cookie_cache(username)
                    except Exception as e:
                        if debug:
                            traceback.print_exc()
                        print(f"⚠️ Cookie 复用失败（{compact_error(e, 100)}），走完整登录")
                        try:
                            await context.clear_cookies()
                        except Exception:
                            pass
                        remove_cookie_cache(username)
                        logged_in = False

            if not logged_in:
                # 登录
                ok, login_msg = await login_with_browser(
                    page, base_url, username, password, debug, max_captcha_retry
                )
                if not ok:
                    return {"success": False, "error": login_msg}
                # 登录成功 → 会话 Cookie 落盘（保存失败不影响签到）
                if use_cookie_cache:
                    await save_cookies_to_cache(context, username)

            # 签到
            sign_result = await sign_with_browser(page, base_url, debug, max_captcha_retry)

            # 签到成功后读取统计；读取失败不影响主签到结果。
            if sign_result.get("success"):
                stats, stats_error = await fetch_sign_stats(page, base_url, debug)
                if stats:
                    sign_result["stats"] = stats
                if stats_error:
                    sign_result["stats_error"] = stats_error

            return sign_result
        except Exception as e:
            if debug:
                traceback.print_exc()
            return {"success": False, "error": f"任务异常: {str(e)[:300]}"}
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# ====================== 9. 主流程 ======================

async def amain():
    title = "老王FIP签到(浏览器版)"
    print("=" * 60)
    print(f"🚀 {title}  开始执行  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    accounts_raw = os.getenv("LWFIP_ACCOUNTS", "").strip()
    proxy, proxy_source = resolve_proxy("LWFIP_PROXY")
    base_url = (os.getenv("LWFIP_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    notify_enabled = env_bool("LWFIP_NOTIFY", True)
    timeout_ms = env_int("LWFIP_TIMEOUT", 30000)
    debug = env_bool("LWFIP_DEBUG", False)
    headful = env_bool("LWFIP_HEADFUL", False)
    chromium_path = (os.getenv("LWFIP_CHROMIUM_PATH") or "").strip()
    max_captcha_retry = env_int("LWFIP_MAX_CAPTCHA_RETRY", 5)
    notify_only_fail = env_bool("LWFIP_NOTIFY_ONLY_FAIL", False)
    max_retry = env_int("LWFIP_MAX_RETRY", MAX_RETRY)
    retry_interval = env_int("LWFIP_RETRY_INTERVAL", RETRY_INTERVAL_SEC)
    use_cookie_cache = env_bool("LWFIP_COOKIE_CACHE", True)

    # 自动搜索系统 Chromium（青龙容器内 Playwright 自带版本常因网络装不上）
    if not chromium_path:
        _system_chromium_paths = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/snap/bin/chromium",
        ]
        for _p in _system_chromium_paths:
            if os.path.isfile(_p):
                chromium_path = _p
                print(f"🔍 自动检测到系统 Chromium: {chromium_path}")
                break
    if not chromium_path:
        # 再用 which/where 找
        for _name in ("chromium", "chromium-browser", "google-chrome"):
            _found = shutil.which(_name)
            if _found:
                chromium_path = _found
                print(f"🔍 自动检测到系统 Chromium: {chromium_path}")
                break

    # 代理校验：缺失代理默认降级为警告直连，仅在显式开启严格模式时退出
    if not proxy:
        msg = (
            "⚠️ 未配置 LWFIP_PROXY，也未检测到青龙全局代理，将直连尝试。\n"
            "   老王FIP 站点被墙，直连大概率失败，请至少二选一：\n"
            "     · 设置 LWFIP_PROXY=http://172.17.0.1:7890\n"
            "     · 在青龙「配置文件 / 环境变量」配置全局代理（HTTPS_PROXY 等）\n"
            "   如需缺代理即报错退出，设置 LWFIP_PROXY_REQUIRED=1。"
        )
        print(msg)
        if env_bool("LWFIP_PROXY_REQUIRED", False):
            if notify_enabled:
                await send_notify_with_retry(title, msg)
            sys.exit(1)

    accounts = parse_credentials(accounts_raw)
    if not accounts:
        msg = (
            "⚠️ 未配置 LWFIP_ACCOUNTS。\n"
            "  LWFIP_ACCOUNTS = 用户名#密码  （多账号 & 或换行分隔）"
        )
        print(msg)
        if notify_enabled:
            await send_notify_with_retry(title, msg)
        sys.exit(1)

    if not proxy:
        proxy_desc = "未配置（直连）"
    elif proxy_source == "LWFIP_PROXY":
        proxy_desc = proxy
    else:
        proxy_desc = f"{proxy}（来源: {proxy_source}）"
    print(f"📋 共 {len(accounts)} 个账号 | 代理: {proxy_desc} | 站点: {base_url}")
    print(f"   debug={debug}, headful={headful}, timeout={timeout_ms}ms, "
          f"captcha_retry={max_captcha_retry}, task_retry={max_retry}次/间隔{retry_interval}s")
    print(f"   cookie_cache={use_cookie_cache}")
    if chromium_path:
        print(f"   Chromium: {chromium_path}")
    print()

    results = []
    success_cnt = 0

    for idx, (u, p) in enumerate(accounts, start=1):
        masked = mask_account(u)
        print(f"[{idx}/{len(accounts)}] 🔄 {masked} 开始登录+签到...")

        result = None
        final_ok = False
        final_msg = ""

        for attempt in range(1, max_retry + 1):
            if attempt > 1:
                print(f"           ⏳ 第 {attempt}/{max_retry} 次重试，等待 {retry_interval} 秒...")
                await asyncio.sleep(retry_interval)
                print(f"           🔄 第 {attempt}/{max_retry} 次重试开始")

            t0 = time.time()
            try:
                result = await run_one_account(
                    u, p, base_url, proxy,
                    headless=not headful,
                    timeout_ms=timeout_ms,
                    debug=debug,
                    max_captcha_retry=max_captcha_retry,
                    chromium_path=chromium_path,
                    use_cookie_cache=use_cookie_cache,
                )
            except Exception as e:
                if debug:
                    traceback.print_exc()
                result = {"success": False, "error": f"主流程异常: {str(e)[:300]}"}
            cost = time.time() - t0

            ok = bool(result.get("success"))
            if ok:
                success_cnt += 1
                final_ok = True
                final_msg = result.get("message", "签到成功")
                print(f"           ✅ {final_msg}  (耗时 {cost:.1f}s)")
                break  # 成功，跳出重试循环

            err = result.get("error", "未知错误")
            print(f"           ❌ {err}  (耗时 {cost:.1f}s)")

            # 判断是否需要重试
            if attempt < max_retry and is_retryable_error(err):
                print("           🔄 检测到可重试错误，准备重试...")
                continue
            else:
                # 不可重试的错误，或已到最后一次
                final_ok = False
                final_msg = err
                break

        results.append({
            "username": masked,
            "success": final_ok,
            "message": final_msg,
            "stats": (result or {}).get("stats", {}),
            "stats_error": (result or {}).get("stats_error", ""),
        })

        if idx < len(accounts):
            await asyncio.sleep(random.uniform(3, 8))

    # 汇总（标题按成败分档 ✅全成功 / ⚠️部分失败 / ❌全失败）
    total = len(accounts)
    fail_cnt = total - success_cnt
    if total > 0 and success_cnt == total:
        status_icon = "✅"
    elif success_cnt == 0:
        status_icon = "❌"
    else:
        status_icon = "⚠️"
    notify_title = f"{status_icon} {title}"

    lines = [f"# {notify_title} - 执行报告", ""]
    lines.append(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"📊 总计 {total} 账号，✅ {success_cnt} / ❌ {fail_cnt}")
    lines.append("\n## 详细结果")
    for r in results:
        icon = "✅" if r["success"] else "❌"
        lines.append(f"- {icon} **[{r['username']}]** {r['message']}")

        stats = r.get("stats") or {}
        if stats:
            ordered = []
            for key in ("连续签到", "签到等级", "积分奖励", "总天数"):
                if key in stats:
                    ordered.append(f"{key}: {stats[key]}")
            if ordered:
                lines.append(f"  - 签到统计：{'；'.join(ordered)}")
        elif r.get("stats_error"):
            lines.append(f"  - 签到统计：读取失败（{r['stats_error']}）")
    report = "\n".join(lines)

    print()
    print("=" * 60)
    print(report)
    print("=" * 60)

    # 仅失败才推送门控
    should_notify = notify_enabled and (not notify_only_fail or fail_cnt > 0)
    if not should_notify:
        if notify_enabled and notify_only_fail and fail_cnt == 0:
            print("ℹ️ 全部成功且已开启 ONLY_FAIL，跳过推送")
    else:
        await send_notify_with_retry(notify_title, report)

    sys.exit(0 if success_cnt > 0 else 1)


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n⚠️ 用户中断")
        sys.exit(2)


if __name__ == "__main__":
    main()
