"""沙箱 env 构建单测（spec §5.4 env dict + §8 缓存覆盖 + §12.1 密钥缺席）。跨平台。"""

from __future__ import annotations

import os
from pathlib import Path

from hiveweave.services.acl_sandbox.service import _build_sandbox_env
from hiveweave.util.safe_env import build_child_env


def test_sandbox_env_redirects_temp(monkeypatch) -> None:
    """TMP/TEMP → 私有 temp（§4.12/§5.4）。"""
    env = _build_sandbox_env(r"D:\ws", r"D:\ws\.hiveweave-cache", r"D:\ws\.hiveweave\sandbox-temp\A001")
    assert env["TEMP"] == r"D:\ws\.hiveweave\sandbox-temp\A001"
    assert env["TMP"] == r"D:\ws\.hiveweave\sandbox-temp\A001"


def test_sandbox_env_cache_overrides() -> None:
    """§8 缓存覆盖：UV/PIP/NPM/pnpm 全部指进项目级共享缓存。"""
    cache = r"D:\ws\.hiveweave-cache"
    env = _build_sandbox_env(r"D:\ws", cache, r"D:\ws\tmp")
    assert env["UV_CACHE_DIR"] == os.path.join(cache, "uv")
    assert env["PIP_CACHE_DIR"] == os.path.join(cache, "pip")
    assert env["NPM_CONFIG_CACHE"] == os.path.join(cache, "npm")
    assert env["npm_config_store_dir"] == os.path.join(cache, "pnpm")


def test_sandbox_env_no_secret_key(monkeypatch) -> None:
    """密钥绝不进受限子进程 env（显式断言，审计#1-4 钉）。"""
    monkeypatch.setenv("HIVEWEAVE_OPENCODE_API_KEY", "super-secret")
    monkeypatch.setenv("ARK_API_KEY", "ark-secret")
    monkeypatch.setenv("PATH", "C:\\bin")
    env = _build_sandbox_env(r"D:\ws", r"D:\ws\.hiveweave-cache", r"D:\ws\tmp")
    assert "HIVEWEAVE_OPENCODE_API_KEY" not in env
    assert "ARK_API_KEY" not in env
    # 任一 *KEY* / *SECRET* 变量都不该进白名单
    for k in env:
        assert "KEY" not in k.upper()
        assert "SECRET" not in k.upper()


def test_sandbox_env_path_preserved(monkeypatch) -> None:
    """PATH/PATHEXT 继承白名单原值（§5.4 v3：不重建 PATH，保 Git Bash/uv/pnpm 解析）。"""
    monkeypatch.setenv("PATH", r"C:\Program Files\Git\cmd;D:\node")
    env = _build_sandbox_env(r"D:\ws", r"D:\ws\.hiveweave-cache", r"D:\ws\tmp")
    assert r"C:\Program Files\Git\cmd" in env["PATH"]


def test_sandbox_env_markers() -> None:
    """受限 bash 子进程仍带 HIVEWEAVE_BASH / WORKSPACE 标记。"""
    env = _build_sandbox_env(r"D:\ws", r"D:\ws\.hiveweave-cache", r"D:\ws\tmp")
    assert env["HIVEWEAVE_BASH"] == "1"
    assert env["HIVEWEAVE_WORKSPACE"] == r"D:\ws"
