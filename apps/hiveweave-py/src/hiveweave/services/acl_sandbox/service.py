"""service.py —— spawn_confined 编排（spec §5.6 + §4.4/§4.9/§4.12/§8）。

异常纪律（审计#1-14）：本模块**只在三种情形返回 None** ——
  a) 非 Windows 平台
  b) HIVEWEAVE_ACL_SANDBOX=off（配置性关闭，与 fail-closed 正交）
  c) 项目 sandbox_mode=danger-full-access（P3 §9 逃生门，显式配置性信任）
其余一切异常（含意外 bug）→ SandboxUnavailableError，绝不降级 native。

⚠ #1 治本（2026-09-14）：上面三条**不再由本模块判定**，而是
`policy.resolve_spawn_decision()` 的三条具名理由；None 的**语义**归判定层所有
（并只由 `entry.spawn_agent_command` 消费）。本模块是**受限执行器**：
传了 `decision` 就用它（不重判），原生判定传进来直接 ValueError。

verify-then-skip（§5.6 v2）：**不做正向 grant 缓存**。每命令读根 DACL 确认
ACE 在场才放行 —— worktree 删除后同路径重建/项目删除重建/workspace 迁移
全都天然正确，正确性不依赖缓存失效钩子。
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

import structlog

from hiveweave.config import settings
from hiveweave.services.acl_sandbox import telemetry, temppatch
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.grant import (
    CACHE_MASK,
    FILE_ALL_ACCESS,
    GRANT_MASK,
    WRITE_DAC,
    WRITE_OWNER,
    WriteGrant,
)
from hiveweave.services.acl_sandbox.policy import (
    ENF_CONFINED,
    SpawnDecision,
    resolve_policy,
)
from hiveweave.services.acl_sandbox.sid import (
    cache_sid,
    extra_sid,
    git_sid,
    shared_sid,
    worktree_sid,
)
from hiveweave.services.acl_sandbox.spawn import ConfinedRunner
from hiveweave.services.acl_sandbox.token import RestrictedTokenFactory
from hiveweave.util.safe_env import build_child_env

log = structlog.get_logger(__name__)

# 拒绝方言（§5.6）：stderr 命中且非零退出 → 追加沙箱提示（限频）
REJECTION_DIALECT = (
    "Access is denied",
    "Access to the path",
    "Permission denied",
    # P0-3 Stage 2c（审计 A4）：中文 Windows 的 ACL 文案同样是**拒绝证据** ——
    # 此前不认 ⇒ 既不追加提示、也不落 denied_by（死支）。
    "访问被拒绝",
    "拒绝访问",
)
_HINT_EVERY_N_ROUNDS = 3
# P0-3：提示文案**由成因驱动**（不再一律说"在授权树之外"）。
#
# 病灶实证（55 库 / 53 行提示 / 33 可判定）：**19 行（57.6%）**的被拒路径其实
# **就在提示自己印出的授权树之内**（全部落在 `.hiveweave\reports|worktrees`
# 这类平台 PROTECTED 面）⇒ agent 被告知"越界、去申请豁免"，而申请毫无用处。
_HINT_BY_DENIED_BY: dict[str, str] = {
    "outside_boundary": (
        "写入被沙箱拒绝：目标在授权树（{boundary}）之外。"
        "git 元数据/缓存目录已授权；确需其他位置用 message_user 申请豁免。"
    ),
    "sealed_git": (
        "写入被沙箱拒绝：目标是平台**封条**保护的 git 引导文件（{boundary} 下）。"
        "这是有意的安全封条 —— 申请豁免也不会放行；请改用平台支持的 git 操作。"
    ),
    "no_write_sid": (
        "写入被沙箱拒绝：目标**在授权树（{boundary}）之内、但不在已授权的写入面**"
        "（例如 .hiveweave 下的平台保护目录）。**这不是越界**，申请豁免无用；"
        "请写到自己的工作区路径，或把需求交回协调者。"
    ),
    "unknown_acl": (
        "写入被沙箱拒绝（成因未能判定；本次结果带 denied_by=unknown_acl，"
        "落库列是下一阶段）："
        "本次命令的授权树是 {boundary}。先换到该树内的路径重试，"
        "**不要**按「越界」处理。"
    ),
}
# 兼容旧名（对外/测试引用的仍是这段默认文案；成因判不出时用它）
_REJECTION_HINT = _HINT_BY_DENIED_BY["outside_boundary"]

# §4.9 缓存覆盖（§8：项目级共享缓存）
_CACHE_ENV_OVERRIDES = {
    "UV_CACHE_DIR": "uv",
    "PIP_CACHE_DIR": "pip",
    "NPM_CONFIG_CACHE": "npm",
    "npm_config_store_dir": "pnpm",
}

# §P1-1 共享缓存目录收集：测试运行器会写入的同名缓存目录，跨 worktree 共享时
# 须补授 AU 写（受限进程自建的 OWNER_RIGHTS-only 目录不继承父 ACE → 互相
# EPERM，M1 死因）。只匹配工作区内固定集合，浅层受控；绝不递归 node_modules
# 主体（只取 node_modules/.cache 一层），跳过平台私有/.git 大目录。
_STD_CACHE_DIR_NAMES = frozenset({
    ".pytest_cache", "__pycache__", ".cache", "coverage", "htmlcov",
    ".tmp", ".tox", ".nyc_output",
})
_SHARED_CACHE_MAX_DEPTH = 6
_SHARED_CACHE_SKIP_DIRS = frozenset({
    ".git", ".hiveweave", ".venv", "venv", "env", "dist", "build",
    ".next", ".nuxt", ".turbo", "out", ".idea", ".vscode",
})


def _collect_shared_cache_dirs(boundary: str) -> list[str]:
    """广度优先受控扫描工作区，返回需补授 AU 写的缓存类目录（已有直接子级先返回）。

    不进入 node_modules 主体（巨型），仅探测其直接子 ``node_modules/.cache``。
    任何为目录读取/越界都 fail-open 跳过 —— 本收集纯增量辅助，绝不阻塞 grant。
    """
    found: list[str] = []
    try:
        root = Path(boundary)
        if not root.is_dir():
            return found
        queue = deque([(root, 0)])
        while queue:
            d, depth = queue.popleft()
            if depth >= _SHARED_CACHE_MAX_DEPTH:
                continue
            try:
                children = [c for c in d.iterdir() if c.is_dir()]
            except OSError:
                continue
            for c in children:
                name = c.name.lower()
                if name in _STD_CACHE_DIR_NAMES:
                    found.append(str(c))
                    continue
                if name == "node_modules":
                    nmc = c / ".cache"
                    if nmc.is_dir():
                        found.append(str(nmc))
                    continue  # 不深钻 node_modules 主体
                if name in _SHARED_CACHE_SKIP_DIRS:
                    continue
                if depth + 1 < _SHARED_CACHE_MAX_DEPTH:
                    queue.append((c, depth + 1))
    except Exception:  # 收集失败不阻断 grant 主线
        return found
    return found


# ── s3c09 git×ACL 死锁修复：shared 子树补授 walker（水位 + verify-then-skip）──
# shared 根的 OI/CI 授予经 SetNamedSecurityInfo 急切传播覆盖存量非 PROTECTED
# 子树；PROTECTED 死岛（如 CPython>=3.12 mkdir(mode!=0o777) 产物）接不到传播，
# 由本 walker 补授。同 temppatch 哲学：浅层受控 + 节点封顶 + 进程内水位。
# 越界防线：junction/symlink/reparse point 一律跳过——跟随授予会把 OI/CI
# 可继承 ACE 落到边界外目标子树（mklink /J 无需特权，持久落盘即全员越界）。
# 判据 = FILE_ATTRIBUTE_REPARSE_POINT 属性（实测 py3.13 junction 的
# DirEntry.is_symlink() 返回 False、is_dir(follow=False) 返回 True，
# is_symlink 只能兜底 symlink）；shared 根自身的 reparse 检查在授予点
# （_ensure_standing_grants，_is_reparse_point）。
_SHARED_WALK_MAX_DEPTH = 6
_SHARED_WALK_MAX_NODES = 2048
# 死岛 normal-pass 补授掩码：FILE_ALL 去 WRITE_DAC/WRITE_OWNER（M7 红线不变）。
# 孤岛 DACL 只有 OWNER_RIGHTS（受限令牌不消费该 SID），须补**真实主体** ACE
# 恢复 normal-pass 的 READ_CONTROL / FILE_TRAVERSE / 读位 —— 实测 write-open
# 的 READ_CONTROL 走 normal pass，只补能力 SID 写位开不了门（dbg 实证钉死）。
_NORMAL_PASS_MASK = FILE_ALL_ACCESS & ~(WRITE_DAC | WRITE_OWNER)  # 0x1301FF
_shared_repaired: set[str] = set()
_shared_repaired_guard = threading.Lock()


def _is_reparse_point(path: str) -> bool:
    """路径是否 junction/symlink（reparse point）。os.stat follow=False 纯 stdlib。"""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _repair_shared_islands(
    shared_dir: str, shared_sid_str: str, user_sid: str | None
) -> tuple[int, int, bool]:
    """shared 子树死岛节点补授（目录与**文件**都扫，junction/symlink 跳过）。

    返回 ``(repaired, failed, truncated)``：
    - repaired —— 实际补授次数；
    - failed —— 单节点授予/扫描失败数（fail-soft，不中断遍历）；
    - truncated —— 节点封顶截断（未扫完整棵子树）。

    每节点两 pass 对齐令牌评估（temppatch 同款双 SID 哲学）：
    - restricting pass：缺 ``shared_sid`` GRANT_MASK ACE → 补（写/删位）；
    - normal pass：节点无任何真实主体写 ACE（OWNER_RIGHTS-only 死岛形态）
      → 补当前用户 ``_NORMAL_PASS_MASK``（只动死岛，健康树 verify 跳过 ——
      不给正常节点重复加 ACE，授予面不扩大）。
    """
    repaired = 0
    failed = 0
    truncated = False
    visited = 0
    stack: list[tuple[str, int]] = [(shared_dir, 0)]
    while stack:
        if visited >= _SHARED_WALK_MAX_NODES:
            truncated = True
            break
        d, depth = stack.pop()
        visited += 1
        try:
            if not WriteGrant.ace_present(d, shared_sid_str, GRANT_MASK):
                WriteGrant.grant_standing(d, shared_sid_str, GRANT_MASK)
                repaired += 1
            if user_sid and not WriteGrant.has_subject_write_ace(d):
                WriteGrant.grant_standing(d, user_sid, _NORMAL_PASS_MASK)
                repaired += 1
        except Exception:  # noqa: BLE001 —— 单节点失败不中断遍历
            failed += 1
            continue
        try:
            children = list(os.scandir(d))
        except OSError:
            failed += 1
            continue
        for c in children:
            if visited >= _SHARED_WALK_MAX_NODES:
                truncated = True
                break
            try:
                # junction/symlink/reparse 一律不跟随、不授予（越界防线）。
                # 实测 py3.13：junction 的 DirEntry.is_symlink() 返回 False、
                # is_dir(follow=False) 返回 True —— is_symlink 只是兜底，
                # 必须以 FILE_ATTRIBUTE_REPARSE_POINT 属性为准（junction 与
                # symlink 均带该位），否则会入栈跟出去给边界外目标落 ACE。
                c_st = c.stat(follow_symlinks=False)
                if c.is_symlink() or (
                        getattr(c_st, "st_file_attributes", 0)
                        & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    continue
                is_dir = stat.S_ISDIR(c_st.st_mode)
            except OSError:
                failed += 1
                continue
            if is_dir:
                if depth < _SHARED_WALK_MAX_DEPTH:
                    stack.append((c.path, depth + 1))
                continue
            visited += 1
            try:
                if not WriteGrant.ace_present(c.path, shared_sid_str, GRANT_MASK):
                    WriteGrant.grant_standing(c.path, shared_sid_str, GRANT_MASK)
                    repaired += 1
                if user_sid and not WriteGrant.has_subject_write_ace(c.path):
                    WriteGrant.grant_standing(c.path, user_sid, _NORMAL_PASS_MASK)
                    repaired += 1
            except Exception:  # noqa: BLE001
                failed += 1
                continue
    return repaired, failed, truncated


def _repair_shared_islands_once(
    shared_dir: str, shared_sid_str: str, user_sid: str | None
) -> tuple[int, int, bool]:
    """水位版 walker：同进程同 shared 根只全跑一次（稳态零扫描）。

    水位键 = normcase(realpath(shared_dir))。**只有全清（failed==0 且未截断）
    才落水位**——失败/截断的子树下一命令 verify-then-skip 重扫；已落水位的
    根命中水位直接返回 ``(0, 0, False)``。
    """
    key = os.path.normcase(os.path.realpath(shared_dir))
    with _shared_repaired_guard:
        if key in _shared_repaired:
            return 0, 0, False
    repaired, failed, truncated = _repair_shared_islands(
        shared_dir, shared_sid_str, user_sid
    )
    if failed == 0 and not truncated:
        with _shared_repaired_guard:
            _shared_repaired.add(key)
    return repaired, failed, truncated


def is_rejection(stderr: str, exit_code: Any) -> bool:
    """拒绝方言命中判定：非零退出且 stderr 含拒绝特征。

    P0-3：判定实现收口到 `tools.fact_positions.is_acl_rejection`（与本模块的
    `REJECTION_DIALECT` 同源 —— 那边惰性引用本常量），避免"同一语义两处实现"。
    """
    from hiveweave.tools.fact_positions import is_acl_rejection

    return is_acl_rejection(stderr, exit_code)


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

    T3.3: 包管理器缓存从项目级共享 ``<project>/.hiveweave-cache/<sub>``
    改为 **agent 私有** ``<temp_dir>/cache/<sub>`` —— 共享缓存上的并发
    ``npm install`` 互相持文件锁导致 EPERM · unlink（TEST_DSH_35 实测
    46 min 可消除税）。私有目录复用既有 temp 生命周期（per-agent、可撤销
    SID、dismiss 撤销 + R11 孤儿清扫），写入天然放行。共享 ``.hiveweave-cache``
    目录与其授权保留在盘（回滚位 / 将来做只读 warm 层），不再注入 env。
    """
    env = build_child_env(cwd, bash_markers=True)
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    # 沙箱不变量（TEMP/TMP/缓存覆盖）在 env_extra 之后强制写回 —— 调用方
    # 增量不得覆盖受限 temp / 私有缓存指针。
    env["TEMP"] = temp_dir
    env["TMP"] = temp_dir
    for var, sub in _CACHE_ENV_OVERRIDES.items():
        env[var] = os.path.join(temp_dir, "cache", sub)
    # P0（2026-09-05，temppatch.py）：私有 TEMP × pytest tmp_path 死锁 ——
    # CPython>=3.12 Windows 把 mkdir(mode=0o700) 落成 PROTECTED OWNER_RIGHTS
    # DACL，pytest 整条 tmp 链都是该形态，在私有锚点下造死岛（实测
    # [WinError 5]）。sandbox-temp 根的 sitecustomize shim 把受限子进程的
    # os.mkdir 中和回 0o777（新目录继承父 DACL —— 仍在「受限令牌 + 能力
    # SID」模型内，见 temppatch.SHIM_SOURCE）。目录前插既有 PYTHONPATH
    # （白名单透传值不丢）；shim 缺席时注入无害（site 静默忽略）。
    shim_dir = temppatch.shim_dir_for_temp_dir(temp_dir)
    existing_pp = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{shim_dir}{os.pathsep}{existing_pp}" if existing_pp else shim_dir
    )
    # GitSpawn 加固（2026-09-14，security-gitspawn-hiveweave-exposure 报告
    # P1-1）：本函数是 agent 受限命令的**唯一** env 构造点 —— bash/pwsh/
    # dev server/python_script 全部经 spawn_confined → ConfinedRunner 的
    # CreateProcessAsUserW，**绕过 util/win_subprocess 漏斗**（故
    # test_spawn_funnel_guard.py 扫不到这条路）。不在此补注入 ⇒ agent 自己
    # 在 pwsh 里跑的 `git status` 仍是裸的，P1-1 静默失效。
    # 位置放在最后：加固键不得被上方 TEMP/PYTHONPATH 等强制写回挤掉。
    from hiveweave.util.win_subprocess import apply_git_hardening

    return apply_git_hardening(env)


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

