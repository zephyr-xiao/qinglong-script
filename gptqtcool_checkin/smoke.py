#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地冒烟脚本：在非青龙环境下跑一次真实流程，验证脚本可用。

用法（Git Bash / PowerShell）::

    export GPTQTCOOL_KEY="sk-user-xxxx"     # 必填；只作为环境变量，不写入任何文件
    python smoke.py                         # 先自检，再真跑一次签到
    python smoke.py --check-only            # 只自检，不签到
    HEADLESS=false python smoke.py          # 显示浏览器窗口（便于肉眼观察滑块）

说明:
  - 会真实访问站点；若今日已签到会幂等跳过（除非 GPTQTCOOL_FORCE_RUN=true）
  - 默认把截图目录指向临时目录，避免污染项目目录
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 保证脚本目录可 import core
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 冒烟默认不往项目里写截图
os.environ.setdefault("GPTQTCOOL_SCREENSHOT_DIR",
                      os.path.join(tempfile.gettempdir(), "gptqtcool_smoke_artifacts"))

from core import flow  # noqa: E402
from core.config import GPTQTCOOL_KEY, GPTQTCOOL_SCREENSHOT_DIR, logger  # noqa: E402


async def _run() -> int:
    if not GPTQTCOOL_KEY:
        print("❌ 未设置 GPTQTCOOL_KEY 环境变量（export GPTQTCOOL_KEY=... 后再运行）")
        return 2

    print(f"ℹ️ 截图目录: {GPTQTCOOL_SCREENSHOT_DIR}")

    rc = await flow.self_check()
    if rc != 0:
        print("❌ 自检未通过，终止冒烟")
        return rc
    if "--check-only" in sys.argv:
        return 0

    logger.info("========== 🧪 冒烟：执行一次真实签到 ==========")
    result = await flow.auto_checkin_with_timeout()
    print("\n========== 冒烟结果 ==========")
    for k, v in result.items():
        print(f"  {k}: {v}")
    print("==============================\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
