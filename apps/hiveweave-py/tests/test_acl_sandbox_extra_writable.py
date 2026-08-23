"""ACL 沙箱 P2 附加可写目录集成测试（spec §5.5b② / §12.2）。

仅 Windows 运行（@pytest.mark.win32）。覆盖：
- 配置的附加可写目录：受限令牌写 PASS（extra SID 已授予）
- 未配置的外部目录：受限令牌写仍被拒（EACCES）
- 附加目录 standing ACE 已实际铺上（verify-then-skip + 读回）
- API 校验：嵌套/系统目录/相对路径拒绝

§4.12 前提：目录必须带真实主体 ACE（OWNER_RIGHTS-only 对受限令牌不可用）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from hiveweave.config import settings
from hiveweave.services.acl_sandbox import service as svc
from hiveweave.services.acl_sandbox.service import spawn_confined

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("ACL sandbox win32 integration tests require Windows",
                allow_module_level=True)


@pytest.fixture(scope="session", autouse=True)
def _shutdown_acl_runner_extra():
    yield
    from hiveweave.services.acl_sandbox.service import shutdown_runner
    from hiveweave.services.acl_sandbox.spawn import stop_watcher

    stop_watcher()
    shutdown_runner()


# ── §4.12 subject-ACE 基础设施（与 test_acl_sandbox_win32.py 同款） ──
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


@pytest.fixture(autouse=True)
def _sandbox_on(monkeypatch):
    monkeypatch.setattr(settings, "acl_sandbox", True)


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir(parents=True)
    _ensure_subject_ace(d)
    return d


@pytest.fixture()
def extra_dir(tmp_path: Path) -> Path:
    """带真实主体 ACE 的外部可写目录（配置为附加可写）。"""
    d = tmp_path / "extra_writable"
    d.mkdir(parents=True)
    _ensure_subject_ace(d)
    return d


@pytest.fixture()
def unauthorized_dir(tmp_path: Path) -> Path:
    """带真实主体 ACE 但**未配置**的外部目录（应拒绝写）。"""
    d = tmp_path / "unauthorized"
    d.mkdir(parents=True)
    _ensure_subject_ace(d)
    return d


def _cmd(inner: str) -> str:
    return f'"{COMSPEC}" /c {inner}'


async def _write(ws: Path, extra: list[str], target: Path,
                 agent_id: str = "A001") -> dict:
    from hiveweave.services.acl_sandbox import integration as acl_integration

    async def _fake_fetch(project_root: str) -> list[str]:
        return [str(p) for p in extra]

    import unittest.mock as mock
    with mock.patch.object(
        acl_integration, "fetch_additional_writable_dirs", new=_fake_fetch
    ):
        return await spawn_confined(
            command=_cmd(f"echo x > {target}"),
            workdir=str(ws),
            workspace_path=str(ws),
            agent_id=agent_id,
            project_workspace_path=str(ws),
            entry="bash",
            timeout_s=30,
        )


# ── 正路径：配置的附加可写目录可写 ──────────────────────────────
async def test_extra_writable_dir_allowed(ws: Path, extra_dir: Path) -> None:
    (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)
    target = extra_dir / "allowed.txt"
    r = await _write(ws, [extra_dir], target)
    assert r is not None
    assert r.get("exit_code") == 0, f"应可写：{r.get('stderr')}"
    assert target.exists()


# ── 拒绝路径：未配置的外部目录仍被拒 ─────────────────────────────
async def test_extra_writable_unauthorized_denied(
    ws: Path, unauthorized_dir: Path
) -> None:
    (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)
    target = unauthorized_dir / "denied.txt"
    r = await _write(ws, [], target)
    assert r is not None
    assert r.get("exit_code") != 0, f"应被拒：{r.get('stdout')}"
    assert not target.exists()


# ── 配置的附加目录 vs 未配置目录同 token 对照 ─────────────────────
async def test_extra_writable_configured_vs_not(
    ws: Path, extra_dir: Path, unauthorized_dir: Path
) -> None:
    (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)
    ok = extra_dir / "ok.txt"
    bad = unauthorized_dir / "bad.txt"
    # 仅配置 extra_dir；同一令牌写 extra_dir 通过、写 unauthorized 拒绝
    import unittest.mock as mock
    from hiveweave.services.acl_sandbox import integration as acl_integration

    async def _fake_fetch(project_root: str) -> list[str]:
        return [str(extra_dir)]

    with mock.patch.object(
        acl_integration, "fetch_additional_writable_dirs", new=_fake_fetch
    ):
        r1 = await spawn_confined(
            command=_cmd(f"echo x > {ok}"),
            workdir=str(ws), workspace_path=str(ws), agent_id="A001",
            project_workspace_path=str(ws), entry="bash", timeout_s=30,
        )
        r2 = await spawn_confined(
            command=_cmd(f"echo x > {bad}"),
            workdir=str(ws), workspace_path=str(ws), agent_id="A001",
            project_workspace_path=str(ws), entry="bash", timeout_s=30,
        )
    assert r1 is not None and r1.get("exit_code") == 0, f"配置目录应可写：{r1}"
    assert r2 is not None and r2.get("exit_code") != 0, f"未配置目录应被拒：{r2}"
    assert ok.exists()
    assert not bad.exists()


# ── standing ACE 实际铺上（verify-then-skip + 读回） ──────────────
async def test_extra_writable_grant_applied(
    ws: Path, extra_dir: Path
) -> None:
    from hiveweave.services.acl_sandbox.grant import GRANT_MASK, WriteGrant
    from hiveweave.services.acl_sandbox.sid import extra_sid

    (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)
    await _write(ws, [extra_dir], extra_dir / "seed.txt")
    g = WriteGrant()
    assert g.ace_present(str(extra_dir), extra_sid(str(extra_dir)), GRANT_MASK)
    # 子目录/子文件应继承 extra SID ACE（OI/CI）
    child = extra_dir / "sub"
    child.mkdir(exist_ok=True)
    assert g.ace_present(str(child), extra_sid(str(extra_dir)), GRANT_MASK)


# ── 沙箱模式逃生门（spec §9 P3） ──────────────────────────────
async def test_sandbox_mode_danger_full_access_returns_none(
    ws: Path
) -> None:
    """项目 sandbox_mode=danger-full-access → spawn_confined 返回 None（native 逃生门）。"""
    import unittest.mock as mock
    from hiveweave.services.acl_sandbox import integration as acl_integration

    async def _fake(project_id):
        return "danger-full-access"

    with mock.patch.object(
        acl_integration, "project_sandbox_mode", new=_fake
    ):
        r = await spawn_confined(
            command=_cmd("echo hi"),
            workdir=str(ws), workspace_path=str(ws), agent_id="A001",
            project_id="p3-escape", entry="bash", timeout_s=10,
        )
    assert r is None


async def test_sandbox_mode_default_keeps_sandbox(
    ws: Path, extra_dir: Path
) -> None:
    """默认模式（''）→ 不触发逃生门，沙箱照常执行。"""
    import unittest.mock as mock
    from hiveweave.services.acl_sandbox import integration as acl_integration

    async def _fake(project_id):
        return ""

    (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)
    with mock.patch.object(
        acl_integration, "project_sandbox_mode", new=_fake
    ):
        r = await spawn_confined(
            command=_cmd("echo hi > out.txt"),
            workdir=str(ws), workspace_path=str(ws), agent_id="A001",
            project_id="p3-default", entry="bash", timeout_s=30,
        )
    assert r is not None
    assert r.get("exit_code") == 0


# ── API 校验：嵌套/系统目录/相对路径拒绝（纯函数，跨平台单测） ──────
def test_validate_additional_writable_dirs_rejects():
    from hiveweave.api.projects import _validate_additional_writable_dirs

    base = Path(__file__).resolve().parents[0]
    ws_path = str(base / "fake_ws")
    rows = [{"workspace_path": ws_path}]

    # 相对路径 → 拒
    with pytest.raises(Exception):
        _validate_additional_writable_dirs(["relative/x"], ws_path, rows)
    # 盘根 → 拒
    drive = os.path.splitdrive(ws_path)[0] or "C:"
    with pytest.raises(Exception):
        _validate_additional_writable_dirs([drive + "\\"], ws_path, rows)
    # 系统目录（Windows）→ 拒
    win = os.environ.get("WINDIR") or r"C:\Windows"
    with pytest.raises(Exception):
        _validate_additional_writable_dirs([win], ws_path, rows)
    # 项目 .hiveweave/ 内 → 拒
    with pytest.raises(Exception):
        _validate_additional_writable_dirs(
            [str(base / "fake_ws" / ".hiveweave" / "data.db")], ws_path, rows
        )
    # 与另一项目 workspace 重叠（被包含）→ 拒（M-2：防 G6 跨项目击穿）
    other_ws = str(base / "other_proj")
    rows2 = [{"workspace_path": ws_path}, {"workspace_path": other_ws}]
    with pytest.raises(Exception):
        _validate_additional_writable_dirs(
            [str(base / "other_proj" / "src")], ws_path, rows2
        )
    with pytest.raises(Exception):
        _validate_additional_writable_dirs([other_ws], ws_path, rows2)
    # 合法外部目录 → 通过
    ok_dir = str(base / "fake_extra_ok")
    result = _validate_additional_writable_dirs([ok_dir], ws_path, rows)
    assert result == [os.path.normpath(ok_dir)]
