"""Executable acceptance checklist for TEST11 evening follow-ups + R2/R4/R7/R8.

Run:
  uv run pytest tests/test_r248_real_regression.py tests/test_soft_warn_evidence.py tests/test_test11_evening_fixes.py -v
  uv run python ../../tasks/_drive_r248_live.py   # needs TEST11 activated
  uv run python ../../tasks/_drive_r7_browse.py   # needs browse.bin + http.server :8765
"""

from __future__ import annotations

# This module is documentation-as-code. The pytest cases below assert that
# the acceptance checklist items remain wired.

from hiveweave.services.turn_session import classify_commit_gate_soft_warn
from hiveweave.services.worktree_review import extract_acceptance_path_refs
from hiveweave.tools.misc_tools import _check_self_merge_gate


def test_checklist_soft_warn_api_present():
    soft, hard = classify_commit_gate_soft_warn("_checklist", ["WAIT_WITHOUT_ASK"])
    assert soft and not hard


def test_checklist_evidence_path_extractor_present():
    assert "src/x.py" in extract_acceptance_path_refs(["src/x.py present"])


def test_checklist_merge_gate_symbol_present():
    assert callable(_check_self_merge_gate)
