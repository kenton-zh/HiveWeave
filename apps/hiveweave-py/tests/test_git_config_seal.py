"""#2 GitSpawn 治本：git 引导文件封条（真令牌端到端 + 阳性对照 + fail-closed）。

对应修复：`services/acl_sandbox/grant.py::seal_agent_aces` / `deny_delete_child`
+ `services/acl_sandbox/service.py::_seal_git_bootstrap_files`。

判据形态（本仓纪律）：
- **状态判据**：断言盘上文件是否被改动 / 删除重建，不看 agent 回执文案；
- **阳性对照**：把关掉封条后的同一攻击跑成**必须成功**，证明"拦住"是封条干的，
  不是别的机制顺手挡住（`probe_git_write_surface.py` 修前基线里 E1–E3 全
  EXECUTED、D1–D3 全 DELETED）；
- **fail-closed**：封条写不下去时**命令不得执行**（封条是安全不变量）；
- **结构网**：封条目标清单必须覆盖已取证的五个载体，少一个就转红。

仅 Windows 运行（`@pytest.mark.win32`）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hiveweave.config import settings
from hiveweave.services.acl_sandbox import service as svc
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.grant import GRANT_MASK, WriteGrant
from hiveweave.services.acl_sandbox.service import spawn_confined

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("ACL sandbox win32 integration tests require Windows",
                allow_module_level=True)

COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from win32security import (  # noqa: E402
    DACL_SECURITY_INFORMATION,
    GetNamedSecurityInfo,
    SE_FILE_OBJECT,
)

pytest.importorskip("win32api")


@pytest.fixture(scope="session", autouse=True)
def _shutdown_acl_runner():
    yield
    from hiveweave.services.acl_sandbox.service import shutdown_runner
    from hiveweave.services.acl_sandbox.spawn import stop_watcher

    stop_watcher()
    shutdown_runner()


# ── 夹具：真实主体 ACE 的 workspace（§4.12） ───────────────────────
def _subject_ace_helpers():
    import win32api
    import win32con
    import win32security as ws

    def has_ace(d: Path, user) -> bool:
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

    def grant(d: Path, user) -> None:
        sd = ws.GetNamedSecurityInfo(
            str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
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

    def ensure(path: Path) -> None:
        tok = ws.OpenProcessToken(win32api.GetCurrentProcess(), ws.TOKEN_QUERY)
        user, _ = ws.GetTokenInformation(tok, ws.TokenUser)
        tok.Close()
        if not has_ace(path, user):
            grant(path, user)
        anc = path.parent
        while anc != anc.parent and str(anc).lower() != str(anc.anchor).lower():
            if has_ace(anc, user):
                break
            grant(anc, user)
            anc = anc.parent

    return ensure


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """项目根 + 一个真实 worktree（平台布局：`.hiveweave/worktrees/A001`）。"""
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    ensure = _subject_ace_helpers()
    ensure(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".hiveweave").mkdir()
    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    (proj / "f.txt").write_text("a\n", encoding="utf-8")
    _raw_git(proj, "add", "f.txt")
    _raw_git(proj, "commit", "-qm", "init")
    return proj


@pytest.fixture
def wt(proj: Path) -> Path:
    wt = proj / ".hiveweave" / "worktrees" / "A001"
    _raw_git(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")
    # 平台在 worktree 创建时开启 worktreeConfig（service_create.py:620）
    _raw_git(wt, "config", "extensions.worktreeConfig", "true")
    return wt


@pytest.fixture(autouse=True)
def _sandbox_on(monkeypatch):
    monkeypatch.setattr(settings, "acl_sandbox", True)


async def _agent(workdir: Path, project: Path, inner: str, *,
                 agent_id: str = "A001", entry: str = "bash"):
    """受限执行：授权树根 = workdir（executor=worktree）；项目根另传。"""
    return await spawn_confined(
        command=f'"{COMSPEC}" /c {inner}', workdir=str(workdir),
        workspace_path=str(workdir), project_workspace_path=str(project),
        agent_id=agent_id, timeout_s=90, entry=entry)


async def _bootstrap(wt: Path, proj: Path) -> None:
    """先跑一条命令把 standing grants + 封条铺上（与生产同序）。"""
    r = await _agent(wt, proj, "echo boot")
    assert r is not None and r["exit_code"] == 0, r


def _capability_aces(path: Path) -> list[str]:
    sd = GetNamedSecurityInfo(
        str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    out = []
    if dacl is None:
        return out
    import win32security as ws

    for i in range(dacl.GetAceCount()):
        ((_t, _f), _m, s) = dacl.GetAce(i)
        sid = ws.ConvertSidToStringSid(s)
        if sid.startswith("S-1-4-"):
            out.append(sid)
    return out


# ══════════════════════════════════════════════════════════════════
# 1. agent 侧：五个载体全部不可写 / 不可删建
# ══════════════════════════════════════════════════════════════════
async def test_agent_cannot_write_git_config(proj: Path, wt: Path) -> None:
    """`.git/config` 是驱动定义主载体：受限 agent 写不进（修前基线 WROTE）。"""
    await _bootstrap(wt, proj)
    cfg = proj / ".git" / "config"
    before = cfg.read_bytes()
    await _agent(wt, proj, r"git config probe.denied 1")
    assert cfg.read_bytes() == before, "受限 agent 改写了 .git/config"


@pytest.mark.xfail(
    strict=True,
    reason="已实测残余（pass-1 层）：Windows 的删除权「对象自己的 DELETE」由**普通令牌**"
           "判定 —— `del <文件>` 走得通，而 create 才归 pass-2 管。所以收窄能挡住"
           "「替换」（= 本条的代码执行跳板），挡不住「删掉」（DoS：config 丢失 ⇒ "
           "身份/ extensions.worktreeConfig 回到默认）。修法（已定位，属计划里的"
           "「双阶段」）：锁死期连**平台主体**的 DELETE 一起摘掉（`git config` 的 "
           "lock+rename 也需 DELETE ⇒ 平台同样写不进去），并把平台侧的 config 写入"
           "全部挪到锁之前 / 改走带外通道。本条转红 = 双阶段落地。",
)
async def test_agent_cannot_delete_git_config(proj: Path, wt: Path) -> None:
    """`del .git/config` 的 DoS —— **已知残余**（见 xfail reason）。"""
    await _bootstrap(wt, proj)
    cfg = proj / ".git" / "config"
    await _agent(wt, proj, r"del ..\..\..\.git\config")
    assert cfg.exists(), "受限 agent 删掉了 .git/config"


async def test_agent_cannot_replace_git_config(proj: Path, wt: Path) -> None:
    """`git config` 是 lock+rename 替换 ⇒ 判据必须是「文件内容不变」。"""
    await _bootstrap(wt, proj)
    cfg = proj / ".git" / "config"
    before = cfg.read_bytes()
    await _agent(wt, proj, "git config probe.denied 1")
    assert cfg.exists() and cfg.read_bytes() == before, (
        "受限 agent 用 lock+rename 替换掉了 .git/config")


async def test_agent_cannot_create_worktree_config_carriers(
        proj: Path, wt: Path) -> None:
    """`.git` 下的两个 worktree-config 载体（K1 实测 git HONORED）都不许 agent 建/写。"""
    await _bootstrap(wt, proj)
    main_carrier = proj / ".git" / "config.worktree"
    assert main_carrier.exists(), "占位载体未预建（agent 就能自己新建同名文件）"
    before_main = main_carrier.read_bytes()

    await _agent(proj, proj, "git config --worktree filter.p.test x",
                 agent_id="CEO", entry="bash_main")
    assert main_carrier.read_bytes() == before_main, "主载体被写入"


@pytest.mark.xfail(
    strict=True,
    reason="已实测残余：worktree gitdir 必须对 agent 可写（index/index.lock），"
           "故 `<gitdir>/config.worktree` 可被 lock+rename 替换掉封条。"
           "根因是平台自己打开 extensions.worktreeConfig ⇒ 该载体被 git 读。"
           "修法（下一批，已定位）：把 per-agent git 身份从 `--worktree` 配置改为 "
           "GIT_AUTHOR_* / GIT_COMMITTER_* 环境变量，然后不再启用该扩展 ⇒ "
           "该载体整体失效，无需依赖 ACL。本条转红 = 修法落地，改回正常断言。",
)
async def test_agent_cannot_write_worktree_gitdir_carrier(
        proj: Path, wt: Path) -> None:
    """`<gitdir>/config.worktree`：**已知残余**（见 xfail reason）。"""
    await _bootstrap(wt, proj)
    carrier = proj / ".git" / "worktrees" / "A001" / "config.worktree"
    assert carrier.exists(), "占位载体未预建"
    before = carrier.read_bytes()
    await _agent(wt, proj, "git config --worktree filter.p.test x")
    assert carrier.read_bytes() == before, "worktree 载体被写入"


async def test_agent_cannot_mutate_main_git_metadata(proj: Path, wt: Path) -> None:
    """行为变更（有意）：agent 不再能改**主树**的 git 元数据。

    收窄的代价面：`.git` 根不授写 ⇒ 主树 `.git/index`·`ORIG_HEAD`·`COMMIT_EDITMSG`
    这类「直接落在 `.git` 下」的写入不再可行（agent 自己的 git 落在 worktree gitdir，
    不受影响）。判据是**状态**：主树 index 内容不得变 —— 不靠退出码/文案。
    """
    await _bootstrap(wt, proj)
    index = proj / ".git" / "index"
    before = index.read_bytes()
    (proj / "mut.txt").write_text("m\n", encoding="utf-8")
    await _agent(proj, proj, "git add -A", agent_id="CEO", entry="bash_main")
    assert index.read_bytes() == before, "受限 agent 改写了主树 .git/index"


async def test_agent_cannot_rewrite_worktree_gitdir_pointer(
        proj: Path, wt: Path) -> None:
    """`<wt>/.git` 是 gitdir 指针：改它 = 平台在 worktree 里的 git 读 agent 的 config。"""
    await _bootstrap(wt, proj)
    ptr = wt / ".git"
    before = ptr.read_bytes()
    await _agent(wt, proj, "echo gitdir: C:/nope > .git")
    assert ptr.read_bytes() == before, "受限 agent 改写了 worktree 的 gitdir 指针"


# ══════════════════════════════════════════════════════════════════
# 2. 平台侧：封条不得伤到平台自己的 git 用法
# ══════════════════════════════════════════════════════════════════
async def test_platform_git_write_paths_still_work(proj: Path, wt: Path) -> None:
    """计划 §四 担心的「收权限会坏三处」：ACL 方案下平台写入必须仍然成功。

    （那是**只读文件属性**探针的产物：属性对一切主体生效；ACL 只摘受限 SID。）
    """
    await _bootstrap(wt, proj)
    assert _raw_git(proj, "config", "user.email", "p@t.t").returncode == 0
    assert "p@t.t" in _raw_git(proj, "config", "--get",
                               "user.email").stdout
    assert _raw_git(wt, "config", "extensions.worktreeConfig",
                    "true").returncode == 0
    wt2 = proj / ".hiveweave" / "worktrees" / "B002"
    assert _raw_git(proj, "worktree", "add", "-q", str(wt2),
                    "-b", "wt/B002").returncode == 0
    (wt / "g.txt").write_text("g\n", encoding="utf-8")
    assert _raw_git(proj, "add", "-A").returncode == 0
    assert _raw_git(proj, "commit", "-qm", "cp").returncode == 0
    assert _raw_git(proj, "status", "--porcelain").returncode == 0
    assert _raw_git(proj, "merge", "--ff-only", "-q", "wt/A001").returncode == 0


async def test_agent_git_flow_still_works_in_worktree(proj: Path, wt: Path) -> None:
    """封条不许把 agent 自己的 git 用法一起封掉（worktree 内 add/commit）。"""
    await _bootstrap(wt, proj)
    (wt / "w.txt").write_text("w\n", encoding="utf-8")
    r = await _agent(wt, proj,
                     "git add w.txt && git -c user.name=A -c user.email=a@a "
                     "commit -qm wt && git log --oneline -1")
    assert r is not None and r["exit_code"] == 0, r


# ══════════════════════════════════════════════════════════════════
# 3. 阳性对照 / fail-closed / 结构网
# ══════════════════════════════════════════════════════════════════
async def test_positive_control_with_old_wide_grant_agent_can_write(
        proj: Path, wt: Path, monkeypatch) -> None:
    """阳性对照：把**修前的宽授**（`.git` 整棵 GRANT_MASK）还回去，同一攻击必须成功。

    否则上文的「拦住」可能另有原因（例如命令没跑、路径写错），封条就不是那个因。
    """
    async def _legacy_wide_grant(policy, agrant):
        from hiveweave.services.acl_sandbox.sid import git_sid
        await svc._grant_if_missing(
            os.path.join(os.path.realpath(policy.project_root), ".git"),
            git_sid(policy.project_root), GRANT_MASK, agrant)
        return ["legacy-wide-grant"]

    monkeypatch.setattr(svc, "_seal_git_bootstrap_files", _legacy_wide_grant)
    await _bootstrap(wt, proj)
    cfg = proj / ".git" / "config"
    before = cfg.read_bytes()
    await _agent(wt, proj, "git config probe.positive 1")
    assert cfg.read_bytes() != before, (
        "阳性对照失败：还回宽授后 agent 仍写不进 .git/config ⇒ 上文的拒绝"
        "不是收窄造成的，需重查探针形态")


async def test_fail_closed_when_seal_raises(proj: Path, wt: Path,
                                            monkeypatch) -> None:
    """封条写不下去 ⇒ 命令**不得执行**（不许静默降级）。"""
    def _boom(self, path, sids):
        raise SandboxUnavailableError(f"boom: {path}")

    monkeypatch.setattr(WriteGrant, "seal_agent_aces", _boom)
    marker = wt / "ran.txt"
    with pytest.raises(Exception) as exc:
        await _agent(wt, proj, "echo x > ran.txt")
    assert "boom" in str(exc.value) or "sandbox" in str(exc.value).lower(), exc
    assert not marker.exists(), "封条失败却仍执行了命令"


async def test_seal_covers_all_known_carriers(proj: Path, wt: Path) -> None:
    """结构网：封条目标必须覆盖已取证的五个载体（漏一个就是缺口回归）。"""
    recorded: list[tuple[str, str]] = []

    class _Recording:
        async def seal_agent_aces_async(self, path, sids):
            recorded.append(("seal", os.path.normcase(path)))
            return False

        async def deny_delete_child_async(self, path, sids):
            recorded.append(("deny-dc", os.path.normcase(path)))
            return 0

        async def ace_present_async(self, path, sid, mask=GRANT_MASK) -> bool:
            recorded.append(("grant", os.path.normcase(path)))
            return True

        async def grant_standing_async(self, path, sid, mask=GRANT_MASK):
            recorded.append(("grant", os.path.normcase(path)))

    from hiveweave.services.acl_sandbox.policy import resolve_policy

    policy = resolve_policy(workspace_path=str(wt), agent_id="A001",
                            entry="bash", project_workspace_path=str(proj))
    await svc._seal_git_bootstrap_files(policy, _Recording())
    sealed = {p for kind, p in recorded if kind == "seal"}
    denied = {p for kind, p in recorded if kind == "deny-dc"}
    granted = {p for kind, p in recorded if kind == "grant"}
    git_dir = os.path.normcase(str(proj / ".git"))
    wt_dir = os.path.normcase(str(wt))
    for expected in (
        os.path.join(git_dir, "config"),
        os.path.join(git_dir, "config.worktree"),
        os.path.join(git_dir, "worktrees", "a001", "config.worktree"),
        os.path.join(git_dir, "worktrees", "a001", "commondir"),
        os.path.join(wt_dir, ".git"),
    ):
        assert os.path.normcase(expected) in sealed, f"未封: {expected}"
    for expected in (git_dir, os.path.join(git_dir, "worktrees", "a001"),
                     wt_dir):
        assert os.path.normcase(expected) in denied, f"未禁删子项: {expected}"
    # 写面收窄：`.git` 根本身**不得**出现在授予面里（授了就能 lock+rename 替换）
    assert os.path.normcase(git_dir) not in granted, \
        "`.git` 根仍被授予写 —— 配置载体可被替换"
    for name in ("objects", "refs", "logs"):
        assert os.path.join(git_dir, name) in granted, f"未授 git 子目录: {name}"


async def test_fail_closed_when_worktree_pointer_is_not_a_file(
        proj: Path, wt: Path) -> None:
    """指针被删/被换成目录 ⇒ **必须 fail-closed**，不许静默跳过封条（审计 A1）。

    审实测：受限 agent 删得掉 `<wt>/.git`（删走 pass-1 的 user ACE），再建一个
    同名**目录**并写入自己的 config ⇒ 平台在该 worktree 的 `git add -A` 会执行
    其载荷。ACL 挡不住这条（create 归 pass-2 管得住、delete 管不住），故判据改成
    「指针**身份**」：不是指向期望 gitdir 的文件 ⇒ 拒绝执行 agent 命令（loud）。
    """
    await _bootstrap(wt, proj)
    ptr = wt / ".git"
    content = ptr.read_text(encoding="utf-8")
    ptr.unlink()
    ptr.mkdir()                      # 换成同名目录（模拟 agent 的替换）
    (ptr / "config").write_text("[filter \"x\"]\n\tclean = calc\n",
                                encoding="utf-8")
    with pytest.raises(Exception) as exc:
        await _agent(wt, proj, "echo should-not-run")
    assert "gitdir" in str(exc.value).lower() or "指针" in str(exc.value), exc
    # 构造一个「文件但内容不对」的形态也必须拒
    shutil.rmtree(ptr)
    ptr.write_text("gitdir: C:/somewhere/else\n", encoding="utf-8")
    with pytest.raises(Exception):
        await _agent(wt, proj, "echo should-not-run")
    ptr.write_text(content, encoding="utf-8")   # 复原，便于同会话其它用例


# ══════════════════════════════════════════════════════════════════
# 4. 原语级（不依赖受限令牌）
# ══════════════════════════════════════════════════════════════════
def test_seal_removes_capability_ace_but_keeps_platform_ace(tmp_path: Path) -> None:
    """摘掉能力 SID 的 ACE，同时**保住平台主体**的写权（否则平台自己写不进去）。"""
    ensure = _subject_ace_helpers()
    ensure(tmp_path)
    f = tmp_path / "s.txt"
    f.write_text("x", encoding="utf-8")
    cap = "S-1-4-123456789-987654321"
    assert WriteGrant.grant_standing(str(f), cap, GRANT_MASK) is True
    assert _capability_aces(f) == [cap]

    assert WriteGrant.seal_agent_aces(str(f), {cap}) is True
    assert _capability_aces(f) == []
    # 幂等
    assert WriteGrant.seal_agent_aces(str(f), {cap}) is False
    # 平台仍可写
    f.write_text("y", encoding="utf-8")
    assert f.read_text(encoding="utf-8") == "y"


def test_deny_delete_child_is_idempotent(tmp_path: Path) -> None:
    ensure = _subject_ace_helpers()
    ensure(tmp_path)
    cap = "S-1-4-123456789-987654321"
    assert WriteGrant.deny_delete_child(str(tmp_path), {cap}) == 1
    assert WriteGrant.deny_delete_child(str(tmp_path), {cap}) == 0
