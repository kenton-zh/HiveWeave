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

import structlog

from hiveweave.services.acl_sandbox.sid import (
    cache_sid,
    extra_sid,
    git_sid,
    shared_sid,
    temp_sid,
    venv_sid,
    worktree_sid,
)

log = structlog.get_logger(__name__)

# 私有 temp 与项目级共享缓存在 workspace 内的相对路径
SANDBOX_TEMP_REL = ".hiveweave/sandbox-temp"
CACHE_REL = ".hiveweave-cache"
# 项目 .venv（39 审计 P0-1）：依赖安装的官方落点——项目根下，全 agent 共享。
# 可写（GRANT_MASK）：agent 经 uv pip 往里装包 = 与写工作区代码同等信任
# （DSH 同构：workspaceRoot 全可写）。
VENV_REL = ".venv"
# 项目共享契约区（git 跟踪、跨 agent 可见可写）。落在 `.hiveweave` PROTECTED
# 区内侧，能力 ACE 必须显式落在 shared 子树（s3c09 git×ACL 死锁修复）。
SHARED_REL = ".hiveweave/shared"

# ══════════════════════════════════════════════════════════════════
# 执行面判定（#1 治本）：**唯一真值源** —— 调用方不再有机会解释它
# ══════════════════════════════════════════════════════════════════
#
# 病灶（改造前）：`acl_sandbox_active()` 由**每个工具各自调用**，各自决定
# confined/native ⇒ ① 策略散在 seam 里；② 没人知道这次 spawn 实际走了哪条；
# ③ 漏接**不产生任何信号**（`start_dev_server` 从未 import 过 sandbox，
# 而它照样跑 —— TEST_DSH_56 的越界写实证）。
#
# 更坏的是：同一个内部信号被各调用方**各自解释**。`spawn_confined` 返回
# `None` 的真实语义是「判定为原生」，但五处读法不同 ——
# `bash`/`run_command`/`dev_server`/`alarm` 读成「沙箱关」→ 回落原生；
# `python_script` 读成「沙箱坏」→ **拒绝执行**。于是一个**显式开启**的
# 项目级逃生门（`sandbox_mode=danger-full-access`）在四条路上跑、在一条路上拒。
# 根因不是那四条或那一条写错了，而是**没有单一判定源**：判定的**含义**
# 由每个调用方自己解释。
#
# 现在：判定只在本模块发生，结果是**必须携带理由的闭合标签**；调用方拿到
# 的是路由后的结果（见 `entry.spawn_agent_command`），不接触判定过程。
ENF_CONFINED = "confined"
ENF_NATIVE = "native"

# 执行**强度**（不是「开/关」）。受限令牌只约束**写**（ACL 授权树 + SID），
# 读与网络不约束 ⇒ 受限一律 = partial。把强度当一等字段上报，是为了让下游
# 不会把 "sandbox on" 读成 "已隔离"（本仓纪律：「加了门禁」≠「问题解决了」，
# 说「有沙箱守着」时必须同时说明它**不守什么**）。
LEVEL_PARTIAL = "partial"
LEVEL_NONE = "none"

# 判定理由（闭合枚举）。**这一张表就是全部判定**：`make_decision` 查不到
# 理由即抛 ValueError（不猜、不回落默认值）。新增成员必须同时给出
# (enforcement, level)，否则构造失败 —— 不允许出现「新增了一条路，
# 但没人知道它算不算受限」的中间态。
R_CONFINED = "sandbox_enabled"
R_NATIVE_PLATFORM = "platform_not_windows"
R_NATIVE_CONFIG_OFF = "config_off"
R_NATIVE_PROJECT_OPT_OUT = "project_danger_full_access"

_DECISIONS: dict[str, tuple[str, str]] = {
    R_CONFINED: (ENF_CONFINED, LEVEL_PARTIAL),
    R_NATIVE_PLATFORM: (ENF_NATIVE, LEVEL_NONE),
    R_NATIVE_CONFIG_OFF: (ENF_NATIVE, LEVEL_NONE),
    R_NATIVE_PROJECT_OPT_OUT: (ENF_NATIVE, LEVEL_NONE),
}

