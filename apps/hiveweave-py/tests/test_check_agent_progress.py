"""TEST21 M13 — check_agent_progress tool registration."""

from __future__ import annotations

import hiveweave.tools.org_tools  # noqa: F401 — register tools

from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    HR_TOOLS,
)
from hiveweave.tools.base import get_tool_def


def test_check_agent_progress_registered():
    spec = get_tool_def("check_agent_progress")
    assert spec is not None
    assert "CEO read-only" in (spec.description or "")


def test_check_agent_progress_ceo_only():
    assert "check_agent_progress" in CEO_TOOLS
    assert "check_agent_progress" not in COORDINATOR_BUILDER_TOOLS
    assert "check_agent_progress" not in HR_TOOLS


def test_check_agent_status_still_shared():
    assert "check_agent_status" in COORDINATOR_BUILDER_TOOLS
