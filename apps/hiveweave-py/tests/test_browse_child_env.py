"""browse 子进程代理策略回归（09-08「搜索通道不稳」修复）。

旧版无条件剥离 HTTP(S)_PROXY 等代理变量 → 外部调研永远直连，用户网络
下搜索引擎必超时。修订后：代理变量**保留**，回环直连交给 NO_PROXY 合并
+ AGENT_BROWSER_PROXY_BYPASS（A155 的正解）。
"""

from __future__ import annotations

import pytest

from hiveweave.tools.browse_tools import _browse_child_env


@pytest.fixture
def proxy_env(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


def test_proxy_vars_are_kept(proxy_env):
    env = _browse_child_env("agent-1")
    assert env.get("HTTP_PROXY") == "http://127.0.0.1:7890"
    assert env.get("HTTPS_PROXY") == "http://127.0.0.1:7890"


def test_loopback_bypass_always_declared(proxy_env):
    env = _browse_child_env("agent-1")
    assert "localhost" in env["NO_PROXY"]
    assert "127.0.0.1" in env["NO_PROXY"]
    assert env["AGENT_BROWSER_PROXY_BYPASS"] == "localhost,127.0.0.1,::1"


def test_existing_no_proxy_is_merged_not_replaced(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "*.internal.corp")
    env = _browse_child_env(None)
    np = env["NO_PROXY"]
    assert "*.internal.corp" in np
    assert "localhost" in np and "127.0.0.1" in np
