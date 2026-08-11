"""F3: browse snapshot ≥50KB → 短契约（标题/URL/元素数/摘要 + 落盘句柄）。

小快照原样返回；非 snapshot 命令不受影响（仍走 executor 层兜底截断）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import hiveweave.tools.browse_tools as bt
from hiveweave.tools.browse_tools import BrowseParams, browse_tool, _contract_snapshot_output


def _fake_tree(n: int, title: str = "Demo 页面", url: str = "http://127.0.0.1:3000/") -> str:
    lines = [f"Page: {title}", f"URL: {url}", ""]
    for i in range(1, n + 1):
        lines.append(f"@e{i} [link] \"条目 {i}\"")
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_large_snapshot_returns_contract_and_disk_handle(
    tmp_path: Path, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    snapshot = _fake_tree(4000)

    async def fake_browse_exec(argv, workspace, timeout_sec=60, agent_id=None):
        return 0, snapshot, ""

    async def fake_attest(**kwargs):
        return ""

    monkeypatch.setattr(bt, "browse_exec", fake_browse_exec)
    monkeypatch.setattr(bt, "issue_browse_e2e_attestation", fake_attest)

    result = await browse_tool(
        BrowseParams(args=["snapshot", "-i"]),
        agent_id="agent-1",
        workspace=str(ws),
    )
    assert result.success is True
    text = result.output
    assert "[snapshot 已落盘:" in text
    assert "页面标题: Demo 页面" in text
    assert "URL: http://127.0.0.1:3000/" in text
    assert "元素数: 4000" in text
    assert "@e1 [link]" in text
    assert len(text) <= 2_500, f"contract must stay compact, got {len(text)} chars"

    m = re.search(r"\[snapshot 已落盘: ([^\]]+)\]", text)
    assert m, "must carry the full-snapshot disk handle"
    disk = Path(m.group(1))
    assert disk.is_file()
    assert disk.read_text(encoding="utf-8") == snapshot
    assert disk.parent.name == "tool_outputs"


@pytest.mark.asyncio
async def test_small_snapshot_unchanged(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    snapshot = _fake_tree(50)

    async def fake_browse_exec(argv, workspace, timeout_sec=60, agent_id=None):
        return 0, snapshot, ""

    async def fake_attest(**kwargs):
        return ""

    monkeypatch.setattr(bt, "browse_exec", fake_browse_exec)
    monkeypatch.setattr(bt, "issue_browse_e2e_attestation", fake_attest)

    result = await browse_tool(
        BrowseParams(args=["snapshot"]),
        agent_id="agent-1",
        workspace=str(ws),
    )
    assert result.success is True
    assert result.output.startswith("Page: Demo 页面")
    assert "[snapshot 已落盘:" not in result.output
    assert "@e50 [link]" in result.output


def test_contract_passthrough_below_threshold(tmp_path: Path):
    small = _fake_tree(10)
    assert _contract_snapshot_output(small, "a1", str(tmp_path)) == small


def test_contract_json_snapshot_counts_roles(tmp_path: Path):
    blob = (
        '{"success":true,"data":{"refs":{"e1":{"role":"heading"},'
        '"e2":{"role":"button"}}}}'
    )
    payload = blob + " pad " * 15_000  # > 50KB
    out = _contract_snapshot_output(payload, "a1", str(tmp_path))
    assert "元素数: 2" in out
    assert "页面标题: (未提取)" in out
    assert "[snapshot 已落盘:" in out