# 上报戳的键名（工具结果 / 日志共用一套，避免两处各起一个名字）。
ENFORCEMENT_STAMP_KEYS: tuple[str, ...] = (
    "enforcement",
    "enforcement_level",
    "enforcement_reason",
    "enforcement_boundary",
)


@dataclass(frozen=True)
class SpawnDecision:
    """单次 agent 命令 spawn 的执行面判定。"""

    enforcement: str          # confined | native
    level: str                # partial | none（见 LEVEL_* 的说明）
    reason: str               # 闭合枚举 member
    project_id: str | None = None

    @property
    def confined(self) -> bool:
        return self.enforcement == ENF_CONFINED

    def stamp(self, *, boundary_root: str | None = None) -> dict[str, str]:
        """上报戳 —— 这次**到底**走了哪条路、强度多少、为什么。

        `boundary_root` 仅在受限侧有值（授权树根，即「被关在哪里」）；
        原生侧**故意留空** —— 「没有边界」本身就是需要被看见的事实，
        补一个假边界会让下游读成「有限制」。
        """
        out: dict[str, str] = {
            "enforcement": self.enforcement,
            "enforcement_level": self.level,
            "enforcement_reason": self.reason,
        }
        if boundary_root:
            out["enforcement_boundary"] = boundary_root
        return out


def make_decision(reason: str, *, project_id: str | None = None) -> SpawnDecision:
    """按**理由**构造判定（唯一构造点）。理由不在闭合枚举内 ⇒ ValueError。

    存在意义：调用方（与测试）要构造判定只有一个入口，且**理由必须显式**；
    想新增一条路径就得先在这张表里表态「它算不算受限、强度多少」。
    """
    pair = _DECISIONS.get(reason)
    if pair is None:
        raise ValueError(
            f"unknown sandbox reason {reason!r}; expected one of "
            f"{sorted(_DECISIONS)}"
        )
    return SpawnDecision(
        enforcement=pair[0], level=pair[1], reason=reason, project_id=project_id
    )


def sandbox_disabled_reason() -> str | None:
    """沙箱**整体**为何不生效（``None`` = 生效）。

    判据次序 = 代价递增：平台 → env。项目级逃生门要查库，不在本函数内
    （见 `resolve_spawn_decision`）。

    与 :func:`acl_sandbox_active` 的关系：后者是本函数的布尔视图（保留给
    尚未迁移的调用方与既有测试）。**新代码不要调它们中的任何一个** ——
    判定请走 `resolve_spawn_decision`，否则又回到「各调用方自己判」。
    """
    import sys

    from hiveweave.config import settings

    if not sys.platform.startswith("win"):
        return R_NATIVE_PLATFORM
    if not settings.acl_sandbox:
        return R_NATIVE_CONFIG_OFF
    mode = getattr(settings, "acl_sandbox_mode", "auto") or "auto"
    if mode == "off":
        return R_NATIVE_CONFIG_OFF
    return None


async def resolve_spawn_decision(project_id: str | None = None) -> SpawnDecision:
    """判定单次 agent 命令 spawn 的执行面 —— **全平台唯一判定点**。

    「未知」一律**不许**被读成「原生」：非 Windows / 显式配置关 / 项目级逃生门
    是三个**具名**的原生理由；其余一切（含项目配置查询失败）都判 confined ——
    受限路径若真的起不来，在**执行处** fail-closed（抛 SandboxUnavailableError），
    绝不在这里被预先降级成原生。这与「fail-open 比 fail-closed 危险」同向：
    判错的代价是拒绝执行（可见），而不是**静默无沙箱执行**（不可见）。
    """
    disabled = sandbox_disabled_reason()
    if disabled is not None:
        return make_decision(disabled, project_id=project_id)
    # P3 (§9)：项目级 sandbox_mode=danger-full-access 逃生门 —— 信任项目，
    # 跳过受限令牌。⚠ 这是**显式、可审计**的配置性开关（DB 行），
    # 与 fail-closed 正交，不是"未知"。
    from hiveweave.services.acl_sandbox.integration import project_sandbox_mode

    try:
        project_mode = await project_sandbox_mode(project_id)
    except Exception as e:  # noqa: BLE001 —— 查询失败**不是**逃生门
        # 未知不许被读成"原生"（fail-safe 方向：判错时宁可拒绝执行）。
        # `project_sandbox_mode` 内部已吞异常返回 ""，这里是**第二层**：
        # 判定层的正确性不能依赖被调用方的异常纪律。
        log.warning(
            "acl_sandbox.project_mode_lookup_failed",
            project_id=project_id,
            error=str(e),
        )
        return make_decision(R_CONFINED, project_id=project_id)
    if project_mode == "danger-full-access":
        return make_decision(R_NATIVE_PROJECT_OPT_OUT, project_id=project_id)
    return make_decision(R_CONFINED, project_id=project_id)


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
    venv_dir: str                           # 项目 .venv（依赖环境，可写）
    venv_sid_str: str
    shared_dir: str                         # 边界内 `.hiveweave/shared`（授予目标，可能不存在）
    extra_dirs: list[str] = field(default_factory=list)   # §5.5b②：附加可写目录（realpath）
    extra_sids: list[str] = field(default_factory=list)
    shared_sid_str: str | None = None       # worktree 边界才有值（项目级派生，非 per-agent）