# P0-3：**封条知识的携带**（装配阶段 → 执行阶段）。
#
# 病灶：`_seal_git_bootstrap_files()` 返回「本次实际封了什么」的戳串，但唯一
# 调用点**丢弃返回值** ⇒ 执行阶段遇到 git 引导文件被拒时无从知道那是封条
#（§0 发现 #1：「知识存在、无消费者」）。
#
# ⚠ 为什么用记忆而**不重推**：封条函数是唯一知道封了什么的地方；在下游重新
#   枚举一遍 ＝ 复制它的判据 ⇒ 必然漂移（本仓「同一语义两处」的通病）。
# 键按 `boundary_root` 与 `project_root`（都是 realpath+normcase）双写 —— 执行
# 阶段拿到的是 boundary，装配阶段两个都知道。
_sealed_by_boundary: dict[str, tuple[str, ...]] = {}
_seal_guard = threading.Lock()


def _remember_sealed(policy, changed: list[str]) -> None:
    """把封条函数本次的产出记下来（幂等、只增不改）。"""
    if not changed:
        return
    keys: set[str] = set()
    for attr in ("boundary_root", "project_root"):
        value = getattr(policy, attr, None)
        if value:
            try:
                keys.add(os.path.normcase(os.path.realpath(value)))
            except Exception:  # noqa: BLE001 — 路径异常不该阻断 spawn
                continue
    if not keys:
        return
    with _seal_guard:
        for key in keys:
            _sealed_by_boundary[key] = tuple(changed)


