"""Screenshot path on attestation — parse hashes + project-root sandbox."""

from __future__ import annotations

import json
from pathlib import Path

from hiveweave.services.attestation import screenshot_path_from_artifact_hashes
from hiveweave.services.vision import resolve_screenshot_under_project


def test_parse_artifact_hashes_dict():
    got = screenshot_path_from_artifact_hashes(
        {"screenshot_path": "/abs/a.png"}
    )
    assert got == "/abs/a.png"


def test_parse_artifact_hashes_json_merged_with_file_hashes():
    raw = json.dumps(
        {"file.txt": "abc123", "screenshot_path": r"D:\wt\x.png"}
    )
    assert screenshot_path_from_artifact_hashes(raw) == r"D:\wt\x.png"


def test_parse_artifact_hashes_camel_case_key():
    assert (
        screenshot_path_from_artifact_hashes(
            {"screenshotPath": "/tmp/shot.png"}
        )
        == "/tmp/shot.png"
    )


def test_parse_artifact_hashes_missing_or_invalid():
    assert screenshot_path_from_artifact_hashes(None) is None
    assert screenshot_path_from_artifact_hashes("") is None
    assert screenshot_path_from_artifact_hashes("{}") is None
    assert screenshot_path_from_artifact_hashes([]) is None
    assert screenshot_path_from_artifact_hashes("not-json") is None
    assert screenshot_path_from_artifact_hashes({"other": "x"}) is None
    assert screenshot_path_from_artifact_hashes({"screenshot_path": "  "}) is None


def test_resolve_screenshot_under_worktree(tmp_path: Path):
    project = tmp_path / "proj"
    shot = project / ".hiveweave" / "worktrees" / "sid" / "x.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")
    got = resolve_screenshot_under_project(str(project), str(shot))
    assert got is not None
    assert got == shot.resolve()


def test_resolve_screenshot_under_worktree_relative(tmp_path: Path):
    project = tmp_path / "proj"
    shot = project / ".hiveweave" / "worktrees" / "sid" / "x.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")
    got = resolve_screenshot_under_project(
        str(project), ".hiveweave/worktrees/sid/x.png"
    )
    assert got == shot.resolve()


def test_resolve_screenshot_outside_project_rejected(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "other" / "x.png"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    assert resolve_screenshot_under_project(str(project), str(outside)) is None


def test_resolve_screenshot_dotdot_escape_rejected(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    # four `..` from worktrees/sid lands beside the project, not inside it
    raw = str(
        project
        / ".hiveweave"
        / "worktrees"
        / "sid"
        / ".."
        / ".."
        / ".."
        / ".."
        / "outside.png"
    )
    assert resolve_screenshot_under_project(str(project), raw) is None
    assert (
        resolve_screenshot_under_project(str(project), "../outside.png")
        is None
    )
