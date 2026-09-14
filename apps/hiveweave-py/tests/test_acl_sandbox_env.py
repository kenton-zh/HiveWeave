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
    """T3.3: UV/PIP/NPM/pnpm 缓存改指 **agent 私有** temp 子目录。

    项目级共享 ``.hiveweave-cache`` 上的并发 ``npm install`` 互相持文件锁
    导致 EPERM · unlink（TEST_DSH_35 实测 46 min 可消除税）；私有目录复用
    temp 的 per-agent 生命周期（可撤销 SID + dismiss 撤销 + 孤儿清扫），
    写入天然放行。"""
    cache = r"D:\ws\.hiveweave-cache"
    temp = r"D:\ws\tmp"
    env = _build_sandbox_env(r"D:\ws", cache, temp)
    assert env["UV_CACHE_DIR"] == os.path.join(temp, "cache", "uv")
    assert env["PIP_CACHE_DIR"] == os.path.join(temp, "cache", "pip")
    assert env["NPM_CONFIG_CACHE"] == os.path.join(temp, "cache", "npm")
    assert env["npm_config_store_dir"] == os.path.join(temp, "cache", "pnpm")
    # 共享缓存路径不得再注入 env（否则又回到共享锁竞争）
    assert env["UV_CACHE_DIR"] != os.path.join(cache, "uv")


def test_sandbox_env_no_secret_key(monkeypatch) -> None:
    """密钥绝不进受限子进程 env（显式断言，审计#1-4 钉）。

    2026-09-14 收窄断言：GitSpawn 加固（`util/win_subprocess.apply_git_hardening`）
    往沙箱 env 注入 `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` —— 这是 git
    官方 env 机制的**固定槽位键名**，原断言 `"KEY" not in k.upper()` 会把
    这些平台常量误判成密钥。排除该前缀后守卫强度不变：真密钥
    （`*_API_KEY` / `*_SECRET`）不以该前缀开头，仍被挡。
    """
    monkeypatch.setenv("HIVEWEAVE_OPENCODE_API_KEY", "super-secret")
    monkeypatch.setenv("ARK_API_KEY", "ark-secret")
    monkeypatch.setenv("PATH", "C:\\bin")
    env = _build_sandbox_env(r"D:\ws", r"D:\ws\.hiveweave-cache", r"D:\ws\tmp")
    assert "HIVEWEAVE_OPENCODE_API_KEY" not in env
    assert "ARK_API_KEY" not in env
    # 任一 *KEY* / *SECRET* 变量都不该进白名单（git 加固的固定槽位键除外）
    for k in env:
        if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            continue
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
