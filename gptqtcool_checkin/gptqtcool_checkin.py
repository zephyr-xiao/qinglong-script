#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new Env('gpt.qt.cool 签到');
cron: 35 8 * * *

gpt.qt.cool 自动签到 - 青龙面板版
基于 Playwright + OpenCV 的滑动验证码识别

环境变量:
  GPTQTCOOL_KEY               必填,签到网站 API Key
  GPTQTCOOL_FORCE_RUN         可选,强制执行(忽略今日已签到),默认 false
  GPTQTCOOL_LOG_LEVEL         可选,日志级别(error/warn/info/debug),默认 info
  GPTQTCOOL_SCREENSHOT_DIR    可选,截图输出目录,默认 artifacts
  GPTQTCOOL_TIMEOUT           可选,页面加载超时(毫秒),默认 30000
  GPTQTCOOL_RETRY_ROUNDS      可选,整轮重试次数(重载页面重来),默认 3
  GPTQTCOOL_MAX_RUNTIME_MIN   可选,单次运行总时长上限(分钟),默认 12
  GPTQTCOOL_NOTIFY            可选,是否推送通知,默认 true
  GPTQTCOOL_NOTIFY_ONLY_FAIL  可选,仅在失败时推送,默认 false
  GPTQTCOOL_SERVERPUSHKEY     可选,Server酱 Turbo KEY,默认复用青龙 SERVERPUSHKEY
  HEADLESS                    可选,浏览器无头模式,默认 true;本地调试可设 false

用法:
  python3 gptqtcool_checkin.py            # 正常签到
  python3 gptqtcool_checkin.py --check    # 自检(不签到,只验证环境与登录态)

依赖:
  pip install playwright opencv-python numpy
  playwright install chromium

代码结构:核心逻辑在 core/ 子包(子包用于避免模块名遮蔽青龙自带 notify.py):
  core/config.py  配置/日志/环境变量
  core/notify.py  通知推送
  core/detect.py  页面检测与缺口定位(纯算法可单测)
  core/slider.py  滑块轨迹与验证码处理
  core/auth.py    登录与会话持久化
  core/flow.py    主流程/结果判定/自检
"""

import sys
from pathlib import Path

# UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 问题，Linux UTF-8 容器无副作用）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 保证脚本目录在 sys.path 上：青龙以 `task gptqtcool_checkin/gptqtcool_checkin.py`
# 方式运行时 sys.path[0] 已是脚本目录，这里再兜一层，确保 core 包始终可 import
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ========== 第三方依赖检查（在 import core 之前，缺依赖时给友好提示） ==========
try:
    import playwright  # noqa: F401
except ImportError:
    print("❌ 缺少 playwright,请执行: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
except ImportError:
    print("❌ 缺少 opencv-python/numpy,请执行: pip install opencv-python numpy")
    sys.exit(1)

import asyncio  # noqa: E402
import traceback  # noqa: E402

from core import flow  # noqa: E402
from core.config import GPTQTCOOL_KEY, NOTIFY, logger  # noqa: E402


async def main() -> None:
    # 自检模式：不执行签到，只验证环境与登录态
    if "--check" in sys.argv:
        sys.exit(await flow.self_check())

    if not GPTQTCOOL_KEY:
        print("❌ 未设置 GPTQTCOOL_KEY 环境变量")
        sys.exit(1)
    try:
        logger.info("========== 🚀 开始签到(青龙 Python 版) ==========")
        result = await flow.auto_checkin_with_timeout()
        if result.get("ok"):
            if result.get("stage") == "already":
                logger.info("✅ 今日已签到(幂等命中)")
            else:
                logger.info("✅ 签到成功")
        else:
            logger.info(f"❌ 签到失败(阶段: {result.get('stage')})")
        flow.notify_result(result)
        sys.exit(0 if result.get("ok") else 1)
    except Exception as e:
        logger.error(f"❌ 签到失败: {e}")
        logger.error(traceback.format_exc())
        if NOTIFY:
            flow.send_notify("❌ gpt.qt.cool 签到异常", f"签到过程异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
