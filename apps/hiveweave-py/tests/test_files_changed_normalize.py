"""Normalize evidence.files_changed worktree prefixes (TEST4)."""

from hiveweave.services.worktree_review import (
    normalize_evidence_path,
    normalize_files_changed,
)


def test_strip_dot_hiveweave_worktree_prefix():
    assert (
        normalize_evidence_path(".hiveweave/worktrees/A004/module_a.py")
        == "module_a.py"
    )


def test_strip_bare_hiveweave_worktree_prefix():
    assert (
        normalize_evidence_path("hiveweave/worktrees/A004/src/mod.py")
        == "src/mod.py"
    )


def test_strip_absolute_worktree_path():
    p = r"D:\PC_AI\Project\TEST4\.hiveweave\worktrees\A005\module_b.py"
    assert normalize_evidence_path(p) == "module_b.py"


def test_already_relative_unchanged():
    assert normalize_evidence_path("module_a.py") == "module_a.py"
    assert normalize_evidence_path("./tests/test_a.py") == "tests/test_a.py"


def test_preserves_dotfile_leading_dot():
    """lstrip('./') must NOT strip .editorconfig → editorconfig."""
    assert normalize_evidence_path(".editorconfig") == ".editorconfig"
    assert normalize_evidence_path("./.editorconfig") == ".editorconfig"
    assert normalize_evidence_path(".gitignore") == ".gitignore"
    assert (
        normalize_files_changed(
            [".hiveweave/worktrees/A005/.editorconfig", "editorconfig"]
        )
        == [".editorconfig", "editorconfig"]
    )


def test_normalize_files_changed_dedupes():
    got = normalize_files_changed(
        [
            ".hiveweave/worktrees/A004/a.py",
            "a.py",
            "hiveweave/worktrees/A004/b.py",
            "",
            None,
        ]
    )
    assert got == ["a.py", "b.py"]