def resolve_temp_dir(workspace_path: str, agent_id: str) -> str:
    """agent 私有 temp 目录（§4.12/§7.2）：workspace 内，agent 长生命周期复用。"""
    root = os.path.realpath(workspace_path)
    return str(Path(root) / SANDBOX_TEMP_REL / agent_id)


def resolve_temp_sid(temp_dir: str) -> str:
    return temp_sid(temp_dir)


def resolve_cache_dir(project_root: str) -> str:
    """项目级共享缓存 `<项目根>/.hiveweave-cache`（§8，全项目 agent 共享）。"""
    return str(Path(os.path.realpath(project_root)) / CACHE_REL)


def resolve_venv_dir(project_root: str) -> str:
    """项目 .venv 目录（39 审计 P0-1：依赖安装官方落点，全 agent 共享）。"""
    return str(Path(os.path.realpath(project_root)) / VENV_REL)


def build_write_sids(
    boundary_root: str,
    project_root: str,
    temp_sid_str: str,
    extra_dirs: tuple[str, ...] = (),
    venv_sid_str: str | None = None,
) -> list[str]:
    """按边界源组装 restricting 写 SID 集。

    - boundary SID：空前缀，派生自边界根（worktree 或项目根），路径即边界；
    - cache\\0 / git\\0 / venv\\0：**派生自项目根**（§4.8/§8）—— 同项目全 agent 共享
      同一 git/cache/venv 能力，跨项目 SID 不同（域前缀 + 路径）；
    - shared\\0：**派生自项目根且仅 worktree 边界携带**（boundary != project）——
      shared 是 git 跟踪的跨 agent 契约区，各 worktree 内 git rebase/checkout
      要写删 `<wt>/.hiveweave/shared/*`；CEO/HR/bash_main 项目根边界不携带，
      HR/只读授予面不变（s3c09 git×ACL 死锁修复，2026-09-05）；
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
    if boundary != project:
        sids.append(shared_sid(project))
    if venv_sid_str:
        sids.append(venv_sid_str)
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
    venv_dir = resolve_venv_dir(project)
    venv_sid_str = venv_sid(project)
    # shared 授予面 = worktree 边界（executor + builder coordinator）：
    # CEO/HR/bash_main 项目根边界（root == project）不授予不携带，行为不变。
    is_worktree_boundary = root != project
    return SandboxPolicy(
        boundary_root=root,
        project_root=project,
        write_sids=build_write_sids(
            root, project, temp_sid_str, extra_paths, venv_sid_str
        ),
        temp_dir=temp_dir,
        temp_sid=temp_sid_str,
        cache_dir=resolve_cache_dir(project),
        venv_dir=venv_dir,
        venv_sid_str=venv_sid_str,
        shared_dir=str(Path(root) / SHARED_REL),
        extra_dirs=extra_paths,
        extra_sids=[extra_sid(d) for d in extra_paths],
        shared_sid_str=shared_sid(project) if is_worktree_boundary else None,
    )
