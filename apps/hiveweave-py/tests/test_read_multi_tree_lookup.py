"""#5 读侧多树查找 —— 判据来自我们自己的四目录共享模型（fixplan §10.2）。

背景（实测复现的两条真 bug，均在本文件守卫）：

1. ``_is_platform_reports_read`` 用了 ``str.lstrip("./")`` —— 它把参数当
   **字符集**，把 ``.hiveweave/reports/x`` 剥成 ``hiveweave/reports/x``
   ⇒ 判定**恒 False**，40 轮 P0-1 的 reports 重定向**从未生效**。
   （本仓 policy.py / worktree_review.py / submit.py 已有三处同族修正。）
2. 即便重定向生效，也只查**一棵树**就报 "no reports directory for id '{eid}'"
   —— 单点查空下全局结论（L17/L20 同族病）。

判据出处：
- ``services/git_worktree/service_create.py:99-105`` 四目录反选入库、
  跨 worktree 可见可合并 ⇒ 写侧单一权威落点（MAIN）是**设计**；
- ``:171-175`` reports = 默认文本合并、预期多方写 ⇒ 同树才成立；
- ``services/acl_sandbox/policy.py:54`` executor 的授权树根 = worktree
  ⇒ 隔离也是有的，读侧必须**跨越**它而不是拆掉它。

断言用**可被代码回退打红**的形态（不看 docstring）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hiveweave.services.vision import resolve_screenshot_path_multi_tree
from hiveweave.tools.file import (
    _is_platform_reports_read,
    _reports_evidence_hint,
    _reports_read_scope,
    read_file,
    strip_dot_slash_prefix,
)
from hiveweave.util.tree_label import READ_MISS_HINT


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, Path]:
    """project/ + 两棵 worktree；共享产物只写在 MAIN，另有一份在兄弟树。"""
    project = tmp_path / "project"
    wt_a = project / ".hiveweave" / "worktrees" / "A044"
    wt_b = project / ".hiveweave" / "worktrees" / "A045"
    wt_a.mkdir(parents=True)
    wt_b.mkdir(parents=True)
    main_rep = project / ".hiveweave" / "reports" / "T123"
    main_rep.mkdir(parents=True)
    (main_rep / "shot.png").write_text("MAIN-EVIDENCE", encoding="utf-8")
    sib_rep = wt_b / ".hiveweave" / "reports" / "T123"
    sib_rep.mkdir(parents=True)
    (sib_rep / "peer.txt").write_text("PEER-EVIDENCE", encoding="utf-8")
    return {"project": project, "wt_a": wt_a, "wt_b": wt_b}


# ── ① P0：lstrip("./") 字符集 bug 直接守卫 ────────────────────────────


def test_strip_dot_slash_prefix_preserves_dotted_leading_segment() -> None:
    """逐段剥 ``./``；**不得**吃掉 ``.hiveweave`` 的前导点。"""
    assert strip_dot_slash_prefix(".hiveweave/reports/x") == ".hiveweave/reports/x"
    assert strip_dot_slash_prefix("./.hiveweave/reports/x") == ".hiveweave/reports/x"
    assert strip_dot_slash_prefix("././a/b") == "a/b"
    # 回归锚：这条断言在旧实现（lstrip("./")) 下必然失败
    assert not strip_dot_slash_prefix(".hiveweave/x").startswith("hiveweave")


def test_platform_reports_read_detector_actually_matches() -> None:
    """守卫「判定恒 False」这一静默失效（旧实现 lstrip 后永不命中）。"""
    assert _is_platform_reports_read(".hiveweave/reports/T123/shot.png") is True
    assert _is_platform_reports_read("./.hiveweave/reports/T123/shot.png") is True
    assert _is_platform_reports_read(".hiveweave/reports") is True
    assert _is_platform_reports_read(".hiveweave/shared/a.md") is False
    assert _is_platform_reports_read("src/x.py") is False


# ── ② 读侧真的跨树命中（不是只改了文案）────────────────────────────


@pytest.mark.asyncio
async def test_read_reports_hits_main_from_leaf_worktree(
    trees: dict[str, Path],
) -> None:
    """叶子树读 MAIN 的共享产物必须命中，且回执**点名在哪棵树**。"""
    result = await read_file(
        file_path=".hiveweave/reports/T123/shot.png",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert "MAIN-EVIDENCE" in result["output"]
    # 多树归因必要条件：回执须说明来源树（fixplan §10.5）
    assert "[read from MAIN" in result["output"]


@pytest.mark.asyncio
async def test_read_reports_hits_sibling_worktree(
    trees: dict[str, Path],
) -> None:
    """本树 + MAIN 都没有时，继续查兄弟树，且点名是哪个兄弟。"""
    result = await read_file(
        file_path=".hiveweave/reports/T123/peer.txt",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert "PEER-EVIDENCE" in result["output"]
    assert "[read from worktree A045" in result["output"]


@pytest.mark.asyncio
async def test_read_own_tree_hit_has_no_cross_tree_note(
    trees: dict[str, Path],
) -> None:
    """本树命中不得加跨树回执噪音（避免全量读取回执膨胀）。"""
    own = trees["wt_a"] / ".hiveweave" / "reports" / "T123"
    own.mkdir(parents=True)
    (own / "local.txt").write_text("LOCAL", encoding="utf-8")
    result = await read_file(
        file_path=".hiveweave/reports/T123/local.txt",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is True
    assert "[read from " not in result["output"]


# ── ③ miss 文案：报事实 + 报查了哪些树，**不断言不存在** ─────────────


@pytest.mark.asyncio
async def test_read_miss_lists_searched_trees_and_does_not_assert_absence(
    trees: dict[str, Path],
) -> None:
    """该 id 目录存在于 2 棵树 → 报「哪几棵有该 id 目录」，仍不断言不存在。"""
    result = await read_file(
        file_path=".hiveweave/reports/T123/absent.png",
        offset=0,
        limit=50,
        workspace_path=str(trees["wt_a"]),
        project_root=str(trees["project"]),
    )
    assert result["success"] is False
    err = result["error"] or ""
    # 逐树查找：点名**真有两棵**（MAIN 与 A045）有该 id 目录
    assert "found in 2 of the searched trees" in err
    assert "MAIN" in err
    assert "worktree A045" in err
    # 不得下全局结论
    assert "确实不存在" not in err


@pytest.mark.asyncio
async def test_read_miss_no_tree_has_id_reports_searched_set(
    tmp_path: Path,
) -> None:
    """全部候选树都没有该 id 目录 → 报「查过哪些树」，**仍不断言不存在**。"""
    project = tmp_path / "project"
    wt_a = project / ".hiveweave" / "worktrees" / "A044"
    wt_b = project / ".hiveweave" / "worktrees" / "A045"
    wt_a.mkdir(parents=True)
    wt_b.mkdir(parents=True)
    result = await read_file(
        file_path=".hiveweave/reports/T999/none.png",
        offset=0,
        limit=50,
        workspace_path=str(wt_a),
        project_root=str(project),
    )
    assert result["success"] is False
    err = result["error"] or ""
    assert "searched 3 tree(s)" in err
    assert "worktree A044" in err
    assert "MAIN" in err
    assert "worktree A045" in err
    assert "not proof the evidence does not exist" in err
    # 旧实现的越界断言必须消失
    assert "no reports directory for id" not in err
    assert "确实不存在" not in err


def test_read_miss_hint_has_no_out_of_scope_assertion() -> None:
    """越界断言守卫：文案不得声称"确实不存在"（它只跑过本树）。"""
    assert "确实不存在" not in READ_MISS_HINT
    assert "Do not search other agents' trees" not in READ_MISS_HINT
    # 仍保留有用事实（共享契约在 MAIN docs/）
    assert "MAIN" in READ_MISS_HINT


def test_reports_evidence_hint_names_tree_that_has_the_id(
    trees: dict[str, Path],
) -> None:
    hint = _reports_evidence_hint(
        ".hiveweave/reports/T123/absent.png",
        str(trees["project"]),
        str(trees["wt_a"]),
    )
    # 该 id 目录在 MAIN 与 A045 都存在 → 两个树都被点名
    assert "MAIN" in hint
    assert "worktree A045" in hint
    assert "shot.png" in hint
    assert "peer.txt" in hint


def test_reports_read_scope_orders_and_dedupes(trees: dict[str, Path]) -> None:
    scope = _reports_read_scope(
        ".hiveweave/reports/T123/x.png",
        str(trees["project"]),
        str(trees["wt_a"]),
    )
    tags = [tag for tag, _ in scope]
    assert tags[0] == "worktree A044"      # 本树先查
    assert "MAIN" in tags                  # 共享落点必在候选里
    assert "worktree A045" in tags         # 兄弟树兜底
    assert len(tags) == len(set(tags))     # 去重
    # 项目根即 workspace 时不得重复
    scope2 = _reports_read_scope(
        ".hiveweave/reports/T123/x.png",
        str(trees["project"]),
        str(trees["project"]),
    )
    assert [t for t, _ in scope2].count("MAIN") == 1


# ── ④ vision 侧同一读侧缺口（look_at_image 必失败的成因）────────────


def test_vision_multi_tree_finds_main_and_names_it(trees: dict[str, Path]) -> None:
    found, note = resolve_screenshot_path_multi_tree(
        str(trees["wt_a"]),
        ".hiveweave/reports/T123/shot.png",
        str(trees["project"]),
    )
    assert found is not None and found.is_file()
    assert "read from MAIN" in note


def test_vision_multi_tree_finds_sibling(trees: dict[str, Path]) -> None:
    found, note = resolve_screenshot_path_multi_tree(
        str(trees["wt_a"]),
        ".hiveweave/reports/T123/peer.txt",
        str(trees["project"]),
    )
    assert found is not None and found.is_file()
    assert "worktree A045" in note


def test_vision_multi_tree_miss_reports_searched_not_absence(
    trees: dict[str, Path],
) -> None:
    found, note = resolve_screenshot_path_multi_tree(
        str(trees["wt_a"]),
        ".hiveweave/reports/T123/absent.png",
        str(trees["project"]),
    )
    assert found is None
    assert "searched" in note
    assert "not proof the image" in note


def test_vision_multi_tree_rejects_dotdot_escape(trees: dict[str, Path]) -> None:
    """扩大读范围**不等于**拆掉隔离：`..` 逃出项目仍必须拒。"""
    found, note = resolve_screenshot_path_multi_tree(
        str(trees["wt_a"]),
        "../../../../secret.png",
        str(trees["project"]),
    )
    assert found is None
    assert note == ""
