"""异常纪律单测（spec §5.6 + §12.1 + §12.3 M2/M6）。跨平台。"""

from __future__ import annotations

import pytest

import hiveweave.services.acl_sandbox.service as svc
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError


@pytest.fixture(autouse=True)
def _force_windows_and_on(monkeypatch):
    monkeypatch.setattr(svc, "_is_windows", lambda: True)
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", True)


async def test_non_windows_returns_none(monkeypatch) -> None:
    """非 Windows → None（两种合法 None 之一）。"""
    monkeypatch.setattr(svc, "_is_windows", lambda: False)
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", True)
    r = await svc.spawn_confined(
        command="echo hi", workdir=r"D:\ws", workspace_path=r"D:\ws",
        agent_id="A001", timeout_s=30)
    assert r is None


async def test_config_off_returns_none(monkeypatch) -> None:
    """配置关 → None（两种合法 None 之二；与 fail-closed 正交）。"""
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "acl_sandbox", False)
    r = await svc.spawn_confined(
        command="echo hi", workdir=r"D:\ws", workspace_path=r"D:\ws",
        agent_id="A001", timeout_s=30)
    assert r is None


async def test_unexpected_exception_raises_not_none(monkeypatch) -> None:
    """M6/M2 靶：意外异常必须 SandboxUnavailableError，绝不返回 None（fail-open）。"""
    monkeypatch.setattr(
        svc, "resolve_policy",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(SandboxUnavailableError):
        await svc.spawn_confined(
            command="echo hi", workdir=r"D:\ws", workspace_path=r"D:\ws",
            agent_id="A001", timeout_s=30)


async def test_sandbox_error_propagates_unwrapped(monkeypatch) -> None:
    """内部抛出的 SandboxUnavailableError 原样传播（不二次包装）。"""
    import hiveweave.services.acl_sandbox.service as svc_mod

    async def _boom(*a, **k):
        raise SandboxUnavailableError("inner", api_name="X")

    monkeypatch.setattr(svc_mod, "_ensure_standing_grants", _boom)
    with pytest.raises(SandboxUnavailableError) as ei:
        await svc.spawn_confined(
            command="echo hi", workdir=r"D:\ws", workspace_path=r"D:\ws",
            agent_id="A001", timeout_s=30)
    assert ei.value.api_name == "X"


async def test_to_tool_dict_blocked(monkeypatch) -> None:
    """§6 fail-closed：错误对象携带 blocked=True（工具层格式化用）。"""
    err = SandboxUnavailableError("沙箱不可用", api_name="CreateRestrictedToken", win32_code=5)
    d = err.to_tool_dict()
    assert d["success"] is False
    assert d["blocked"] is True
    assert "fail-closed" in d["error"]
