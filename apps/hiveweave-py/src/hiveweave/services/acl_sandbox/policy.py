"""policy.py —— 边界源解析 + 角色 SID 组装（spec §5.5 / §5.6）。

定则（§5.5 v2）：边界源 = 工具调用传入的 workspace_path（规范化后），
与 agent 身份无关。executor 传 worktree、bash_main/无 worktree 角色传项目根，
同一 SID 派生法 —— 路径本身即边界，无需第二套角色判断（角色由既有权限
矩阵表达：谁能调 bash_main）。

temp 一律位于 workspace 内（§4.12 v4 推翻 %TEMP% —— OWNER_RIGHTS-only 目录
对 write-restricted 令牌不可用）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from hiveweave.services.acl_sandbox.sid import (
    cache_sid,
    extra_sid,
    git_sid,
    temp_sid,
    worktree_sid,
)

# 私有 temp 与项目级共享缓存在 workspace 内的相对路径
SANDBOX_TEMP_REL = ".hiveweave/sandbox-temp"
CACHE_REL = ".hiveweave-cache"

# §5.7 六入口 → 边界语义（全部以工具传入的 workspace_path 为边界源）
ENTRY_BOUNDARY: dict[str, str] = {
    "bash": "boundary",           # executor → worktree；无 worktree 角色 → 项目根
    "bash_main": "project_root",
    "run_command": "boundary",
    "dev_server": "boundary",
    "alarm": "project_root",
    "python_script": "boundary",  # E11 工具与 bash 同边界语义（注册面/白名单脱节修复，TEST_DSH_32 P11）
}


@dataclass
class SandboxPolicy:
    """单次受限命令的完整授权集。"""

    boundary_root: str                      # 授权树根（executor=worktree / 项目根角色=项目根，realpath）
    project_root: str                       # 项目 workspace 根（git/cache SID 派生源，§4.8/§8）
    write_sids: list[str]                   # restricting 写 SID 集
    temp_dir: str                           # agent 私有 temp（workspace 内）
    temp_sid: str
    cache_dir: str                          # 项目级共享缓存
    extra_dirs: list[str] = field(default_factory=list)   # §5.5b②：附加可写目录（realpath）
    extra_sids: list[str] = field(default_factory=list)


def resolve_temp_dir(workspace_path: str, agent_id: str) -> str:
    """agent 私有 temp 目录（§4.12/§7.2）：workspace 内，agent 长生命周期复用。"""
    root = os.path.realpath(workspace_path)
    return str(Path(root) / SANDBOX_TEMP_REL / agent_id)


def resolve_temp_sid(temp_dir: str) -> str:
    return temp_sid(temp_dir)


def resolve_cache_dir(project_root: str) -> str:
    """项目级共享缓存 `<项目根>/.hiveweave-cache`（§8，全项目 agent 共享）。"""
    return str(Path(os.path.realpath(project_root)) / CACHE_REL)


def build_write_sids(
    boundary_root: str,
    project_root: str,
    temp_sid_str: str,
    extra_dirs: tuple[str, ...] = (),
) -> list[str]:
    """按边界源组装 restricting 写 SID 集。

    - boundary SID：空前缀，派生自边界根（worktree 或项目根），路径即边界；
    - cache\\0 / git\\0：**派生自项目根**（§4.8/§8）—— 同项目全 agent 共享
      同一 git/cache 能力，跨项目 SID 不同（域前缀 + 路径）；
    - temp\\0 / extra\\0 各自域分离。跨项目同 SID 撞车需全 60-bit 碰撞（~2⁻⁵⁴）。
    """
    boundary = os.path.realpath(boundary_root)
    project = os.path.realpath(project_root)
    sids = [
        worktree_sid(boundary),
        cache_sid(project),
        git_sid(project),
        temp_sid_str,
    ]
    for d in extra_dirs:
        sids.append(extra_sid(d))
    return sids


def resolve_policy(
    *,
    workspace_path: str,
    agent_id: str,
    entry: str = "bash",
    project_workspace_path: str | None = None,
    extra_dirs: tuple[str, ...] = (),
) -> SandboxPolicy:
    """组装单命令的 SandboxPolicy。entry 非法时抛 ValueError（fail-closed）。

    ``project_workspace_path`` = 项目根（git/cache SID 派生源）；缺省回退到
    workspace_path（P0 单目录测试形态：边界即项目根）。
    """
    if entry not in ENTRY_BOUNDARY:
        raise ValueError(f"unknown sandbox entry: {entry}")
    root = os.path.realpath(workspace_path)
    project = os.path.realpath(project_workspace_path) if project_workspace_path else root
    temp_dir = resolve_temp_dir(root, agent_id)
    temp_sid_str = resolve_temp_sid(temp_dir)
    extra_paths = [os.path.realpath(d) for d in extra_dirs]
    return SandboxPolicy(
        boundary_root=root,
        project_root=project,
        write_sids=build_write_sids(root, project, temp_sid_str, extra_paths),
        temp_dir=temp_dir,
        temp_sid=temp_sid_str,
        cache_dir=resolve_cache_dir(project),
        extra_dirs=extra_paths,
        extra_sids=[extra_sid(d) for d in extra_paths],
    )
