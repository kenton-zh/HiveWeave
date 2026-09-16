"""probe_project_seal_readback.py —— #20「三项目封条 read-back」复现探针。

## 为什么需要这个探针

`#20`：`office-godot` / `TEST_DSH_54` / `TEST_DSH_52_A` 三个**老项目**在 09-15 的
打包实跑里报 `acl_sandbox_sentinel_failed` ⇒ 平台 fail-closed **拒绝执行 agent 命令**。
失败机理在 `services/acl_sandbox/service.py::_seal_git_bootstrap_files`：授权阶段跑完
`seal_agent_aces_async(<proj>/.git)` 之后必须**读回复核**，只要 `.git` 上仍留着
带写位的能力 SID（`S-1-4-*`）就抛 `SandboxUnavailableError`，拒绝继续执行。

09-16 只读复核发现**静置状态下读回是通过的**（`_agent_aces_leaking() == []`）。但
「静置通过」≠「复现不了」：那些 leaking ACE 是**平台自己授予后没摘干净**的产物 ⇒
必须**再触发一次授权**才能判。本探针就是干这件事，且**用平台自己的入口**
（`spawn_confined`，与真实 agent 命令同一条路径），不需要起后端、不需要 LLM。

## 用法（仓库根）

    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/probe_project_seal_readback.py
    ... --projects TEST_DSH_54,TEST_DSH_52_A
    ... > /tmp/probe20.log 2>&1     # ⚠ 不要用 `| tail`（管道缓冲，看不到进度）

判据（全部是**状态判据**，不看文案）：
  R0  静置读数            —— 授权前 `.git` 上有没有能力 SID
  R1  触发（spawn_confined）—— 平台自己的授权 + 封条 + 读回复核
  R2  复读                —— 授权后 `.git` / 各 worktree gitdir 上的能力 SID
  ⇒ `R2` 非空 或 `R1` 返回 fail-closed ⇒ **REPRODUCED**
  ⇒ `R2` 为空 且 `R1` 正常执行 ⇒ **NOT-REPRODUCED**（附本次读数，供下次对照）

⚠ 本探针**会写**：它做的就是真实 agent 命令会做的事（给项目 `.git` 落封条 /
deny-DC / 授权）。这是 `#20` 明确要求的一步，不是副作用。项目清单里
`office-godot` 已不在磁盘上（09-16 核实），默认只跑现存的两个。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = Path(os.environ["HW_SRC"]) if os.environ.get("HW_SRC") else (
    REPO / "apps" / "hiveweave-py" / "src")
sys.path.insert(0, str(SRC))

TESTPROJ = Path("D:/PC_AI/Project/HiveTestProject")
DEFAULT_PROJECTS = ["TEST_DSH_54", "TEST_DSH_52_A"]
COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


def _cmd(inner: str) -> str:
    """同 `probe_git_write_surface.py`：整串交 `cmd /c`（避免 PATH 上的
    PortableGit `echo.exe` 在受限令牌下起不来 —— 那是探针侧假阳性）。"""
    return f'"{COMSPEC}" /c {inner}'

rows: list[tuple[str, str, str]] = []


def note(tag: str, verdict: str, detail: str = "") -> None:
    rows.append((tag, verdict, detail))
    print(f"PROBE\t{tag}\t{verdict}\t{detail}", flush=True)


def _hard_exit(code: int = 0) -> None:
    """flush 后硬退 —— 探针进程的优雅收尾会阻塞（见 probe_git_write_surface.py）。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _worktrees(proj: Path) -> list[Path]:
    """**有效**工作树（`git worktree list` 且在册、指针是**文件**）。

    ⚠ 第一版直接 `iterdir()` 取第一个目录，结果拿到 `.stale-A042-…`（husk：`.git`
    是目录）⇒ 平台按 #2「A1」拒绝（**正确的** fail-closed），却被我读成 #20 的
    封条失败 —— **假的 REPRODUCED**。"探针的形态和结论一样要交代"。
    """
    import subprocess

    r = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=str(proj),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out: list[Path] = []
    for line in r.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        p = Path(line[len("worktree "):].strip())
        if p.parent.name != "worktrees" or p.parent.parent.name != ".hiveweave":
            continue                       # 只认本项目的 per-agent worktree
        if not (p / ".git").is_file():
            continue                       # husk / 被替换成目录 ⇒ 不是合法执行位
        out.append(p)
    return out


def _sid_report(path: Path) -> tuple[bool, list[str], list[str]]:
    """(存在, 能力 SID 全集, leaking 子集) —— 读盘现状，不做推断。

    ``leaking`` 用**生产判据**（`service._agent_aces_leaking`），不是本地重写：
    "读回是否通过"只有那一个权威。

    ⚠ 判据只能用在**该封的路径**上（`<proj>/.git`、两个 config 载体）：
    worktree 根/ gitdir **本来就该**对 agent 可写（agent 的 add/commit 落在那里）
    ⇒ 对它们算 "leaking" 是本探针第一版的第 2 个假阳性。
    """
    from hiveweave.services.acl_sandbox.service import (
        _agent_aces_leaking, _grant_aces,
    )

    if not path.exists():
        return False, [], []
    aces = _grant_aces(str(path))
    sids = sorted({sid for _t, _f, _m, sid in aces if sid.startswith("S-1-4-")})
    return True, sids, _agent_aces_leaking(str(path))


