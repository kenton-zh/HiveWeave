"""TEST_DSH_64 #10② cwd 越界误报修正：存在但越界 ≠ does not exist。

现场签名（L8 错位）：worktree 内 agent 把 workdir/cwd 写成
`.hiveweave/worktrees/<别的 id>` ⇒ 相对拼接出本树内双嵌套路径 ⇒ 不存在 ⇒
旧文案报「Working directory does not exist: [worktree A133 …A135]」——
误报（那棵树实存）+ 两个树 id 混排。修正后：
- 引用的是**实存**的别的树 → 「directory exists but is outside your tree
  boundary ([worktree 本树]) — requested: …」（自带归因头，
  with_cwd_display 不再叠加混排行）；
- 真实缺失 → 旧文案原样保留。
"""

from __future__ import annotations

import pytest

from hiveweave.config import settings
from hiveweave.tools.bash import _cwd_missing_error, execute_run_command


@pytest.fixture(autouse=True)
def _sandbox_off(monkeypatch):
    """与 test_f4_f7_r11_wiring 同口径：本文件测文案，关沙箱。"""
    monkeypatch.setattr(settings, "acl_sandbox", False)


def _layout(tmp_path, *, foreign_exists: bool):
    root = tmp_path / "proj"
    own = root / ".hiveweave" / "worktrees" / "A133"
    own.mkdir(parents=True)
    if foreign_exists:
        foreign = root / ".hiveweave" / "worktrees" / "A135"
        foreign.mkdir(parents=True)
        (foreign / "keep.txt").write_text("x", encoding="utf-8")
    return own


def test_cwd_missing_error_foreign_tree_exists_reports_boundary(tmp_path):
    own = _layout(tmp_path, foreign_exists=True)
    nested = own / ".hiveweave" / "worktrees" / "A135"
    err = _cwd_missing_error(
        str(nested), ".hiveweave/worktrees/A135", str(own),
    )
    assert "directory exists but is outside your tree boundary" in err
    assert "[worktree A133]" in err  # 自己树归因头（幂等标记，防混排叠加）
    assert "requested: .hiveweave/worktrees/A135" in err
    assert "does not exist" not in err
    assert "read_file / list_files" in err  # 跨树只读指路


def test_cwd_missing_error_nonexistent_foreign_keeps_old_copy(tmp_path):
    own = _layout(tmp_path, foreign_exists=False)  # A999 无此树
    nested = own / ".hiveweave" / "worktrees" / "A999"
    err = _cwd_missing_error(
        str(nested), ".hiveweave/worktrees/A999", str(own),
    )
    assert "Working directory does not exist" in err


def test_cwd_missing_error_plain_missing_keeps_old_copy(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    err = _cwd_missing_error(
        str(ws / "no-such-dir"), "no-such-dir", str(ws),
    )
    assert "Working directory does not exist" in err
    assert "outside your tree boundary" not in err


def test_cwd_missing_error_own_tree_ref_not_treated_as_foreign(tmp_path):
    own = _layout(tmp_path, foreign_exists=False)
    nested = own / ".hiveweave" / "worktrees" / "A133" / "sub"
    err = _cwd_missing_error(
        str(nested), ".hiveweave/worktrees/A133/sub", str(own),
    )
    # 引用自己树不算越界 → 旧文案
    assert "Working directory does not exist" in err


@pytest.mark.asyncio
async def test_run_command_foreign_cwd_reports_boundary(tmp_path):
    own = _layout(tmp_path, foreign_exists=True)
    result = await execute_run_command(
        command="echo hi",
        cwd=".hiveweave/worktrees/A135",
        timeout_ms=5000,
        workspace_path=str(own),
    )
    assert result["success"] is False
    assert result["blocked"] is True
    assert result.get("runner_failed") is True  # F4：命令从未执行
    err = result["error"] or ""
    assert "exists but is outside your tree boundary" in err
    assert "does not exist" not in err
    # with_cwd_display 幂等：不再追加第二行混排 cwd 头
    assert err.count("worktree A133") == 1
