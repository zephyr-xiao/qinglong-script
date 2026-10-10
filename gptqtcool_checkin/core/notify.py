# -*- coding: utf-8 -*-
"""通知推送。

优先自实现 Server酱（GET + 浏览器 UA + 超时 + 重试），否则回退青龙 notify.py。
本模块名为 ``notify``，但位于 ``core`` 子包内，不会遮蔽青龙顶层的 ``notify.py``
（子包内的 ``from notify import send`` 走绝对导入，命中 sys.path 上的青龙模块）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from urllib.parse import urlencode

from .config import logger

# Server酱 Turbo KEY：优先独立变量 GPTQTCOOL_SERVERPUSHKEY，否则复用青龙 SERVERPUSHKEY
_SERVERJ_KEY = os.environ.get("GPTQTCOOL_SERVERPUSHKEY") or os.environ.get("SERVERPUSHKEY")
# 进程内屏蔽青龙 notify.py 的 serverJ 渠道（青龙 send() 每次调用时才读环境变量，pop 有效；
# 仅影响本进程，青龙其它任务的 SERVERPUSHKEY 不受影响），避免重复推送与异常刷屏。
# 必须在 import 青龙 notify 之前执行。
os.environ.pop("SERVERPUSHKEY", None)

_qinglong_send = None
try:
    # 青龙运行时会把 notify.py 加入 sys.path
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

_SERVERJ_UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]


def send_serverj(title: str, content: str) -> bool:
    """自实现 Server酱 Turbo 推送(GET+浏览器UA+超时+3次重试)。

    青龙内置 notify.py 的 serverJ 渠道无 timeout/UA/重试,且为异步子线程,网络一抖即丢通知;
    此处绕过它直连 sctapi.ftqq.com,保证可靠送达。
    """
    if not _SERVERJ_KEY:
        logger.warning("⚠️ 未配置 Server酱 KEY(GPTQTCOOL_SERVERPUSHKEY / SERVERPUSHKEY),跳过")
        return False
    api_url = f"https://sctapi.ftqq.com/{_SERVERJ_KEY}.send?" + urlencode({
        "title": title,
        "desp": content,
    })
    max_attempts, retry_delay = 3, 2
    for attempt in range(1, max_attempts + 1):
        try:
            # 每次轮换 UA,降低被服务端风控概率
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": _SERVERJ_UA_LIST[attempt % len(_SERVERJ_UA_LIST)]},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0:
                logger.info(f"✅ Server酱 推送成功(第 {attempt} 次)")
                return True
            # 服务端明确返回错误,重试无意义
            logger.warning(f"⚠️ Server酱 返回异常: {result}")
            return False
        except Exception as e:
            logger.warning(f"⚠️ Server酱 第 {attempt}/{max_attempts} 次推送失败: {e}")
            if attempt < max_attempts:
                logger.info(f"   等待 {retry_delay} 秒后重试...")
                time.sleep(retry_delay)
    logger.warning("⚠️ Server酱 推送最终失败")
    return False


def send_notify(title: str, content: str) -> bool:
    """推送通知。优先自实现 Server酱(GET+UA+超时+重试),否则走青龙 notify.py。返回是否成功。"""
    if _SERVERJ_KEY:
        # 自实现 Server酱:绕过青龙内置 serverJ 渠道(无 timeout/UA/重试且异步丢通知)
        return send_serverj(title, content)
    if not _qinglong_send:
        logger.info("ℹ️ 未找到 notify.py(仅在青龙环境内可用),跳过推送")
        logger.info(f"📢 [通知内容] {title}: {content}")
        return False
    max_attempts, retry_delay = 3, 3
    for attempt in range(1, max_attempts + 1):
        try:
            _qinglong_send(title, content)
            logger.info("📨 已通过 notify.py 推送")
            return True
        except Exception as e:
            logger.warning(f"⚠️ 推送第 {attempt}/{max_attempts} 次失败: {e}")
            if attempt < max_attempts:
                logger.info(f"   等待 {retry_delay} 秒后重试推送...")
                time.sleep(retry_delay)
            else:
                logger.warning("⚠️ notify.py 推送最终失败")
    return False
