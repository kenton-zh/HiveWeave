"""s3c09 git×ACL 死锁修复验收：`.hiveweave/shared` git 跟踪文件的受限令牌写/删。

42 轮实证（s3-clone_09 苍岩）：worktree 内 ``git rebase main`` 报
``unable to create file .hiveweave/shared/m2-interface.md: Permission denied``
+ unlink warning —— shared 是 git **跟踪** 的跨 agent 契约区
（info/exclude 反选维持跟踪），受限令牌的 git 操作要写删
``<wt>/.hiveweave/shared/*``；而 ``.hiveweave`` 建树时 break_inheritance 成
PROTECTED（「先裁剪后根 grant」不变量），shared 子树对受限令牌双 pass 全落空。

修复 = 项目级 ``shared_sid``（``shared\\0`` 域派生，**per-project** 非
per-agent）：

- 授予：``_ensure_standing_grants`` 对 worktree 边界 ``<wt>/.hiveweave/shared``
  子树授 GRANT_MASK + OI/CI（在 break_inheritance 之后同批，verify-then-skip
  幂等；存量 PROTECTED 死岛走水位 walker 补授，fail-soft）；
- 携带：仅 worktree 边界（executor + builder coordinator）的 token 带
  shared_sid —— CEO/HR/bash_main 项目根边界不授予不携带，HR/只读授予面不变。

本文件用真实受限令牌（spawn_confined / _create_restricted_token）做端到端验收，
基础设施与 test_acl_sandbox_basetemp / test_acl_sandbox_mutation 同款（自包含）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from hiveweave.config import settings
from hiveweave.services.acl_sandbox.grant import GRANT_MASK, WriteGrant
from hiveweave.services.acl_sandbox.policy import resolve_policy
from hiveweave.services.acl_sandbox.service import (
    _AsyncGrant,
    _build_sandbox_env,
    _ensure_runner,
    _ensure_standing_grants,
    _ensure_temp,
    spawn_confined,
)
from hiveweave.services.acl_sandbox.token import (
    RestrictedTokenFactory,
    _create_restricted_token,
)

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("ACL sandbox shared-dir tests require Windows",
                allow_module_level=True)


@pytest.fixture(scope="session", autouse=True)
def _shutdown_acl_runner():
    """会话结束回收排空池/watcher 线程 —— 非守护线程会阻塞进程退出。"""
    yield
    from hiveweave.services.acl_sandbox.service import shutdown_runner
    from hiveweave.services.acl_sandbox.spawn import stop_watcher

    stop_watcher()
    shutdown_runner()


COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


# ── 与 basetemp/mutation 同款基础设施（自包含，避免跨测试文件导入） ──
def _dir_has_user_ace(d: Path, ws, user) -> bool:
    try:
        sd = ws.GetNamedSecurityInfo(
            str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
    except ws.error:
        return False
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return False
    for i in range(dacl.GetAceCount()):
        ((t, _f), _m, s) = dacl.GetAce(i)
        if t == ws.ACCESS_ALLOWED_ACE_TYPE and s == user:
            return True
    return False


def _ensure_subject_ace(path: Path) -> None:
    """§4.12：给目录及其 OWNER_RIGHTS-only 祖先补当前用户 SID 全权 ACE。"""
    import win32api
    import win32security as ws

    tok = ws.OpenProcessToken(win32api.GetCurrentProcess(), ws.TOKEN_QUERY)
    user, _ = ws.GetTokenInformation(tok, ws.TokenUser)
    tok.Close()

    def _grant(d: Path) -> None:
        import win32con

        sd = ws.GetNamedSecurityInfo(
            str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            return
        dacl.SetEntriesInAcl([{
            "AccessPermissions": 0x1F01FF,
            "AccessMode": ws.GRANT_ACCESS,
            "Inheritance": win32con.CONTAINER_INHERIT_ACE
            | win32con.OBJECT_INHERIT_ACE,
            "Trustee": {
                "MultipleTrustee": None,
                "MultipleTrusteeOperation": 0,
                "TrusteeForm": ws.TRUSTEE_IS_SID,
                "TrusteeType": ws.TRUSTEE_IS_UNKNOWN,
                "Identifier": user,
            },
        }])
        ws.SetNamedSecurityInfo(
            str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION,
            sd.GetSecurityDescriptorOwner(), sd.GetSecurityDescriptorGroup(),
            dacl, None)

    if not _dir_has_user_ace(path, ws, user):
        _grant(path)
    anc = path.parent
    while anc != anc.parent and str(anc).lower() != str(anc.anchor).lower():
        if _dir_has_user_ace(anc, ws, user):
            break
        _grant(anc)
        anc = anc.parent


@pytest.fixture(autouse=True)
def _sandbox_on(monkeypatch):
    monkeypatch.setattr(settings, "acl_sandbox", True)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """项目根（模拟用户常规目录），含 MAIN 侧 `.hiveweave/shared`。"""
    d = tmp_path / "proj"
    d.mkdir(parents=True)
    _ensure_subject_ace(d)
    (d / ".hiveweave" / "shared").mkdir(parents=True, exist_ok=True)
    return d


def _make_worktree(project: Path, short_id: str):
    """worktree 边界布局（`<project>/.hiveweave/worktrees/<sid>` 同构真实形态）。"""
    wt = project / ".hiveweave" / "worktrees" / short_id
    shared = wt / ".hiveweave" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    return wt, shared


async def _spawn(project: Path, wt: Path, agent_id: str, command: str, *,
                 timeout_s: float = 60):
    return await spawn_confined(
        command=command, workdir=str(wt),
        workspace_path=str(wt),
        project_workspace_path=str(project),
        agent_id=agent_id, timeout_s=timeout_s, entry="bash")


async def _spawn_main(project: Path, agent_id: str, command: str, *,
                      timeout_s: float = 60):
    """**项目根边界**形态（CEO / HR / `pwsh_main`；`root == project`）。

    ⚠ 形态必须交代：`workspace_path == project_workspace_path` ⇒
    `resolve_policy` 得 `boundary == project`。P2-2 之前这一侧**不带**
    shared SID（正是网盘写不了的来源）。
    """
    return await spawn_confined(
        command=command, workdir=str(project),
        workspace_path=str(project),
        project_workspace_path=str(project),
        agent_id=agent_id, timeout_s=timeout_s, entry="bash_main")


def _cmd(inner: str) -> str:
    return f'"{COMSPEC}" /c {inner}'


# ── 端到端：真实受限令牌写/删/重建 shared 下的 tracked 文件 ──────
async def test_shared_tracked_file_write_unlink_recreate(project: Path) -> None:
    """修复验收：受限令牌对 `<wt>/.hiveweave/shared/m2-interface.md` 写、删、
    重建全部成功（s3c09 rebase/unlink 死锁形态的最小复刻）。"""
    wt, shared = _make_worktree(project, "A001")
    tracked = shared / "m2-interface.md"
    tracked.write_text("v1\n", encoding="utf-8")

    policy = resolve_policy(workspace_path=str(wt), agent_id="A001",
                            project_workspace_path=str(project))
    assert policy.shared_sid_str, "worktree 边界必须携带 shared SID"
    # sanity（修复前形态）：授前 shared 子树无 shared_sid ACE
    assert not WriteGrant.ace_present(
        str(shared), policy.shared_sid_str, GRANT_MASK)

    r = await _spawn(
        project, wt, "A001",
        _cmd("echo v2 > .hiveweave\\shared\\m2-interface.md "
             "&& del .hiveweave\\shared\\m2-interface.md "
             "&& echo recreated > .hiveweave\\shared\\m2-interface.md"))
    assert r is not None, "spawn_confined 返回 None（沙箱未启用？）"
    assert r["exit_code"] == 0, {
        "exit": r["exit_code"], "stdout": (r.get("stdout") or "")[-400:],
        "stderr": (r.get("stderr") or "")[-600:]}

    # 删旧写新确实发生在原 tracked 文件上，且平台可读
    assert tracked.read_text(encoding="utf-8").strip() == "recreated"
    # standing ACE 已落盘（OI/CI 形态，幂等跳过判据同款）
    assert WriteGrant.ace_present(
        str(shared), policy.shared_sid_str, GRANT_MASK)


async def test_shared_write_denied_without_shared_sid(project: Path) -> None:
    """sanity（修复前拒绝形态，M5 同构）：ACE 已在盘、token 不带 shared_sid →
    写 shared 下 tracked 文件仍被拒 —— 证明解锁机制正是 shared_sid 能力位。"""
    wt, shared = _make_worktree(project, "A001")
    tracked = shared / "m2-interface.md"
    tracked.write_text("v1\n", encoding="utf-8")

    policy = resolve_policy(workspace_path=str(wt), agent_id="A001",
                            project_workspace_path=str(project))
    agrant = _AsyncGrant(WriteGrant())
    await _ensure_standing_grants(policy, agrant)
    await _ensure_temp(policy, agrant)
    assert WriteGrant.ace_present(
        str(shared), policy.shared_sid_str, GRANT_MASK)

    runner = _ensure_runner()
    env = _build_sandbox_env(str(wt), policy.cache_dir, policy.temp_dir)

    # 变异：restricting 去掉 shared_sid
    sids_no_shared = [s for s in policy.write_sids
                      if s != policy.shared_sid_str]
    token_mut = _create_restricted_token(sids_no_shared, policy.temp_sid)
    try:
        r = await asyncio.to_thread(
            runner._run_foreground_sync, token_mut,
            _cmd(f"echo pwned > {shared}\\m2-interface.md"),
            str(wt), env, 60)
        assert r["exit_code"] != 0, f"无 shared_sid 竟能写 shared: {r}"
        assert tracked.read_text(encoding="utf-8").strip() == "v1"
    finally:
        token_mut.Close()

    # 对照：带全量 write_sids（含 shared_sid）→ 可写
    token_ok = RestrictedTokenFactory().create(
        policy.write_sids, policy.temp_sid)
    try:
        r2 = await asyncio.to_thread(
            runner._run_foreground_sync, token_ok,
            _cmd(f"echo v2 > {shared}\\m2-interface.md"),
            str(wt), env, 60)
        assert r2["exit_code"] == 0, f"对照失败: {r2}"
        assert tracked.read_text(encoding="utf-8").strip() == "v2"
    finally:
        token_ok.Close()


# ── 隔离回归：shared_sid 不泄露到锚点 / .hiveweave 根 / 项目根边界 ──
async def test_shared_sid_isolation_and_cross_agent_share(project: Path) -> None:
    """三条边界一次钉死：

    (a) MAIN shared（项目根边界）**也**被授予 —— P2-2（2026-09-22）起授予面
        覆盖**所有边界**（"网盘谁都可以读写"）；
    (b) `.hiveweave` 根 / sandbox-temp 根 / A 的私有锚点无 shared_sid ACE，
        B 的受限 token 写 A 的锚点仍被拒；
    (c) 同项目 shared = 跨 agent 共享：B 的 token 可写 A worktree 的 shared
        （per-project 同一 SID 的本意）。

    ⚠ 旧的 (a) 断言是「MAIN shared **不**授予」—— 那是 P2-2 之前的行为。它当时
    仍然"绿"，**只是因为本用例全程只走 worktree 边界的 `_spawn`**，从不触发
    MAIN 授予（陈旧断言 + 走不到的路径 = 空洞守卫，审计 P2-3 实测）。
    现改为**先跑一条 MAIN 边界的命令再断言 ACE 已落** ⇒ 改回旧行为即转红。
    """
    wt_a, shared_a = _make_worktree(project, "A001")
    (shared_a / "from-a.md").write_text("a\n", encoding="utf-8")

    r = await _spawn(project, wt_a, "A001", _cmd("echo boot"))
    assert r is not None and r["exit_code"] == 0, r

    policy_a = resolve_policy(workspace_path=str(wt_a), agent_id="A001",
                              project_workspace_path=str(project))
    sid = policy_a.shared_sid_str

    # (a) P2-2：MAIN shared **也授**（项目根边界同样携带并授予 shared SID）
    main_shared = project / ".hiveweave" / "shared"
    main_shared.mkdir(parents=True, exist_ok=True)
    # 授权前 sanity（修复前形态）：ACE 不该已经在
    assert not WriteGrant.ace_present(str(main_shared), sid, GRANT_MASK)
    r_main = await _spawn_main(project, "CEO", _cmd("echo boot"))
    assert r_main is not None and r_main["exit_code"] == 0, r_main
    assert WriteGrant.ace_present(str(main_shared), sid, GRANT_MASK), (
        "P2-2 起 MAIN 边界的 shared 也必须拿到 shared_sid ACE"
    )
    # (b) PROTECTED 区各根 + A 的锚点：无 shared_sid ACE
    #     （worktree 布局下 temp 锚点在 worktree 侧；MAIN 侧 shared 已在 (a) 钉过）
    a_anchor = wt_a / ".hiveweave" / "sandbox-temp" / "A001"
    for d in (project / ".hiveweave",
              wt_a / ".hiveweave",
              wt_a / ".hiveweave" / "sandbox-temp",
              a_anchor):
        assert d.exists(), f"布局缺失: {d}"
        assert not WriteGrant.ace_present(str(d), sid, GRANT_MASK), (
            f"shared_sid 泄露到 {d}")

    # B 激活（同项目，per-project 同一 shared SID）
    wt_b, _shared_b = _make_worktree(project, "B001")
    policy_b = resolve_policy(workspace_path=str(wt_b), agent_id="B001",
                              project_workspace_path=str(project))
    assert policy_b.shared_sid_str == sid
    token_b = RestrictedTokenFactory().create(
        policy_b.write_sids, policy_b.temp_sid)
    runner = _ensure_runner()
    env_b = _build_sandbox_env(str(wt_b), policy_b.cache_dir, policy_b.temp_dir)
    try:
        # (b) B 写 A 的私有 temp 锚点 → 拒
        r_b = await asyncio.to_thread(
            runner._run_foreground_sync, token_b,
            _cmd(f"echo pwned > {a_anchor}\\evil.txt"),
            str(wt_b), env_b, 60)
        assert r_b["exit_code"] != 0, f"B 竟能写 A 的 temp 锚点: {r_b}"
        assert not (a_anchor / "evil.txt").exists()
        # (c) B 写 A worktree 的 shared → 成功（跨 agent 共享契约区）
        r_c = await asyncio.to_thread(
            runner._run_foreground_sync, token_b,
            _cmd(f"echo from-b > {shared_a}\\from-b.md"),
            str(wt_b), env_b, 60)
        assert r_c["exit_code"] == 0, f"B 写 A 的 shared 失败: {r_c}"
        assert (shared_a / "from-b.md").read_text(
            encoding="utf-8").strip() == "from-b"
    finally:
        token_b.Close()


# ── 幂等/重入 + 存量死岛补授 + shared 缺席跳过 ────────────────
async def test_shared_grant_idempotent_and_walker_watermarked(
    project: Path, monkeypatch
) -> None:
    """重复 spawn：verify-then-skip 不重复写盘（grant_standing 只调一次），
    walker 水位同进程只全跑一次。"""
    from hiveweave.services.acl_sandbox import service as svc

    wt, shared = _make_worktree(project, "A001")
    (shared / "f.md").write_text("x\n", encoding="utf-8")
    policy = resolve_policy(workspace_path=str(wt), agent_id="A001",
                            project_workspace_path=str(project))
    sid = policy.shared_sid_str

    grant_calls: list[str] = []
    real_grant = WriteGrant.grant_standing

    def spy_grant(path, sid_arg, mask=GRANT_MASK):
        if sid_arg == sid:
            grant_calls.append(str(path))
        return real_grant(path, sid_arg, mask)

    monkeypatch.setattr(
        WriteGrant, "grant_standing", staticmethod(spy_grant))

    walk_calls: list[str] = []
    real_walk = svc._repair_shared_islands

    def spy_walk(shared_dir, sid_arg, user_sid=None):
        walk_calls.append(str(shared_dir))
        return real_walk(shared_dir, sid_arg, user_sid)

    monkeypatch.setattr(svc, "_repair_shared_islands", spy_walk)

    r1 = await _spawn(project, wt, "A001", _cmd("echo one"))
    assert r1 is not None and r1["exit_code"] == 0, r1
    assert grant_calls.count(str(shared)) == 1, grant_calls
    assert walk_calls.count(str(shared)) == 1, walk_calls

    r2 = await _spawn(project, wt, "A001", _cmd("echo two"))
    assert r2 is not None and r2["exit_code"] == 0, r2
    # 第二条命令：ACE 已在场（verify-then-skip）+ walker 命中水位 → 零重写
    assert grant_calls.count(str(shared)) == 1, grant_calls
    assert walk_calls.count(str(shared)) == 1, walk_calls
    assert WriteGrant.ace_present(str(shared), sid, GRANT_MASK)


async def test_shared_protected_island_repaired(project: Path) -> None:
    """存量升级兼容：shared 内 mode=0o700 PROTECTED 死岛（接不到根授予的
    急切传播）由水位 walker verify-then-skip 补授 —— 子树内可写。"""
    wt, shared = _make_worktree(project, "A001")
    island = shared / "legacy-island"
    island.mkdir(mode=0o700)
    (island / "old.md").write_text("v1\n", encoding="utf-8")

    r = await _spawn(
        project, wt, "A001",
        _cmd("echo v2 > .hiveweave\\shared\\legacy-island\\old.md"))
    assert r is not None and r["exit_code"] == 0, {
        "exit": r["exit_code"], "stderr": (r.get("stderr") or "")[-600:]}
    assert (island / "old.md").read_text(
        encoding="utf-8").strip() == "v2"
    assert WriteGrant.ace_present(str(island), resolve_policy(
        workspace_path=str(wt), agent_id="A001",
        project_workspace_path=str(project)).shared_sid_str, GRANT_MASK)


async def test_shared_absent_skip_then_backfill(project: Path) -> None:
    """老项目 shared 尚未物化：跳过不报错；物化后下一条命令补授（重入）。"""
    wt = project / ".hiveweave" / "worktrees" / "A001"
    (wt / ".hiveweave").mkdir(parents=True)  # 无 shared
    policy = resolve_policy(workspace_path=str(wt), agent_id="A001",
                            project_workspace_path=str(project))
    assert policy.shared_sid_str

    r = await _spawn(project, wt, "A001", _cmd("echo boot"))
    assert r is not None and r["exit_code"] == 0, r
    assert not (wt / ".hiveweave" / "shared").exists()

    # 平台物化 shared（_materialize_shared_dir 同款），下一条命令补授
    shared = wt / ".hiveweave" / "shared"
    shared.mkdir(parents=True)
    late = shared / "late.md"
    late.write_text("v1\n", encoding="utf-8")
    r2 = await _spawn(
        project, wt, "A001",
        _cmd("echo v2 > .hiveweave\\shared\\late.md"))
    assert r2 is not None and r2["exit_code"] == 0, {
        "exit": r2["exit_code"], "stderr": (r2.get("stderr") or "")[-600:]}
    assert late.read_text(encoding="utf-8").strip() == "v2"
    assert WriteGrant.ace_present(str(shared), policy.shared_sid_str, GRANT_MASK)


# ── P0（audit）：junction 越界防线 + P1 水位失败语义 ──────────────
async def test_shared_junction_not_followed_no_escape_ace(project: Path) -> None:
    """shared 内 mklink /J 指向边界外目录：walker/授予不穿越 reparse point ——
    目标目录零 shared_sid ACE，受限令牌对目标仍不可写（攻击链闭合）。"""
    import subprocess

    from hiveweave.services.acl_sandbox import service as svc
    from hiveweave.services.acl_sandbox.temppatch import current_user_sid

    wt, shared = _make_worktree(project, "A001")
    (shared / "inside.md").write_text("in\n", encoding="utf-8")
    outside = project / "outside-vault"
    outside.mkdir()
    (outside / "secret.txt").write_text("S", encoding="utf-8")
    _ensure_subject_ace(outside)  # 部署形态：边界外目录带真实主体 ACE

    junction = shared / "jump"
    mk = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True)
    assert mk.returncode == 0, (
        f"mklink /J 失败（测试前置）: {mk.stderr.decode('gbk', 'replace')}")
    # py3.13 实证：Path.is_symlink() 对 junction 返回 False —— 用 reparse
    # 属性断言（walker 的越界判据同源）
    import stat as _stat

    j_st = os.stat(str(junction), follow_symlinks=False)
    assert j_st.st_file_attributes & _stat.FILE_ATTRIBUTE_REPARSE_POINT, (
        "junction 应带 REPARSE_POINT 属性")

    policy = resolve_policy(workspace_path=str(wt), agent_id="A001",
                            project_workspace_path=str(project))
    sid = policy.shared_sid_str
    svc._shared_repaired.clear()  # 隔离模块级水位

    # walker 全跑：junction 不入栈、不授予
    repaired, failed, truncated = svc._repair_shared_islands(
        str(shared), sid, current_user_sid())
    assert (failed, truncated) == (0, False), (repaired, failed, truncated)
    assert WriteGrant.ace_present(str(shared), sid, GRANT_MASK)
    assert not WriteGrant.ace_present(str(outside), sid, GRANT_MASK), (
        "P0 越界：junction 目标目录不得落下 shared_sid ACE")

    # 端到端：standing grants + 受限令牌 —— shared 内可写，junction 目标不可写
    r = await _spawn(project, wt, "A001", _cmd("echo boot"))
    assert r is not None and r["exit_code"] == 0, r
    assert not WriteGrant.ace_present(str(outside), sid, GRANT_MASK), (
        "spawn 路径同样不得越界授 ACE")
    r2 = await _spawn(
        project, wt, "A001",
        _cmd(f"echo pwned > {outside}\\evil.txt"))
    assert r2 is not None and r2["exit_code"] != 0, (
        f"受限令牌竟能经 junction 越界写: {r2}")
    assert not (outside / "evil.txt").exists()
    # shared 正常能力不受影响
    r3 = await _spawn(
        project, wt, "A001",
        _cmd("echo ok > .hiveweave\\shared\\inside.md"))
    assert r3 is not None and r3["exit_code"] == 0, r3


async def test_shared_walk_watermark_retry_on_failure(
    project: Path, monkeypatch
) -> None:
    """P1：单节点授予失败 → failed>0、水位不落；下一轮重扫成功后才落水位；
    命中水位直接 (0,0,False)。"""
    from hiveweave.services.acl_sandbox import service as svc
    from hiveweave.services.acl_sandbox.temppatch import current_user_sid

    wt, shared = _make_worktree(project, "A001")
    # PROTECTED 死岛形态（mode=0o700）：根授予的可继承 ACE 传播进不来，
    # walker 必须逐点授予 —— 注入的瞬时失败才会真实命中
    sub = shared / "sub"
    sub.mkdir(mode=0o700)
    (sub / "a.md").write_text("a\n", encoding="utf-8")
    policy = resolve_policy(workspace_path=str(wt), agent_id="A001",
                            project_workspace_path=str(project))
    sid = policy.shared_sid_str
    usid = current_user_sid()
    key = os.path.normcase(os.path.realpath(str(shared)))
    svc._shared_repaired.clear()  # 隔离模块级水位

    real = WriteGrant.grant_standing
    boom = {"n": 0}

    def flaky_grant(path, sid_arg, mask=GRANT_MASK):
        if sid_arg == sid and str(path) == str(sub) and boom["n"] == 0:
            boom["n"] += 1
            raise OSError("transient boom")
        return real(path, sid_arg, mask)

    monkeypatch.setattr(WriteGrant, "grant_standing", staticmethod(flaky_grant))

    repaired, failed, truncated = svc._repair_shared_islands_once(
        str(shared), sid, usid)
    assert boom["n"] == 1 and failed == 1, (repaired, failed, truncated)
    assert truncated is False
    assert key not in svc._shared_repaired, "失败时不得落水位"

    # 第二轮（注入只炸一次）→ 全清，落水位
    repaired2, failed2, truncated2 = svc._repair_shared_islands_once(
        str(shared), sid, usid)
    assert (failed2, truncated2) == (0, False), (repaired2, failed2, truncated2)
    assert key in svc._shared_repaired, "全清后应落水位"
    assert WriteGrant.ace_present(str(sub), sid, GRANT_MASK)

    # 第三轮 → 命中水位，零扫描
    assert svc._repair_shared_islands_once(str(shared), sid, usid) == (0, 0, False)


# ── P2-2（2026-09-22）：**项目根边界**（CEO/HR）也能写网盘 ──────────
async def test_main_boundary_can_write_netdisk(project: Path) -> None:
    """P2-2 验收：项目根边界（CEO/HR 形态）对 `<proj>/.hiveweave/shared/…`
    可写 —— 与 worktree 边界**一致**（"网盘谁都可以操作"）。

    真令牌实测（探针 `scripts/probe_netdisk_write_surface.py` 的 N2 格同构）：
    修复前该格 **DENIED**（`bash_main` 不带 shared SID）。
    """
    shared = project / ".hiveweave" / "shared"
    shared.mkdir(parents=True, exist_ok=True)

    policy = resolve_policy(workspace_path=str(project), agent_id="CEO",
                            project_workspace_path=str(project))
    assert policy.shared_sid_str, "项目根边界必须携带 shared SID（P2-2）"
    # sanity（修复前形态）：授前该子树无 shared_sid ACE
    assert not WriteGrant.ace_present(
        str(shared), policy.shared_sid_str, GRANT_MASK)

    # ⚠⚠ 量具自纠：**必须先把「个人夹」建出来**再测"能不能写文件"。
    # 否则 `echo > …\shared\ceo\note.md` 的失败原因是 cmd 的"系统找不到指定的路径"，
    # 而同一条非零退出把「没权限」与「路径不存在」混成一句话 —— 探针首跑就吃过
    # 这个假阴性（`scripts/probe_netdisk_write_surface.py` §18.3）。
    (shared / "ceo").mkdir(parents=True, exist_ok=True)

    target = shared / "ceo" / "note.md"
    r = await _spawn_main(
        project, "CEO",
        _cmd(r"echo hi > .hiveweave\shared\ceo\note.md"))
    assert r is not None, "spawn_confined 返回 None（沙箱未启用？）"
    assert r["exit_code"] == 0, {
        "exit": r["exit_code"], "stdout": (r.get("stdout") or "")[-400:],
        "stderr": (r.get("stderr") or "")[-600:]}
    assert target.read_text(encoding="utf-8").strip() == "hi"
    # 另：**"个人夹能否自建"** 单列一格（探针 N1m/N2m 同构）——
    # 授予带 OI/CI ⇒ 子树内新建目录自动继承 ACE。
    own = shared / "hr"
    r2 = await _spawn_main(project, "HR", _cmd(r"mkdir .hiveweave\shared\hr"))
    assert r2 is not None and r2["exit_code"] == 0, (
        "MAIN 边界必须能自建个人夹（否则'个人夹'要平台代建）", r2 and r2.get("stderr"))
    assert own.is_dir()
    # standing ACE 已落盘（写文件那一格）
    assert WriteGrant.ace_present(
        str(target.parent), policy.shared_sid_str, GRANT_MASK)


async def test_main_boundary_still_denied_outside_shared(project: Path) -> None:
    """⭐ 作用域精确守卫：放开 shared **不得**顺带放开 `.hiveweave` 其余部分。

    靶子 = `.hiveweave/data.db`（平台自管系统文件）。判据是**看盘**：
    文件内容必须仍是原值、且**新建文件也不得出现**。探针 N3a 同构。

    ⚠⚠ 两处量具纪律（审计 P2 实测，都已修正）：
      1. 靶文件**必须预先创建** —— 否则 `exit != 0` 也可能是 cmd 的
         "系统找不到指定的路径"，把「没权限」与「路径不存在」混成一句话
         （探针首跑就吃过这个假阴性 ⇒ **先建靶路径，失败原因才唯一**）。
      2. 两个动作**各起一次 spawn**，不要用 `&&` 串起来 —— 串起来时第一条一失败
         第二条就不执行，于是"新文件未出现"这条断言**信息量归零**（它绿得没有意义）。
    """
    db = project / ".hiveweave" / "data.db"
    db.write_text("orig", encoding="utf-8")

    r = await _spawn_main(
        project, "CEO", _cmd(r"echo pwned > .hiveweave\data.db"))
    assert r is not None
    assert r["exit_code"] != 0, "MAIN 边界写 data.db 必须失败"
    assert db.read_text(encoding="utf-8") == "orig", "data.db 内容被改了"

    evil = project / ".hiveweave" / "evil.md"
    r2 = await _spawn_main(
        project, "CEO", _cmd(r"echo x > .hiveweave\evil.md"))
    assert r2 is not None
    assert r2["exit_code"] != 0, "MAIN 边界在 .hiveweave 下新建文件必须失败"
    assert not evil.exists(), "`.hiveweave` 下不得出现新文件（作用域被放宽了？）"