def _sealed_for_boundary(boundary: str | None) -> tuple[str, ...]:
    """读取该边界下的封条戳；没有记录则空（= 「不知道」，不是「没封」）。"""
    if not boundary:
        return ()
    try:
        key = os.path.normcase(os.path.realpath(boundary))
    except Exception:  # noqa: BLE001
        return ()
    with _seal_guard:
        return _sealed_by_boundary.get(key, ())


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

    async def seal_agent_aces_async(self, path, sids: set[str],
                                    *, lock_against_delete: bool = False
                                    ) -> bool:
        return await asyncio.to_thread(
            self._g.seal_agent_aces, path, sids,
            strip_platform_delete=lock_against_delete)

    async def deny_delete_child_async(self, path, sids: set[str]) -> int:
        return await asyncio.to_thread(self._g.deny_delete_child, path, sids)

    async def deny_child_delete_for_all_async(self, path) -> int:
        return await asyncio.to_thread(
            self._g.deny_child_delete_for_all, path)


# ── #2 GitSpawn 治本：git「引导文件」封条（2026-09-15） ────────────────
# 判据来源是 `scripts/probe_git_write_surface.py`（真令牌，修前修后各跑一次），
# 不是读码推断。实测事实（修前）：
#   K1/K2  `extensions.worktreeConfig=true` 时 git **真的会读** `.git/config.worktree`
#          与 `.git/worktrees/<id>/config.worktree`（平台自己在 worktree 创建时就
#          打开这个扩展 ⇒ 两个载体天然活着）；
#   E1–E3  **三条** config 载体各自都能让平台的 `git add`（经唯一 spawn 漏斗）
#          执行 agent 的载荷 —— `filter.<n>.clean` 是**动态键名**，`GIT_CONFIG_*`
#          静态清单覆盖不到（既有边界 ①）；
#   R1/R1b  `<worktree>/.git` 这个 **gitdir 指针文件**可被 agent 改写 ⇒ 平台的
#          `git add` 直接去读 agent 目录里的 config ⇒ 载荷 EXECUTED（绕开全部 config 封条）；
#   D1–D3  三条载体在「只摘 ACE」的形态下**都能被 del 掉再重建**（Windows 删子项
#          两条准入路径：子对象 DELETE **或** 父目录 FILE_DELETE_CHILD）。
# ⇒ 故封条 = 「摘能力 SID ACE + PROTECTED」×〔载体文件〕 ＋「父目录 deny DC」。
#
# 封的是**能力 SID 全族**（`S-1-4-` 前缀，见 sid.py）：判据与「当前是谁在跑」无关
# —— 否则换个角色（bash_main vs worktree）同一文件又开一次口子。
_AGENT_SID_PREFIX = "S-1-4-"
# 需要「占位」的载体：不存在时由平台先建**空文件**再封 —— 否则 agent 可以自己
# 新建这个同名文件（父目录仍有写权），封条对它无效。空 config 对 git 是合法的。
_PLACEHOLDER_CARRIERS = ("config.worktree",)
# 「退休 worktree-config 扩展」的进程内去重：每项目每进程只跑一次（会 spawn 一个
# git 进程，不能每命令都跑）。⚠ 只做缓存不做失效：该标志一旦置 false 就永久有效
# （把 agent 写面关掉是单向动作），且失败**不**入缓存（下次重试）。
_WORKTREE_CONFIG_RETIRED: set[str] = set()


