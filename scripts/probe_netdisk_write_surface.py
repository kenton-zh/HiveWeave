"""probe_netdisk_write_surface.py —— P2-2「团队网盘」可写面探针（**真受限令牌**）。

**为什么单独一个脚本**：`probe_git_write_surface.py` 是 `.git` 写面的取证脚本，
在**当前 HEAD 上已跑不动** —— 它在 K1 收尾处 `unlink(.git/config.worktree)` 时
吃 `PermissionError [WinError 5]`（封条把该文件也封了，探针进程自己删不掉）。
⇒ 本脚本**复用它的夹具与 `_run`（单一来源，不再抄一遍 `spawn_confined` 形态）**，
只跑网盘那几格，不依赖它能否跑完。

判据（**全部看盘**，不看回执文案）：

  N1  executor 形态（授权树根 = worktree）写 `<wt>/.hiveweave/shared/<短号>/n1.md`
  N2  **MAIN 形态**（授权树根 = 项目根；CEO/HR/`pwsh_main`）写 `<proj>/.hiveweave/shared/…`
      ← **这一格就是"要不要放开 MAIN 边界"的对象**
  N1m/N2m 同两格但**由 agent 自己建目录**（验个人夹能否自建）
  N3a/N3b/N3c 负对照，**必须仍拒**：`data.db` / 别家 worktree / 边界外

用法（仓库根；**不要用 `| tail`**，管道会缓冲到进程结束）：
    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/probe_netdisk_write_surface.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "apps" / "hiveweave-py" / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(REPO / "scripts"))

# 复用既有探针的夹具/执行形态（**单一来源**）：_run 里那条
# "workspace_path=workdir + project_workspace_path=project" 的注释值千金,
# 抄一份出来就等于埋一次「测的形态和以为的不一样」。
from probe_git_write_surface import (  # noqa: E402
    _ensure_subject_ace,
    _fwd,
    _hard_exit,
    _raw_git,
    _run,
    note,
    rows,
)

from hiveweave.config import settings  # noqa: E402


async def main() -> int:
    if not sys.platform.startswith("win"):
        print("win32 only")
        return 2
    settings.acl_sandbox = True
    if not shutil.which("git"):
        print("git not on PATH")
        return 2

    base = Path(tempfile.mkdtemp(prefix="hw-netdisk-"))
    _ensure_subject_ace(base)
    proj = base / "proj"
    proj.mkdir()
    wt = proj / ".hiveweave" / "worktrees" / "A001"

    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    _raw_git(proj, "config", "commit.gpgsign", "false")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    _raw_git(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")

    sd_wt = wt / ".hiveweave" / "shared"
    sd_pj = proj / ".hiveweave" / "shared"
    sd_pj.mkdir(parents=True, exist_ok=True)
    other_wt = proj / ".hiveweave" / "worktrees" / "A002"
    other_wt.mkdir(parents=True, exist_ok=True)
    (proj / ".hiveweave" / "data.db").write_text("x", encoding="utf-8")
    outside = base / "outside.txt"
    # ⚠⚠ 量具自纠（首跑实测）：**必须先把靶目录建出来**。
    # 首跑 N1/N2 判 DENIED，但那不是 ACL 拒绝 —— 是 `cmd` 的
    # "系统找不到指定的路径"（父目录不存在）⇒ 同一条 exit=1 把
    # **"没权限"** 与 **"路径不存在"** 混成一句话。**先把路径建好**，
    # 剩下的唯一失败原因就只能是权限（其余见 N1m/N2m：它们测"能不能建目录"）。
    (sd_wt / "A001").mkdir(parents=True, exist_ok=True)
    (sd_pj / "ceo").mkdir(parents=True, exist_ok=True)
    print(f"# project  = {proj}")
    print(f"# worktree = {wt}")

    async def attempt(tag: str, inner: str, target: Path, *,
                      main_boundary: bool, expect: str) -> None:
        """跑一条受限命令，然后**看盘判结果**（存在=ALLOWED，不存在=DENIED）。"""
        workdir = proj if main_boundary else wt
        who = "CEO" if main_boundary else "A001"
        r = await _run(workdir, who, inner, entry="bash_main" if main_boundary
                       else "bash", workspace=workdir, project=proj)
        got = "ALLOWED" if target.exists() else "DENIED"
        verdict = got if got == expect else f"**{got}!={expect}**"
        note(tag, verdict, f"exit={r.get('exit_code') if r else None} "
                           f"{target.name}")

    try:
        boot0 = await _run(proj, "CEO", "echo boot", entry="bash_main",
                           workspace=proj, project=proj)
        boot1 = await _run(wt, "A001", "echo boot", workspace=wt, project=proj)
        print(f"# bootstrap bash_main={boot0 and boot0.get('exit_code')} "
              f"bash(A001)={boot1 and boot1.get('exit_code')}")

        await attempt("N1-exec-write-netdisk",
                      f'echo hi > "{_fwd(sd_wt / "A001" / "n1.md")}"',
                      sd_wt / "A001" / "n1.md",
                      main_boundary=False, expect="ALLOWED")
        await attempt("N2-main-write-netdisk",
                      f'echo hi > "{_fwd(sd_pj / "ceo" / "n2.md")}"',
                      sd_pj / "ceo" / "n2.md",
                      main_boundary=True, expect="ALLOWED")
        await attempt("N1m-exec-mkdir-fold",
                      f'mkdir "{_fwd(sd_wt / "A009")}"',
                      sd_wt / "A009",
                      main_boundary=False, expect="ALLOWED")
        await attempt("N2m-main-mkdir-fold",
                      f'mkdir "{_fwd(sd_pj / "hr")}"',
                      sd_pj / "hr",
                      main_boundary=True, expect="ALLOWED")
        # ── 负对照（必须 DENIED）──────────────────────────────
        await attempt("N3a-deny-datadb",
                      f'echo pwned > "{_fwd(proj / ".hiveweave" / "data.db")}"',
                      proj / ".hiveweave" / "data.db.marker",
                      main_boundary=False, expect="DENIED")
        await attempt("N3b-deny-other-worktree",
                      f'echo pwned > "{_fwd(other_wt / "x.txt")}"',
                      other_wt / "x.txt",
                      main_boundary=False, expect="DENIED")
        await attempt("N3c-deny-outside",
                      f'echo pwned > "{_fwd(outside)}"',
                      outside,
                      main_boundary=False, expect="DENIED")

        # 关闭 set 判定：data.db 本体没被改（负对照的补充状态判据）
        note("N3a-note-datadb-intact",
             "INTACT" if (proj / ".hiveweave" / "data.db").read_text(
                 encoding="utf-8") == "x" else "**MODIFIED**", "data.db")

        print("\n== SUMMARY ==")
        for tag, verdict, detail in rows:
            print(f"{verdict:16s} {tag}  {detail}")
    except BaseException:
        import traceback

        traceback.print_exc()
        shutil.rmtree(base, ignore_errors=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(3)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        _hard_exit()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
