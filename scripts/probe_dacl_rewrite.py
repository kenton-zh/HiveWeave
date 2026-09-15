"""probe_dacl_rewrite.py —— 受限 agent 能否自己改回被封印文件的 DACL？

**为什么问这个**（2026-09-15 审计自曝的未闭环项）：封条把能力 SID 的 ACE 摘掉、
「锁死档」再把平台 ACE 的 DELETE/DC 也摘掉。但 **属主**（owner）天生带隐式
`WRITE_DAC` —— 而受限令牌的**普通 SID 就是那个属主用户**。若 agent 能改写 DACL，
封条与锁死档整体失效。

判据必须跑出来：用平台自己的 `spawn_confined`（真受限令牌）去试
`icacls <file> /grant *<agent-sid>:W`、以及 `python -c SetNamedSecurityInfo`。

用法：
    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/probe_dacl_rewrite.py
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
from hiveweave.services.acl_sandbox.grant import (  # noqa: E402
    GRANT_MASK,
    WriteGrant,
)
from hiveweave.services.acl_sandbox.service import (  # noqa: E402
    spawn_confined,
    unlock_git_lockdown,
)

COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


def note(tag: str, verdict: str, detail: str = "") -> None:
    print(f"PROBE\t{tag}\t{verdict}\t{detail}", flush=True)


def _ensure_subject_ace(path: Path) -> None:
    tok = ws.OpenProcessToken(win32api.GetCurrentProcess(), ws.TOKEN_QUERY)
    user, _ = ws.GetTokenInformation(tok, ws.TokenUser)
    tok.Close()

    def has(d: Path) -> bool:
        try:
            sd = ws.GetNamedSecurityInfo(
                str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
        except ws.error:
            return False
        dacl = sd.GetSecurityDescriptorDacl() if sd else None
        if dacl is None:
            return False
        for i in range(dacl.GetAceCount()):
            ((t, _f), _m, s) = dacl.GetAce(i)
            if t == ws.ACCESS_ALLOWED_ACE_TYPE and s == user:
                return True
        return False

    def grant(d: Path) -> None:
        sd = ws.GetNamedSecurityInfo(
            str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        dacl.SetEntriesInAcl([{
            "AccessPermissions": 0x1F01FF, "AccessMode": ws.GRANT_ACCESS,
            "Inheritance": win32con.CONTAINER_INHERIT_ACE
            | win32con.OBJECT_INHERIT_ACE,
            "Trustee": {"MultipleTrustee": None,
                        "MultipleTrusteeOperation": 0,
                        "TrusteeForm": ws.TRUSTEE_IS_SID,
                        "TrusteeType": ws.TRUSTEE_IS_UNKNOWN,
                        "Identifier": user}}])
        ws.SetNamedSecurityInfo(
            str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION,
            sd.GetSecurityDescriptorOwner(), sd.GetSecurityDescriptorGroup(),
            dacl, None)

    if not has(path):
        grant(path)
    anc = path.parent
    while anc != anc.parent and str(anc).lower() != str(anc.anchor).lower():
        if has(anc):
            break
        grant(anc)
        anc = anc.parent


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


async def _run(workdir: Path, agent_id: str, inner: str, *, project: Path):
    return await spawn_confined(
        command=f'"{COMSPEC}" /c {inner}', workdir=str(workdir),
        workspace_path=str(workdir), project_workspace_path=str(project),
        agent_id=agent_id, timeout_s=90, entry="bash")


def _sid_aces(path: str) -> list[tuple[str, int, int]]:
    sd = ws.GetNamedSecurityInfo(str(path), ws.SE_FILE_OBJECT,
                                 ws.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    out = []
    for i in range(dacl.GetAceCount()):
        ((_t, f), m, s) = dacl.GetAce(i)
        out.append((ws.ConvertSidToStringSid(s), f, m & 0xFFFFFFFF))
    return out


async def main() -> int:
    settings.acl_sandbox = True
    base = Path(tempfile.mkdtemp(prefix="hw-dacl-"))
    _ensure_subject_ace(base)
    proj = base / "proj"
    proj.mkdir()
    (proj / ".hiveweave" / "worktrees").mkdir(parents=True)
    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    wt = proj / ".hiveweave" / "worktrees" / "A001"
    _raw_git(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")
    workdir = wt
    cfg = proj / ".git" / "config"
    cfg.write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")

    try:
        # 先跑一条受限命令 ⇒ 让平台**自己**把封条铺上（与生产同序；手工铺会与
        # 服务端的读回复核打架 —— 实测会 fail-closed）
        r0 = await _run(workdir, "A001", "echo boot", project=proj)
        note("S0-bootstrap", "OK" if (r0 and r0.get("exit_code") == 0) else "FAILED",
             f"exit={r0 and r0.get('exit_code')}")
        cap = None
        aces_now = _sid_aces(str(cfg))
        caps = [x for x, _f, _m in aces_now if x.startswith("S-1-4-")]
        note("S0-locked", "OK" if not caps else "BAD",
             f"cfg_aces={len(aces_now)} cap_aces={len(caps)}")
        # 取一个能力 SID 用来做「agent 自己给自己补权限」的尝试（用 worktree 的）
        cap = caps[0] if caps else "S-1-4-555000111-222333444"

        owner = ws.ConvertSidToStringSid(
            ws.GetNamedSecurityInfo(str(cfg), ws.SE_FILE_OBJECT,
                                    ws.OWNER_SECURITY_INFORMATION
                                    ).GetSecurityDescriptorOwner())
        tok = ws.OpenProcessToken(win32api.GetCurrentProcess(), ws.TOKEN_QUERY)
        user = ws.ConvertSidToStringSid(
            ws.GetTokenInformation(tok, ws.TokenUser)[0])
        tok.Close()
        note("S1-owner-vs-token-user", "INFO",
             f"owner={owner} token_user={user} same={owner == user}")

        # ① agent 用 icacls 给自己补写权
        r = await _run(workdir, "A001",
                       f'icacls "{cfg}" /grant *{cap}:W', project=proj)
        note("A1-agent-icacls-grant-self",
             "SUCCEEDED" if (r and r.get("exit_code") == 0) else "DENIED",
             f"exit={r and r.get('exit_code')} out={(r or {}).get('stdout', '')[:80]!r} "
             f"err={(r or {}).get('stderr', '')[:80]!r}")

        # ② agent 用 python SetNamedSecurityInfo（走属主隐式 WRITE_DAC）
        script = (
            "import win32security as w,sys;"
            f"p=r'{cfg}';"
            "sd=w.GetNamedSecurityInfo(p,w.SE_FILE_OBJECT,w.DACL_SECURITY_INFORMATION);"
            "d=sd.GetSecurityDescriptorDacl();"
            f"d.SetEntriesInAcl([{{'AccessPermissions':0x1F01FF,'AccessMode':w.GRANT_ACCESS,'Inheritance':0,'Trustee':{{'MultipleTrustee':None,'MultipleTrusteeOperation':0,'TrusteeForm':w.TRUSTEE_IS_SID,'TrusteeType':w.TRUSTEE_IS_UNKNOWN,'Identifier':w.ConvertStringSidToSid('"
            f"{cap}" + "')}}}]);"
            "w.SetNamedSecurityInfo(p,w.SE_FILE_OBJECT,w.DACL_SECURITY_INFORMATION,sd.GetSecurityDescriptorOwner(),sd.GetSecurityDescriptorGroup(),d,None);"
            "print('REWRITTEN')"
        )
        await _run(workdir, "A001",
                   f'python -c "{script}"', project=proj)
        aces = [s for s, _f, _m in _sid_aces(str(cfg)) if s == cap]
        note("A2-agent-python-setnamedsecurityinfo",
             "REWRITTEN" if aces else "DENIED",
             f"cap_aces_now={len(aces)}")

        # ③ 关键后果：DACL 被改回之后，agent 能不能写/删该文件
        r = await _run(workdir, "A001",
                       f'echo pwned >> "{cfg}"', project=proj)
        note("A3-agent-write-after-rewrite",
             "WROTE" if "pwned" in cfg.read_text(encoding="utf-8",
                                                 errors="replace")
             else "DENIED", f"exit={r and r.get('exit_code')}")

        # ④ 解锁后不重封（审计 ②）：再跑一次封条，DELETE 是否仍在
        unlock_git_lockdown(str(proj))
        before = [m for s, _f, m in _sid_aces(str(cfg))
                  if s == user and m & 0x10000]
        WriteGrant.seal_agent_aces(str(cfg), {cap}, strip_platform_delete=True)
        after = [m for s, _f, m in _sid_aces(str(cfg))
                 if s == user and m & 0x10000]
        note("B1-reseal-after-unlock",
             "STILL-HAS-DELETE" if after else "RE-LOCKED",
             f"before={len(before)} after={len(after)}")
    except BaseException:
        import traceback

        traceback.print_exc()   # 不吞失败（早先 finally 里的 os._exit 把异常一起吞了）
        shutil.rmtree(base, ignore_errors=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(3)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
