"""② `.hiveweave/shared/**` 与 `reports/**` 走**同一份**跨树读实现。

背景（09-16 侦察 + 本文件落地）：
- `reports/**` 有跨树读（#5 / 09-12），`shared/**` **没有** —— 只有一句手写
  提示，且 `READ_MISS_HINT` 一份字符串同供 read_file / list_files、内容却
  只讲 reports。
- 但**不能照抄 reports 的候选序**：顺序由各子目录的**合并策略**推出
  （`service_create.py:239-245` 生成的 `.gitattributes`）：
  `shared/**/*.md` = `merge=binary`（双方改动即冲突 ⇒ **无单一权威落点**）
  ⇒ 本树优先；`reports/` = 默认文本合并、预期多方写 ⇒ MAIN 是权威落点。
  ⇒ 形态 = 同一实现 + 候选序按子目录参数化 + 文案按前缀分派。

本文件同时守卫三处既有漂移：
1. `tools/file.py::_reports_read_scope` 与 `services/vision.py::_multi_tree_bases`
   **在"请求者树不是排序第一个兄弟"时给出不同顺序**（旧 vision 把兄弟树全排在
   请求者树之前）—— 现由 `util/tree_scope.ordered_tree_roots` 唯一权威给出。
2. `list_files` 里第三份手写判据（自己按 `_rel` 段匹配拼 shared 文案）。
3. `_reports_evidence_hint` 用 `Path(base).parent.parent` 上溯 —— 只对
   "`<subdir>/<key>/<file>` 恰好两层"成立，深一层就算错目录。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hiveweave.services.vision import _multi_tree_bases, resolve_screenshot_path_multi_tree
from hiveweave.tools.file import (
    _platform_shared_read_subdir,
    _reports_evidence_hint,
    _reports_read_scope,
    _shared_evidence_hint,
    _shared_read_scope,
    list_files,
    read_file,
)
from hiveweave.util.tree_label import READ_MISS_HINT, tree_tag
from hiveweave.util.tree_scope import (
    cross_tree_read_enabled,
    local_first_for,
    miss_hint_for,
    ordered_tree_roots,
)


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, Path]:
    """project(MAIN) + 两棵 worktree；shared/reports 各有一份在 MAIN、一份在兄弟树。"""
    project = tmp_path / "project"
    wt_a = project / ".hiveweave" / "worktrees" / "A044"
    wt_b = project / ".hiveweave" / "worktrees" / "A045"
    wt_a.mkdir(parents=True)
    wt_b.mkdir(parents=True)
    main_shared = project / ".hiveweave" / "shared"
    main_shared.mkdir(parents=True)
    (main_shared / "contract.md").write_text("MAIN-OLD-CONTRACT", encoding="utf-8")
    sib_shared = wt_b / ".hiveweave" / "shared"
    sib_shared.mkdir(parents=True)
    (sib_shared / "peer-only.md").write_text("PEER-ONLY", encoding="utf-8")
    main_rep = project / ".hiveweave" / "reports" / "T123"
    main_rep.mkdir(parents=True)
    (main_rep / "shot.png").write_text("MAIN-EVIDENCE", encoding="utf-8")
    return {"project": project, "wt_a": wt_a, "wt_b": wt_b}


# ── ① 候选序：按子目录参数化，且请求者树排在兄弟树之前 ────────────────


def test_order_is_parameterized_by_subdir_strategy(trees: dict[str, Path]) -> None:
    """shared = 本树优先；reports = MAIN 优先（理由 = 各自合并策略）。

    ⚠ **期望值是写死的**（独立判据），不是"和另一个消费者比一比"——
    两处共用一个函数之后，互比是**恒真**的（同源判据），证明不了顺序对不对。
    """
    shared_tags = [
        tag
        for tag, _ in _shared_read_scope(
            "shared", ".hiveweave/shared/x.md", str(trees["project"]), str(trees["wt_a"])
        )
    ]
    reports_tags = [
        tag
        for tag, _ in _shared_read_scope(
            "reports", ".hiveweave/reports/T123/x.png",
            str(trees["project"]), str(trees["wt_a"]),
        )
    ]
    assert shared_tags == ["worktree A044", "MAIN", "worktree A045"]      # 本树优先
    assert reports_tags == ["MAIN", "worktree A044", "worktree A045"]     # MAIN 优先
    assert set(shared_tags) == set(reports_tags)   # 候选**集合**相同，只有顺序不同


def test_requester_tree_precedes_siblings_for_both_consumers(
    trees: dict[str, Path],
) -> None:
    """判别性用例：请求者树排**最后**（A999）时，旧实现会与 file 侧分家。

    旧 `vision._multi_tree_bases` 的顺序是 MAIN → 兄弟(排序) → 本树 ⇒
    请求者树落在兄弟树之后；而 `_reports_read_scope` 是 MAIN → 本树 → 兄弟。
    用 workspace=A999（排序在 A044/A045 之后）就能把两者分开。
    """
    wt_late = trees["project"] / ".hiveweave" / "worktrees" / "A999"
    wt_late.mkdir(parents=True)
    file_tags = [
        tag
        for tag, _ in _reports_read_scope(
            ".hiveweave/reports/T123/x.png", str(trees["project"]), str(wt_late)
        )
    ]
    vision_tags = [
        tree_tag(b) for b in _multi_tree_bases(str(wt_late), str(trees["project"]))
    ]
    assert file_tags == vision_tags, (file_tags, vision_tags)
    # 期望顺序写死（独立判据）——两处互比是同源判据、恒真，不能单独当验收。
    assert file_tags == ["MAIN", "worktree A999", "worktree A044", "worktree A045"]


def test_drafts_and_handoffs_are_not_cross_tree() -> None:
    """范围守卫：drafts/handoffs 是 individual（prompt 明写），刻意不开跨树读。"""
    assert cross_tree_read_enabled("shared") is True
    assert cross_tree_read_enabled("reports") is True
    assert cross_tree_read_enabled("drafts") is False
    assert cross_tree_read_enabled("handoffs") is False
    assert local_first_for("shared") is True
    assert local_first_for("reports") is False


def test_ordered_tree_roots_single_tree_dedup(tmp_path: Path) -> None:
    """workspace == root（CEO/协调者）时不得重复出候选。"""
    p = tmp_path / "proj"
    p.mkdir()
    assert ordered_tree_roots(str(p), str(p), local_first=True) == [str(p)]
    assert ordered_tree_roots(str(p), str(p), local_first=False) == [str(p)]


# ── ② shared 读侧真的跨树（且本树优先 = 不读到 MAIN 旧版）────────────


@pytest.mark.asyncio
async def test_shared_local_newer_beats_main_older(trees: dict[str, Path]) -> None:
    """⭐ 本树有新版 + MAIN 有旧版 ⇒ 必须读到**本树**（local_first 的核心证据）。

    顺序写反（照抄 reports 的 MAIN 优先）时这条转红：会读到 MAIN-OLD-CONTRACT。
    """
    own = trees["wt_a"] / ".hiveweave" / "shared"
    own.mkdir(parents=True)
    (own / "contract.md").write_text("LOCAL-NEW-CONTRACT", encoding="utf-8")
    result = await read_file(
        file_path=".hiveweave/shared/contract.md",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert "LOCAL-NEW-CONTRACT" in result["output"]
    assert "MAIN-OLD-CONTRACT" not in result["output"]
    assert "[read from " not in result["output"]  # 本树命中不加跨树噪音


@pytest.mark.asyncio
async def test_shared_hits_main_when_local_missing(trees: dict[str, Path]) -> None:
    """本树没有（worktree 里空目录不物化）⇒ 命中 MAIN 并点名。"""
    result = await read_file(
        file_path=".hiveweave/shared/contract.md",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert "MAIN-OLD-CONTRACT" in result["output"]
    assert "[read from MAIN" in result["output"]


@pytest.mark.asyncio
async def test_shared_hits_sibling_worktree(trees: dict[str, Path]) -> None:
    result = await read_file(
        file_path=".hiveweave/shared/peer-only.md",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert "PEER-ONLY" in result["output"]
    assert "[read from worktree A045" in result["output"]


@pytest.mark.asyncio
async def test_drafts_do_not_leak_across_trees(trees: dict[str, Path]) -> None:
    """范围守卫（阴性）：兄弟树的 drafts 不得被跨树读到（individual 语义）。"""
    sib_drafts = trees["wt_b"] / ".hiveweave" / "drafts"
    sib_drafts.mkdir(parents=True)
    (sib_drafts / "note.md").write_text("PEER-DRAFT", encoding="utf-8")
    result = await read_file(
        file_path=".hiveweave/drafts/note.md",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is False
    assert "PEER-DRAFT" not in (result.get("output") or "")


# ── ③ 文案按前缀分派（一份文案不再同时讲 reports 与 shared）──────────


@pytest.mark.asyncio
async def test_shared_miss_hint_is_shared_specific(trees: dict[str, Path]) -> None:
    """shared miss：给 shared 的顺序（本树 → MAIN）+ 落地姿势；不搬 reports 的话术。"""
    result = await read_file(
        file_path=".hiveweave/shared/absent.md",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is False
    err = result["error"] or ""
    assert "本树 → MAIN → 兄弟树" in err
    assert "MAIN → 本树" not in err          # 不得套用 reports 的顺序话术
    assert "write_file to .hiveweave/shared/<file>" in err
    assert "checkpoint" in err
    assert "本次已查 3 棵树" in err           # 报事实：真查过哪些树
    assert "确实不存在" not in err


@pytest.mark.asyncio
async def test_reports_miss_hint_is_reports_specific(trees: dict[str, Path]) -> None:
    result = await read_file(
        file_path=".hiveweave/reports/T123/absent.png",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is False
    err = result["error"] or ""
    assert "MAIN → 本树 → 兄弟树" in err
    assert "本树 → MAIN" not in err
    assert "found in 1 of the searched trees" in err   # MAIN 有 T123 目录


def test_generic_miss_hint_no_longer_speaks_for_reports() -> None:
    """通用 hint 不再内嵌 reports 专属话术（该段已下移到子目录文案）。"""
    assert "hiveweave/reports" not in READ_MISS_HINT
    assert "MAIN" in READ_MISS_HINT              # 仍保留"共享契约在 MAIN docs/"
    assert miss_hint_for("") == ""
    assert "reports/" in miss_hint_for("reports")
    assert "shared/" in miss_hint_for("shared")


# ── ④ 逐树取证提示：深一层路径也要算对目录（旧实现算成 <key>/<key>）───


def test_evidence_hint_counts_deep_path_key_dir(trees: dict[str, Path]) -> None:
    """`reports/T123/sub/x.png`：key 目录仍是 `reports/T123`（不是 T123/T123）。"""
    hint = _shared_evidence_hint(
        "reports",
        ".hiveweave/reports/T123/sub/x.png",
        str(trees["project"]),
        str(trees["wt_a"]),
    )
    assert "shot.png" in hint                    # MAIN 的 T123 目录内容被列出
    assert "found in 1 of the searched trees" in hint
    # shared 侧同一实现（key = shared 下第一段）
    hint2 = _shared_evidence_hint(
        "shared",
        ".hiveweave/shared/sub/x.md",
        str(trees["project"]),
        str(trees["wt_a"]),
    )
    assert "no shared directory for this key" in hint2


def test_reports_evidence_hint_wrapper_keeps_old_contract(trees: dict[str, Path]) -> None:
    """`_reports_evidence_hint` 仍是 #5 的对外面：非 reports 路径返回空串。"""
    assert _reports_evidence_hint("", str(trees["project"]), str(trees["wt_a"])) == ""
    assert _reports_evidence_hint(
        ".hiveweave/shared/a.md", str(trees["project"]), str(trees["wt_a"])
    ) == ""
    assert "reports/T123" in _reports_evidence_hint(
        ".hiveweave/reports/T123/absent.png", str(trees["project"]), str(trees["wt_a"])
    )


