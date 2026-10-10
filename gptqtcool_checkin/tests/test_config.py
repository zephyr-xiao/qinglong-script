# -*- coding: utf-8 -*-
"""配置解析纯函数单测。"""
import importlib

from core import config


def test_env_bool_truthy(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "y", "on", " On "):
        monkeypatch.setenv("X_FLAG", v)
        assert config.env_bool("X_FLAG", False) is True


def test_env_bool_falsy_and_default(monkeypatch):
    for v in ("0", "false", "no", "off", "abc"):
        monkeypatch.setenv("X_FLAG", v)
        assert config.env_bool("X_FLAG", True) is False
    monkeypatch.setenv("X_FLAG", "")
    assert config.env_bool("X_FLAG", True) is True
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config.env_bool("X_FLAG", True) is True


def test_env_int_valid_and_fallback(monkeypatch):
    monkeypatch.setenv("X_NUM", "42")
    assert config.env_int("X_NUM", 7) == 42
    monkeypatch.setenv("X_NUM", "not-a-number")
    assert config.env_int("X_NUM", 7) == 7
    monkeypatch.setenv("X_NUM", "")
    assert config.env_int("X_NUM", 7) == 7
    monkeypatch.delenv("X_NUM", raising=False)
    assert config.env_int("X_NUM", 7) == 7


def test_state_file_lives_in_script_dir():
    # 登录态文件必须落在脚本目录（core 的上一级），随青龙数据卷持久化
    assert config.STATE_FILE.name == "gptqtcool_state.json"
    assert (config.STATE_FILE.parent / "gptqtcool_checkin.py").exists()


def test_retry_defaults(monkeypatch):
    # 未显式设置时，默认 3 轮 / 12 分钟
    monkeypatch.delenv("GPTQTCOOL_RETRY_ROUNDS", raising=False)
    monkeypatch.delenv("GPTQTCOOL_MAX_RUNTIME_MIN", raising=False)
    mod = importlib.reload(config)
    assert mod.RETRY_ROUNDS == 3
    assert mod.MAX_RUNTIME_MIN == 12
    # 非法/过小值被夹到 >=1
    monkeypatch.setenv("GPTQTCOOL_RETRY_ROUNDS", "0")
    monkeypatch.setenv("GPTQTCOOL_MAX_RUNTIME_MIN", "abc")
    mod = importlib.reload(config)
    assert mod.RETRY_ROUNDS == 1
    assert mod.MAX_RUNTIME_MIN == 12
    # 还原，避免影响其它测试
    monkeypatch.delenv("GPTQTCOOL_RETRY_ROUNDS", raising=False)
    monkeypatch.delenv("GPTQTCOOL_MAX_RUNTIME_MIN", raising=False)
    importlib.reload(config)
