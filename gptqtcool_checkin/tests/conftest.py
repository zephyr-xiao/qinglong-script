# -*- coding: utf-8 -*-
"""pytest 公共配置：把脚本目录加入 sys.path，并把截图目录指向临时路径。

必须在 import core.* 之前设置环境变量，因此放在 conftest 顶层。
"""
import os
import sys
import tempfile
from pathlib import Path

# 截图目录改到临时目录，避免单测在仓库里生成 artifacts/
os.environ.setdefault("GPTQTCOOL_SCREENSHOT_DIR", os.path.join(tempfile.gettempdir(), "gptq_test_artifacts"))

# 脚本目录（core 包的父目录）加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
