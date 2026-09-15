"""probe_git_trust_anchor.py —— 平台侧 git 信任锚（#2 残余 R2）取证 / 验收探针。

**问题**：平台的 git 跑在不受限进程里，而 agent 能写自己 worktree 的 `.git` 指针、
也能写 gitdir 里 git 会读的文件 ⇒ 可让平台的 git 去读 **agent 写好的 config**，
从而执行 `filter.<n>.clean` 这类**动态键名**驱动（`GIT_CONFIG_*` 静态清单覆盖不到）。

**修法**（`services/git_worktree/git_anchor.py` + `git_cmd.py::_git`）：gitdir 与
common dir 由平台**派生并钉住**（`--git-dir` + `GIT_COMMON_DIR`），一律不读
agent 可写指针的内容 ⇒ 无 TOCTOU；可证篡改形态 ⇒ 拒绝执行（loud）。

本探针的每一条**先做阳性对照**（裸 git = 修复前形态，必须中招），再看平台路径。
用法（仓库根）：
    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/probe_git_trust_anchor.py
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

try:  # 修前（纯净树）没有这个模块 —— 探针必须两侧都能跑
    from hiveweave.services.git_worktree import git_anchor as ga  # noqa: E402
except ImportError:  # pragma: no cover - 只在「修前」那一侧走到
    ga = None  # type: ignore[assignment]
from hiveweave.services.git_worktree.git_cmd import _git  # noqa: E402

HAVE_ANCHOR = ga is not None

KEY = "probe.anchor"


def note(tag: str, verdict: str, detail: str = "") -> None:
    print(f"PROBE\t{tag}\t{verdict}\t{detail}", flush=True)


def raw(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """裸 git（**修复前**形态）：让 git 自己找 gitdir/common —— 阳性对照用。"""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def platform(cwd: Path, *args: str) -> tuple[bool, str]:
    """平台真实路径（`_git`：信任锚 + 漏斗加固）。"""
    return asyncio.run(_git(list(args), str(cwd)))


def main() -> int:
    if not sys.platform.startswith("win"):
        print("win32 only")
        return 2
    base = Path(tempfile.mkdtemp(prefix="hw-anchor-"))
    proj = base / "proj"
    proj.mkdir()
    (proj / ".hiveweave" / "worktrees").mkdir(parents=True)
    raw(proj, "init", "-q", "-b", "main")
    raw(proj, "config", "user.email", "t@t.t")
    raw(proj, "config", "user.name", "T")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    raw(proj, "add", "f.txt")
    raw(proj, "commit", "-qm", "init")
    wt = proj / ".hiveweave" / "worktrees" / "A001"
    raw(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")
    gitdir = proj / ".git" / "worktrees" / "A001"
    print(f"# project  = {proj}")
    print(f"# worktree = {wt}")

    try:
        # ── D 锚点派生 ────────────────────────────────────────
        if not HAVE_ANCHOR:
            note("D0-anchor-module", "ABSENT", "修前：git_cmd 不做任何锚定")
        else:
            a = ga.resolve_anchor(str(wt))
            note("D1-worktree-anchor",
                 "OK" if a and a.kind == "worktree" else "BAD",
                 f"git_dir={getattr(a, 'git_dir', None)}")
            a2 = ga.resolve_anchor(str(proj))
            note("D2-main-anchor", "OK" if a2 and a2.kind == "main" else "BAD",
                 f"git_dir={getattr(a2, 'git_dir', None)}")

        # ── A 指针 → 自建仓（agent 真实手法：rm .git && git init） ──
        (wt / ".git").unlink()
        r = raw(wt, "init", "-q")
        assert r.returncode == 0, r.stderr
        raw(wt, "config", KEY, "FROM_EVIL_POINTER")
        note("A1-positive-control-raw-git",
             "EXECUTED" if raw(wt, "config", "--get", KEY).stdout.strip()
             == "FROM_EVIL_POINTER" else "BLOCKED",
             "裸 git（修复前形态）")
        ok, out = platform(wt, "config", "--get", KEY)
        note("A2-platform-refuses",
             "REFUSED" if (not ok and "FROM_EVIL_POINTER" not in out) else
             "EXECUTED",
             f"ok={ok} out={out[:70]!r}")
        # 复原 worktree（清掉自建仓，重建指针）
        shutil.rmtree(wt / ".git")
        (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

        # ── B commondir → 有效 evil common ────────────────────
        evil = base / "evilcommon"
        (evil / "objects").mkdir(parents=True, exist_ok=True)
        (evil / "refs").mkdir(exist_ok=True)
        (evil / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n"
            f"[probe]\n\tanchor = FROM_EVIL_COMMON\n", encoding="utf-8")
        rel = os.path.relpath(evil, gitdir).replace("\\", "/")
        (gitdir / "commondir").write_text(rel, encoding="utf-8")
        note("B1-positive-control-raw-git",
             "EXECUTED" if raw(wt, "config", "--get", KEY).stdout.strip()
             == "FROM_EVIL_COMMON" else "BLOCKED",
             "裸 git（修复前形态）")
        ok, out = platform(wt, "config", "--get", KEY)
        note("B2-platform-blocked",
             "BLOCKED" if "FROM_EVIL_COMMON" not in out else "EXECUTED",
             f"ok={ok} out={out[:70]!r}")
        ok, out = platform(wt, "config", "--get", "core.repositoryformatversion")
        note("B3-platform-normal-read-still-works",
             "OK" if ok and out.strip() == "0" else "FAILED", f"out={out!r}")
        (gitdir / "commondir").write_text("../..", encoding="utf-8")

        # ── C 功能面（钉住之后平台的 git 用法照常） ─────────────
        (wt / "w.txt").write_text("w\n", encoding="utf-8")
        flows = [
            ("C1-status", lambda: platform(wt, "status", "--porcelain")),
            ("C2-add", lambda: platform(wt, "add", "-A")),
            ("C3-commit", lambda: platform(wt, "commit", "-qm", "wt")),
            ("C4-rev-parse",
             lambda: platform(wt, "rev-parse", "--abbrev-ref", "HEAD")),
            ("C5-worktree-list",
             lambda: platform(proj, "worktree", "list", "--porcelain")),
            ("C6-merge", lambda: platform(proj, "merge", "--ff-only", "-q",
                                          "wt/A001")),
            ("C7-main-config",
             lambda: platform(proj, "config", "user.email", "p@t.t")),
            ("C8-main-add", lambda: platform(proj, "add", "-A")),
            ("C9-main-commit", lambda: platform(proj, "commit", "-qm", "main")),
        ]
        for tag, fn in flows:
            ok, out = fn()
            note(tag, "OK" if ok else "FAILED", (out or "")[:70])
        wt2 = proj / ".hiveweave" / "worktrees" / "B002"
        ok, out = platform(proj, "worktree", "add", "-q", str(wt2),
                           "-b", "wt/B002")
        note("C10-worktree-add", "OK" if ok else "FAILED", (out or "")[:70])
        ok, out = platform(wt2, "status", "--porcelain")
        note("C11-new-worktree-status", "OK" if ok else "FAILED",
             (out or "")[:70])

        # ── E 兄弟布局（合法但派生不出）⇒ 不拒绝 ────────────────
        sib = base / "sibling-wt"
        raw(proj, "worktree", "add", "-q", str(sib), "-b", "wt/sib")
        anchor, refusal = (ga.anchor_for_git(str(sib)) if HAVE_ANCHOR
                           else (None, None))
        note("E1-sibling-layout-not-refused",
             "OK" if anchor is None and refusal is None else "REFUSED",
             f"anchor={anchor} refusal={refusal}")
        ok, out = platform(sib, "status", "--porcelain")
        note("E2-sibling-layout-git-works", "OK" if ok else "FAILED",
             (out or "")[:70])
    finally:
        shutil.rmtree(base, ignore_errors=True)
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
