"""L2 结构化事实包测试：dispatch_facts 采集与渲染。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hiveweave.services.dispatch_facts import (
    collect_and_format,
    collect_dispatch_facts,
    format_facts_block,
)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "index.html").write_text("<html>town</html>", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


def test_collect_returns_main_head_and_file_facts(tmp_path):
    _init_repo(tmp_path)
    facts = collect_dispatch_facts(str(tmp_path))
    assert any("MAIN HEAD" in f for f in facts)
    assert any("index.html" in f and "bytes" in f for f in facts)


def test_collect_worktree_branch_state(tmp_path):
    main = tmp_path / "main"
    _init_repo(main)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "hw/A1/work", str(wt)], cwd=main, check=True)
    (wt / "new.py").write_text("x=1", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "wip"],
        cwd=wt,
        check=True,
    )
    facts = collect_dispatch_facts(str(main), str(wt), "A1")
    assert any("领先 main 1" in f for f in facts)


def test_format_block_empty_is_silent():
    assert format_facts_block([]) == ""


def test_collect_and_format_never_raises(tmp_path):
    # 不存在的 workspace → 空串（fail-open）
    assert collect_and_format(str(tmp_path / "nope")) == ""