def _refusal_kind(text: str) -> str:
    """把拒绝原因**分型** —— 否则 husk/锚拒绝会冒充 #20 的封条失败。"""
    t = text or ""
    if "seal read-back failed" in t:
        return "SEAL-READBACK"          # ← #20 的签名
    if "worktree gitdir 指针" in t or "git_anchor" in t or "信任锚" in t:
        return "ANCHOR-REFUSED"
    if "无法退休 extensions.worktreeConfig" in t:
        return "RETIRE-REFUSED"
    return "OTHER"


async def probe_one(proj: Path, *, dry: bool) -> None:
    from hiveweave.services.acl_sandbox.service import spawn_confined

    name = proj.name
    git_dir = proj / ".git"
    if not git_dir.exists():
        note(f"{name}:R0", "SKIP", f"no .git at {git_dir}")
        return

    wt = _worktrees(proj)
    workdir = wt[0] if wt else proj
    exists0, sids0, leak0 = _sid_report(git_dir)
    note(f"{name}:R0-idle", "INFO",
         f"exists={exists0} .git_agent_sids={sids0 or '[]'} .git_leaking={leak0 or '[]'} "
         f"valid_worktrees={len(wt)}")

    if dry:
        note(f"{name}:R1", "SKIPPED", "--dry")
        return

    # ── R1：平台自己的授权 + 封条 + 读回复核（= 真实 agent 命令的同一条路）──
    # ⚠ 命令必须走 `cmd /c`（同 `probe_git_write_surface.py::_cmd`）：裸 `echo`
    # 在本机会解析到 PortableGit 的 `echo.exe`，msys 运行时在受限令牌下
    # CreateFileMapping 失败（`Win32 error 5`）⇒ 那是**探针侧**假阳性，
    # 与封条毫无关系（第一版就是这么误判的）。
    err = ""
    res: dict | None = None
    try:
        res = await spawn_confined(
            command=_cmd("echo probe20"),
            workdir=str(workdir),
            workspace_path=str(workdir),
            project_workspace_path=str(proj),
            agent_id="__probe20__",
            entry="bash",
            timeout_s=90,
        )
        verdict = "EXECUTED" if (res or {}).get("exit_code") == 0 else "REFUSED"
        err = ((res or {}).get("error") or (res or {}).get("stderr") or "")[:240]
        if (res or {}).get("exit_code") != 0 and not err:
            err = f"exit_code={(res or {}).get('exit_code')}"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:240]
        verdict = "REFUSED"
    kind = "OK" if verdict == "EXECUTED" else _refusal_kind(err)
    note(f"{name}:R1-spawn", verdict, f"cwd={workdir.name} kind={kind} err={err!r}")

    # ── R2：复读**该封的路径**（`.git` + 两个 config 载体）─────────────
    for label, p in (
        (".git", git_dir),
        (".git/config", git_dir / "config"),
        (".git/config.worktree", git_dir / "config.worktree"),
    ):
        r_exists, r_sids, r_leak = _sid_report(p)
        note(f"{name}:R2{label}", "LEAKING" if r_leak else "CLEAN",
             f"exists={r_exists} agent_sids={r_sids or '[]'} leaking={r_leak or '[]'}")
    # worktree 侧只作 INFO（那里对 agent 可写是**设计**，不是泄漏）
    for w in wt[:3]:
        w_exists, w_sids, _ = _sid_report(w)
        note(f"{name}:R2-wt:{w.name}", "INFO",
             f"exists={w_exists} agent_sids={len(w_sids)} (可写=设计)")

    # 判型：**只有** seal-readback 才是 #20 复现；其余拒绝原因分开报，
    # 免得把 husk/锚 的 fail-closed 记成 #20（第一版的错就在这里）。
    reproduced = kind == "SEAL-READBACK" or bool(leak0)
    note(f"{name}:VERDICT",
         "REPRODUCED" if reproduced else "NOT-REPRODUCED",
         f"refusal_kind={kind} spawn={verdict} "
         f"idle_leak={leak0 or '[]'} worktrees={len(wt)}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default=",".join(DEFAULT_PROJECTS))
    ap.add_argument("--root", default=str(TESTPROJ))
    ap.add_argument("--dry", action="store_true",
                    help="只读静置读数，不触发授权")
    args = ap.parse_args()

    if not sys.platform.startswith("win"):
        print("win32 only")
        return 2
    from hiveweave.config import settings

    settings.acl_sandbox = True
    print(f"# src = {SRC}")

    for n in [s.strip() for s in args.projects.split(",") if s.strip()]:
        proj = Path(args.root) / n
        if not proj.is_dir():
            note(f"{n}:R0", "ABSENT", f"{proj} 不在磁盘上")
            continue
        await probe_one(proj, dry=args.dry)

    print("\n== SUMMARY ==")
    for tag, verdict, detail in rows:
        print(f"{verdict:16s} {tag}  {detail}")
    return 0


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException:
        import traceback

        traceback.print_exc()
        _hard_exit(3)
    _hard_exit(0)
