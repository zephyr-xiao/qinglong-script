# -*- coding: utf-8 -*-
"""
new Env('代理环境诊断');
cron: 0 0 1 1 *

用途：排查青龙任务里「代理配了但脚本不生效」的问题。
只读检测，不修改任何配置，可安全反复运行。

背景：青龙 config.sh 里的 ProxyUrl 是面板自身拉库/装依赖用的配置项，
不会注入任务脚本的运行环境；脚本只能读到「环境变量」页里的变量。

青龙里运行本脚本后按输出判断：
  第 1 节没有 HTTPS_PROXY / HTTP_PROXY  → 青龙「环境变量」页没配，这就是代理不生效的直接原因
  第 4 节「代理」一行能出 IP 且与直连不同 → 代理链路正常
"""

import os
import sys
import urllib.request

# 与各签到脚本保持一致的回退键顺序
GLOBAL_PROXY_KEYS = (
    "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)

# 青龙侧可能存在的配置项，列出来便于确认它们有没有进入任务环境
QINGLONG_KEYS = (
    "ProxyUrl", "GithubProxyUrl",
    "GLOBAL_AGENT_HTTP_PROXY", "GLOBAL_AGENT_HTTPS_PROXY",
)

IP_API = "https://api.ipify.org?format=json"


def mask(value: str) -> str:
    """代理地址可能含账号密码，打印时打码。"""
    if "@" not in value:
        return value
    head, tail = value.rsplit("@", 1)
    scheme = head.split("://", 1)[0] + "://" if "://" in head else ""
    return f"{scheme}***@{tail}"


def probe(proxy_url: str, label: str) -> None:
    """按给定代理请求出口 IP；proxy_url 为空表示显式禁用代理直连。"""
    handlers = urllib.request.ProxyHandler({} if not proxy_url
                                          else {"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handlers)
    try:
        with opener.open(IP_API, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
            print(f"   {label}: {body}")
    except Exception as e:
        print(f"   {label}: 失败 - {type(e).__name__}: {str(e)[:120]}")


def main() -> None:
    print("=" * 60)
    print("🔍 代理环境诊断")
    print("=" * 60)

    print("\n【1】所有含 proxy 的环境变量（大小写不敏感）")
    hits = sorted((k, v) for k, v in os.environ.items() if "proxy" in k.lower())
    if not hits:
        print("   （一个都没有）")
    for k, v in hits:
        print(f"   {k} = {mask(v)}")

    print("\n【2】青龙面板配置项是否进入了任务环境")
    for k in QINGLONG_KEYS:
        v = os.environ.get(k)
        print(f"   {k} = {mask(v) if v else '（不存在）'}")

    print("\n【3】签到脚本的代理解析结果（同一套逻辑）")
    resolved, source = "", ""
    for name in GLOBAL_PROXY_KEYS:
        value = (os.environ.get(name) or "").strip()
        if value:
            resolved, source = value, name
            break
    if resolved:
        print(f"   命中 {source} → {mask(resolved)}")
    else:
        print("   未命中任何键，脚本会直连")
        print("   → 需在青龙「环境变量」页添加 HTTPS_PROXY 与 HTTP_PROXY")

    print("\n【4】出口 IP 实测")
    probe("", "直连")
    if resolved:
        probe(resolved, f"代理({source})")

    print("\n" + "=" * 60)
    print("怎么看：")
    print("  · 第 1 节若没有 HTTPS_PROXY / HTTP_PROXY，说明青龙「环境变量」页里没配")
    print("    —— config.sh 的 ProxyUrl 不会出现在这里，它只供面板自身拉库/装依赖")
    print("  · 第 4 节「代理」显示出 IP 且与「直连」不同，说明代理链路可用")
    print("=" * 60)


if __name__ == "__main__":
    main()
