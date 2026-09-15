"""probe_git_write_surface.py —— #2「收回 .git/config 写权限」取证 / 验收探针（真令牌）。

判据必须跑出来，不要读出来。本探针用平台**自己的** `spawn_confined`（同一受限
令牌路径）逐条实测 agent 对 `.git` 下文件的写能力，并把每条能力接到「它是不是
GitSpawn 的跳板」上。**同一脚本修前修后各跑一次**，即为 #2 的对照证据。

用法（仓库根）：
    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/probe_git_write_surface.py
    ... > /tmp/probe.log 2>&1      # ⚠ 不要用 `| tail`（管道缓冲到进程结束，看不到进度）

分区：
  K*  载体语义（平台侧）：git 到底认哪个 config 文件
  V*  agent 写能力（受限令牌）：能否写/删建 config 及其它候选载体
  E*  端到端攻击链：agent 写载体 → 平台**经漏斗**跑 git → 载荷是否执行
  F*  可用性：平台三处 config 写入 / git 编排 / agent 自己的 git 流
  G*  残余写面盘点：封条后 `.git` 下还有哪些路径 agent 可写

失败**不吞**：任何意外异常直接抛出（宁可见红也不要假绿）。
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
# 允许指向**纯净树**（`git archive HEAD` 导出）：修前基线必须跑在未改动的 src 上，
# 否则"修前/修后对照"两边跑的是同一份代码。用法见文件头。
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

rows: list[tuple[str, str, str]] = []


def note(tag: str, verdict: str, detail: str = "") -> None:
    rows.append((tag, verdict, detail))
    print(f"PROBE\t{tag}\t{verdict}\t{detail}", flush=True)


def _hard_exit() -> None:
    """flush 后硬退 —— 见 main() 的 finally 注释（优雅收尾会阻塞）。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def _fwd(p: Path | str) -> str:
    return str(p).replace("\\", "/")


# ── 夹具：模拟用户常规目录（真实主体 ACE）+ 平台项目布局 ──────────
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
    """§4.12：给目录及其 OWNER_RIGHTS-only 祖先补当前用户 SID 写 ACE。"""
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


def _aces(path: str) -> list[tuple[str, int, int]]:
    """[(sid_str, ace_flags, mask)] —— 读盘现状，不做推断。"""
    sd = ws.GetNamedSecurityInfo(
        path, ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    out: list[tuple[str, int, int]] = []
    if dacl is None:
        return out
    for i in range(dacl.GetAceCount()):
        ((_t, f), m, s) = dacl.GetAce(i)
        out.append((ws.ConvertSidToStringSid(s), f, m & 0xFFFFFFFF))
    return out


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """平台侧（不受限）git —— 用于**从平台侧**核对攻击/可用性，不看 agent 回执。"""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _funnel_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """经平台唯一 spawn 漏斗（加固 env 注入点）跑 git —— 平台的真实路径。"""
    env = {k: v for k, v in os.environ.items()}
    return wsub.hidden_run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )


def _cmd(inner: str) -> str:
    return f'"{COMSPEC}" /c {inner}'


async def _run(workdir: Path, agent_id: str, inner: str, *,
               entry: str = "bash", workspace: Path | None = None,
               project: Path | None = None):
    """受限执行。``workspace``≠``workdir`` + 传 ``project`` ⇒ 真 executor 形态
    （授权树根 = worktree，git/cache SID 派生自项目根）。

    ⚠ 这里踩过一次坑：早先版本没传 `project_workspace_path`，于是**每个**用例的
    授权树根都退化成项目根 —— 测出来的既不是 executor 形态，也让 worktree 子树
    （在 PROTECTED 的 `.hiveweave` 下）根本没有能力 SID。探针的**形态**和结论一样
    要交代。
    """
    return await spawn_confined(
        command=_cmd(inner), workdir=str(workdir),
        # 授权树根 = workdir（executor 形态下即 worktree）；
        # 项目根（git/cache SID 派生源）= workspace。⚠ 别把 workspace 当
        # workspace_path：那会把每个用例都测成「项目根边界」，并让 worktree
        # 子树（PROTECTED 的 .hiveweave 之下）一个能力 SID 都拿不到。
        workspace_path=str(workdir),
        project_workspace_path=str(project or workspace or workdir),
        agent_id=agent_id, timeout_s=90, entry=entry,
    )


