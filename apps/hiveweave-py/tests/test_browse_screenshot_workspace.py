"""Screenshot CLI always gets an explicit workspace-relative path."""

from __future__ import annotations

from pathlib import Path

from hiveweave.tools.browse_tools import (
    _inject_shot_abs_path,
    _pin_shot_path,
    _screenshot_argv,
    _screenshot_missing_diagnostic,
    _screenshot_path_from_argv,
    default_screenshot_relpath,
    ensure_screenshot_argv,
)


def test_screenshot_argv_injects_reports_path_when_omitted():
    out = ensure_screenshot_argv(["screenshot"], agent_id="A100")
    joined = " ".join(out).replace("\\", "/")
    assert ".hiveweave/reports/" in joined
    rel = _screenshot_path_from_argv(out)
    assert rel is not None
    assert rel.replace("\\", "/").startswith(".hiveweave/reports/")
    assert rel.endswith(".png")


def test_screenshot_argv_keeps_explicit_path():
    out = ensure_screenshot_argv(
        ["screenshot", "evidence/flow.png"], agent_id="A100"
    )
    assert _screenshot_path_from_argv(out) == "evidence/flow.png"
    assert ".hiveweave/reports/" not in " ".join(out)


def test_default_screenshot_relpath_uses_agent_dir():
    rel = default_screenshot_relpath("A100", now_ms=1_700_000_000_000)
    assert rel == ".hiveweave/reports/A100/shot-1700000000000.png"


def test_screenshot_path_from_argv_omitted_is_none():
    assert _screenshot_path_from_argv(["screenshot"]) is None
    assert _screenshot_path_from_argv(["shoot"]) is None


def test_inject_shot_abs_path_resolves_under_workspace(tmp_path):
    abs_shot = _inject_shot_abs_path(
        str(tmp_path), ".hiveweave/reports/A100/shot-1.png"
    )
    assert Path(abs_shot).is_absolute()
    assert abs_shot.replace("\\", "/").startswith(
        str(tmp_path.resolve()).replace("\\", "/") + "/"
    )
    assert abs_shot.replace("\\", "/").endswith(
        ".hiveweave/reports/A100/shot-1.png"
    )


def test_inject_shot_abs_path_keeps_absolute_input(tmp_path):
    given = str(tmp_path / "x.png")
    assert _inject_shot_abs_path(str(tmp_path), given) == str(
        Path(given).resolve()
    )


def test_missing_diagnostic_locates_drifted_file(tmp_path):
    marker = Path(tmp_path) / "shot-999.png"
    marker.write_bytes(b"png")
    diag = _screenshot_missing_diagnostic(
        str(tmp_path), ".hiveweave/reports/A100/shot-999.png"
    )
    assert marker.name in diag and diag.startswith("Screenshot was written to")


def test_missing_diagnostic_none_found_has_actionable_hint(tmp_path):
    diag = _screenshot_missing_diagnostic(
        str(tmp_path), ".hiveweave/reports/A100/shot-absent.png"
    )
    assert "no PNG was found" in diag
    assert 'browse(args=["restart"])' in diag


def test_pin_shot_path_replaces_tail_relative_with_abs(tmp_path):
    mapped = ["screenshot", ".hiveweave/reports/A100/shot-1.png"]
    shot_rel = ".hiveweave/reports/A100/shot-1.png"
    out = _pin_shot_path(mapped, str(tmp_path), shot_rel)
    assert len(out) == 2
    assert out[0] == "screenshot"
    assert Path(out[1]).is_absolute()
    assert out[1].replace("\\", "/").endswith(shot_rel)


def test_pin_shot_path_appends_abs_when_tail_not_raw_rel(tmp_path):
    # flag/selector case: tail is the path already, so replace; if the tail
    # does not equal the raw rel (e.g. a selector got inserted before it),
    # append the pinned absolute path instead of losing it.
    mapped = ["screenshot", "#canvas", "evidence/x.png"]
    shot_rel = "evidence/x.png"
    out = _pin_shot_path(mapped, str(tmp_path), shot_rel)
    assert out[0] == "screenshot" and out[1] == "#canvas"
    assert Path(out[-1]).is_absolute()
    assert out[-1].replace("\\", "/").endswith("evidence/x.png")


def test_screenshot_argv_path_is_last_positional_with_flags():
    # --selector + selector inserted right after subcommand; path stays last.
    ab = _screenshot_argv(["canvas", "--full", "evidence/x.png"])
    assert ab[0] == "screenshot"
    assert ab[-1] == "evidence/x.png"
    ab2 = _screenshot_argv(["--selector", "#canvas", "-f", "evidence/x.png"])
    assert "screenshot" in ab2
    assert ab2[-1] == "evidence/x.png"
