"""🔁#4 回归：worktree 目录别名（relocation 后缀）→ porcelain 绑定分支。

现象（真实项目 3 次，21:11:34 ×2 / 23:59:39）：
``No worktree branch found matching 'A461-b'``（`A463-b` 同）。
而 `git worktree list --porcelain` 早就发布了绑定
``worktree …/A461-b`` ↔ ``branch refs/heads/hw/A461/work``；
旧代码却把别名 `_slugify` 后拼 `hw/<caller>/<slug>`、再 glob
`hw/*/<slug>` —— 对 `A461-b` 必然 0 命中。

修法（本文件钉住，全走真实代码路径 + 真实临时 git 仓）：
1. **权威源优先**：porcelain 绑定（worktree 目录基名 / 分支任务段）→ 取绑定分支；
2. **回退有界**：porcelain 无绑定时，至多剥**一层** relocation 后缀
   （`_RELOCATION_SUFFIXES` 常量驱动）→ 短号 `hw/<sid>/*`；
3. 删除 `_slugify` 猜-再-glob 链。

阳性对照（证明是"修正解析"而非"放宽拒合"）：
- 真不存在的分支**仍被拒**，且错误里**仍有候选分支清单**；
- 多匹配**仍报歧义**并列出候选。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.tools.misc_tools import (
    GitWorktreeMergeParams,
    git_worktree_merge_tool,
)


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return res.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@hiveweave.local")
    _git(repo, "config", "user.name", "HiveWeave Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, dir_name: str, branch: str, filename: str) -> Path:
    """真 `git worktree add`：目录基名（别名）与绑定分支由 git 真实发布。

    ``dir_name`` 是 worktree 目录名（可能是带 relocation 后缀的别名，如
    ``A461-b``），``branch`` 是它检出的真实分支（如 ``hw/A461/work``）。
    返回 worktree 路径。
    """
    wt = repo / ".hiveweave" / "worktrees" / dir_name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(wt))
    (wt / filename).write_text(f"print('from {branch}')\n", encoding="utf-8")
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", f"add {filename}")
    return wt


async def _call_tool(repo: Path, caller: str, branch_name: str):
    params = GitWorktreeMergeParams(branchName=branch_name)
    # patch 上下文必须覆盖 await 执行期（与既有 merge 解析测试同款）
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(repo), caller, "proj-x")),
    ), patch(
        "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
        new=AsyncMock(return_value=0),
    ):
        return await git_worktree_merge_tool(params, "agent-x", str(repo))


def _hw_branches(repo: Path) -> str:
    return _git(repo, "branch", "--list", "hw/*/*")


def _merge_branch_and_cleanup(repo: Path, branch: str, filename: str) -> None:
    """造一条**真实** merge commit 后删掉分支（模拟「已合入并清理」的尾声）。

    分支形态一律取**平台真实产生**的名字（`compute_branch_name`：无 task_id
    时 `hw/<sid>/work`，有 task_id 时 `hw/<sid>/t-<8hex>`），merge 的 subject
    由 **git 自己生成**（``Merge branch 'hw/A500/work'``）—— 不手写 `-m`，
    这样测的就是真实历史形态。
    """
    _git(repo, "checkout", "-b", branch, "main")
    (repo / filename).write_text(f"print('{branch}')\n", encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", f"work on {branch}")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", branch)  # git 生成真实 merge subject
    _git(repo, "branch", "-d", branch)


def _force_cleanup_worktree(repo: Path, dir_name: str, branch: str) -> None:
    """尽力把 worktree 与分支清干净（平台可能已清掉一部分，故全部容忍失败）。

    顺序有讲究：先 `worktree remove --force` + `prune` 解除注册，再
    `branch -d` —— 否则分支仍被 worktree 占用，`branch -d` 会被 git 拒绝
    （实测 `git_worktree.branch_preserved ... used by worktree`）。
    """
    wt = repo / ".hiveweave" / "worktrees" / dir_name
    for args in (
        ("worktree", "remove", "--force", str(wt)),
        ("worktree", "prune"),
        ("branch", "-d", branch),
    ):
        subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True
        )


# ── 1. 别名 → 绑定分支（权威源）──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias_dir,real_branch",
    [("A461-b", "hw/A461/work"), ("A463-b", "hw/A463/work")],
)
async def test_worktree_dir_alias_resolves_to_bound_branch(
    git_repo: Path, alias_dir: str, real_branch: str
) -> None:
    """`A461-b` / `A463-b`（目录别名）必须解析到 porcelain 绑定的真实分支。"""
    filename = f"{alias_dir.split('-')[0].lower()}.py"
    _add_worktree(git_repo, alias_dir, real_branch, filename)

    # fixture 真实性：porcelain 确实发布「目录别名 ↔ 真实分支」绑定
    porcelain = _git(git_repo, "worktree", "list", "--porcelain")
    assert f"worktree" in porcelain and alias_dir in porcelain
    assert f"branch refs/heads/{real_branch}" in porcelain

    result = await _call_tool(git_repo, "A003", alias_dir)

    assert result.success is True, result.error
    assert (git_repo / filename).exists(), "绑定分支的文件必须已合入 main"
    # 回执必须指向**绑定分支**（别名本身不是分支名）
    assert real_branch in (result.output or ""), result.output


@pytest.mark.asyncio
async def test_alias_takes_bound_branch_not_short_id_first_match(
    git_repo: Path,
) -> None:
    """别名必须取 **porcelain 绑定**，而不是短号枚举的第一个（结构性差异）。

    `A461-b` 绑定 `hw/A461/work`；同时存在**未注册**的诱饵分支
    `hw/A461/aaa`（字母序在 `work` 之前）。若解析退回按短号
    `hw/A461/*` 取第一个，会合错成 `aaa` —— 本用例断言合入的是绑定分支。
    这就是「权威源优先于猜」的判别式。
    """
    wt = _add_worktree(git_repo, "A461-b", "hw/A461/work", "bound.py")
    # 诱饵：同短号、字母序更靠前的未注册分支
    _git(wt, "checkout", "-b", "hw/A461/aaa")
    (wt / "decoy.py").write_text("print('decoy')\n", encoding="utf-8")
    _git(wt, "add", "decoy.py")
    _git(wt, "commit", "-m", "decoy")
    _git(wt, "checkout", "hw/A461/work")

    result = await _call_tool(git_repo, "A003", "A461-b")

    assert result.success is True, result.error
    assert (git_repo / "bound.py").exists(), "必须合入绑定分支 hw/A461/work"
    assert not (git_repo / "decoy.py").exists(), (
        "不得退化成按短号枚举取第一个（会合错成诱饵 hw/A461/aaa）"
    )


# ── 2. 阳性对照①：不存在的分支仍被拒，且带候选清单 ──────────


@pytest.mark.asyncio
async def test_nonexistent_branch_still_rejected_with_candidate_listing(
    git_repo: Path,
) -> None:
    """阳性对照：真不存在的分支**仍拒绝**，错误仍含可用分支清单。

    这条证明本修复是「修正解析」而不是「放宽拒合」：解析不到仍 fail loud。
    """
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")

    result = await _call_tool(git_repo, "A003", "no-such-branch-b")

    assert result.success is False
    err = result.error or ""
    assert "No worktree branch found matching" in err, err
    assert "Available worktree branches" in err, err
    assert "hw/A461/work" in err, "候选清单必须列出真实分支"


# ── 3. 阳性对照②：多匹配仍报歧义 ─────────────────────────────


@pytest.mark.asyncio
async def test_multi_match_still_errors_as_ambiguous(git_repo: Path) -> None:
    """阳性对照：同任务段命中 >1 个分支 → 仍报歧义并列出候选。"""
    _add_worktree(git_repo, "A004", "hw/A004/feat-x", "a.py")
    _add_worktree(git_repo, "A005", "hw/A005/feat-x", "b.py")

    result = await _call_tool(git_repo, "A003", "feat-x")

    assert result.success is False
    err = result.error or ""
    assert "Ambiguous" in err, err
    assert "hw/A004/feat-x" in err and "hw/A005/feat-x" in err, err


# ── 4. 回退：porcelain 无绑定 → 至多剥一层后缀 → 短号查找 ─────


@pytest.mark.asyncio
async def test_alias_falls_back_to_short_id_when_worktree_unregistered(
    git_repo: Path,
) -> None:
    """worktree 已拆除（porcelain 无绑定）但分支还在 → 别名仍可解析。

    回退路径：`A463-b` →（剥一层 `-b`）→ 短号 `A463` → `hw/A463/*`。
    后缀清单来自 `_RELOCATION_SUFFIXES` 常量，不手写枚举。
    """
    wt = _add_worktree(git_repo, "A463-b", "hw/A463/work", "a463.py")
    _git(git_repo, "worktree", "remove", "--force", str(wt))

    # 前提确认：porcelain 里已无该 worktree，但分支仍在
    porcelain = _git(git_repo, "worktree", "list", "--porcelain")
    assert "A463-b" not in porcelain
    assert "hw/A463/work" in _hw_branches(git_repo)

    result = await _call_tool(git_repo, "A003", "A463-b")

    assert result.success is True, result.error
    assert (git_repo / "a463.py").exists()


# ── 5. 调用者自有分支仍优先（保留既有行为）────────────────────


@pytest.mark.asyncio
async def test_caller_own_branch_still_takes_precedence(git_repo: Path) -> None:
    """`feat-x` 同时命中调用者(A003)与 A004 → 调用者自己的分支优先。"""
    _add_worktree(git_repo, "A003", "hw/A003/feat-x", "own.py")
    _add_worktree(git_repo, "A004", "hw/A004/feat-x", "foreign.py")

    approved_task = {
        "id": "t-own-1",
        "status": "approved",
        "assignee_id": "agent-x",
        "evidence": {"reviewed_by": "ceo-1"},
    }
    from hiveweave.services.task import TaskService

    params = GitWorktreeMergeParams(branchName="feat-x")
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(git_repo), "A003", "proj-x")),
    ), patch(
        "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
        new=AsyncMock(return_value=0),
    ), patch.object(
        TaskService, "list_tasks", AsyncMock(return_value=[approved_task])
    ):
        result = await git_worktree_merge_tool(
            params, "agent-x", str(git_repo)
        )

    assert result.success is True, result.error
    assert (git_repo / "own.py").exists(), "调用者自有分支应优先合并"
    assert not (git_repo / "foreign.py").exists()


# ── 6. F1 阳性对照（**别名形状**）＋ 判别式对照 ────────────────


@pytest.mark.asyncio
async def test_nonexistent_ALIAS_still_rejected_with_candidate_listing(
    git_repo: Path,
) -> None:
    """F1 阳性对照（**别名形状**）：`Z999-b` 不存在 ⇒ 仍拒绝 + 候选清单。

    未修前：`git log -1 --grep=…` 在**无匹配**时仍 rc=0 且输出为空，被
    `_already` 当成真 ⇒ 别名形态也假成功 `already_merged` —— 恰好违反本次
    验收判据「真不存在的分支仍拒」。旧行只污染裸短号，是本次改动把它扩散
    到别名形状。
    """
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")

    result = await _call_tool(git_repo, "A003", "Z999-b")

    assert result.success is False, (
        f"不存在的别名必须被拒，实际 success/output = "
        f"{result.success}/{result.output!r}"
    )
    assert "already_merged" not in (result.output or ""), result.output
    err = result.error or ""
    assert "No worktree branch found" in err, err
    assert "Available worktree branches" in err, err
    assert "hw/A461/work" in err, err


@pytest.mark.asyncio
async def test_nonexistent_bare_short_id_still_rejected(git_repo: Path) -> None:
    """F1：裸短号形态同样必须被拒（旧病原位）。"""
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")

    result = await _call_tool(git_repo, "A003", "Z999")

    assert result.success is False, (
        f"不存在的短号必须被拒，实际 = {result.output or result.error}"
    )
    assert "already_merged" not in (result.output or ""), result.output


@pytest.mark.asyncio
async def test_already_merged_requires_a_real_merge_commit(git_repo: Path) -> None:
    """F1/A 判别式对照：「无匹配」与「真已合并」必须可区分。

    N6：分支形态必须是**平台真实产生**的 `hw/<sid>/work`（`compute_branch_name`
    在无 task_id 时的形态）。旧用例造的是 `hw/A500`（两段）—— 平台**从不**
    产生这种名字，因此它测的是一个假前提，恰好掩盖了 A：短号查找拼的是
    `_candidate = hw/<sid>`，而真实分支/真实 merge subject 都是
    `hw/<sid>/<name>` ⇒ 短号重入的幂等分支是死路。

    造一条**真实** merge commit（git 生成 subject，随后分支被清理）→ 短号
    `A500` 与全名 `hw/A500/work` 都允许 `already_merged`；同形状但**无**该
    merge commit 的 `Z999` → 必须**拒绝**。判据 = 确实解析出一条 merge
    commit，而不是 rc。
    """
    _merge_branch_and_cleanup(git_repo, "hw/A500/work", "a500.py")
    assert "hw/A500/work" not in _hw_branches(git_repo)  # ref 已清理

    by_short = await _call_tool(git_repo, "A003", "A500")
    by_full = await _call_tool(git_repo, "A003", "hw/A500/work")
    ghost = await _call_tool(git_repo, "A003", "Z999")

    assert by_short.success is True and "already_merged" in (by_short.output or ""), (
        f"短号重入必须幂等成功（A），实际 = {by_short.output or by_short.error}"
    )
    assert by_full.success is True and "already_merged" in (by_full.output or ""), (
        f"全名重入必须幂等成功，实际 = {by_full.output or by_full.error}"
    )
    assert ghost.success is False, (
        f"无对应 merge commit 的必须被拒，实际 = {ghost.output or ghost.error}"
    )
    assert "already_merged" not in (ghost.output or ""), ghost.output


def test_platform_branch_shape_is_always_three_segment() -> None:
    """N6 结构性前提：平台分支**恒**为 `hw/<sid>/<name>` 三段。

    `compute_branch_name` 只有两种产物 —— 无 task_id `hw/<sid>/work`、有
    task_id `hw/<sid>/t-<8hex>`；**从不**产生两段的 `hw/<sid>`。旧用例造
    `hw/A500` 就是造了一个平台不会出现的形状，使「短号重入幂等」的对照组
    测到假前提。此结构性前提一旦被改坏，本文件所有形态断言都失去意义。
    """
    from hiveweave.services.git_worktree.naming import compute_branch_name

    for sid in ("A500", "A461"):
        for tid in (None, "deadbeef", "0a1b2c3d"):
            name = compute_branch_name(sid, tid)
            assert name.split("/")[0] == "hw"
            assert name.count("/") == 2, name  # 三段 —— 不是 hw/<sid>
            assert name.split("/")[1] == sid
            assert name.split("/")[2].startswith(("work", "t-"))


# ── 7. F2 对照组：主 worktree / main 永不成为合并目标 ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["repo", "main"])
async def test_main_worktree_target_is_rejected_and_no_unscoped_settle(
    git_repo: Path, bad: str
) -> None:
    """F2：传主 worktree 基名（仓目录名）或 `main` ⇒ 必须拒绝。

    且解析失败时**不得**触达义务结算（否则 `merged_short` 为空会走到
    `fulfill_by_owner(short_id=None)` 无范围兜底，可能清掉调用者名下全部
    pending merge 义务）。
    """
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")

    settle = AsyncMock()
    params = GitWorktreeMergeParams(branchName=bad)
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(git_repo), "A003", "proj-x")),
    ), patch(
        "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
        new=AsyncMock(return_value=0),
    ), patch(
        "hiveweave.tools.misc_tools._settle_merge_obligations_after_merge",
        settle,
    ):
        result = await git_worktree_merge_tool(
            params, "agent-x", str(git_repo)
        )

    assert result.success is False, (
        f"{bad!r} 不得成为合并目标，实际 = {result.output or result.error}"
    )
    assert "already_merged" not in (result.output or ""), result.output
    settle.assert_not_awaited()


# ── 8. F3 回归：任务名走 `_slugify` 归一化（旧 glob 的合法能力）──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_name,real_branch",
    [("feat x", "hw/A004/feat-x"), ("前端 页面", "hw/A004/前端-页面")],
)
async def test_task_name_normalised_by_slugify_resolves(
    git_repo: Path, task_name: str, real_branch: str
) -> None:
    """F3：任务名比较必须过 `_slugify`（空格/斜杠→`-`），与旧行为对齐。

    旧 glob 路径过 `_slugify` ⇒ `"feat x"` 命中 `hw/A004/feat-x`；
    本改动只做**字面**尾段比较，静默丢了这条能力。
    """
    _add_worktree(git_repo, "A004", real_branch, "feat.py")

    result = await _call_tool(git_repo, "A003", task_name)

    assert result.success is True, result.error
    assert (git_repo / "feat.py").exists()
    assert real_branch in (result.output or ""), result.output


# ── 9. F4 对照组：别名后缀回退不得静默改选别的分支 ───────────


@pytest.mark.asyncio
async def test_alias_suffix_strip_does_not_silently_pick_other_branch(
    git_repo: Path,
) -> None:
    """F4：只有目录 `A461`（绑定 `hw/A461/decoy`），传 `A461-b`。

    别名自身 worktree 缺失时**不得**静默把同 short id 下的**别的**分支当
    目标 → 必须报候选/拒绝，绝不合并 decoy。
    """
    _add_worktree(git_repo, "A461", "hw/A461/decoy", "decoy.py")

    result = await _call_tool(git_repo, "A003", "A461-b")

    assert not (git_repo / "decoy.py").exists(), (
        "不得静默改选 hw/A461/decoy —— 别名 A461-b 指向的不是这条分支"
    )
    assert result.success is False, (
        f"应报候选/拒绝，实际 = {result.output or result.error}"
    )


# ── 10. F5：未注册 worktree 的错误必须给恢复 route ─────────────


@pytest.mark.asyncio
async def test_unregistered_name_error_has_recovery_route(git_repo: Path) -> None:
    """F5：查不到绑定的错误文本必须给出恢复 route（改传全名/短号）。"""
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")

    result = await _call_tool(git_repo, "A003", "ghost-name")

    assert result.success is False
    err = result.error or ""
    assert "Available worktree branches" in err, err
    assert "hw/A461/work" in err, err
    low = err.lower()
    assert "pass the full branch name" in low or "hw/<shortid>/<name>" in low, (
        f"错误必须给恢复 route，实际 = {err!r}"
    )


# ── 11. F6：三面描述必须同时宣告三种形态 ──────────────────────


def test_branchName_descriptions_cover_all_three_forms() -> None:
    """F6：pydantic / @tool / 手写 `TOOL_PARAM_SCHEMAS` 三面语义一致。

    三种可接受形态都要写明：完整分支名 `hw/<shortId>/<name>` /
    worktree 目录别名（`A461-b`）/ 任务名。
    """
    import hiveweave.tools  # noqa: F401 — populate @tool registry

    from hiveweave.tools.base import get_tool_def
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS
    from hiveweave.tools.misc_tools import GitWorktreeMergeParams

    surfaces = {
        "pydantic": GitWorktreeMergeParams.model_fields["branch_name"].description,
        "@tool": get_tool_def("git_worktree_merge").description,
        "hand-written-schema": TOOL_PARAM_SCHEMAS["git_worktree_merge"][
            "properties"
        ]["branchName"]["description"],
    }
    for name, desc in surfaces.items():
        low = (desc or "").lower()
        assert "hw/<shortid>/<name>" in low, (name, desc)
        assert "a461-b" in low, (name, desc)
        assert "task name" in low or "taskname" in low, (name, desc)


def test_worktree_remove_branchName_semantics_untouched() -> None:
    """F6 反向守卫：`git_worktree_remove` 的 branchName 语义不同（task 名），
    不得被顺手统一成 merge 的三形态文案。"""
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    desc = TOOL_PARAM_SCHEMAS["git_worktree_remove"]["properties"]["branchName"]
    assert "description" not in desc or "hw/<shortId>/<name>" not in (
        desc.get("description") or ""
    )


# ── 12. N1：`hw/` 透传必须同样拒绝「查无此分支」且不得结算义务 ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "full_name", ["hw/A462", "hw/Z999/work", "hw/Z999/anything"]
)
async def test_nonexistent_full_name_rejected_and_no_settle(
    git_repo: Path, full_name: str
) -> None:
    """N1（P1）：`hw/` 全名透传绕过了 `_resolve_short_id_merge`，直达
    `service_merge.merge_by_branch`；该处 else 支此前**只判 rc**（`git log
    --grep` 无匹配也是 rc=0、输出空）⇒ 任意不存在的 `hw/...` 假成功
    `outcome=already_merged`，并**继续走义务结算**（审计实测
    `settle_awaits=1`：假成功还清了 merge 义务）。

    判据：既不存在的全名被拒，且 `_settle_merge_obligations_after_merge`
    **从未被 await**。
    """
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")

    settle = AsyncMock()
    params = GitWorktreeMergeParams(branchName=full_name)
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(git_repo), "A003", "proj-x")),
    ), patch(
        "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
        new=AsyncMock(return_value=0),
    ), patch(
        "hiveweave.tools.misc_tools._settle_merge_obligations_after_merge",
        settle,
    ):
        result = await git_worktree_merge_tool(
            params, "agent-x", str(git_repo)
        )

    assert result.success is False, (
        f"不存在的全名 {full_name!r} 必须被拒，实际 = "
        f"{result.output or result.error}"
    )
    assert "already_merged" not in (result.output or ""), result.output
    settle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("degenerate", ["hw/", "hw//"])
async def test_degenerate_hw_prefix_rejected_and_no_unscoped_settle(
    git_repo: Path, degenerate: str
) -> None:
    """N2 收口：退化形态 `hw/` / `hw//` 必须被拒。

    这两种形状 `short_id` 解析为 None 且仍带 `hw/` 前缀 ⇒ 会一路走到 service
    的假 already_merged，再进义务结算的 `fulfill_by_owner(short_id=None)`
    无范围兜底（可能清掉调用者名下**全部** pending merge 义务）。N1 的修法
    必须同时堵住这条：拒绝 + 不结算。
    """
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")

    settle = AsyncMock()
    params = GitWorktreeMergeParams(branchName=degenerate)
    with patch(
        "hiveweave.tools.misc_tools._get_worktree_context",
        new=AsyncMock(return_value=(str(git_repo), "A003", "proj-x")),
    ), patch(
        "hiveweave.tools.task_tools.nudge_verify_tasks_after_merge",
        new=AsyncMock(return_value=0),
    ), patch(
        "hiveweave.tools.misc_tools._settle_merge_obligations_after_merge",
        settle,
    ):
        result = await git_worktree_merge_tool(
            params, "agent-x", str(git_repo)
        )

    assert result.success is False, (
        f"退化形态 {degenerate!r} 必须被拒，实际 = "
        f"{result.output or result.error}"
    )
    assert "already_merged" not in (result.output or ""), result.output
    settle.assert_not_awaited()


# ── 13. A：真实生命周期的幂等重入 ────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sid,real_branch",
    [("A500", "hw/A500/work"), ("A501", "hw/A501/t-deadbeef")],
)
async def test_real_lifecycle_short_id_reentry_is_idempotent(
    git_repo: Path, sid: str, real_branch: str
) -> None:
    """A：真实生命周期——merge → 清理 worktree → 删分支 → 再传短号。

    两种**平台真实形态**都要覆盖（`compute_branch_name`）：无 task_id 的
    `hw/<sid>/work` 与有 task_id 的 `hw/<sid>/t-<8hex>`。`git merge --no-ff`
    生成的 subject 分别是 ``Merge branch 'hw/A500/work'`` /
    ``Merge branch 'hw/A501/t-deadbeef'``。旧 `_candidate = hw/<sid>` 既不是
    真实分支、也不是真实 subject 的（带引号）前缀 ⇒ 短号重入黑盒失败
    （`success=False`），而全名却成功 —— 同一事件两个相反结论。

    注意：清理由**平台自己**在 merge 成功时完成（`git_worktree.delete`），
    测试只确定性补齐并**断言**已进入「ref 已删 + 无 worktree 绑定」态。
    """
    filename = f"{sid.lower()}.py"
    _add_worktree(git_repo, sid, real_branch, filename)
    first = await _call_tool(git_repo, "A003", sid)
    assert first.success is True, first.error
    assert (git_repo / filename).exists(), "首次合并必须落盘"

    _force_cleanup_worktree(git_repo, sid, real_branch)
    assert real_branch not in _hw_branches(git_repo), (
        "重入前分支必须已消失（F13b 尾声）"
    )
    assert sid not in _git(git_repo, "worktree", "list", "--porcelain")

    reentry = await _call_tool(git_repo, "A003", sid)

    assert reentry.success is True, (
        f"短号重入必须幂等成功，实际 = {reentry.output or reentry.error}"
    )
    assert "already_merged" in (reentry.output or ""), reentry.output


@pytest.mark.asyncio
async def test_real_lifecycle_alias_reentry_is_idempotent(git_repo: Path) -> None:
    """A：真实生命周期——别名形态重入也必须幂等成功。

    `A461-b` 的树与分支都在首个 merge 后消失；再传别名 `A461-b` 时 porcelain
    已无绑定 ⇒ 剥一层后缀 → 短号 `A461` → 必须靠真实 merge 历史给出
    `already_merged`，而不是报「查无分支」。
    """
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")
    first = await _call_tool(git_repo, "A003", "A461-b")
    assert first.success is True, first.error
    _force_cleanup_worktree(git_repo, "A461-b", "hw/A461/work")
    assert "hw/A461/work" not in _hw_branches(git_repo)
    assert "A461-b" not in _git(git_repo, "worktree", "list", "--porcelain")

    reentry = await _call_tool(git_repo, "A003", "A461-b")

    assert reentry.success is True, (
        f"别名重入必须幂等成功，实际 = {reentry.output or reentry.error}"
    )
    assert "already_merged" in (reentry.output or ""), reentry.output


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["Z999-b", "hw/A462"])
async def test_real_lifecycle_ghost_shapes_still_rejected(
    git_repo: Path, bad: str
) -> None:
    """A/N1 反向对照：真实历史不存在的别名形状与全名形状**仍拒**。

    这条与上面的幂等用例配成判别式：证明 `already_merged` 来自**真实解析出
    的 merge 行**，而不是「任何输入都成功」。
    """
    _merge_branch_and_cleanup(git_repo, "hw/A500/work", "a500.py")
    _add_worktree(git_repo, "A461-b", "hw/A461/work", "a461.py")

    result = await _call_tool(git_repo, "A003", bad)

    assert result.success is False, (
        f"{bad!r} 无对应 merge 历史，必须被拒，实际 = "
        f"{result.output or result.error}"
    )
    assert "already_merged" not in (result.output or ""), result.output


# ── 14. N3：别名树缺失 + 同短号诱饵树 ⇒ 别名拒、全名可恢复 ──────


@pytest.mark.asyncio
async def test_alias_tree_removed_with_decoy_recovers_via_full_name(
    git_repo: Path,
) -> None:
    """N3：`A461-b` 的树被拆除、同短号另有一棵诱饵树 `A461`（绑定
    `hw/A461/decoy`）时：

    - 名字 `A461-b` **必须被拒**（F4：不得静默改选别的树的分支）；
    - **全名** `hw/A461/work` 必须仍能成功合并 —— 恢复 route 有效，
      调用者不会被永久卡死。
    """
    wt = _add_worktree(git_repo, "A461-b", "hw/A461/work", "work.py")
    _git(git_repo, "worktree", "remove", "--force", str(wt))
    _add_worktree(git_repo, "A461", "hw/A461/decoy", "decoy.py")

    porcelain = _git(git_repo, "worktree", "list", "--porcelain")
    assert "A461-b" not in porcelain
    assert "hw/A461/decoy" in porcelain
    branches = _hw_branches(git_repo)
    assert "hw/A461/work" in branches and "hw/A461/decoy" in branches

    by_alias = await _call_tool(git_repo, "A003", "A461-b")
    assert by_alias.success is False, (
        f"别名 A461-b 自身的树已拆除、同短号是别的树 ⇒ 必须拒绝，实际 = "
        f"{by_alias.output or by_alias.error}"
    )
    assert not (git_repo / "decoy.py").exists(), "不得合并诱饵分支"

    by_full = await _call_tool(git_repo, "A003", "hw/A461/work")
    assert by_full.success is True, (
        f"全名恢复 route 必须有效，实际 = {by_full.output or by_full.error}"
    )
    assert (git_repo / "work.py").exists(), "全名必须合入 hw/A461/work"

