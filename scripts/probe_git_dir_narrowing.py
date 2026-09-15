"""probe_git_dir_narrowing.py —— 「收窄 `.git` 根写面」的功能代价实测。

背景（一手实测，见 probe_git_write_surface.py / _dbg_seal.py）：
  `git config` 写的是 lock 文件 + **rename 替换**；故把 `.git/config` 文件本身封住
  **无效** —— 删（走 pass-1 的 user DELETE）+ 重建（走父目录的 create 权，pass-2）
  会把它换成一个继承 `.git` ACE 的新文件，封条当场失效。
  留得住的唯一杠杆 = **父目录的 create/写面**（实测：工作区外 create 被拒 ⇒ create
  是 pass-2 管的；delete 不是）。

本探针手工模拟「收窄」：`.git` 根本身不授 agent 写，改授 `objects/`·`refs/`·
`logs/` 与每个 `<gitdir>/`，然后逐条测 agent 的 git 用法还剩下哪些能跑。

用法：
    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/probe_git_dir_narrowing.py
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
from hiveweave.services.acl_sandbox.grant import GRANT_MASK, WriteGrant  # noqa: E402
from hiveweave.services.acl_sandbox.service import spawn_confined  # noqa: E402

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


async def _run(workdir: Path, agent_id: str, inner: str, *,
               project: Path, entry: str = "bash"):
    return await spawn_confined(
        command=f'"{COMSPEC}" /c {inner}', workdir=str(workdir),
        workspace_path=str(workdir), project_workspace_path=str(project),
        agent_id=agent_id, timeout_s=90, entry=entry)


async def main() -> int:
    settings.acl_sandbox = True
    base = Path(tempfile.mkdtemp(prefix="hw-narrow-"))
    _ensure_subject_ace(base)
    proj = base / "proj"
    proj.mkdir()
    (proj / ".hiveweave").mkdir()
    wt = proj / ".hiveweave" / "worktrees" / "A001"
    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    _raw_git(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")

    try:
        # 前两轮把 standing grants 铺开（授权树根分别 = worktree / 项目根）
        r0 = await _run(wt, "A001", "echo boot", project=proj)
        r1 = await _run(proj, "CEO", "echo boot", project=proj,
                        entry="bash_main")
        print(f"# bootstrap wt={r0 and r0.get('exit_code')} "
              f"main={r1 and r1.get('exit_code')}")

        git_dir = proj / ".git"
        # ── 收窄：.git 根摘干净，改授 git 真正需要写的子目录 ──
        sids = {w for w in (
            __import__("hiveweave.services.acl_sandbox.sid",
                       fromlist=["x"]).git_sid(str(proj)),
            __import__("hiveweave.services.acl_sandbox.sid",
                       fromlist=["x"]).worktree_sid(str(proj)),
        )}
        from hiveweave.services.acl_sandbox.sid import git_sid, worktree_sid
        sids = {git_sid(str(proj)), worktree_sid(str(proj)),
                worktree_sid(str(wt))}
        for sid in sids:
            WriteGrant.seal_agent_aces(str(git_dir), {sid})
        granted = []
        for rel in ("objects", "refs", "logs", "worktrees", "worktrees/A001"):
            d = git_dir / rel
            if d.is_dir():
                for sid in sids:
                    WriteGrant.grant_standing(str(d), sid, GRANT_MASK)
                granted.append(rel)
        note("N0-narrowing-applied", "INFO", f"granted={granted}")
        # 直方图：`.git` 根上还有没有能力 SID 写位
        leaking = [s for t, _f, m, s in WriteGrant.list_aces(str(git_dir))
                   if s.startswith("S-1-4-") and m & GRANT_MASK]
        note("N1-.git-root-still-writable", "YES" if leaking else "NO",
             f"{len(leaking)} sids")

        # ── 攻击面：agent 还能不能写/替换 config ──
        cfg = git_dir / "config"
        before = cfg.read_bytes()
        r = await _run(wt, "A001", "git config probe.narrow 1", project=proj)
        note("N2-agent-write-.git/config",
             "WROTE" if cfg.read_bytes() != before else "DENIED",
             f"exit={r and r.get('exit_code')}")

        # ── 功能面：agent 自己的 git 用法 ──
        cases = [
            ("N3-wt-git-add-commit",
             "git add w.txt && git -c user.name=A -c user.email=a@a "
             "commit -qm w && git log --oneline -1"),
            ("N4-wt-git-status", "git status --short"),
            ("N5-wt-git-checkout-file", "echo b > f.txt && git checkout -- f.txt"),
            ("N6-wt-git-rebase-main", "git rebase main"),
        ]
        (wt / "w.txt").write_text("w\n", encoding="utf-8")
        for tag, inner in cases:
            r = await _run(wt, "A001", inner, project=proj)
            note(tag, "OK" if (r and r.get("exit_code") == 0) else "FAILED",
                 f"exit={r and r.get('exit_code')} "
                 f"err={(r or {}).get('stderr', '')[:60]!r}")

        main_cases = [
            ("N7-main-git-status", "git status --short"),
            ("N8-main-git-add-commit",
             "git add -A && git -c user.name=C -c user.email=c@c "
             "commit -qm m"),
            ("N9-main-git-checkout-file",
             "echo c > f.txt && git checkout -- f.txt"),
            ("N10-main-git-log", "git log --oneline -1"),
        ]
        for tag, inner in main_cases:
            r = await _run(proj, "CEO", inner, project=proj, entry="bash_main")
            note(tag, "OK" if (r and r.get("exit_code") == 0) else "FAILED",
                 f"exit={r and r.get('exit_code')} "
                 f"err={(r or {}).get('stderr', '')[:60]!r}")

        # ── 平台侧不受影响（对照） ──
        p = _raw_git(proj, "config", "user.name", "T2")
        note("N11-platform-git-config",
             "OK" if p.returncode == 0 else "FAILED", f"rc={p.returncode}")
        p = _raw_git(proj, "add", "-A")
        p2 = _raw_git(proj, "commit", "-qm", "platform")
        note("N12-platform-add-commit",
             "OK" if p.returncode == 0 and p2.returncode == 0 else "FAILED",
             f"rc={p.returncode}/{p2.returncode}")
        p = _raw_git(proj, "merge", "--ff-only", "-q", "wt/A001")
        note("N13-platform-merge",
             "OK" if p.returncode == 0 else "FAILED",
             f"rc={p.returncode} err={p.stderr.strip()[:60]!r}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
