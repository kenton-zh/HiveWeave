"""批 A 第 0 步（2026-09-26）：MAIN gitdir 数据面 —— CEO 在 MAIN 就地改码/还原。

对应改动：`acl_sandbox/sid.py::git_main_sid`（仅 MAIN 边界携带）、
`acl_sandbox/grant.py::grant_git_main_dir_aces`（`.git` 根成对窄 ACE：
非继承创建位 + OI|IO|NP 只作用直接子文件的删/写位）、
`acl_sandbox/service.py`（`.git` 根成对窄 ACE 授予 + `.git/worktrees` 目录
授予 + hooks 缺席补建封条（并摘除 NP 传播的惰性 IO 副本）+ 封条读回的
IO 跳过与创建位**掩码+旗标**精确豁免）。

判据形态（本仓纪律，与 `test_git_config_seal.py` 同族）：
- **状态判据**：看盘上文件是否被改（f.txt 是否还原、index 字节是否变化、
  hook 文件是否出现），不看 agent 回执文案；
- **封条负面对照**：数据面放开**不得**连带 config/config.worktree/hooks ——
  这三个 RCE 载体在此逐个再钉一次（MAIN 边界 = 最强形态：令牌持创建位）；
- **范围对照**：worktree 边界令牌**不携带** git_main_sid ⇒ 主树 `.git`
  数据面对它们依旧全封闭（能力是 MAIN 位的，不是全家族的）。

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
from hiveweave.services.acl_sandbox.grant import WriteGrant
from hiveweave.services.acl_sandbox.service import spawn_confined

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("ACL sandbox win32 integration tests require Windows",
                allow_module_level=True)

COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


@pytest.fixture(scope="session", autouse=True)
def _shutdown_acl_runner():
    yield
    from hiveweave.services.acl_sandbox.service import shutdown_runner
    from hiveweave.services.acl_sandbox.spawn import stop_watcher

    stop_watcher()
    shutdown_runner()


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """项目根 + 一个真实 worktree（平台布局：`.hiveweave/worktrees/A001`）。"""
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    from tests.test_git_config_seal import _subject_ace_helpers

    _subject_ace_helpers()(tmp_path)
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
    r = _raw_git(proj, "worktree", "add", "-q", str(wt), "-b", "wt/A001")
    assert r.returncode == 0, r.stderr
    return wt


@pytest.fixture(autouse=True)
def _sandbox_on(monkeypatch):
    monkeypatch.setattr(settings, "acl_sandbox", True)


async def _agent(workdir: Path, project: Path, inner: str, *,
                 agent_id: str = "A001", entry: str = "bash"):
    """受限执行：MAIN 用 workdir=project + entry=bash_main。"""
    return await spawn_confined(
        command=f'"{COMSPEC}" /c {inner}', workdir=str(workdir),
        workspace_path=str(workdir), project_workspace_path=str(project),
        agent_id=agent_id, timeout_s=90, entry=entry)


async def _bootstrap_main(proj: Path, wt: Path) -> None:
    """先跑一条 MAIN 命令把 standing grants + 封条 + 数据面授予铺上（与生产同序）。"""
    r = await _agent(proj, proj, "echo boot", agent_id="CEO", entry="bash_main")
    assert r is not None and r["exit_code"] == 0, r


# ══════════════════════════════════════════════════════════════════
# 1. e2e：MAIN 就地改文件 + git checkout 还原（批 A 的验收主判据）
# ══════════════════════════════════════════════════════════════════

async def test_ceo_checkout_restores_file_at_main(proj: Path, wt: Path) -> None:
    """CEO（MAIN 边界受限令牌）改工作树文件 + `git checkout --` 还原不再被 ACL 拒。

    还原路径要写 `.git/index`（lock + rename）——正是旧契约封死、本批放开的
    数据面。判据是状态：f.txt 字节回到已提交版本。
    """
    await _bootstrap_main(proj, wt)
    r = await _agent(proj, proj, "echo dirty> f.txt",
                     agent_id="CEO", entry="bash_main")
    assert r is not None and r["exit_code"] == 0, r
    assert (proj / "f.txt").read_text(
        encoding="utf-8", errors="replace").startswith("dirty"), "前置弄脏失败"
    r = await _agent(proj, proj, "git checkout -- f.txt",
                     agent_id="CEO", entry="bash_main")
    assert r is not None, r
    assert r.get("enforcement") == "confined", "e2e 必须走受限路径（否则判据落空）"
    assert r["exit_code"] == 0, r
    assert (proj / "f.txt").read_text(
        encoding="utf-8", errors="replace").strip() == "a", "checkout 未还原文件"


async def test_ceo_commit_at_main_updates_refs(proj: Path, wt: Path) -> None:
    """MAIN 边界受限 git 全流程：add + commit（index/objects/refs/logs 数据面）。"""
    await _bootstrap_main(proj, wt)
    (proj / "new.txt").write_text("n\n", encoding="utf-8")
    r = await _agent(
        proj, proj,
        "git add new.txt && git -c user.name=C -c user.email=c@c commit -qm c1",
        agent_id="CEO", entry="bash_main")
    assert r is not None, r
    # 必须真的走受限路径 —— native 降级会让本测试假绿（对齐 checkout 主判据）
    assert r.get("enforcement") == "confined", r
    assert r["exit_code"] == 0, r
    log = _raw_git(proj, "log", "--oneline", "-1")
    assert "c1" in log.stdout, f"平台侧看不到 MAIN 新提交: {log.stdout!r}"


# ══════════════════════════════════════════════════════════════════
# 2. 封条负面对照：数据面放开不连带 RCE 载体（MAIN 边界最强形态）
# ══════════════════════════════════════════════════════════════════

async def test_ceo_cannot_write_git_config_at_main(proj: Path, wt: Path) -> None:
    """MAIN 令牌持创建位后，`git config`（lock+rename）仍必须被锁死档挡住。"""
    await _bootstrap_main(proj, wt)
    cfg = proj / ".git" / "config"
    before = cfg.read_bytes()
    r = await _agent(proj, proj, "git config probe.main.denied 1",
                     agent_id="CEO", entry="bash_main")
    assert cfg.exists() and cfg.read_bytes() == before, "受限 agent 改写了 .git/config"


async def test_ceo_cannot_touch_hooks_at_main(proj: Path, wt: Path) -> None:
    """hooks/ 是执行载体：既有 hook 改写与新建 hook 文件都必须被拒。"""
    await _bootstrap_main(proj, wt)
    hooks = proj / ".git" / "hooks"
    hook = hooks / "pre-commit"
    hook.write_text("# platform\n", encoding="utf-8")
    before = hook.read_bytes()
    await _agent(proj, proj, r"echo evil> .git\hooks\pre-commit",
                 agent_id="CEO", entry="bash_main")
    assert hook.read_bytes() == before, "受限 agent 改写了既有 hook"
    await _agent(proj, proj, r"echo evil> .git\hooks\post-commit",
                 agent_id="CEO", entry="bash_main")
    assert not (hooks / "post-commit").exists(), "受限 agent 新建了 hook 文件"


async def test_missing_hooks_dir_recreated_and_sealed(proj: Path, wt: Path) -> None:
    """hooks/ 缺席 ⇒ 封条阶段平台先建空目录；agent 仍不能在里面投放文件。

    创建位（FILE_ADD_SUBDIRECTORY on `.git` 根）让缺失的 hooks/ 变成 agent
    可自建目录 = RCE 复活口 —— 平台补建 + 封条是创建位的前置硬约束。
    """
    shutil.rmtree(proj / ".git" / "hooks")
    await _bootstrap_main(proj, wt)
    hooks = proj / ".git" / "hooks"
    assert hooks.is_dir(), "平台未补建 hooks/ —— agent 可自建并投放 hook"
    await _agent(proj, proj, r"echo evil> .git\hooks\pre-commit",
                 agent_id="CEO", entry="bash_main")
    assert not (hooks / "pre-commit").exists(), "补建的 hooks/ 未被封住"


async def test_git_root_carries_only_narrow_create_ace(proj: Path, wt: Path) -> None:
    """结构网：`.git` 根上能力 SID ACE = git_main_sid 的两条精确形态，别无其他。

    ① (GIT_MAIN_CREATE_MASK, flags=0) 非继承创建位；② (GIT_MAIN_FILE_INHERIT_MASK,
    OI|IO) 只随直接子文件继承的删/写位。防止本批的读回豁免被将来某次「顺手
    放宽」变成宽授 —— 任何其他掩码/形态出现，封条读回必须 fail-closed
    （service 侧逻辑），这里钉的是**盘面终态**。
    """
    await _bootstrap_main(proj, wt)
    from hiveweave.services.acl_sandbox.grant import (
        GIT_MAIN_CREATE_MASK,
        GIT_MAIN_FILE_INHERIT_MASK,
        OI_IO_NO_PROPAGATE,
    )
    from hiveweave.services.acl_sandbox.sid import git_main_sid

    expected_sid = git_main_sid(str(proj))
    aces = WriteGrant.list_aces(str(proj / ".git"))
    agent_aces = sorted(
        (sid, mask, flags) for ace_type, flags, mask, sid in aces
        if ace_type == 0 and sid.startswith("S-1-4-")  # 0 = ACCESS_ALLOWED_ACE_TYPE
    )
    assert agent_aces == sorted([
        (expected_sid, GIT_MAIN_CREATE_MASK, 0),
        (expected_sid, GIT_MAIN_FILE_INHERIT_MASK, OI_IO_NO_PROPAGATE),
    ]), f"`.git` 根能力 ACE 形态漂移: {agent_aces}"


# ══════════════════════════════════════════════════════════════════
# 3. 范围对照：git_main_sid 只属于 MAIN 边界
# ══════════════════════════════════════════════════════════════════

async def test_worktree_agent_cannot_write_main_git_data_plane(
        proj: Path, wt: Path) -> None:
    """worktree 边界令牌不携带 git_main_sid ⇒ 主树 `.git/index` 对其依旧全封闭。

    这是「ceo family 写面」的 ACL 表达形态：能力按**边界**（MAIN）而非
    family 授予 —— 在自己 worktree 里干活的 agent（executor/builder）拿不到。
    """
    await _bootstrap_main(proj, wt)
    index = proj / ".git" / "index"
    before = index.read_bytes()
    await _agent(wt, proj, r"(echo x)>> ..\..\..\.git\index")
    assert index.read_bytes() == before, "worktree 令牌写到了主树 .git/index"
