# -*- coding: utf-8 -*-
"""验证码处理控制流单测：wait_and_handle_captcha_with_retry 的三态返回。

用假 page + monkeypatch 替换检测/处理函数，避免真实浏览器与真实等待。
"""
import asyncio

import pytest

from core import slider


class _FakeLocator:
    async def count(self):
        return 0


class _FakePage:
    async def evaluate(self, _script):
        return None

    def locator(self, _selector):
        return _FakeLocator()


@pytest.fixture
def fast_sleep(monkeypatch):
    """把 slider 里的 asyncio.sleep 变成空操作，让控制流测试秒级完成。"""
    async def _noop(_seconds):
        return None
    monkeypatch.setattr(slider.asyncio, "sleep", _noop)


def _async_seq(values):
    """返回一个按序产出 values 的 async 假函数（用尽后重复最后一个值）。"""
    seq = list(values)
    state = {"i": 0}

    async def _fn(*_args, **_kwargs):
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    return _fn


def test_no_captcha_returns_none(monkeypatch, fast_sleep):
    monkeypatch.setattr(slider, "detect_slider_captcha", _async_seq([False]))
    called = {"handle": 0}

    async def _handle(_page):
        called["handle"] += 1
        return True

    monkeypatch.setattr(slider, "handle_slider_captcha", _handle)
    result = asyncio.run(slider.wait_and_handle_captcha_with_retry(_FakePage()))
    assert result is None          # 全程无验证码 → 判定"无需处理"，不是失败
    assert called["handle"] == 0   # 不应去处理验证码


def test_captcha_passed_returns_true(monkeypatch, fast_sleep):
    monkeypatch.setattr(slider, "detect_slider_captcha", _async_seq([True]))
    monkeypatch.setattr(slider, "handle_slider_captcha", _async_seq([True]))
    result = asyncio.run(slider.wait_and_handle_captcha_with_retry(_FakePage()))
    assert result is True


def test_captcha_disappears_after_handle_returns_true(monkeypatch, fast_sleep):
    # 第1轮检测到 → 处理返回 False → 随后验证码消失 → 视为通过
    monkeypatch.setattr(slider, "detect_slider_captcha", _async_seq([True, False]))
    monkeypatch.setattr(slider, "handle_slider_captcha", _async_seq([False]))
    result = asyncio.run(slider.wait_and_handle_captcha_with_retry(_FakePage()))
    assert result is True


def test_captcha_never_passes_returns_false(monkeypatch, fast_sleep):
    # 验证码始终在、处理始终失败 → 多轮后判失败
    monkeypatch.setattr(slider, "detect_slider_captcha", _async_seq([True]))
    monkeypatch.setattr(slider, "handle_slider_captcha", _async_seq([False]))
    result = asyncio.run(slider.wait_and_handle_captcha_with_retry(_FakePage()))
    assert result is False
