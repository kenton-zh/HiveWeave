"""变异测试守卫（spec §12.3 M1/M4/M5/M7）—— 真实受限令牌，仅 Windows。

这些测试构造「变异后的令牌」直接钉住各机制的必要性：若有人在生产路径
删掉对应保证，回归即失败。M2/M6 见 test_acl_sandbox_exceptions.py；
M3 见 test_acl_sandbox_grant.py 精确 ACE 跳过计数；M8 见
test_acl_sandbox_win32.py；M9 见 test_acl_sandbox_sid.py 域分离。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from hiveweave.services.acl_sandbox.grant import (
    GRANT_MASK,
    WriteGrant,
)
from hiveweave.services.acl_sandbox.policy import resolve_policy
from hiveweave.services.acl_sandbox.service import (
    _AsyncGrant,
    _build_sandbox_env,
    _ensure_runner,
    _ensure_standing_grants,
    _ensure_temp,
)
from hiveweave.services.acl_sandbox.sid import worktree_sid
from hiveweave.services.acl_sandbox.spawn import ConfinedRunner
from hiveweave.services.acl_sandbox.token import _create_restricted_token

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("ACL sandbox win32 mutation tests require Windows",
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


def _grant_user_ace(d: Path, ws, user) -> None:
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


def _ensure_subject_ace(path: Path) -> None:
    """§4.12 夹具：目标目录 + OWNER_RIGHTS-only 祖先链补用户 ACE（不碰盘根）。"""
    import win32api
    import win32security as ws

    tok = ws.OpenProcessToken(win32api.GetCurrentProcess(), ws.TOKEN_QUERY)
    user, _ = ws.GetTokenInformation(tok, ws.TokenUser)
    tok.Close()

    if not _dir_has_user_ace(path, ws, user):
        _grant_user_ace(path, ws, user)
    anc = path.parent
    while anc != anc.parent and str(anc).lower() != str(anc.anchor).lower():
        if _dir_has_user_ace(anc, ws, user):
            break
        _grant_user_ace(anc, ws, user)
        anc = anc.parent


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir(parents=True)
    _ensure_subject_ace(d)
    (d / ".hiveweave").mkdir(exist_ok=True)
    return d


async def _prep_grants(ws: Path) -> None:
    policy = resolve_policy(workspace_path=str(ws), agent_id="A001")
    await _ensure_standing_grants(policy, _AsyncGrant(WriteGrant()))
    await _ensure_temp(policy, _AsyncGrant(WriteGrant()))
    return policy


def _cmd(inner: str) -> str:
    return f'"{COMSPEC}" /c {inner}'


async def _run_token(runner: ConfinedRunner, token, ws: Path, env: dict,
                     command: str) -> dict:
    return await asyncio.to_thread(
        runner._run_foreground_sync, token, command, str(ws), env, 60)


# ── M1：默认 DACL 注入是管道生死线 ──────────────────────────
async def test_m1_default_dacl_injection_required(ws: Path) -> None:
    """§4.5：无注入 → 受限子进程内管道（type|findstr）写端失败 → 非零。"""
    policy = await _prep_grants(ws)
    (ws / "f.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    runner = _ensure_runner()

    # 变异：不注入默认 DACL
    token_no_inject = _create_restricted_token(
        policy.write_sids, policy.temp_sid, inject_default_dacl=False)
    try:
        env = _build_sandbox_env(str(ws), policy.cache_dir, policy.temp_dir)
        r = await _run_token(runner, token_no_inject, ws, env,
                       _cmd("type f.txt | findstr alpha"))
        assert r["exit_code"] != 0, f"M1: 无注入管道应失败, got {r}"
    finally:
        token_no_inject.Close()

    # 对照：正常注入 → 管道成功
    from hiveweave.services.acl_sandbox.token import RestrictedTokenFactory

    token_ok = RestrictedTokenFactory().create(policy.write_sids, policy.temp_sid)
    try:
        r = await _run_token(runner, token_ok, ws, env, _cmd("type f.txt | findstr alpha"))
        assert r["exit_code"] == 0, f"M1 对照失败: {r}"
    finally:
        token_ok.Close()


# ── M4：restricting 必须含 Everyone（保活组） ────────────────
async def test_m4_everyone_required_for_startup(ws: Path) -> None:
    """§4.6：去 Everyone → pwsh（CNG/BCrypt）DLL 初始化死 0xE0434352。

    实测：`cmd` 无 Everyone 也能跑；但 pwsh 依赖 BCrypt.dll（CNG），无
    Everyone 时 .NET 加载失败（"Unable to load DLL 'BCrypt.dll' ... DLL
    初始化例程失败"）。这正是 §4.6 保活组的实证 —— cmd 不撞、pwsh 必撞。
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not available")
    policy = await _prep_grants(ws)
    runner = _ensure_runner()

    token_no_everyone = _create_restricted_token(
        policy.write_sids, policy.temp_sid, include_everyone=False)
    try:
        env = _build_sandbox_env(str(ws), policy.cache_dir, policy.temp_dir)
        r = await _run_token(runner, token_no_everyone, ws, env,
                             f'"{pwsh}" -NoProfile -Command "1+1"')
        assert r["exit_code"] != 0, f"M4: 无 Everyone pwsh 竟正常退出 {r}"
        assert "BCrypt" in r["stderr"] or r["exit_code"] != 0
    finally:
        token_no_everyone.Close()

    # 对照：带 Everyone → pwsh 正常
    from hiveweave.services.acl_sandbox.token import RestrictedTokenFactory

    token_ok = RestrictedTokenFactory().create(policy.write_sids, policy.temp_sid)
    try:
        r = await _run_token(runner, token_ok, ws, env,
                             f'"{pwsh}" -NoProfile -Command "1+1"')
        assert r["exit_code"] == 0, f"M4 对照失败: {r}"
    finally:
        token_ok.Close()