def _is_agent_sid(sid: str) -> bool:
    return sid.startswith(_AGENT_SID_PREFIX)


def _seal_subject_sids(policy) -> set[str]:
    """封条的 subject 集合 —— **派生**，不看目标文件上现在有什么 ACE。

    ⚠ 为什么不能用「文件上现有的能力 SID」当 subject（一手实测）：首轮 grant 与
    封条在**同一次** standing-grants 里先后执行，目标文件上那一刻可能还没有能力
    ACE ⇒ 摘了个寂寞，而下一轮又会被灌进来。实测（本批修后首跑）：只封到两个
    `config.worktree` 占位，`.git/config` 全程可写、E1–E3 仍 EXECUTED。
    （子对象何时拿到继承 ACE 与 NTFS 传播时机有关，本仓不再依赖该时机。）

    ⇒ subject = 本项目/本边界**所有可能被授予**的能力 SID（项目级 git/cache/
    shared/venv + 边界 + 每个已存在 worktree 自己 + temp/extras）。多给没关系：
    不存在的 SID 摘不掉任何东西，且 `seal_agent_aces` 幂等。
    """
    project = policy.project_root
    sids: set[str] = {
        git_sid(project),
        worktree_sid(project),
        cache_sid(project),
        shared_sid(project),
        policy.venv_sid_str,
        policy.temp_sid,
        worktree_sid(policy.boundary_root),
    }
    sids.update(policy.extra_sids or ())
    wt_root = os.path.join(project, ".git", "worktrees")
    if os.path.isdir(wt_root):
        for name in os.listdir(wt_root):
            sids.add(worktree_sid(os.path.join(wt_root, name)))
    sids.discard(None)
    return {s for s in sids if s}


def _agent_aces_leaking(path: str) -> list[str]:
    """读回复核：该路径上仍带写/删/改 DACL 位的**允许**能力 SID。

    只算 allow：deny（父目录禁删子项）本来就是我们要的形态。
    """
    from hiveweave.services.acl_sandbox.grant import ACE_ALLOWED

    leaking = []
    for ace_type, _f, mask, sid in _grant_aces(path):
        if ace_type == ACE_ALLOWED and _is_agent_sid(sid) and (
                mask & _SEAL_WRITE_BITS):
            leaking.append(sid)
    return sorted(leaking)


# 封条要摘干净的位：写 + 删 + 删子项 + 改 DACL/属主（后两者本仓从不授予，
# 但读回复核按「一律不许」判，免得将来某处放宽后封条静默失效）
_SEAL_WRITE_BITS = GRANT_MASK | WRITE_DAC | WRITE_OWNER


def _grant_aces(path: str) -> list[tuple[int, int, int, str]]:
    """[(ace_type, flags, mask, sid)] —— 供 service 层判「封条是否已生效」。"""
    from hiveweave.services.acl_sandbox.grant import WriteGrant

    return WriteGrant.list_aces(path)


def unlock_git_lockdown(project_root: str) -> int:
    """**清理前解锁**：把 R1 锁死的 git 路径恢复成可删（best-effort，同步）。

    给两个清理路径用：项目删除的 `rmtree`、worktree 移除（`git worktree remove`）——
    否则它们会在 `.git/config` / `<gitdir>/config.worktree` 上 PermissionError
    （锁死档 + 全主体 FC deny 把删位摘掉了）。

    解锁项 = 三个载体文件 + 承载它们的目录（`.git`、各 `<gitdir>`、项目根）的
    Everyone-FC deny。返回改动数；**失败只记 warning**（清理路径不该因解锁失败而中止）。
    """
    from hiveweave.services.acl_sandbox.grant import WriteGrant

    project = os.path.realpath(project_root)
    git_dir = os.path.join(project, ".git")
    if not os.path.isdir(git_dir):
        return 0
    targets: list[str] = []
    for name in ("config", "config.worktree"):
        targets.append(os.path.join(git_dir, name))
    wt_root = os.path.join(git_dir, "worktrees")
    if os.path.isdir(wt_root):
        for name in sorted(os.listdir(wt_root)):
            gd = os.path.join(wt_root, name)
            if os.path.isdir(gd):
                targets.append(os.path.join(gd, "config.worktree"))
                targets.append(gd)
    targets.append(git_dir)
    targets.append(project)
    changed = 0
    for path in targets:
        try:
            if WriteGrant.unlock_for_delete(path):
                changed += 1
        except Exception:
            log.warning("acl_sandbox.unlock_for_delete_failed", path=path)
    if changed:
        log.info("acl_sandbox.git_lockdown_unlocked", count=changed,
                 project=project)
    return changed


