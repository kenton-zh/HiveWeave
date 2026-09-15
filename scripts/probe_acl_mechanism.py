"""probe_acl_mechanism.py —— 受限令牌「凭什么写得了」的最小机制探针。

起因：全量探针里 `ACL:<wt>` / `ACL:<wt>/.git` 读不到任何能力 SID（S-1-4-*），
但同一目录下 agent 的写/删却成功（F8/F10/F11）。⇒ 我之前那句「pass-2 落空 =
没有 ACE 授予该受限 SID」是**读出来的**模型，与本机实测不符。本探针把
「谁在授权」打到盘上：全量 ACE（解析 SID 名字）+ 逐路径真写一遍。

用法：
    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/probe_acl_mechanism.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "apps" / "hiveweave-py" / "src"))

import win32api  # noqa: E402
import win32con  # noqa: E402
import win32security as ws  # noqa: E402

from hiveweave.config import settings  # noqa: E402
from hiveweave.services.acl_sandbox.service import (  # noqa: E402
    shutdown_runner,
    spawn_confined,
)
from hiveweave.services.acl_sandbox.spawn import stop_watcher  # noqa: E402

COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


def sid_name(sid) -> str:
    try:
        return f"{ws.ConvertSidToStringSid(sid)}={ws.LookupAccountSid(None, sid)[0]}"
    except Exception:
        try:
            return ws.ConvertSidToStringSid(sid)
        except Exception:
            return "<unknown>"


def dump(label: str, path: str) -> None:
    print(f"\n### {label}  {path}")
    if not os.path.exists(path):
        print("    ABSENT")
        return
    sd = ws.GetNamedSecurityInfo(
        path, ws.SE_FILE_OBJECT,
        ws.DACL_SECURITY_INFORMATION | ws.PROTECTED_DACL_SECURITY_INFORMATION
        | ws.OWNER_SECURITY_INFORMATION)
    ctrl, _rev = sd.GetSecurityDescriptorControl()
    owner = ws.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
    print(f"    protected={bool(ctrl & ws.SE_DACL_PROTECTED)} owner={owner}")
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        print("    NULL DACL")
        return
    for i in range(dacl.GetAceCount()):
        ((t, f), m, s) = dacl.GetAce(i)
        kind = {0: "ALLOW", 1: "DENY"}.get(t, f"T{t}")
        inh = []
        if f & 0x10:
            inh.append("I")          # INHERITED
        if f & 0x1:
            inh.append("OI")
        if f & 0x2:
            inh.append("CI")
        if f & 0x8:
            inh.append("IO")
        print(f"    {kind:5s} mask=0x{m & 0xFFFFFFFF:08x} "
              f"flags={','.join(inh) or '-':6s} {sid_name(s)}")


def _dir_has_user_ace(d: Path, user) -> bool:
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


def _grant_user_ace(d: Path, user) -> None:
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
            "MultipleTrustee": None, "MultipleTrusteeOperation": 0,
            "TrusteeForm": ws.TRUSTEE_IS_SID,
            "TrusteeType": ws.TRUSTEE_IS_UNKNOWN, "Identifier": user,
        },
    }])
    ws.SetNamedSecurityInfo(
        str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION,
        sd.GetSecurityDescriptorOwner(), sd.GetSecurityDescriptorGroup(),
        dacl, None)


def _ensure_subject_ace(path: Path) -> None:
    tok = ws.OpenProcessToken(win32api.GetCurrentProcess(), ws.TOKEN_QUERY)
    user, _ = ws.GetTokenInformation(tok, ws.TokenUser)
    tok.Close()
    if not _dir_has_user_ace(path, user):
        _grant_user_ace(path, user)
    anc = path.parent
    while anc != anc.parent and str(anc).lower() != str(anc.anchor).lower():
        if _dir_has_user_ace(anc, user):
            break
        _grant_user_ace(anc, user)
        anc = anc.parent


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


async def _run(workdir: Path, agent_id: str, inner: str, *,
               entry: str = "bash", workspace: Path | None = None):
    return await spawn_confined(
        command=f'"{COMSPEC}" /c {inner}', workdir=str(workdir),
        workspace_path=str(workspace or workdir),
        agent_id=agent_id, timeout_s=90, entry=entry)


async def main() -> int:
    settings.acl_sandbox = True
    base = Path(tempfile.mkdtemp(prefix="hw-aclmech-"))
    _ensure_subject_ace(base)
    proj = base / "proj"
    proj.mkdir()
    (proj / ".hiveweave").mkdir()
    outside = base / "outside"
    outside.mkdir()
    _ensure_subject_ace(outside)
    (outside / "seed.txt").write_text("s\n", encoding="utf-8")

    wt = proj / ".hiveweave" / "worktrees" / "A001"
    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    _raw_git(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")

    try:
        r = await _run(wt, "A001", "echo boot", workspace=proj)
        print(f"bootstrap bash(A001) exit={r and r.get('exit_code')}")
        r = await _run(proj, "CEO", "echo boot", entry="bash_main",
                       workspace=proj)
        print(f"bootstrap bash_main exit={r and r.get('exit_code')}")

        dump("base", str(base))
        dump("proj", str(proj))
        dump("proj/.hiveweave", str(proj / ".hiveweave"))
        dump("<wt>", str(wt))
        dump("<wt>/.git", str(wt / ".git"))
        dump("<wt gitdir>", str(proj / ".git" / "worktrees" / "A001"))
        dump("outside", str(outside))

        cases = [
            ("<wt> 新建文件", wt, "echo x > mech1.txt", True),
            ("<wt> 写既有文件", wt, "echo x > f.txt", True),
            ("<wt> 新建子目录+文件", wt, "mkdir sub2 & echo x > sub2\\a.txt", True),
            ("outside 新建文件", wt, f"echo x > {outside}\\mech2.txt", False),
            ("outside 删既有文件", wt, f"del {outside}\\seed.txt", False),
            ("<wt>/.git 覆写指针", wt, "echo gitdir: C:/nope > .git", True),
            ("<wt>/.git 删除", wt, "del .git", True),
            ("<wt gitdir>/config.worktree", wt,
             "echo [x] > ..\\..\\..\\.git\\worktrees\\A001\\config.worktree", True),
        ]
        for label, cwd, inner, _expect in cases:
            r = await _run(cwd, "A001", inner, workspace=proj)
            print(f"WRITE {label:34s} exit={r and r.get('exit_code')} "
                  f"stderr={(r or {}).get('stderr', '')[:60]!r}")
    finally:
        stop_watcher()
        shutdown_runner()
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
