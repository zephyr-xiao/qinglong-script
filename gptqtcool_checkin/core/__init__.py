# -*- coding: utf-8 -*-
"""gpt.qt.cool 签到脚本的内部包。

放在子包内是为了避免模块名（尤其是 ``notify``）遮蔽青龙自带的同名模块：
脚本目录会进 ``sys.path[0]``，若在顶层放一个 ``notify.py``，
``from notify import send`` 会命中本包而非青龙的 ``notify.py``，
导致通知兜底失效。子包内的模块名只在 ``core.`` 命名空间下解析，不会污染顶层。
"""
