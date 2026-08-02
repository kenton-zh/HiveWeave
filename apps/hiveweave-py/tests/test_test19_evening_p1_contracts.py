"""TEST19 evening P1: skill list contract + truncation dual-cap + path normalize."""

from __future__ import annotations

import re

from hiveweave.conversation.token_utils import (
    PREVIEW_MAX_CHARS,
    build_tool_output_preview,
    truncate_tool_output,
)
from hiveweave.services.skill_registry import (
    SKILL_LIST_DESC_MAX_CHARS,
    SkillRegistryService,
    _sanitize_skill_list_desc,
)
from hiveweave.services.worktree_review import normalize_evidence_path


def test_sanitize_skill_desc_collapses_html_debris():
    """Upstream contract: HTML leftovers become a short single line."""
    blob = (
        "git-worktrees: only\\u003c/strong\\u003e\\u003c/td\\u003e\\n\\u003ctr\\u003e"
        + ("x" * 5000)
    )
    out = _sanitize_skill_list_desc(blob)
    assert "\n" not in out
    assert len(out) <= SKILL_LIST_DESC_MAX_CHARS
    assert "<td>" not in out
    assert "\\u003c" not in out


def test_sanitize_skill_desc_caps_length():
    out = _sanitize_skill_list_desc("a" * 1000)
    assert len(out) <= SKILL_LIST_DESC_MAX_CHARS
    assert out.endswith("…")


def test_extract_summary_rejects_html_dump():
    html = (
        "<html>Summary</html>"
        + ("<td>only</strong></td>" * 2000)
        + "Installation"
    )
    got = SkillRegistryService._extract_summary(html)
    # Either None or a short sanitized line — never a multi-KB dump / tag debris
    if got is not None:
        assert len(got) <= 500
        assert not re.search(r"<[A-Za-z/!?]", got)


def test_extract_summary_keeps_plain_less_than_prose():
    """Sanitize must not discard 'a < b' style prose as HTML."""
    html = "Summary use a < b carefully when comparing Installation"
    got = SkillRegistryService._extract_summary(html)
    assert got is not None
    assert "a < b" in got
    assert len(got) <= 500


def test_preview_char_dual_cap_defeats_single_line_dump():
    """Fallback: line truncation alone must not return ~75KB previews."""
    huge_line = "A" * 80_000
    output = "header\n" + huge_line + "\nfooter\n" + "\n".join(f"L{i}" for i in range(30))
    path = "/tmp/full_skills.txt"
    preview = build_tool_output_preview(output, path)
    assert len(preview) <= PREVIEW_MAX_CHARS + 50  # small slack for marker
    assert path in preview  # mid-layer handle must survive total cap
    assert "truncated" in preview.lower()
    assert "A" * 1000 not in preview  # per-line cap applied


def test_preview_preserves_path_under_fat_multiline_head():
    """Many medium-long lines must not erase the saved-file path."""
    lines = [f"{'B' * 400}-line-{i}" for i in range(40)]
    output = "\n".join(lines)
    path = "D:/ws/.hiveweave/tool_outputs/agent_ts_tool.txt"
    preview = build_tool_output_preview(output, path)
    assert path in preview
    assert "Full output saved to" in preview
    assert len(preview) <= PREVIEW_MAX_CHARS + 80


def test_truncate_tool_output_uses_dual_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("TMP", str(tmp_path))
    huge = "X" * 60_000  # over 50KB, few lines
    out = truncate_tool_output(huge)
    assert out != huge
    assert len(out) <= PREVIEW_MAX_CHARS + 80
    assert "truncated" in out.lower() or "preview capped" in out.lower()
    assert "Full output saved to" in out


def test_normalize_preserves_dot_hiveweave_reports():
    """submit/dispatch must not turn .hiveweave/reports into hiveweave/reports."""
    p = ".hiveweave/reports/c74fa450/evidence.md"
    assert normalize_evidence_path(p) == p
    assert normalize_evidence_path("./.hiveweave/reports/x.md") == (
        ".hiveweave/reports/x.md"
    )
    # Contrast: the forbidden lstrip behaviour
    assert p.lstrip("./") == "hiveweave/reports/c74fa450/evidence.md"


def test_match_agent_recipient_accepts_uuid():
    from hiveweave.tools.orchestration_tools import match_agent_recipient

    ceo = {
        "id": "8766c772-1ccc-4dcb-82de-0f45f4423097",
        "short_id": "A028",
        "name": "归零",
        "role": "ceo",
        "status": "active",
    }
    match, archived = match_agent_recipient(ceo["id"], [ceo])
    assert match is ceo
    assert archived is False
    match2, _ = match_agent_recipient("A028", [ceo])
    assert match2 is ceo
    match3, arch = match_agent_recipient(
        ceo["id"],
        [],
        [{**ceo, "status": "archived"}],
    )
    assert match3 is None
    assert arch is True


def test_match_agent_recipient_uuid_case_insensitive():
    from hiveweave.tools.orchestration_tools import match_agent_recipient

    ceo = {
        "id": "8766c772-1ccc-4dcb-82de-0f45f4423097",
        "short_id": "A028",
        "name": "归零",
        "role": "ceo",
        "status": "active",
    }
    upper = ceo["id"].upper()
    match, archived = match_agent_recipient(upper, [ceo])
    assert match is ceo
    assert archived is False
    match_arch, arch = match_agent_recipient(
        upper, [], [{**ceo, "status": "archived"}]
    )
    assert match_arch is None
    assert arch is True
