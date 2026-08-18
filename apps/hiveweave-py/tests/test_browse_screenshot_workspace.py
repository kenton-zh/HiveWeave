"""Screenshot CLI always gets an explicit workspace-relative path."""

from __future__ import annotations

from hiveweave.tools.browse_tools import (
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
