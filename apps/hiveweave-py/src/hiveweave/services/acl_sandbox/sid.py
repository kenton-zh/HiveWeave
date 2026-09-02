"""SID 派生（spec §4.3）— 纯函数，可跨平台单测。

五类能力 SID 全部确定性派生自规范化路径：
- worktree / project-root：无前缀（同一路径两种角色不可能并存 ——
  嵌套 workspace 校验 m-1 保证 project root 不落在他人 worktree 内）；
- cache\\0 / git\\0 / temp\\0 / extra\\0：域分离前缀，防跨域同路径撞车。
路径输入必须已经过 realpath 规范化（大小写/符号链接收敛；§4.3）。
"""

from __future__ import annotations

import hashlib
import os


def _canonical(path: str) -> str:
    return os.path.realpath(path)


def _digest_sid(prefix: str, path: str, extra: tuple[int, ...] = ()) -> str:
    d = hashlib.sha256((prefix + "\0" + _canonical(path)).encode("utf-8")).digest()
    a = int.from_bytes(d[0:4], "little") % (2**30 - 1) + 1
    b = int.from_bytes(d[4:8], "little") % (2**30 - 1) + 1
    return "S-1-4-" + "-".join(str(x) for x in (a, b, *extra))


def worktree_sid(worktree_path: str) -> str:
    """executor worktree 根。"""
    return _digest_sid("", worktree_path)


def project_root_sid(project_root: str) -> str:
    """项目根（bash_main / 无 worktree 角色）。"""
    return _digest_sid("", project_root)


def cache_sid(workspace_path: str) -> str:
    """项目级共享缓存 `<ws>/.hiveweave-cache/`（§8）。"""
    return _digest_sid("cache", workspace_path)


def git_sid(workspace_path: str) -> str:
    """项目 `<ws>/.git` 元数据（§4.8）。"""
    return _digest_sid("git", workspace_path)


def temp_sid(temp_dir: str) -> str:
    """agent 私有 temp（§4.3/§7.2）；extra=(1,) 对齐 DSH。"""
    return _digest_sid("temp", temp_dir, (1,))


def extra_sid(path: str) -> str:
    """附加可写目录（§5.5b② P2）。"""
    return _digest_sid("extra", path)


def venv_sid(workspace_path: str) -> str:
    """项目 `.venv` 依赖环境（39 审计 P0-1：依赖安装官方落点）。

    域分离自 cache/git —— .venv 内的 site-packages 是 agent 可写面，
    与共享缓存（只写缓存类）和 git 元数据（只写元数据）能力不同。
    """
    return _digest_sid("venv", workspace_path)
