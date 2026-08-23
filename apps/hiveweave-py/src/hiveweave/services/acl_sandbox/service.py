"""service.py —— spawn_confined 编排（spec §5.6 + §4.4/§4.9/§4.12/§8）。

异常纪律（审计#1-14）：本模块**只在三种情形返回 None** ——
  a) 非 Windows 平台
  b) HIVEWEAVE_ACL_SANDBOX=off（配置性关闭，与 fail-closed 正交）
  c) 项目 sandbox_mode=danger-full-access（P3 §9 逃生门，显式配置性信任）
其余一切异常（含意外 bug）→ SandboxUnavailableError，绝不降级 native。

verify-then-skip（§5.6 v2）：**不做正向 grant 缓存**。每命令读根 DACL 确认
ACE 在场才放行 —— worktree 删除后同路径重建/项目删除重建/workspace 迁移
全都天然正确，正确性不依赖缓存失效钩子。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any

from hiveweave.config import settings
from hiveweave.services.acl_sandbox import telemetry
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.grant import (
    CACHE_MASK,
    GRANT_MASK,
    WriteGrant,
)
from hiveweave.services.acl_sandbox.policy import resolve_policy
from hiveweave.services.acl_sandbox.sid import cache_sid, extra_sid, git_sid, worktree_sid
from hiveweave.services.acl_sandbox.spawn import ConfinedRunner
from hiveweave.services.acl_sandbox.token import RestrictedTokenFactory
from hiveweave.util.safe_env import build_child_env

# 拒绝方言（§5.6）：stderr 命中且非零退出 → 追加沙箱提示（限频）
REJECTION_DIALECT = (
    "Access is denied",
    "Access to the path",
    "Permission denied",
)
_HINT_EVERY_N_ROUNDS = 3
_REJECTION_HINT = (
    "写入被沙箱拒绝：目标在授权树（{boundary}）之外。"
    "git 元数据/缓存目录已授权；确需其他位置用 message_user 申请豁免。"
)

# §4.9 缓存覆盖（§8：项目级共享缓存）
_CACHE_ENV_OVERRIDES = {
    "UV_CACHE_DIR": "uv",
    "PIP_CACHE_DIR": "pip",
    "NPM_CONFIG_CACHE": "npm",
    "npm_config_store_dir": "pnpm",
}


def is_rejection(stderr: str, exit_code: Any) -> bool:
    """拒绝方言命中判定：非零退出且 stderr 含拒绝特征。"""
    if not exit_code or exit_code == 0:
        return False
    if not stderr:
        return False
    return any(d in stderr for d in REJECTION_DIALECT)


def _build_sandbox_env(
    cwd: str,
    cache_dir: str,
    temp_dir: str,
    env_extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """白名单 env（§5.4 显式 env dict）+ 缓存覆盖 + TMP/TEMP → 私有 temp。

    绝不继承父进程环境（HIVEWEAVE_OPENCODE_API_KEY 等密钥全量挡在
    build_child_env 白名单外）；PATH/PATHEXT 继承白名单原值（§5.4 v3）。
    ``env_extra`` = 调用方增量（dev server 的 PORT 注入等，白名单之上）。
    """
    env = build_child_env(cwd, bash_markers=True)
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    # 沙箱不变量（TEMP/TMP/缓存覆盖）在 env_extra 之后强制写回 —— 调用方
    # 增量不得覆盖受限 temp / 项目缓存指针。
    env["TEMP"] = temp_dir
    env["TMP"] = temp_dir
    for var, sub in _CACHE_ENV_OVERRIDES.items():
        env[var] = os.path.join(cache_dir, sub)
    return env


def _is_windows() -> bool:
    return sys.platform.startswith("win")


async def ensure_standing_grants(
    *,
    workspace_path: str,
    project_workspace_path: str | None = None,
    agent_id: str = "system",
) -> None:
    """§7.1 创建钩子 / 启动回填：只铺 standing 授予（裁剪 + 根 + git + 缓存）。

    幂等（verify-then-skip）；不建 token、不生成 temp —— 首次受限命令仍会
    兜底补 temp。沙箱未启用（非 Windows / 配置关）时直接返回。
    """
    if not _is_windows() or not settings.acl_sandbox:
        return
    agrant = _AsyncGrant(WriteGrant())
    from hiveweave.services.acl_sandbox.integration import (
        fetch_additional_writable_dirs,
    )

    extra_dirs = await fetch_additional_writable_dirs(
        project_workspace_path or workspace_path
    )
    policy = resolve_policy(
        workspace_path=workspace_path, agent_id=agent_id,
        project_workspace_path=project_workspace_path,
        extra_dirs=tuple(extra_dirs),
    )
    lock = await _root_lock(policy.boundary_root)
    async with lock:
        await _ensure_standing_grants(policy, agrant)


async def revoke_agent_temp(
    *, workspace_path: str, agent_id: str, project_workspace_path: str | None = None,
) -> None:
    """§7.2：agent dismiss / 项目删除 / 后端退出时撤销私有 temp 的 revocable ACE。

    best-effort（fail-quiet）；沙箱未启用时直接返回。删除 ACE 后目录残留由
    平台清理（R11 启动扫孤儿 temp 属 P2）。P1 未接入 dismiss 钩子 —— 此助手
    供 P2 生命周期接线。
    """
    if not _is_windows() or not settings.acl_sandbox:
        return
    policy = resolve_policy(
        workspace_path=workspace_path, agent_id=agent_id,
        project_workspace_path=project_workspace_path,
    )
    lock = await _root_lock(policy.boundary_root)
    async with lock:
        await asyncio.to_thread(
            WriteGrant.revoke_revocable, policy.temp_dir, policy.temp_sid
        )


# ── 模块级状态（per-root 锁 / 提示限频 / runner 懒单例） ────────────────
# §4.4 并发纪律：grant 物化按 **边界根** 持 asyncio.Lock —— 同一 workspace 的
# 根/.git/.hiveweave-cache/.hiveweave 子树互相重叠，若各自独立加锁并发
# SetNamedSecurityInfo（带继承传播）会在 NTFS 层死锁；整个 grant 阶段按根
# 串行化既防 lost-update（CEO+HR 并发首命令）也防传播死锁。
# _root_locks_guard 只用 threading.Lock 保护同步 setdefault：它从不跨 await
# 持有，用 asyncio.Lock 反而会被绑定到 pytest 首个触碰它的测试事件循环，
# 后续 async 测试在新 loop 上复用会抛 "bound to a different event loop"。
_root_locks: dict[str, asyncio.Lock] = {}
_root_locks_guard = threading.Lock()

_runner: ConfinedRunner | None = None
_runner_guard = threading.Lock()

_hint_counts: dict[str, int] = {}
_hint_guard = threading.Lock()


async def _root_lock(path: str) -> asyncio.Lock:
    norm = os.path.realpath(path)
    with _root_locks_guard:
        return _root_locks.setdefault(norm, asyncio.Lock())


def _ensure_runner() -> ConfinedRunner:
    global _runner
    with _runner_guard:
        if _runner is None:
            _runner = ConfinedRunner(max_workers=settings.acl_max_concurrent)
        return _runner


def shutdown_runner() -> None:
    """后端退出时调用（Job 语义已保证受限子进程全灭，仅回收线程池）。"""
    global _runner
    with _runner_guard:
        if _runner is not None:
            _runner.shutdown()
            _runner = None


async def _grant_if_missing(path: str, sid: str, mask: int, agrant: _AsyncGrant) -> None:
    """verify-then-skip 补授 + §4.11 grant 后读回验证（fail-closed 重试一次）。"""
    if await agrant.ace_present_async(path, sid, mask):
        return
    await agrant.grant_standing_async(path, sid, mask)
    if not await agrant.ace_present_async(path, sid, mask):  # 读回复核
        await agrant.grant_standing_async(path, sid, mask)
        if not await agrant.ace_present_async(path, sid, mask):
            raise SandboxUnavailableError(
                f"grant read-back verification failed: {path} sid={sid}",
                api_name="SetNamedSecurityInfo")


class _AsyncGrant:
    """WriteGrant 的 to_thread 薄壳（§5.2 线程纪律：所有 ACL 调用经 to_thread）。"""

    def __init__(self, grant: WriteGrant):
        self._g = grant

    async def ace_present_async(self, path, sid, mask=GRANT_MASK) -> bool:
        return await asyncio.to_thread(self._g.ace_present, path, sid, mask)

    async def grant_standing_async(self, path, sid, mask=GRANT_MASK) -> None:
        await asyncio.to_thread(self._g.grant_standing, path, sid, mask)

    async def has_subject_write_ace_async(self, path) -> bool:
        return await asyncio.to_thread(self._g.has_subject_write_ace, path)

    async def break_inheritance_async(self, path) -> None:
        await asyncio.to_thread(self._g.break_inheritance, path)


async def _ensure_standing_grants(policy, agrant: _AsyncGrant) -> None:
    """verify-then-skip 补授：主体探测 → .hiveweave 裁剪 → 边界根 + 项目级 git/缓存。"""
    root = policy.boundary_root
    project = policy.project_root
    if not await agrant.has_subject_write_ace_async(root):
        raise SandboxUnavailableError(
            f"workspace 根 {root} 无真实主体写 ACE（OWNER_RIGHTS-only 或缺失 ACL），"
            f"write-restricted 令牌不可用。请把 workspace 放在用户常规目录下"
            f"（如 C:\\Users\\<user>\\ 或含 AuthUsers:Modify 的目录）。")

    # §4.9：必须先裁剪 .hiveweave 再授予项目根 —— 否则根的可继承 ACE 会先
    # 传播进 .hiveweave、随后被 PROTECTED 固化为显式 ACE，形成泄漏。
    # 裁剪后：data.db/平台系统区对受限令牌 pass-2 落空（幂等，已 PROTECTED 跳过）。
    hw = os.path.join(root, ".hiveweave")
    if os.path.isdir(hw):
        await agrant.break_inheritance_async(hw)

    await _grant_if_missing(root, worktree_sid(root), GRANT_MASK, agrant)

    # §4.8/§8：git 元数据与共享缓存是**项目级** —— 授在项目根下，SID 从项目根派生。
    # 边界是 worktree 时，realpath 下的 `.git`（gitdir 指针文件）不在这里授。
    git_path = os.path.join(project, ".git")
    if os.path.exists(git_path):
        await _grant_if_missing(git_path, git_sid(project), GRANT_MASK, agrant)

    cache_dir = policy.cache_dir
    if not os.path.isdir(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    await _grant_if_missing(cache_dir, cache_sid(project), CACHE_MASK, agrant)

    # §5.5b②（P2）：附加可写目录 —— 每目录独立 extra SID（"extra\0" 域派生），
    # standing 授予 GRANT_MASK + OI/CI。§4.12 部署前提同样适用：目录必须已存在
    # 且带真实主体写 ACE，否则 fail-closed。**不自动创建**（平台自建目录是
    # OWNER_RIGHTS-only，对受限令牌不可用，会让主体探测必然失败）。
    for d in policy.extra_dirs:
        if not os.path.isdir(d):
            raise SandboxUnavailableError(
                f"附加可写目录不存在: {d} —— 请先创建该目录（放在用户常规目录下，"
                f"勿用平台/临时自动创建），再保存项目配置。")
        if not await agrant.has_subject_write_ace_async(d):
            raise SandboxUnavailableError(
                f"附加可写目录 {d} 无真实主体写 ACE（OWNER_RIGHTS-only 或缺失 ACL），"
                f"write-restricted 令牌不可用。请把目录放在用户常规目录下。")
        await _grant_if_missing(d, extra_sid(d), GRANT_MASK, agrant)


async def _ensure_temp(policy, agrant: _AsyncGrant) -> None:
    """agent 私有 temp：重建 + revocable 授予（verify-then-skip，无正向缓存）。"""
    temp_dir = policy.temp_dir
    if not os.path.isdir(temp_dir):
        os.makedirs(temp_dir, exist_ok=True)
    if not await agrant.ace_present_async(temp_dir, policy.temp_sid, GRANT_MASK):
        await agrant.grant_standing_async(temp_dir, policy.temp_sid, GRANT_MASK)


def _maybe_append_rejection_hint(agent_id: str, boundary: str, result: dict) -> dict:
    hit = is_rejection(result.get("stderr", ""), result.get("exit_code"))
    telemetry.record_rejection(hit)
    if not hit:
        return result
    with _hint_guard:
        n = _hint_counts.get(agent_id, 0)
        _hint_counts[agent_id] = n + 1
    if n % _HINT_EVERY_N_ROUNDS != 0:
        return result
    hint = _REJECTION_HINT.format(boundary=boundary)
    stderr = result.get("stderr", "") + f"\n\n[沙箱提示] {hint}"
    result = dict(result)
    result["stderr"] = stderr
    return result


async def spawn_confined(
    *,
    command: str,
    workdir: str,
    workspace_path: str,
    agent_id: str,
    project_id: str | None = None,
    project_workspace_path: str | None = None,
    timeout_s: float | None = None,
    entry: str = "bash",
    long_running: bool = False,
    env_extra: dict[str, str] | None = None,
) -> dict | None:
    """受限执行入口。返回 None 的仅两种情形：非 Windows / 配置关。

    ``project_workspace_path`` = 项目根（git/cache SID 派生源 §4.8/§8）；
    缺省回退到 workspace_path（P0 单目录形态）。
    ``env_extra`` = 调用方增量 env（dev server 端口注入等）。
    """
    if not _is_windows():
        return None
    if not settings.acl_sandbox:
        return None

    # P3 (§9)：项目级 sandbox_mode=danger-full-access 逃生门 —— 信任项目，
    # 跳过受限令牌（降级 native，属显式配置性开关，与 fail-closed 正交）。
    from hiveweave.services.acl_sandbox.integration import project_sandbox_mode

    if await project_sandbox_mode(project_id) == "danger-full-access":
        return None

    agrant = _AsyncGrant(WriteGrant())
    try:
        from hiveweave.services.acl_sandbox.integration import (
            fetch_additional_writable_dirs,
        )

        extra_dirs = await fetch_additional_writable_dirs(
            project_workspace_path or workspace_path
        )
        policy = resolve_policy(
            workspace_path=workspace_path, agent_id=agent_id,
            entry=entry,
            project_workspace_path=project_workspace_path,
            extra_dirs=tuple(extra_dirs),
        )
        # §4.4 并发纪律：grant 阶段按边界根串行（防传播死锁 + lost-update）
        lock = await _root_lock(policy.boundary_root)
        _prop_sw = telemetry._Stopwatch()
        async with lock:
            await _ensure_standing_grants(policy, agrant)
            await _ensure_temp(policy, agrant)
        telemetry.record_propagation_ms(_prop_sw.elapsed())

        factory = RestrictedTokenFactory()
        _mint_sw = telemetry._Stopwatch()
        token = await asyncio.to_thread(factory.create, policy.write_sids, policy.temp_sid)
        telemetry.record_mint_ms(_mint_sw.elapsed())
        try:
            env = _build_sandbox_env(
                workdir, policy.cache_dir, policy.temp_dir, env_extra)
            runner = _ensure_runner()
            if long_running:
                job = await runner.run_long_running(token, command, workdir, env)
                return {
                    "long_running": True,
                    "job": job,
                    "pid": job.pid,
                    "temp_dir": policy.temp_dir,
                    "cache_dir": policy.cache_dir,
                }
            result = await runner.run_foreground(token, command, workdir, env, timeout_s)
        finally:
            token.Close()
    except SandboxUnavailableError:
        telemetry.record_fail_closed()
        raise
    except Exception as e:  # 意外异常也必须 fail-closed，不得返回 None
        telemetry.record_fail_closed()
        raise SandboxUnavailableError(f"ACL sandbox execution failed: {e}") from e

    # 超时击杀后 exit_code=259(STILL_ACTIVE) 会误触拒绝方言 → 超时不追加提示
    if result.get("timed_out"):
        return result
    return _maybe_append_rejection_hint(agent_id, policy.boundary_root, result)