async def _seal_git_bootstrap_files(policy, agrant: _AsyncGrant) -> list[str]:
    """封住 git 的引导文件（agent 不可写、不可删建）。返回本次实际改动的项。

    fail-closed：任一环节失败抛 `SandboxUnavailableError` —— 封条是安全不变量，
    「封不上还继续跑」正是本仓最反感的「看似有守卫」。列不出目标（`.git` 不存在）
    是合法空态（项目未初始化 git），直接返回。
    """
    project = os.path.realpath(policy.project_root)
    git_dir = os.path.join(project, ".git")
    if not os.path.isdir(git_dir):
        return []
    changed: list[str] = []
    sids = _seal_subject_sids(policy)

    async def seal_file(path: str, *, lock_against_delete: bool = False
                        ) -> None:
        created = False
        if not os.path.exists(path):
            if os.path.basename(path) not in _PLACEHOLDER_CARRIERS:
                return  # 非占位载体不存在 ⇒ 无事可做（不是缺口）
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8"):
                    pass
                created = True
            except OSError as exc:
                raise SandboxUnavailableError(
                    f"cannot create git bootstrap placeholder {path}: {exc}. "
                    f"agent 可自行新建该文件并让平台 git 读它 ⇒ 拒绝继续",
                ) from exc
        wrote = await agrant.seal_agent_aces_async(
            path, sids, lock_against_delete=lock_against_delete)
        # 读回复核（§4.11 同族纪律）：摘完必须**读到干净**，否则 fail-closed。
        # 没有这步，「摘了个寂寞」与「已封」在日志上长得一样。
        leaking = _agent_aces_leaking(path)
        if leaking:
            raise SandboxUnavailableError(
                f"seal read-back failed: {path} 仍有能力 SID 写位 {leaking} "
                f"—— git 引导文件未封住，拒绝继续执行 agent 命令",
                platform_side=True,
            )
        if wrote or created:
            changed.append(f"{'create+' if created else ''}seal:{path}")

    async def deny_dc(path: str) -> None:
        """禁删子项 —— **对所有人**（不只能力 SID）。

        见 `WriteGrant.deny_child_delete_for_all` 的 docstring：删子项两条路径都走
        pass-1，只 deny 能力 SID 挡不住（D1–D3 / rmdir .git 实测）。
        """
        if await agrant.deny_child_delete_for_all_async(path):
            changed.append(f"deny-dc-all:{path}")

    # ① 退休 `extensions.worktreeConfig`（fixqueue #2 残余 R3）。
    #    ⚠ **必须早于下面任何一个 seal**：锁死档会连平台主体的 DELETE 一起摘，
    #    之后再 `git config <写>` 就会失败（本行顺序是硬约束，改动前先读这段）。
    #    只要它开着，git 就会读 `<proj>/.git/config.worktree` 与
    #    `<gitdir>/config.worktree` —— 后者在 agent 可写面内（worktree gitdir 必须
    #    可写），且 `filter.<n>.clean`/`merge.<n>.driver` 是动态键名
    #    （`GIT_CONFIG_*` 覆盖不到）⇒ 实测平台进程会执行 agent 写的驱动。
    #    ⚠ 存量项目也要治（老版本在 worktree 创建时开过它）⇒ 放在**每次受限命令的
    #    grant 阶段**（去重成每项目每进程一次），不依赖「新建 worktree」。
    #    fail-quiet：关不上只记 warning（封条仍在，且 identity 已改 `-c` 注入）。
    if project not in _WORKTREE_CONFIG_RETIRED:
        try:
            from hiveweave.services.git_worktree.git_identity import (
                retire_worktree_config,
            )

            if await retire_worktree_config(project):
                _WORKTREE_CONFIG_RETIRED.add(project)
            else:
                # 审计 2026-09-15 ②-8：关不上就等于 R3 载体仍然活着（git 会读
                # `<gitdir>/config.worktree` 里的动态键名驱动）⇒ **fail-closed**，
                # 不许静默降级成 warning。
                raise SandboxUnavailableError(
                    f"无法退休 extensions.worktreeConfig（{project}）—— "
                    f"worktree config 载体仍会被 git 读，拒绝执行 agent 命令。"
                    f"修复：确认 `.git/config` 可写（必要时删掉 "
                    f"<data_root>/git-anchor 下的记录或用 unlock 解锁）后重试")
        except SandboxUnavailableError:
            raise
        except Exception as exc:
            log.warning("acl_sandbox.worktree_config_retire_error",
                        project=project, error=str(exc)[:200])
            raise SandboxUnavailableError(
                f"退休 extensions.worktreeConfig 时出错（{project}）：{exc}") from exc

    # ② `.git` **根**：不授写 —— 这是本条的病因，也是唯一留得住的杠杆。
    #    为什么不「只封住 config 文件」：`git config` 是 **lock + rename 替换**，
    #    而「删」走 pass-1（user ACE，封条挡不住）、「建」走父目录 create 权
    #    （pass-2 管得住 —— 实测工作区外 create 被拒）。只封文件 ⇒ agent 一条
    #    `git config` 就把封条换成一个继承 `.git` ACE 的新文件（实测：_dbg_seal
    #    里 config 从 protected=True 变回 protected=False 且带回能力 ACE）。
    #    ⇒ 把写面从「`.git` 整棵」收成「git 真正需要写的子目录」。
    await agrant.seal_agent_aces_async(git_dir, sids)
    leaking_root = _agent_aces_leaking(git_dir)
    if leaking_root:
        raise SandboxUnavailableError(
            f"seal read-back failed: {git_dir} 仍可被 agent 写 {leaking_root} "
            f"—— 配置载体可被 lock+rename 替换，拒绝继续执行 agent 命令",
            platform_side=True,
        )
    # git 真正需要 agent 写的子目录（agent 自己的 add/commit 落在 worktree gitdir
    # + 共享 objects/refs/logs；**不含** `.git` 根、不含 `info/`、不含 `hooks/`）
    for name in ("objects", "refs", "logs"):
        d = os.path.join(git_dir, name)
        if os.path.isdir(d):
            await _grant_if_missing(d, git_sid(project), GRANT_MASK, agrant)

    # ② 主 config + 主 worktree config（占位）——**锁死档**：连平台主体的
    #    DELETE/DC 一起摘（R1 双阶段）。理由：`git config` 是 lock+rename，
    #    只摘能力 SID 时 agent 仍能凭**普通令牌的 user ACE** 把文件删掉（实测
    #    D1/V2 = DELETED）⇒ 删掉后平台下次 `git config` 会以**继承 ACE** 重建它，
    #    窗口期等于把封条让给了 agent。锁死后谁都替换不掉。
    #    代价：平台自己也不再用 `git config <写>` 改它 —— 已核对平台写点只有
    #    ① `ensure_git_repo` 的两处 init 写入（本函数之前）② `retire_worktree_config`
    #    （已前置到本函数之上且改成「先读、非 false 才写」）。
    for name in ("config", "config.worktree"):
        await seal_file(os.path.join(git_dir, name), lock_against_delete=True)

    # ③ 每个 worktree 的 gitdir：本身要继续可写（agent 的 index/index.lock 在
    #    那里），故这里封的 `config.worktree` / `commondir` **只是提高门槛**：
    #    gitdir 可写 ⇒ 仍可删+重建（残余，见 fixqueue #2「已实测残余」）。
    wt_root = os.path.join(git_dir, "worktrees")
    if os.path.isdir(wt_root):
        for name in sorted(os.listdir(wt_root)):
            gd = os.path.join(wt_root, name)
            if not os.path.isdir(gd):
                continue
            await _grant_if_missing(gd, git_sid(project), GRANT_MASK, agrant)
            # ⚠ 这里**只做普通封条、不锁死**：该载体已随 `extensions.worktreeConfig`
            #   退休而死（git 根本不读它），而 `git worktree prune/remove` 要删掉**整个
            #   `<gitdir>`** —— 锁死档会让 prune 删不掉（审计 2026-09-15 实测：
            #   `prune` rc=0 但报 `failed to delete ...: Invalid argument`，注册项残留
            #   ⇒ 同名 worktree 再建永久失败）。锁死不带来安全收益，只带来这个副作用。
            await seal_file(os.path.join(gd, "config.worktree"))
            await seal_file(os.path.join(gd, "commondir"))
            await deny_dc(gd)
    await deny_dc(git_dir)
    # `.git` 本体的删除走**父目录**的 DC 那一条路（`.git` 自己已无 agent DELETE）
    # ⇒ 项目根也要禁删子项，否则 agent 一条 `rmdir /s /q .git` 就把整个仓端掉
    # （实测：项目根边界形态 rc=0 删成功）。只影响「直接子项里自己没有 agent DELETE」
    # 的那些（`.git`/`.hiveweave`）——普通文件仍可删。
    if os.path.realpath(policy.boundary_root) == project:
        await deny_dc(project)

    # ③ worktree 边界：`<wt>/.git` 是 gitdir 指针 —— 改它 = 平台在 worktree 里的
    #    git 去读 agent 目录的 config。
    #    ⚠ **先校验再封**（审计 2026-09-15 A1）：`os.path.isfile` 为假时**不能静默
    #    跳过**（早先形态：指针被删后每次都跳过，等于封条无声消失）；也不是
    #    「写不进去」就安全 —— 删这一侧走 pass-1（user ACE），agent 删得掉指针，
    #    再建一个**同名目录** `.git/`（内含自己的 config）⇒ 平台在该 worktree 的
    #    `git add -A` 照样执行其载荷（审计实测三段全通）。
    #    ⇒ 判据落成「指针**身份**」：必须是指向本 worktree 期望 gitdir 的文件；
    #    否则 fail-closed（把静默变成 loud，且不给它继续工作的权限）。
    boundary = os.path.realpath(policy.boundary_root)
    if boundary != project:
        wt_git = os.path.join(boundary, ".git")
        if not os.path.isfile(wt_git):
            raise SandboxUnavailableError(
                f"worktree gitdir 指针不是文件（或已被删除/替换成目录）：{wt_git}"
                f" —— 平台在该 worktree 的 git 会去读非平台指定的 gitdir，"
                f"拒绝继续执行 agent 命令")
        expected = os.path.join(git_dir, "worktrees", os.path.basename(boundary))
        if not _points_at(wt_git, expected):
            raise SandboxUnavailableError(
                f"worktree gitdir 指针未指向期望 gitdir：{wt_git} "
                f"（期望 {expected}）—— 可能已被 agent 改写，拒绝继续执行")
        await seal_file(wt_git)
        await deny_dc(boundary)

    if changed:
        log.info("acl_sandbox.git_bootstrap_sealed", count=len(changed),
                 items=changed[:8])

    return changed


