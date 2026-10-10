# -*- coding: utf-8 -*-
"""配置、日志与环境变量解析。

本模块是 core 包的最底层，不 import 其它 core 模块，任何模块都可安全依赖它。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# UTF-8 强制重配（解决 Windows GBK / 部分容器 locale 问题；Linux UTF-8 容器无副作用）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def env_bool(name: str, default: bool) -> bool:
    """解析布尔型环境变量(1/true/yes/y/on 视为真)"""
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    """解析整型环境变量,非法值回退默认"""
    try:
        return int(os.getenv(name) or default)
    except Exception:
        return default


# ====================== 站点与账号 ======================
GPTQTCOOL_KEY = os.getenv("GPTQTCOOL_KEY", "").strip()
# 必填校验放在入口 main()，import 阶段不应直接退出（便于单测与自检）
CHECKIN_URL = "https://gpt.qt.cool/checkin"

# ====================== 运行行为 ======================
GPTQTCOOL_FORCE_RUN = env_bool("GPTQTCOOL_FORCE_RUN", False)
GPTQTCOOL_LOG_LEVEL = os.getenv("GPTQTCOOL_LOG_LEVEL", "info").strip().upper()
GPTQTCOOL_SCREENSHOT_DIR = Path(os.getenv("GPTQTCOOL_SCREENSHOT_DIR", "artifacts"))
GPTQTCOOL_TIMEOUT = env_int("GPTQTCOOL_TIMEOUT", 30000)  # 页面加载超时（毫秒）
NOTIFY = env_bool("GPTQTCOOL_NOTIFY", True)
NOTIFY_ONLY_FAIL = env_bool("GPTQTCOOL_NOTIFY_ONLY_FAIL", False)
HEADLESS = env_bool("HEADLESS", True)

# 整轮重试：验证码失败/结果不明时，重载页面重新走一遍签到，最多 RETRY_ROUNDS 轮；
# MAX_RUNTIME_MIN 封顶单次运行总时长，避免青龙任务长时间卡死
RETRY_ROUNDS = max(1, env_int("GPTQTCOOL_RETRY_ROUNDS", 3))
MAX_RUNTIME_MIN = max(1, env_int("GPTQTCOOL_MAX_RUNTIME_MIN", 12))

# 登录态持久化文件（Playwright storage_state：含 cookie + localStorage）
# 放在脚本目录（core/ 的上一级），随青龙数据卷持久化
STATE_FILE = Path(__file__).resolve().parent.parent / "gptqtcool_state.json"

# ====================== 日志 ======================
logging.basicConfig(
    level=getattr(logging, GPTQTCOOL_LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("checkin")

GPTQTCOOL_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