# ── M5：restricting 去 worktree SID → 写自己 worktree 必失败 ─
async def test_m5_workspace_sid_required_for_write(ws: Path) -> None:
    """§4.2：restricting 缺 worktree SID → pass-2 落空 → 写工作区 EACCES。"""
    policy = await _prep_grants(ws)
    runner = _ensure_runner()
    # 变异：restricting 只带 temp SID，去掉 worktree SID
    token_mut = _create_restricted_token(
        [policy.temp_sid], policy.temp_sid)
    try:
        env = _build_sandbox_env(str(ws), policy.cache_dir, policy.temp_dir)
        r = await _run_token(runner, token_mut, ws, env,
                       _cmd(f"echo x > inside.txt"))
        assert r["exit_code"] != 0, "M5: 无 worktree SID 竟能写工作区"
        assert not (ws / "inside.txt").exists()
    finally:
        token_mut.Close()

    # 对照：带 worktree SID → 可写
    from hiveweave.services.acl_sandbox.token import RestrictedTokenFactory

    token_ok = RestrictedTokenFactory().create(policy.write_sids, policy.temp_sid)
    try:
        r = await _run_token(runner, token_ok, ws, env, _cmd("echo x > inside.txt"))
        assert r["exit_code"] == 0, f"M5 对照失败: {r}"
    finally:
        token_ok.Close()


# ── M7：GRANT_MASK 排除 WRITE_DAC/WRITE_OWNER（DACL 改写逃逸） ─
async def test_m7_grant_mask_excludes_dacl_write(ws: Path) -> None:
    """§4.4/§12.3 M7 双靶：
    (a) 受限进程对工作区内文件 SetNamedSecurityInfo（WRITE_DAC）须失败；
    (b) SetNamedSecurityInfo OWNER（take-ownership，WRITE_OWNER）须失败。
    若 GRANT_MASK 换成 FILE_ALL_ACCESS，两靶都崩。
    """
    policy = await _prep_grants(ws)
    runner = _ensure_runner()
    (ws / "victim.txt").write_text("x", encoding="utf-8")
    from hiveweave.services.acl_sandbox.token import RestrictedTokenFactory

    token = RestrictedTokenFactory().create(policy.write_sids, policy.temp_sid)
    try:
        env = _build_sandbox_env(str(ws), policy.cache_dir, policy.temp_dir)
        # (a) icacls /grant 改 DACL 需要 WRITE_DAC
        r = await _run_token(runner, token, ws, env,
                       _cmd(f'icacls "{ws / "victim.txt"}" /grant everyone:F'))
        assert r["exit_code"] != 0, "M7a: 受限进程竟能改 DACL"
        # (b) takeown 需要 WRITE_OWNER
        r2 = await _run_token(runner, token, ws, env,
                        _cmd(f'takeown /f "{ws / "victim.txt"}"'))
        assert r2["exit_code"] != 0, "M7b: 受限进程竟能 take-ownership"

        # 掩码本身校验：worktree 根 ACE 必须是 GRANT_MASK（非 FILE_ALL_ACCESS）
        import win32security

        sd = win32security.GetNamedSecurityInfo(
            str(ws), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        ws_sid_str = worktree_sid(str(ws))
        found = None
        for i in range(dacl.GetAceCount()):
            ((t, _f), m, s) = dacl.GetAce(i)
            s = win32security.ConvertSidToStringSid(s)
            if t == win32security.ACCESS_ALLOWED_ACE_TYPE and s == ws_sid_str:
                found = m & 0xFFFFFFFF
        assert found is not None, "worktree 根应有能力 SID ACE"
        assert found == GRANT_MASK, f"M7c: 掩码应为 GRANT_MASK={GRANT_MASK:#x}, got {found:#x}"
    finally:
        token.Close()