# ── ⑤ list_files：第三份手写判据退役，改走同一实现 ────────────────────


@pytest.mark.asyncio
async def test_list_files_shared_falls_back_to_main(trees: dict[str, Path]) -> None:
    """worktree 里没有 `.hiveweave/shared/`、MAIN 有 ⇒ 列出 MAIN 并标注树标签。

    旧实现（只查本树）返回 Directory not found ⇒ 本用例转红。
    """
    result = await list_files(
        path=str(trees["wt_a"] / ".hiveweave" / "shared"),
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert result["output"].startswith("Listing: MAIN\n")
    assert "contract.md" in result["output"]


@pytest.mark.asyncio
async def test_list_files_shared_miss_still_teaches_write_path(
    tmp_path: Path,
) -> None:
    """全都没有时仍是错误 + shared 教学文案（P1-1 回归，不能退化成通用 hint）。"""
    project = tmp_path / "project"
    wt = project / ".hiveweave" / "worktrees" / "A136"
    wt.mkdir(parents=True)
    result = await list_files(
        path=str(wt / ".hiveweave" / "shared"),
        workspace_path=str(wt),
        project_root=str(project),
    )
    assert result["success"] is False
    err = result["error"] or ""
    assert "Directory not found" in err
    assert "write_file to .hiveweave/shared/<file>" in err
    assert "checkpoint" in err
    assert "Not in this tree" not in err
    assert "hiveweave/reports" not in err        # 不再拿 reports 的话术讲 shared


@pytest.mark.asyncio
async def test_list_files_reports_falls_back_across_trees(trees: dict[str, Path]) -> None:
    """reports 目录同样受益（同一实现）：本树没有 ⇒ 列出 MAIN 的该 id 目录。"""
    result = await list_files(
        path=str(trees["wt_a"] / ".hiveweave" / "reports" / "T123"),
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert result["output"].startswith("Listing: MAIN\n")
    assert "shot.png" in result["output"]


# ── ⑦ 审计必修：跨树拼接不得携带 `..`（越权读的唯一闸口）──────────────


@pytest.mark.asyncio
async def test_dotdot_shared_path_cannot_read_protected_db(
    tmp_path: Path,
) -> None:
    """⭐ 审计实测的高危：`.hiveweave/shared/../data.db` 不能读到 MAIN 的库。

    机制：`rel` 被 `os.path.join` 原样带到**另一棵树**上再 realpath ⇒
    `<MAIN>/.hiveweave/shared/../data.db` = `<MAIN>/.hiveweave/data.db`，
    而本树侧那次守卫看到的是 `<worktree>/.hiveweave/data.db`（落在
    `worktrees/` 白名单内 ⇒ 放行）——"守卫检查的路径"与"实际读的路径"分家。
    修法是先归一（`tree_scope.normalize_shared_rel`）：归约后不再是共享路径。
    """
    project = tmp_path / "project"
    wt = project / ".hiveweave" / "worktrees" / "A044"
    wt.mkdir(parents=True)
    (project / ".hiveweave" / "shared").mkdir(parents=True)
    (project / ".hiveweave" / "data.db").write_text(
        "MAIN-DB-SECRET", encoding="utf-8"
    )
    result = await read_file(
        file_path=".hiveweave/shared/../data.db",
        offset=0,
        limit=50,
        workspace_path=str(wt),
        project_root=str(project),
    )
    assert "MAIN-DB-SECRET" not in (result.get("output") or "")
    assert result["success"] is False


@pytest.mark.asyncio
async def test_dotdot_shared_path_cannot_escape_project(tmp_path: Path) -> None:
    """同族：`..` 逃出项目根读到外部文件（审计实测的第二条）。"""
    project = tmp_path / "project"
    wt = project / ".hiveweave" / "worktrees" / "A044"
    wt.mkdir(parents=True)
    (project / ".hiveweave" / "shared").mkdir(parents=True)
    (tmp_path / "victim.txt").write_text("OUTSIDE-SECRET", encoding="utf-8")
    result = await read_file(
        file_path=".hiveweave/shared/../../../victim.txt",
        offset=0,
        limit=50,
        workspace_path=str(wt),
        project_root=str(project),
    )
    assert "OUTSIDE-SECRET" not in (result.get("output") or "")
    assert result["success"] is False


def test_normalize_shared_rel_is_the_only_accepted_form() -> None:
    """归一是"唯一接受形态"：逃出共享子目录的 `..` 一律不再算共享路径。"""
    from hiveweave.util.tree_scope import normalize_shared_rel, shared_subdir_of

    assert normalize_shared_rel(".hiveweave/shared/./a/../b.md") == ".hiveweave/shared/b.md"
    assert shared_subdir_of(".hiveweave/shared/../data.db") is None
    assert shared_subdir_of(".hiveweave/shared/../../x") is None
    # 归约后仍在共享子目录内 ⇒ 正常命中（reports/.. 走到 shared 是合法形态）
    assert shared_subdir_of(".hiveweave/reports/../shared/c.md") == "shared"
    assert _shared_read_scope(
        "shared", ".hiveweave/shared/../data.db",
        "C:/proj", "C:/proj/.hiveweave/worktrees/A044",
    ) == []


# ── ⑧ 审计必修：跨树命中的归因文案按子目录分派 ───────────────────────


@pytest.mark.asyncio
async def test_cross_tree_hit_note_is_subdir_specific(
    trees: dict[str, Path],
) -> None:
    """命中回执不得拿 reports 的"written to MAIN by design"讲 shared（审计实测）。"""
    shared_hit = await read_file(
        file_path=".hiveweave/shared/peer-only.md",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert "PEER-ONLY" in shared_hit["output"]
    assert "[read from worktree A045" in shared_hit["output"]
    assert "reports/ is written to MAIN by design" not in shared_hit["output"]

    reports_hit = await read_file(
        file_path=".hiveweave/reports/T123/shot.png",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert "MAIN-EVIDENCE" in reports_hit["output"]
    assert "reports/ is written to MAIN by design" in reports_hit["output"]
    assert "merge=binary" not in reports_hit["output"]


# ── ⑨ 审计必修：跨树替换后 include_ignored 要按"实际列出的树"算 ────────


@pytest.mark.asyncio
async def test_cross_tree_listing_ignore_scope_follows_listed_tree(
    trees: dict[str, Path],
) -> None:
    """跨树列**另一棵**树时，`include_ignored` 必须按那棵树算，不是按本树。

    唯一可观测差异就是 `.hiveweave` 这一个目录（`include_ignored=True` 时
    `ignored = IGNORED_DIRS - {".hiveweave"}`，其余忽略项两种口径都挡）。所以
    本用例刻意在目标树里嵌一个 `.hiveweave/` 子目录 —— 这是能观测到差异的
    形态，也是审计实测的那条（旧写法用替换前的本树路径算 ⇒ 本树在 worktrees/
    之下 ⇒ 恒 True ⇒ 把 MAIN 的 `.hiveweave` 也列出来）。
    """
    shared = trees["project"] / ".hiveweave" / "shared"
    (shared / ".hiveweave").mkdir(parents=True)
    (shared / ".hiveweave" / "secret.txt").write_text("x", encoding="utf-8")
    result = await list_files(
        path=str(trees["wt_a"] / ".hiveweave" / "shared"),
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert "contract.md" in result["output"]
    # ⚠ 断言必须落在**目录条目**上：`recursive` 默认 False ⇒ depth=1 ⇒
    # 子目录不会被下钻，只看 `secret.txt` 会两种口径都绿（本用例第一版就是这个
    # 假绿，靠阳性对照才发现）。
    assert ".hiveweave/" not in result["output"]
    assert "secret.txt" not in result["output"]


def _make_dir_link(link: Path, target: Path) -> bool:
    """造目录链接（symlink 或 junction）；本机无权限时返回 False。"""
    import subprocess

    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    try:
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        return r.returncode == 0 and os.path.isdir(str(link))
    except OSError:
        return False


@pytest.mark.asyncio
async def test_leading_dotdot_rel_is_not_a_shared_path(tmp_path: Path) -> None:
    """⭐ 二轮审计必修：`../.hiveweave/shared/x` **不是**共享路径。

    它是"从上层目录折回来"的路径，会被拼到候选树上变成**项目外**的文件
    （实测读到 `<parent>/.hiveweave/shared/leak.md`，且回执误标 `[read from MAIN]`）。
    修法：归一后必须以 `.hiveweave/` 开头才算共享路径。
    """
    project = tmp_path / "project"
    wt = project / ".hiveweave" / "worktrees" / "A044"
    wt.mkdir(parents=True)
    (project / ".hiveweave" / "shared").mkdir(parents=True)
    (project / ".hiveweave" / "shared" / "ok.md").write_text("IN-PROJ", encoding="utf-8")
    outside = tmp_path / ".hiveweave" / "shared"
    outside.mkdir(parents=True)
    (outside / "leak.md").write_text("OUTSIDE-PARENT-LEAK", encoding="utf-8")

    # 阳性对照（in-test）：机制是活的 —— 合规相对路径照常跨树命中 MAIN
    ok = await read_file(
        file_path=".hiveweave/shared/ok.md", offset=0, limit=50,
        workspace_path=str(wt), project_root=str(project),
    )
    assert ok["success"] is True and "IN-PROJ" in ok["output"]

    leak = await read_file(
        file_path="../.hiveweave/shared/leak.md", offset=0, limit=50,
        workspace_path=str(wt), project_root=str(project),
    )
    assert "OUTSIDE-PARENT-LEAK" not in (leak.get("output") or "")
    assert leak["success"] is False


@pytest.mark.asyncio
async def test_symlinked_shared_subdir_cannot_reach_protected_dir(
    tmp_path: Path,
) -> None:
    """⭐ 二轮审计必修：`shared/` 里放一个指向 `.hiveweave` 的链接也读不到。

    `_shared_read_scope` 用 `realpath` ⇒ 链接会被解掉，最终落到
    `<MAIN>/.hiveweave/data.db`；而本树侧那次守卫看到的是
    `<worktree>/.hiveweave/shared/linkdir/data.db`（`worktrees/` 白名单内 ⇒
    放行）。修法：**跨树命中后复检同一个守卫**（按解链接后的最终路径判）。
    """
    project = tmp_path / "project"
    wt = project / ".hiveweave" / "worktrees" / "A044"
    wt.mkdir(parents=True)
    shared = project / ".hiveweave" / "shared"
    shared.mkdir(parents=True)
    (shared / "ok.md").write_text("IN-PROJ", encoding="utf-8")
    (project / ".hiveweave" / "data.db").write_text(
        "MAIN-DB-SECRET", encoding="utf-8"
    )
    link = shared / "linkdir"
    if not _make_dir_link(link, project / ".hiveweave"):
        pytest.skip("本机无法创建目录链接（symlink/junction 均被拒）")

    ok = await read_file(
        file_path=".hiveweave/shared/ok.md", offset=0, limit=50,
        workspace_path=str(wt), project_root=str(project),
    )
    assert ok["success"] is True and "IN-PROJ" in ok["output"]

    via_link = await read_file(
        file_path=".hiveweave/shared/linkdir/data.db", offset=0, limit=50,
        workspace_path=str(wt), project_root=str(project),
    )
    assert "MAIN-DB-SECRET" not in (via_link.get("output") or "")
    assert via_link["success"] is False

    # 目录列举同样要挡住（跨树回退返回的是解链接后的目录）
    listed = await list_files(
        path=str(wt / ".hiveweave" / "shared" / "linkdir"),
        workspace_path=str(wt), project_root=str(project),
    )
    assert "MAIN-DB-SECRET" not in (listed.get("output") or "")
    assert listed["success"] is False


@pytest.mark.asyncio
async def test_symlink_out_of_project_is_rejected(tmp_path: Path) -> None:
    """同类第二条：链接指向**项目外**时，`.hiveweave` 守卫看不见（目标不在里面）
    ⇒ 必须靠 `_inside_any` 复检挡（与 reports 侧依赖同一对守卫）。

    ⚠ 文件名**不能**叫 `secret.txt` 之类 —— 那会撞上 `_is_sensitive` 的
    拒绝分支，于是"用例通过"其实与本次守卫无关（本用例第一版就是这个假绿，
    靠把守卫临时关掉才暴露）。
    """
    project = tmp_path / "project"
    wt = project / ".hiveweave" / "worktrees" / "A044"
    wt.mkdir(parents=True)
    shared = project / ".hiveweave" / "shared"
    shared.mkdir(parents=True)
    (shared / "ok.md").write_text("IN-PROJ", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.md").write_text("OUT-OF-PROJECT", encoding="utf-8")
    link = shared / "outlink"
    if not _make_dir_link(link, outside):
        pytest.skip("本机无法创建目录链接（symlink/junction 均被拒）")

    res = await read_file(
        file_path=".hiveweave/shared/outlink/payload.md", offset=0, limit=50,
        workspace_path=str(wt), project_root=str(project),
    )
    assert "OUT-OF-PROJECT" not in (res.get("output") or "")
    assert res["success"] is False
    assert "Sandbox violation" in (res.get("error") or "")


# ── ⑥ 与 vision 侧同源（读侧两个消费者共用一个顺序）──────────────────


def test_every_cross_tree_subdir_has_own_hints() -> None:
    """表驱动：凡开了跨树读的子目录，**必须**有自己的命中/缺失文案。

    审计指出：顺序的散文写在 docstring、权威却在 `POLICIES` —— 日后新增第 5 个
    子目录只补 `POLICIES`，文案会**静默退化成空串/通用句**（不报错、不打日志）。
    这条把"三张表同步"变成可核验的断言。
    """
    from hiveweave.util.tree_scope import POLICIES, hit_note_for, miss_hint_for

    for subdir, pol in POLICIES.items():
        if not pol.cross_tree_read:
            continue
        assert miss_hint_for(subdir).strip(), f"{subdir} 缺 miss 文案"
        assert hit_note_for(subdir).strip(), f"{subdir} 缺 hit 文案"
        # 两份文案必须各自点名子目录（否则就是"一份文案讲两件事"的老病）
        assert f"{subdir}/" in miss_hint_for(subdir)
    # 未开跨树读的子目录不该有专属文案（有就说明表不同步）
    assert miss_hint_for("drafts") == "" and hit_note_for("drafts") != ""


def test_vision_and_file_share_one_ordering(trees: dict[str, Path]) -> None:
    # ⚠ 本断言在 09-16 之后是**恒真**的（两处已共用 `ordered_tree_roots`）——
    # 它只防"有人又写回各自一套"，不构成顺序正确性的证据（那由
    # test_order_is_parameterized_by_subdir_strategy 的写死期望值负责）。
    scope = _reports_read_scope(
        ".hiveweave/reports/T123/x.png", str(trees["project"]), str(trees["wt_a"])
    )
    assert [t for t, _ in scope] == [
        tree_tag(b) for b in _multi_tree_bases(str(trees["wt_a"]), str(trees["project"]))
    ]
    found, note = resolve_screenshot_path_multi_tree(
        str(trees["wt_a"]),
        ".hiveweave/reports/T123/shot.png",
        str(trees["project"]),
    )
    assert found is not None and "read from MAIN" in note


def test_subdir_detector_covers_both_and_rejects_others() -> None:
    assert _platform_shared_read_subdir(".hiveweave/shared/a.md") == "shared"
    assert _platform_shared_read_subdir("./.hiveweave/reports/T1/x") == "reports"
    assert _platform_shared_read_subdir(".hiveweave/drafts/a.md") == "drafts"
    assert _platform_shared_read_subdir(".hiveweave/data.db") is None
    assert _platform_shared_read_subdir("src/shared/a.md") is None
