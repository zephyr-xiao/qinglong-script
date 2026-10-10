# -*- coding: utf-8 -*-
"""pytest 公共配置：把脚本目录加入 sys.path，并为 patchright 装占位模块。

单测只覆盖纯函数（不启动真实浏览器），但脚本模块顶层会 import patchright
并在缺失时 sys.exit(1)，因此必须在导入脚本之前注入占位模块。
"""
import sys
import types
from pathlib import Path

# 脚本目录（tests 的父目录）加入 sys.path，使 `import whos_tv_checkin` 可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if "patchright" not in sys.modules:
    _patchright = types.ModuleType("patchright")
    _sync_api = types.ModuleType("patchright.sync_api")
    _sync_api.sync_playwright = lambda *args, **kwargs: None
    _patchright.sync_api = _sync_api
    sys.modules["patchright"] = _patchright
    sys.modules["patchright.sync_api"] = _sync_api