def _agent_sids(paths: list[str]) -> set[str]:
    """从 standing grant 现场取「受限 SID 集」（S-1-4-* 全部视为受限面）。"""
    out: set[str] = set()
    for p in paths:
        try:
            sd = ws.GetNamedSecurityInfo(
                p, ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
        except ws.error:
            continue
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            continue
        for i in range(dacl.GetAceCount()):
            ((_t, _f), _m, s) = dacl.GetAce(i)
            sid = ws.ConvertSidToStringSid(s)
            if sid.startswith("S-1-4-"):
                out.add(sid)
    return out


async def main() -> int:
    if not sys.platform.startswith("win"):
        print("win32 only")
        return 2
    settings.acl_sandbox = True
    if not shutil.which("git"):
        print("git not on PATH")
        return 2

    base = Path(tempfile.mkdtemp(prefix="hw-gitprobe-"))
    _ensure_subject_ace(base)
    proj = base / "proj"
    proj.mkdir()
    (proj / ".hiveweave").mkdir()
    wt = proj / ".hiveweave" / "worktrees" / "A001"
    git_dir = proj / ".git"
    flag = base / "flag_pwned.txt"
    payload = base / "payload.cmd"
    payload.write_text(
        f"@echo off\r\necho hit > \"{flag}\"\r\n", encoding="utf-8", newline="")

    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    _raw_git(proj, "config", "commit.gpgsign", "false")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    _raw_git(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")
    # 平台在 worktree 创建时开启 worktreeConfig（service_create.py:620）
    _raw_git(wt, "config", "extensions.worktreeConfig", "true")

    print(f"# project  = {proj}")
    print(f"# worktree = {wt}")

    def carrier_reset() -> None:
        for p in (git_dir / "config.worktree",
                  git_dir / "worktrees" / "A001" / "config.worktree",
                  git_dir / "worktrees" / "C003" / "config.worktree",
                  proj / ".gitattributes", wt / ".gitattributes",
                  wt / "r.txt", wt / "e.txt",
                  git_dir / "info" / "attributes"):
            try:
                if p.exists():
                    os.chmod(p, 0o600)
                    p.unlink()
            except OSError:
                pass
        # 探针写进 .git/config 的测试键必须清掉 —— 否则后续用例的「平台侧核对」
        # 会读到上一个用例的残留：R1 的 REDIRECTED 就是这么来的假阳性。
        for key in ("filter.ev1.clean", "filter.ev2.clean", "filter.ev3.clean",
                    "filter.rdr.clean", "filter.evil.clean"):
            subprocess.run(["git", "config", "--unset-all", key],
                           cwd=str(proj), capture_output=True, text=True)

    try:
        r0 = await _run(proj, "CEO", "echo boot", entry="bash_main",
                        workspace=proj)
        r1 = await _run(wt, "A001", "echo boot", workspace=proj)
        print(f"# bootstrap bash_main={r0 and r0.get('exit_code')} "
              f"bash(A001)={r1 and r1.get('exit_code')}")

        # ── K 载体语义（平台侧） ────────────────────────────────
        def carrier_effective(where: Path, label: str) -> None:
            p = _raw_git(where, "config", "--get", "probe.k")
            verdict = (
                "HONORED" if p.stdout.strip() == "V1"
                else "ERROR" if p.returncode not in (0, 1)
                else "IGNORED"
            )
            note(label, verdict, f"rc={p.returncode} out={p.stdout.strip()!r} "
                                 f"err={p.stderr.strip()[:80]!r}")

        (git_dir / "config.worktree").write_text(
            "[probe]\n\tk = V1\n", encoding="utf-8")
        carrier_effective(proj, "K1-git-reads-.git/config.worktree")
        (git_dir / "config.worktree").unlink()

        wcw = git_dir / "worktrees" / "A001" / "config.worktree"
        wcw.parent.mkdir(parents=True, exist_ok=True)
        wcw.write_text("[probe]\n\tk = V1\n", encoding="utf-8")
        carrier_effective(wt, "K2-git-reads-<wt>/config.worktree")
        wcw.unlink()

        _raw_git(proj, "config", "--unset", "extensions.worktreeConfig")
        (git_dir / "config.worktree").write_text(
            "[probe]\n\tk = V1\n", encoding="utf-8")
        carrier_effective(proj, "K3-carrier-without-worktreeConfig")
        (git_dir / "config.worktree").unlink()
        _raw_git(proj, "config", "extensions.worktreeConfig", "true")

        carrier_reset()

        # ── V agent 写能力（受限令牌） ──────────────────────────
        cfg = git_dir / "config"
        baseline_cfg = cfg.read_bytes()
        await _run(wt, "A001", "git config probe.wrote 1", workspace=proj)
        note("V1-write-.git/config",
             "WROTE" if cfg.read_bytes() != baseline_cfg else "DENIED")
        _raw_git(proj, "config", "--unset", "probe.wrote")

        r = await _run(wt, "A001",
                       r"del ..\..\..\.git\config"
                       r" & echo [core] > ..\..\..\.git\config"
                       r" & echo repositoryformatversion=0 >> "
                       r"..\..\..\.git\config", workspace=proj)
        # 三种形态分开报：REPLACED = 代码执行跳板（能写回内容）；
        # DELETED-DoS = 删得掉、建不回来（Windows 删除权走 pass-1 的 user ACE，
        #   收窄管的是 create ⇒ 这是残余 DoS，不是跳板）
        if not cfg.exists():
            verdict = "DELETED-DoS"
        elif cfg.read_bytes() != baseline_cfg:
            verdict = "REPLACED"
        else:
            verdict = "DENIED"
        note("V2-delete-and-recreate-.git/config", verdict,
             f"exit={r and r.get('exit_code')}")
        if not cfg.exists() or cfg.read_bytes() != baseline_cfg:
            cfg.write_bytes(baseline_cfg)

        # 主树里用 git --worktree 写 => .git/config.worktree（**占位已存在**，
        # 故判据只能是「内容变没变」—— 早先用 exists() 判会把占位误报成 WROTE）
        mcw = git_dir / "config.worktree"
        mcw_before = mcw.read_bytes() if mcw.exists() else None
        await _run(proj, "CEO", f"git config --worktree filter.evil.clean "
                                f"{_fwd(payload)}",
                   entry="bash_main", workspace=proj)
        if not mcw.exists():
            verdict = "DELETED-DoS"
        elif mcw_before is None:
            verdict = "CREATED"
        elif mcw.read_bytes() != mcw_before:
            verdict = "WROTE"
        else:
            verdict = "DENIED"
        note("V3-write-.git/config.worktree", verdict,
             f"existed_before={mcw_before is not None}")

        # worktree 里写 => <wt gitdir>/config.worktree
        wcw_before = wcw.read_bytes() if wcw.exists() else None
        await _run(wt, "A001", f"git config --worktree filter.evil2.clean "
                               f"{_fwd(payload)}", workspace=proj)
        if not wcw.exists():
            verdict = "DELETED-DoS"
        elif wcw_before is None:
            verdict = "CREATED"
        elif wcw.read_bytes() != wcw_before:
            verdict = "WROTE"
        else:
            verdict = "DENIED"
        note("V4-write-<wt>/config.worktree", verdict,
             f"existed_before={wcw_before is not None}")

        r = await _run(wt, "A001",
                       r"echo * > ..\..\..\.git\info\attributes", workspace=proj)
        note("V5-write-.git/info/attributes",
             "WROTE" if (git_dir / "info" / "attributes").exists() else "DENIED")

        hk = git_dir / "hooks" / "post-commit"
        await _run(wt, "A001",
                   r"echo @echo x > ..\..\..\.git\hooks\post-commit",
                   workspace=proj)
        note("V6-create-.git/hooks/post-commit",
             "WROTE" if hk.exists() else "DENIED")

        # ── D 删除能力（「删掉再重建」是否可行 = 单文件封条能不能被绕过） ──
        async def delete_probe(target: Path, label: str, restore: bytes | None,
                               inner: str) -> None:
            await _run(wt, "A001", inner, workspace=proj)
            gone = not target.exists()
            note(label, "DELETED" if gone else "DENIED")
            if gone and restore is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(restore)

        (git_dir / "config.worktree").write_text("", encoding="utf-8")
        wcw.parent.mkdir(parents=True, exist_ok=True)
        wcw.write_text("", encoding="utf-8")
        await delete_probe(
            git_dir / "config", "D1-delete-.git/config",
            cfg.read_bytes(), r"del ..\..\..\.git\config")
        await delete_probe(
            git_dir / "config.worktree", "D2-delete-.git/config.worktree",
            b"", r"del ..\..\..\.git\config.worktree")
        await delete_probe(
            wcw, "D3-delete-<wt>/config.worktree", b"",
            r"del ..\..\..\.git\worktrees\A001\config.worktree")
        await delete_probe(
            wt / ".git", "D4-delete-<wt>/.git",
            (wt / ".git").read_bytes(), r"del .git")

        carrier_reset()

        # ── E 端到端攻击链（agent 写载体 → 平台经漏斗跑 git） ────
        def pivot_present(key: str, where: Path) -> bool:
            """从**平台侧**问 git 是否拿到了驱动定义 —— 不看文件文本（探针自伤过）。

            ⚠ 两处都曾经错过：① 名字要唯一（ev1/ev2/ev3）+ 用例间清理，否则读到
            上一个用例的残留；② **查询目录必须是该载体生效的树** —— worktree 局部
            config 只在那个 worktree 里生效，在项目根查它永远是"没有"（E3 的
            PIVOT-BLOCKED 就是这么来的假结论）。
            """
            p = _raw_git(where, "config", "--get", key)
            return p.returncode == 0 and p.stdout.strip() != ""

        async def attack_chain(carrier: str, where: Path, label: str,
                               agent_cwd: Path, agent_entry: str,
                               agent_id: str, fname: str) -> None:
            """carrier ∈ {config, config.worktree, wt-config.worktree}。"""
            flag.unlink(missing_ok=True)
            carrier_reset()
            # attributes 是 agent 可写面（tracked 文件）；写在 funnel git 的 cwd 树里
            (where / ".gitattributes").write_text(
                f"* filter={fname}\n", encoding="utf-8", newline="\n")
            # 载体由 **agent（受限令牌）** 写 —— 这正是 #2 要收的那一步
            key = f"filter.{fname}.clean"
            if carrier == "config":
                await _run(agent_cwd, agent_id,
                           f"git config {key} {_fwd(payload)}",
                           entry=agent_entry, workspace=proj)
            else:
                await _run(agent_cwd, agent_id,
                           f"git config --worktree {key} {_fwd(payload)}",
                           entry=agent_entry, workspace=proj)
            if not pivot_present(key, where):
                note(label, "PIVOT-BLOCKED", f"carrier={carrier} key={key}")
                return
            (where / "e.txt").write_text("e\n", encoding="utf-8")
            _funnel_git(where, "add", "e.txt")
            note(label,
                 "EXECUTED" if flag.exists() else "BLOCKED",
                 f"carrier={carrier} via=funnel")
            (where / "e.txt").unlink(missing_ok=True)

        await attack_chain("config", proj, "E1-e2e-via-.git/config",
                           wt, "bash", "A001", "ev1")
        await attack_chain("config.worktree", proj,
                           "E2-e2e-via-.git/config.worktree",
                           proj, "bash_main", "CEO", "ev2")
        await attack_chain("wt-config.worktree", wt,
                           "E3-e2e-via-<wt>/config.worktree",
                           wt, "bash", "A001", "ev3")
        carrier_reset()

        def _dump(label: str, p: Path) -> None:
            if not p.exists():
                note(f"ACL:{label}", "ABSENT")
                return
            sd = ws.GetNamedSecurityInfo(
                str(p), ws.SE_FILE_OBJECT,
                ws.DACL_SECURITY_INFORMATION
                | ws.PROTECTED_DACL_SECURITY_INFORMATION)
            ctrl, _rev = sd.GetSecurityDescriptorControl()
            note(f"ACL:{label}", "MODE",
                 f"protected={bool(ctrl & ws.SE_DACL_PROTECTED)}")
            for sid, flags, mask in _aces(str(p)):
                if sid.startswith("S-1-4-"):
                    note(f"ACL:{label}", "ACE",
                         f"{sid} flags=0x{flags:x} mask=0x{mask:x}")

        _dump("proj/.git", git_dir)
        _dump("<wt>", wt)
        _dump("<wt>/.git", wt / ".git")

        # F10/F11：worktree 根级文件删除 / 子目录删除（判「父目录禁 DC」的功能代价）
        (wt / "rootplat.txt").write_text("x\n", encoding="utf-8")
        r = await _run(wt, "A001", "del rootplat.txt", workspace=proj)
        note("F10-agent-delete-root-file",
             "OK" if (r and r.get("exit_code") == 0) and
             not (wt / "rootplat.txt").exists() else "FAILED",
             f"exit={r and r.get('exit_code')}")
        (wt / "sub").mkdir(exist_ok=True)
        (wt / "sub" / "s.txt").write_text("x\n", encoding="utf-8")
        r = await _run(wt, "A001", r"del sub\s.txt", workspace=proj)
        note("F11-agent-delete-subdir-file",
             "OK" if (r and r.get("exit_code") == 0) and
             not (wt / "sub" / "s.txt").exists() else "FAILED",
             f"exit={r and r.get('exit_code')}")

        # R（gitdir 重定向跳板）已拆到 scripts/probe_git_gitdir_redirect.py —— 它会改坏 worktree 的 .git 指针，
        # 跑在 availability 用例之前会让「修前基线」假红。

        # ── F 可用性 ────────────────────────────────────────────
        p = _raw_git(proj, "config", "user.email", "platform@t.t")
        ok = _raw_git(proj, "config", "--get", "user.email").stdout.strip()
        note("F1-platform-writes-user.email",
             "OK" if p.returncode == 0 and "platform@t.t" in ok else "FAILED",
             f"rc={p.returncode}")

        fresh = base / "fresh"
        fresh.mkdir()
        p = _raw_git(fresh, "init", "-q", "-b", "main")
        note("F2-platform-git-init",
             "OK" if p.returncode == 0 else "FAILED",
             f"rc={p.returncode} err={p.stderr.strip()[:80]!r}")

        wt2 = proj / ".hiveweave" / "worktrees" / "B002"
        p = _raw_git(proj, "worktree", "add", "-q", str(wt2), "-b", "wt/B002")
        note("F3-platform-worktree-add",
             "OK" if p.returncode == 0 else "FAILED",
             f"rc={p.returncode} err={p.stderr.strip()[:80]!r}")
        # 新 worktree 的 config 写入（service_create.py:89-90/620 形态）
        p = _raw_git(wt2, "config", "extensions.worktreeConfig", "true")
        note("F4-platform-worktree-config-write",
             "OK" if p.returncode == 0 else "FAILED", f"rc={p.returncode}")
        # 新 worktree 上再跑一次受限命令 ⇒ 封条应覆盖它
        await _run(wt2, "B002", "echo boot", workspace=proj)
        w2cw = git_dir / "worktrees" / "B002" / "config.worktree"
        await _run(wt2, "B002",
                   f"git config --worktree filter.evil3.clean {_fwd(payload)}",
                   workspace=proj)
        note("F5-new-worktree-carrier-write",
             "WROTE" if w2cw.exists() else "DENIED")

        p = _raw_git(proj, "merge", "--ff-only", "-q", "wt/A001")
        note("F6-platform-git-merge",
             "OK" if p.returncode == 0 else "FAILED",
             f"rc={p.returncode} err={p.stderr.strip()[:80]!r}")
        p = _raw_git(proj, "status", "--porcelain")
        note("F7-platform-git-status",
             "OK" if p.returncode == 0 else "FAILED", f"rc={p.returncode}")

        (wt / "g.txt").write_text("g\n", encoding="utf-8")
        r = await _run(wt, "A001",
                       "git add g.txt && git -c user.name=A -c user.email=a@a "
                       "commit -qm wtcommit && git log --oneline -1",
                       workspace=proj)
        note("F8-agent-worktree-git-flow",
             "OK" if (r and r.get("exit_code") == 0) else "FAILED",
             f"exit={r and r.get('exit_code')}")

        r = await _run(proj, "CEO",
                       "git -c user.name=C -c user.email=c@c status --short",
                       entry="bash_main", workspace=proj)
        note("F9-agent-main-tree-git-status",
             "OK" if (r and r.get("exit_code") == 0) else "FAILED",
             f"exit={r and r.get('exit_code')}")

        # ── G 残余写面盘点（静态候选 → 真写一遍） ───────────────
        sids = _agent_sids([str(git_dir), str(proj)])
        note("G0-restricted-sid-count", "INFO", f"{len(sids)} sids")
        probes = [
            (git_dir / "config", "echo [x] >> ..\\..\\..\\.git\\config"),
            (git_dir / "info" / "attributes",
             "echo * > ..\\..\\..\\.git\\info\\attributes"),
            (git_dir / "hooks" / "pre-commit",
             "echo @echo x > ..\\..\\..\\.git\\hooks\\pre-commit"),
            (git_dir / "index.lock", "echo x > ..\\..\\..\\.git\\index.lock"),
            (git_dir / "ORIG_HEAD", "echo x > ..\\..\\..\\.git\\ORIG_HEAD"),
            (git_dir / "objects" / "probe.tmp",
             "echo x > ..\\..\\..\\.git\\objects\\probe.tmp"),
            (git_dir / "worktrees" / "A001" / "probe.tmp",
             "echo x > ..\\..\\..\\.git\\worktrees\\A001\\probe.tmp"),
        ]
        for target, inner in probes:
            existed = target.exists()
            before_bytes = target.read_bytes() if existed else None
            await _run(wt, "A001", inner, workspace=proj)
            if not target.exists():
                note(f"G1-{target.name}", "DELETED", str(target.relative_to(git_dir)))
            elif not existed:
                note(f"G1-{target.name}", "CREATED", str(target.relative_to(git_dir)))
            elif target.read_bytes() != before_bytes:
                note(f"G1-{target.name}", "WROTE", str(target.relative_to(git_dir)))
            else:
                note(f"G1-{target.name}", "DENIED", str(target.relative_to(git_dir)))
        carrier_reset()
        # 汇总必须在**收尾之前**打：finally 里硬退（见下）。
        print("\n== SUMMARY ==")
        for tag, verdict, detail in rows:
            print(f"{verdict:16s} {tag}  {detail}")
    except BaseException:
        # ⚠ 探针**不许吞失败**：先打印 traceback 再退出（早先的 `finally: os._exit`
        # 会把异常一起吞掉，实测表现为"日志停在半截、退出码 0"）。
        import traceback

        traceback.print_exc()
        shutil.rmtree(base, ignore_errors=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(3)
    finally:
        # ⚠ 探针进程**不要**走优雅收尾：`stop_watcher()` / `shutdown_runner()`
        # 在本 harness 下会阻塞（实测：测量全跑完、汇总打不出来，卡到被 timeout
        # 杀掉）。这里是抛出即弃的取证进程，清掉临时目录后直接硬退（仅成功路径）。
        shutil.rmtree(base, ignore_errors=True)
        _hard_exit()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