def _points_at(pointer_path: str, expected_gitdir: str) -> bool:
    """`<wt>/.git` 指针内容是否指向期望 gitdir（身份判据，不是意图判据）。

    容忍 git 的书写差异：`gitdir:` 前缀、正/反斜杠、大小写、尾随空白/换行。
    比较目标路径的 realpath，避免 `..`/短名造成假红。
    """
    try:
        raw = open(pointer_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    text = raw.strip()
    if text.lower().startswith("gitdir:"):
        text = text[len("gitdir:"):].strip()
    if not text:
        return False
    got = os.path.realpath(text.replace("\\", os.sep).replace("/", os.sep))
    want = os.path.realpath(expected_gitdir)
    return os.path.normcase(got) == os.path.normcase(want)



async def _ensure_standing_grants(policy, agrant: _AsyncGrant) -> None:
    """verify-then-skip 补授：主体探测 → .hiveweave 裁剪 → 边界根 + 项目级 git/缓存。"""
    root = policy.boundary_root
    project = policy.project_root
    if not await agrant.has_subject_write_ace_async(root):
        # ✅ 2026-09-17 第四轮审计 HIGH：**保留** `platform_side=True` —— 与其他
        # 「无真实主体写 ACE」点**不同族**。理由（信息优势）：`boundary_root` 是
        # 平台**装配面**（workspace 根由平台/部署流程给定，不是 agent 执行期内
        # 自建的目录），本点判的是"部署前提未满足"（用户把 workspace 放在
        # OWNER_RIGHTS-only 目录下）⇒ 构造点确有信息优势。对照下方
        # `_ensure_standing_grants` 里 additional-dir 那条**已退回不标**：
        # 那里 agent 可自建目录，判据不再指向平台。
        raise SandboxUnavailableError(
            f"workspace 根 {root} 无真实主体写 ACE（OWNER_RIGHTS-only 或缺失 ACL），"
            f"write-restricted 令牌不可用。请把 workspace 放在用户常规目录下"
            f"（如 C:\\Users\\<user>\\ 或含 AuthUsers:Modify 的目录）。",
            platform_side=True,
        )

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
        # #2：**不再**对 `.git` 整棵授 GRANT_MASK（那正是「agent 能改 config」的
        # 来源，且会被 lock+rename 一路穿透封条）。改由封条函数收窄写面 + 只授
        # git 真正需要写的子目录（objects/refs/logs + 各 worktree gitdir）。
        # P0-3：**接住返回值** —— 封条函数说的话是执行阶段判「封条拒绝」的唯一依据。
        _remember_sealed(policy, await _seal_git_bootstrap_files(policy, agrant))

    cache_dir = policy.cache_dir
    if not os.path.isdir(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    await _grant_if_missing(cache_dir, cache_sid(project), CACHE_MASK, agrant)

    # 39 审计 P0-1：项目 .venv 是依赖安装的**官方落点**（venv_setup 在项目创建
    # 时铺设；本处补 ACL 授予）。GRANT_MASK 全权（agent 经 uv pip 往里装包 =
    # 与写工作区代码同等信任，DSH 同构：workspaceRoot 全可写）。目录不存在时
    # makedirs 兜底（老项目/venv_setup 失败的形态——空目录仍可被 agent 用
    # `python -m venv` 或 `uv venv` 补建）。
    venv_dir = policy.venv_dir
    if not os.path.isdir(venv_dir):
        os.makedirs(venv_dir, exist_ok=True)
    await _grant_if_missing(venv_dir, policy.venv_sid_str, GRANT_MASK, agrant)

    # s3c09 git×ACL 死锁修复（42 轮实证，2026-09-05）：`.hiveweave/shared`
    # 是 git **跟踪** 的跨 agent 契约区（info/exclude 反选维持跟踪），worktree
    # 内 git rebase/checkout/unlink 要写删 `<wt>/.hiveweave/shared/*`；上方
    # break_inheritance 已把 `.hiveweave` 裁成 PROTECTED（先裁剪后根 grant 的
    # 既有不变量），shared 子树对受限令牌双 pass 全落空 —— 实证
    # `unable to create file .hiveweave/shared/m2-interface.md` + unlink
    # warning。对边界 shared 子树补授**项目级** shared_sid（GRANT_MASK 同
    # worktree/git 授法；OI/CI 继承 → 子树内新建文件自动带 ACE）。
    # 授予面 = **所有边界**（2026-09-22 P2-2「团队网盘」起；此前只授 worktree
    # 边界 ⇒ MAIN 边界（CEO/HR/bash_main）写不了网盘）。授予面**只在 shared
    # 子树**，其余 `.hiveweave` 子目录不受影响（真令牌探针 N3a/N3b/N3c 全拒）。
    # ⚠ 目录不存在仍**跳过**（老项目/尚未物化）—— 那是**刻意设计**、有守卫
    # （`test_shared_absent_skip_then_backfill`：跳过不报错、物化后下一条命令补授）。
    # 本轮**不**改成"平台代建"：那会越过 P2-2 的范围，且物化本就有自愈路径
    # （`write_file` 平台侧可建 / worktree 内 agent 可 mkdir —— 探针 N1m ALLOWED）。
    if policy.shared_sid_str:
        try:
            if os.path.isdir(policy.shared_dir):
                # P0（audit 2026-09-05）：shared 根本身是 junction/symlink
                #（如被跟踪的 symlink 经 checkout 物化）→ 整棵跳过 —— 对
                # reparse 根授予 = 给边界外目标子树落持久可继承 ACE。
                # GetFileAttributes 含 FILE_ATTRIBUTE_REPARSE_POINT 即拦。
                if await asyncio.to_thread(_is_reparse_point, policy.shared_dir):
                    log.warning(
                        "acl_sandbox.shared_dir_reparse_skip",
                        shared_dir=policy.shared_dir,
                        hint="shared 根是 junction/symlink，拒绝授予与子树扫描",
                    )
                else:
                    try:
                        await _grant_if_missing(
                            policy.shared_dir, policy.shared_sid_str,
                            GRANT_MASK, agrant,
                        )
                    except SandboxUnavailableError as e:
                        log.warning(
                            "acl_sandbox.shared_grant_failed",
                            shared_dir=policy.shared_dir, error=str(e),
                        )
                    else:
                        # 存量升级：PROTECTED 死岛没接到自动传播 —— 水位 walker
                        # verify-then-skip 补授（能力 SID + 死岛 normal-pass 用户
                        # SID，双 pass 对齐令牌评估；junction/symlink 跳过）。
                        # failed/truncated>0 时不落水位 —— 下一命令重扫。
                        user_sid = await asyncio.to_thread(
                            temppatch.current_user_sid
                        )
                        repaired, failed, truncated = await asyncio.to_thread(
                            _repair_shared_islands_once,
                            policy.shared_dir, policy.shared_sid_str, user_sid,
                        )
                        if repaired:
                            log.info(
                                "acl_sandbox.shared_islands_repaired",
                                shared_dir=policy.shared_dir,
                                repaired=repaired,
                            )
                        if failed or truncated:
                            log.warning(
                                "acl_sandbox.shared_walk_incomplete",
                                shared_dir=policy.shared_dir,
                                failed=failed, truncated=truncated,
                            )
            else:
                log.debug(
                    "acl_sandbox.shared_dir_absent_skip",
                    shared_dir=policy.shared_dir,
                )
        except Exception as e:  # noqa: BLE001 —— shared 补授绝不阻断 spawn
            log.warning(
                "acl_sandbox.shared_grant_failed",
                shared_dir=policy.shared_dir, error=str(e),
            )

    # §P1-1 工作区共享缓存目录补授 AU 写（M1 测试类命令 EPERM 根因之一）：
    # pytest/vitest/node 会写 .pytest_cache/__pycache__/node_modules/.cache 等，
    # 受限进程自建时是 OWNER_RIGHTS-only 且不继承父 ACE → 多 worktree 写同一
    # 目录互斥。对已存在的缓存类目录补授 AU 写（verify-then-skip，幂等）。
    # 不影响任何受限代理的**外部**写能力（AU ACE 只作用于工作区内这些缓存目录）。
    for cdir in await asyncio.to_thread(_collect_shared_cache_dirs, root):
        await asyncio.to_thread(WriteGrant.grant_shared_cache_write, cdir)

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
            # ⚠ 2026-09-17 第四轮审计 HIGH：**故意不标** `platform_side`
            # （与上方 workspace 根那条**不同**，勿顺手补回）。
            # 判据 `has_subject_write_ace_async(d)` 只是"/该目录 ACL 的实际状态/",
            # 构造点对这种状态**没有信息优势**：本段注释上一句自己就写了
            # "**不自动创建**（平台自建目录是 OWNER_RIGHTS-only，对受限令牌不可
            # 用）" ⇒ agent 在数据根下 `mkdir` 一个附加可写目录、再把它填进
            # 项目配置，就是**已知可达路径**；此时 ACL 不满足正是 agent 自己的
            # 部署动作造成的。标成平台侧 = 替 agent 卸责（agent 收到「不是你
            # 的问题」而放弃自查）。故退回默认 `False` ⇒ `outcome_unknown`。
            # 标注标准见 `errors.is_platform_side` docstring：只有构造点对该故障
            # **确有信息优势**（真的在调 Win32 API / 在装配平台自己的目录）才标。
            raise SandboxUnavailableError(
                f"附加可写目录 {d} 无真实主体写 ACE（OWNER_RIGHTS-only 或缺失 ACL），"
                f"write-restricted 令牌不可用。请把目录放在用户常规目录下。",
            )
        await _grant_if_missing(d, extra_sid(d), GRANT_MASK, agrant)


async def _ensure_temp(policy, agrant: _AsyncGrant) -> None:
    """agent 私有 temp：重建 + revocable 授予（verify-then-skip，无正向缓存）。"""
    temp_dir = policy.temp_dir
    if not os.path.isdir(temp_dir):
        os.makedirs(temp_dir, exist_ok=True)
    if not await agrant.ace_present_async(temp_dir, policy.temp_sid, GRANT_MASK):
        await agrant.grant_standing_async(temp_dir, policy.temp_sid, GRANT_MASK)
    # P0（2026-09-05）：sitecustomize shim（幂等）+ 存量 OWNER_RIGHTS 死岛
    # 浅层补授（上一轮遗留的 pytest-of-* 等）。fail-soft —— 任一失败只告警，
    # 回退既有行为，下一命令 verify-then-skip 重试。P2（audit）：walker 走
    # 进程内 per-anchor 水位（同锚点只全跑一次，稳态不再每命令扫锚点子树；
    # probe_private_temp 路径不受水位限制仍全跑）。
    try:
        shim_dir = await asyncio.to_thread(
            temppatch.ensure_sitecustomize_shim,
            temppatch.shim_dir_for_temp_dir(temp_dir),
        )
        user_sid = await asyncio.to_thread(temppatch.current_user_sid)
        repaired = await asyncio.to_thread(
            temppatch.repair_temp_islands_once, temp_dir, policy.temp_sid,
            user_sid,
        )
        if repaired:
            log.info(
                "acl_sandbox.temp_islands_repaired",
                temp_dir=temp_dir, repaired=repaired, shim_dir=shim_dir,
            )
    except Exception as e:  # noqa: BLE001 —— 死锁修复是增量，绝不阻断 spawn
        log.warning(
            "acl_sandbox.temp_patch_failed", temp_dir=temp_dir, error=str(e)
        )


def _maybe_append_rejection_hint(agent_id: str, boundary: str, result: dict) -> dict:
    """拒绝提示：**成因先判定，文案再驱动**（P0-3）。

    `denied_by` 落在结果上（结构化，供 run_steps / 回执消费），文案按它选模板
    —— 旧实现无论成因一律说"目标在授权树之外"，实测 57.6% 是假话。
    """
    from hiveweave.tools.fact_positions import classify_denied_by, sealed_match
    from hiveweave.tools.result import SEALED_BY_PREFIX

    stderr_in = result.get("stderr", "")
    # P0-3：把**装配阶段记住的封条**带进来 ⇒ 才知道这次拒绝是不是封条所致。
    sealed = _sealed_for_boundary(boundary)
    denied_by = classify_denied_by(
        stderr_in, result.get("exit_code"), boundary_root=boundary,
        sealed=sealed,
    )
    telemetry.record_rejection(denied_by is not None)
    if denied_by is None:
        return result
    result = dict(result)
    result["denied_by"] = denied_by

    if denied_by == "sealed_git":
        # 如实记录「谁封的 + 封的是哪个目标」（值形态见 SEALED_BY_PREFIX）
        matched = sealed_match(stderr_in, sealed)
        if matched:
            result["sealed_by"] = f"{SEALED_BY_PREFIX}{matched}"
    # P0-3：`blocked_by_environment` = 「方言命中**且 runner 没失败**」。
    # 判据用状态（exit_code 非 None = 进程确实跑过），不用文案。
    result["blocked_by_environment"] = (
        True if result.get("exit_code") is not None else None
    )
    # P0-3 / 审计 A3：判成因与选文案都要看**同结果上已有的事实位**。
    # `enforcement` 是决策面戳（confined|native）：native 回落时沙箱**没生效**，
    # 此处不过是一次普通 OS 权限拒绝 ⇒ 再印「写入被沙箱拒绝…授权树（X）之外」
    # 就是新的假话（那棵树在 native 模式下并不存在）。
    # ⚠ 结构化事实照落（`denied_by` 与 `blocked_by_environment` 都要记），只是不追加文案。
    enforcement = result.get("enforcement")
    if enforcement is not None and enforcement != ENF_CONFINED:
        return result
    with _hint_guard:
        n = _hint_counts.get(agent_id, 0)
        _hint_counts[agent_id] = n + 1
    if n % _HINT_EVERY_N_ROUNDS != 0:
        return result
    hint = _HINT_BY_DENIED_BY.get(denied_by, _REJECTION_HINT).format(boundary=boundary)
    result["stderr"] = stderr_in + f"\n\n[沙箱提示] {hint}"
    return result


async def spawn_confined(
    *,
    command: str | None = None,
    argv: list[str] | None = None,
    workdir: str,
    workspace_path: str,
    agent_id: str,
    project_id: str | None = None,
    project_workspace_path: str | None = None,
    timeout_s: float | None = None,
    entry: str = "bash",
    long_running: bool = False,
    env_extra: dict[str, str] | None = None,
    decision: SpawnDecision | None = None,
) -> dict | None:
    """受限执行入口。返回 None 的仅两种情形：非 Windows / 配置关 / 项目级逃生门。

    ``project_workspace_path`` = 项目根（git/cache SID 派生源 §4.8/§8）；
    缺省回退到 workspace_path（P0 单目录形态）。
    ``env_extra`` = 调用方增量 env（dev server 端口注入等）。
    E10：优先 ``argv``（逐元素引用修剥引号根因）；不传回退整串 ``command``。

    ``decision``（#1 治本）：**判定由 `policy.resolve_spawn_decision` 做，
    本函数的职责只是执行**。不传 ⇒ 本函数自己调判定（兼容既有调用方，
    行为与改造前逐字节一致）；传了 ⇒ **不重判**（避免判定与执行之间的
    TOCTOU），且判定为原生时**抛 ValueError** —— 路由必须由
    `entry.spawn_agent_command` 做，把原生判定传进来再期待"自动降级"
    正是本条要消灭的形态（同一个 None 被五处各自解释）。

    结果字典随 `enforcement*` 戳一并返回（见 `SpawnDecision.stamp`）：
    强度是 **partial**（受限令牌只约束写），不是"已隔离"。
    """
    if argv is None and command is None:
        raise ValueError("spawn_confined requires command or argv")

    decision_explicit = decision is not None
    if decision is None:
        # 经模块属性调用（不是 import 名）：判定点是全平台唯一的接缝。
        from hiveweave.services.acl_sandbox import policy

        decision = await policy.resolve_spawn_decision(project_id)
    if not decision.confined:
        if decision_explicit:
            raise ValueError(
                f"spawn_confined 收到原生判定（reason={decision.reason!r}）——"
                "spawn 路由必须由 entry.spawn_agent_command 做；"
                "不要把判定结果传进来再期待它自动降级（#1：None 的五种解释）"
            )
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
            # 0-3：把「这份 env 到底带没带 git 加固」读成事实位随结果上报。
            # `_build_sandbox_env` 末尾调 `apply_git_hardening`（幂等），所以这里
            # 读到的是**实际**写进子进程的那份 env —— 不是对代码路径的推断。
            from hiveweave.util.win_subprocess import git_hardened as _gh_env

            _git_hardened = _gh_env(env)
            runner = _ensure_runner()
            if long_running:
                job = await runner.run_long_running(
                    token, command, workdir, env,
                    **({"argv": argv} if argv is not None else {}),
                )
                return {
                    "long_running": True,
                    "job": job,
                    "pid": job.pid,
                    "temp_dir": policy.temp_dir,
                    "cache_dir": policy.cache_dir,
                    # T3.3: 实际注入 env 的包管理器缓存根（agent 私有）
                    "private_cache_dir": os.path.join(
                        policy.temp_dir, "cache"
                    ),
                    "git_hardened": _git_hardened,
                    **decision.stamp(boundary_root=policy.boundary_root),
                }
            result = await runner.run_foreground(
                token, command, workdir, env, timeout_s,
                **({"argv": argv} if argv is not None else {}),
            )
            result = {
                **(result or {}),
                "git_hardened": _git_hardened,
                **decision.stamp(boundary_root=policy.boundary_root),
            }
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
