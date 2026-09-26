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


def git_main_sid(project_root: str) -> str:
    """MAIN gitdir 数据面（`.git` 根下 index/HEAD/packed-refs + index.lock 等
    锁文件的创建），批 A 第 0 步（2026-09-26）。

    **只授给 MAIN 边界的令牌**（boundary == project：CEO/HR/bash_main）——
    worktree agent 的 index 在自己的 gitdir（已有 git_sid 授予），不需要也不应
    拿到主树 `.git` 根的写面。域前缀 `gitmain` 防与 git/worktree/cache 撞车；
    **刻意不进封条的 subject 集**（`service._seal_subject_sids`）：封条只摘
    它认识的 subject，本 SID 的 ACE 才能在 `.git` 根上稳定存活（豁免范围由
    `_agent_aces_leaking` 的精确掩码匹配收紧，见 service.py）。
    """
    return _digest_sid("gitmain", project_root)


def shared_sid(workspace_path: str) -> str:
    """项目共享契约区 `.hiveweave/shared/`（s3c09 git×ACL 死锁修复）。

    **per-project**（派生自项目根，同项目全 agent 同一 SID），不是
    per-anchor —— shared 本意就是跨 agent 共享；各 worktree 内嵌的
    `.hiveweave/shared` 子树共用这同一能力（ACE 逐边界落盘，SID 全项目
    一致）。域分离前缀防与 worktree/git/temp 撞车。
    """
    return _digest_sid("shared", workspace_path)


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
