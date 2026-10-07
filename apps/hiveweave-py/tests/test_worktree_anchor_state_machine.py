"""批26-B：worktree 生命周期状态机收敛 —— gitdir 身份指纹的 add/remove/read 三侧。

A673 根因（B5 取证）：gitdir 同路径重建（新 inode）→ 指纹文件仍录旧 inode →
`git_anchor.anchor_for_git` fail-closed 拒跑（**正确**）→ 读侧两层吞拒因（**错误**）：

- `service_lifecycle.info` 吞成 ``status=None`` ⇒ `git_worktree_status` 渲染
  **found=false**（还配「合并时会自动拆除」的误导文案）；
- `service_merge.preflight_merge` 吞成 **worktree_detached**。

而 `list()`（锚在 MAIN）正常 ⇒ 三读数互斥。写侧则只有 `service_create` 在 add 后
重录指纹，`reconcile._try_reattach_worktree`（两处 add）与
`service_merge._auto_repair_husk` 都不重录。

本文件钉住收敛后的状态机：

1. **add→record 单点**（`git_cmd.git_worktree_add_recorded`）：重建路径
   （reconcile reattach / husk repair / create 重试链）成功后指纹**必**被重录；
2. **remove→clear 单点**（`git_anchor.clear_gitdir_identity_if_gone`）：delete
   后 gitdir 确已消失 ⇒ 旧指纹清除，同路径重建不被旧 `.id` 卡死；
3. **读侧 anchor_refused 单列**：指纹失配注入 ⇒ info 返回 ``anchor_refused``
   形态（非 found=false）、merge 前置/dry-run 返回 ``anchor_refused``
   （非 worktree_detached）；
4. **防洗白**：create 幂等路径（树已存在、无 add）**不**重录 —— 不把同路径
   替换的假仓 TOFU 洗白（旧代码无条件 record 的缺陷，一并封掉）。

win32 门（与 `test_git_anchor_identity.py` 同款）：(st_dev, st_ino) 在 NTFS 上 =
卷序列号 + 文件索引，「同路径重建必变」是该平台的实测语义。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hiveweave.services.git_worktree import git_anchor as ga
from hiveweave.services.git_worktree.git_cmd import _git, git_worktree_add_recorded
from hiveweave.services.git_worktree.service import GitWorktreeService

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("win32（NTFS inode 语义 + git for windows）",
                allow_module_level=True)


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    """进程级身份缓存会跨用例残留（同 realpath 才有影响）—— 每例清一次。"""
    ga._IDENTITY_CACHE.clear()
    yield
    ga._IDENTITY_CACHE.clear()


def _raw_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _force_rmtree(path: Path) -> None:
    """删目录（git objects 里有只读文件，Windows 上 shutil.rmtree 会 WinError 5）。"""
    subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(path)],
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    proj = tmp_path / "proj"
    proj.mkdir()
    _raw_git(proj, "init", "-q", "-b", "main")
    _raw_git(proj, "config", "user.email", "t@t.t")
    _raw_git(proj, "config", "user.name", "T")
    (proj / "README.md").write_text("hello\n", encoding="utf-8")
    _raw_git(proj, "add", "README.md")
    _raw_git(proj, "commit", "-qm", "init")
    return proj


async def _create(repo: Path, short_id: str) -> dict:
    res = await GitWorktreeService().create(str(repo), short_id)
    assert res.get("success"), res
    return res


def _gitdir_of(repo: Path, res: dict) -> str:
    return ga.platform_worktree_gitdir(str(repo), res["path"])


def _inject_mismatch(gitdir: str) -> None:
    """指纹失配注入（A673 现场形态）：把记录改成不可能的 (dev, ino)。"""
    Path(ga._identity_file(gitdir)).write_text("999999:999999\n",
                                               encoding="utf-8")
    ga._IDENTITY_CACHE.clear()


# ── 1. add→record 单点：重建路径后指纹必重录 ──────────────────────────

async def test_create_records_worktree_gitdir_identity(repo: Path) -> None:
    """阳性基线：service create 走单点后，worktree gitdir 指纹已记录且通过校验。"""
    res = await _create(repo, "A001")
    gitdir = _gitdir_of(repo, res)
    assert ga.read_gitdir_identity(gitdir) is not None
    assert ga.verify_gitdir_identity(gitdir) is None
    ok, out = await _git(["status", "--porcelain"], res["path"],
                         project_root=str(repo))
    assert ok, out


async def test_add_recorded_records_new_gitdir(repo: Path) -> None:
    """单点出口直接验收：remove→clear 清掉旧指纹后，用单点重建同一 gitdir ⇒
    新指纹即刻重录并通过校验。"""
    res = await _create(repo, "A001")
    gitdir = _gitdir_of(repo, res)
    d = await GitWorktreeService().delete(str(repo), "A001")
    assert d.get("removed") is True, d
    assert ga.read_gitdir_identity(gitdir) is None, "remove→clear 未清旧指纹"

    ok, out = await git_worktree_add_recorded(
        str(repo), res["path"],
        ["-b", "hw/A001/work", res["path"].replace("\\", "/"), "HEAD"],
    )
    assert ok, out
    assert ga.read_gitdir_identity(gitdir) is not None
    assert ga.verify_gitdir_identity(gitdir) is None


async def test_reattach_rerecords_identity_after_gitdir_rebuild(
    repo: Path,
) -> None:
    """A673 主现场：gitdir 同路径重建（旧 gitdir 消失、新 gitdir 同路径生成，
    新 inode）⇒ 旧指纹文件残留（毒丸）。reconcile 的 reattach 是该形态的修复路
    （空 husk 目录 + 分支仍在 —— 与生产注释「`worktree add --force` 会把空目录
    修复成活树」一致）：add 成功 ⇒ 单点重录 ⇒ 读侧不再 MISMATCH。修复前：
    reattach 成功后指纹仍旧 ⇒ 平台对该树永久拒跑。"""
    res = await _create(repo, "A001")
    wt = Path(res["path"])
    gitdir = _gitdir_of(repo, res)
    old_record = ga.read_gitdir_identity(gitdir)
    assert old_record is not None

    # 拆注册 + gitdir（worktree remove 连目录一起删），分支保留 —— 指纹文件
    # 是路径键控的，**不**随 gitdir 消失（A673 毒丸本体）。
    _raw_git(repo, "worktree", "remove", "--force",
             str(wt).replace("\\", "/"))
    assert not Path(gitdir).exists()
    _raw_git(repo, "branch", "--list", "hw/A001/work")
    assert Path(ga._identity_file(gitdir)).exists()

    # 留一个空 husk 目录（生产 reconcile 修复的形态）
    wt.mkdir(parents=True, exist_ok=True)
    ga._IDENTITY_CACHE.clear()

    from hiveweave.services.git_worktree.reconcile import _try_reattach_worktree

    assert await _try_reattach_worktree(str(repo), "A001", str(wt)) is True
    # 收敛断言：新 gitdir 指纹已重录（≠ 旧记录），读侧恢复
    assert ga.read_gitdir_identity(gitdir) is not None
    assert ga.read_gitdir_identity(gitdir) != old_record
    assert ga.verify_gitdir_identity(gitdir) is None
    ok, out = await _git(["status", "--porcelain"], str(wt),
                         project_root=str(repo))
    assert ok, f"重建后平台仍拒跑（A673 未修）：{out}"


async def test_husk_auto_repair_rerecords_identity(repo: Path) -> None:
    """husk repair 路径（service_merge._auto_repair_husk）：add 成功 ⇒ 指纹重录。
    修复前该路径不重录 ⇒ 修完 husk 平台反而对该树永久拒跑。"""
    res = await _create(repo, "A002")
    wt = Path(res["path"])
    branch = res["branch"]
    gitdir = _gitdir_of(repo, res)
    old_record = ga.read_gitdir_identity(gitdir)
    assert old_record is not None

    # 彻底拆掉（目录 + gitdir），同路径留一个无 .git 的 husk
    _force_rmtree(wt)
    _force_rmtree(Path(gitdir))
    wt.mkdir()
    (wt / "husk.txt").write_text("husk\n", encoding="utf-8")
    # 旧指纹残留（毒丸）；缓存清空，逼 verify 走磁盘
    assert Path(ga._identity_file(gitdir)).exists()
    ga._IDENTITY_CACHE.clear()

    err, diag = await GitWorktreeService()._auto_repair_husk(
        str(repo), "A002", str(wt), branch
    )
    assert err is None, (err, diag)
    assert ga.read_gitdir_identity(gitdir) is not None
    assert ga.read_gitdir_identity(gitdir) != old_record
    assert ga.verify_gitdir_identity(gitdir) is None
    ok, out = await _git(["status", "--porcelain"], str(wt),
                         project_root=str(repo))
    assert ok, out


# ── 2. remove→clear 单点：gitdir 消失 ⇒ 旧指纹清除 ────────────────────

async def test_delete_clears_fingerprint_and_same_path_rebuild_reads_ok(
    repo: Path,
) -> None:
    """delete 后 gitdir 确已消失 ⇒ 旧指纹被清（remove→clear 单点）；同路径重建
    ⓪ 走 add→record 单点 ⇒ 新指纹生效，读侧通。"""
    res = await _create(repo, "A005")
    gitdir = _gitdir_of(repo, res)
    assert ga.read_gitdir_identity(gitdir) is not None

    gwt = GitWorktreeService()
    d = await gwt.delete(str(repo), "A005")
    assert d.get("success") and d.get("removed") is True, d
    assert not Path(gitdir).exists()
    assert ga.read_gitdir_identity(gitdir) is None, "gitdir 已消失但旧指纹未清"

    res2 = await gwt.create(str(repo), "A005")
    assert res2.get("success"), res2
    assert ga.verify_gitdir_identity(gitdir) is None
    ok, out = await _git(["status", "--porcelain"], res2["path"],
                         project_root=str(repo))
    assert ok, out


async def test_clear_gitdir_identity_if_gone_semantics(repo: Path) -> None:
    """remove→clear 的判据语义：gitdir **确已消失**才清；gitdir 幸存（husk /
    锁死残留）时不清 —— 指纹仍是该目录身份的真相，贸然清会制造 TOFU 窗口。"""
    res = await _create(repo, "A006")
    gitdir = _gitdir_of(repo, res)
    # gitdir 还在 ⇒ 不清（防 TOFU 窗口）
    assert ga.clear_gitdir_identity_if_gone(str(repo), res["path"]) is False
    assert ga.read_gitdir_identity(gitdir) is not None
    # gitdir 消失 ⇒ 清
    _force_rmtree(Path(gitdir))
    assert ga.clear_gitdir_identity_if_gone(str(repo), res["path"]) is True
    assert ga.read_gitdir_identity(gitdir) is None


# ── 3. 读侧 anchor_refused 单列：不再伪装成 found=false / detached ────

async def test_identity_mismatch_info_reports_anchor_refused(repo: Path) -> None:
    """指纹失配注入 ⇒ info() 返回 anchor_refused 形态，**不是**
    {success: True, status: None}（那会被工具层渲染成 found=false）。"""
    res = await _create(repo, "A003")
    gitdir = _gitdir_of(repo, res)
    _inject_mismatch(gitdir)

    r = await GitWorktreeService().info(str(repo), "A003")
    assert r.get("anchor_refused") is True
    assert r.get("success") is False
    assert r.get("status") is None
    msg = r.get("message") or ""
    assert "[anchor_refused]" in msg
    assert "TOFU" in msg, "拒因未附运维 TOFU 处方"
    # 阳性对照：机制活着 —— 同路径下裸重建后的树仍被拒（不是"永远放行"）
    ok, out = await _git(["status", "--porcelain"], res["path"],
                         project_root=str(repo))
    assert not ok and "FROM_AGENT" not in out


async def test_identity_mismatch_preflight_reports_anchor_refused_not_detached(
    repo: Path,
) -> None:
    """指纹失配注入 ⇒ merge dry-run 报 anchor_refused，**不再**伪装成
    worktree_detached（B5 取证的读侧第二层吞拒因）。"""
    res = await _create(repo, "A004")
    gitdir = _gitdir_of(repo, res)
    _inject_mismatch(gitdir)

    report = await GitWorktreeService().preflight_merge(
        str(repo), "A004", res["branch"]
    )
    codes = [m["code"] for m in report.get("missing", [])]
    assert "anchor_refused" in codes
    assert "worktree_detached" not in codes
    anchor_item = next(m for m in report["missing"]
                       if m["code"] == "anchor_refused")
    assert "[anchor_refused]" in anchor_item["message"]
    assert report.get("success") is False


async def test_identity_mismatch_merge_refused_as_anchor_refused(
    repo: Path,
) -> None:
    """merge() 全链：指纹失配 ⇒ 前置校验单列拒绝（reason=anchor_refused），
    不落入 detached HEAD 文案、也不会继续 checkout/merge。"""
    res = await _create(repo, "A007")
    gitdir = _gitdir_of(repo, res)
    _inject_mismatch(gitdir)

    r = await GitWorktreeService().merge(str(repo), "A007")
    assert r.get("success") is False
    assert r.get("reason") == "anchor_refused"
    assert r.get("anchor_refused") is True
    # 不是 detached 伪装：稳定 code 不出现（文案里"这不是 detached"是澄清，
    # 判据看 code token 而不是子串）
    assert "worktree_detached" not in (r.get("message") or "")
    # main 未被 checkout/merge 触碰（拒绝发生在前置校验）
    ok_br, br = await _git(["rev-parse", "--abbrev-ref", "HEAD"],
                           str(repo), project_root=str(repo))
    assert ok_br and br.strip() == "main", br


# ── 4. 防洗白：幂等路径不重录篡改现场 ─────────────────────────────────

async def test_create_idempotent_path_does_not_launder_tampered_identity(
    repo: Path,
) -> None:
    """树已存在（幂等路径、无 add）时 create **不**重录指纹 —— 否则同路径被
    替换的假仓会被 TOFU 洗白（旧代码无条件 record 的缺陷）。失配保持失配，
    恢复只能走运维 TOFU 逃生门。"""
    res = await _create(repo, "A008")
    gitdir = _gitdir_of(repo, res)
    _inject_mismatch(gitdir)

    r2 = await GitWorktreeService().create(str(repo), "A008")
    assert r2.get("success"), r2  # 幂等路径照常返回
    assert ga.read_gitdir_identity(gitdir) == (999999, 999999), \
        "幂等路径重录了篡改现场（TOFU 洗白）"
    assert ga.verify_gitdir_identity(gitdir) is not None


# ── 5. 正常路径零回归（本文件内的基线） ───────────────────────────────

async def test_untampered_roundtrip_reads_ok(repo: Path) -> None:
    """无失配基线：create → info 出状态 → preflight 无 anchor_refused。"""
    res = await _create(repo, "A009")
    gwt = GitWorktreeService()
    r = await gwt.info(str(repo), "A009")
    assert r.get("success") is True and isinstance(r.get("status"), dict)
    assert r["status"]["branch"] == res["branch"]
    report = await gwt.preflight_merge(str(repo), "A009", res["branch"])
    codes = [m["code"] for m in report.get("missing", [])]
    assert "anchor_refused" not in codes
    assert "anchor_refused" not in (report.get("message") or "")
