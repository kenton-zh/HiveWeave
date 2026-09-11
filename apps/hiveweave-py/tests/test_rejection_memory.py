"""批次 2a「拒绝无记忆」：RETRY 出路标记 + 同因连拒计数 + edit 版本戳。

45 轮实锤：账本拒 2×/50 秒同文案、降级终验拒 3×、edit_file 同文件
4 败夹 3 成——每次拒绝都是现拼文案，agent 看不到「已第 N 次」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hiveweave.services import rejection_memory as rm
from hiveweave.tools.file import clear_file_versions_for_tests, record_file_version
from hiveweave.tools.patch import _apply_single


@pytest.fixture(autouse=True)
def _clean_rejection_state():
    rm.reset_for_tests()
    clear_file_versions_for_tests()
    yield
    rm.reset_for_tests()
    clear_file_versions_for_tests()


# ── 同因连拒计数 ──────────────────────────────────────────────


def test_first_rejection_not_annotated_second_is():
    text = "SUBMIT REJECTED (degraded verify): something long enough to pass signature threshold"
    assert rm.annotate_repeat_rejection("submit_task", text) == ""
    second = rm.annotate_repeat_rejection("submit_task", text)
    assert "[REPEAT REJECTION #2 via submit_task]" in second
    assert "勿原样重试" in second
    assert rm.rejection_count(text) == 2


def test_different_signatures_count_independently():
    a = "commit_turn REJECTED (synchronous gate): 1) [CREATOR_MUST_MERGE] long text aaaa"
    b = "commit_turn REJECTED (synchronous gate): 1) [UNREPLIED_ASKS] long text bbbb"
    assert rm.annotate_repeat_rejection("commit_turn", a) == ""
    assert rm.annotate_repeat_rejection("commit_turn", b) == ""
    assert "[REPEAT REJECTION #2" in rm.annotate_repeat_rejection("commit_turn", a)
    assert rm.rejection_count(a) == 2
    assert rm.rejection_count(b) == 1


def test_short_text_not_counted():
    assert rm.annotate_repeat_rejection("edit_file", "too short") == ""
    assert rm.rejection_count("too short") == 0


# ── edit_file：oldString not found 的 RETRY 标记 + 连拒标注 ──────────


def _write(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


def test_edit_not_found_has_retry_marker(tmp_path):
    _write(tmp_path, "a.txt", "hello world\n")
    patch = {
        "op": "update",
        "filePath": "a.txt",
        "oldString": "no such line here",
        "newString": "x",
    }
    msg = _apply_single(patch, str(tmp_path))
    assert "oldString not found" in msg
    assert "RETRY[action=reread_file_then_reapply" in msg


def test_edit_not_found_repeat_gets_annotation(tmp_path):
    _write(tmp_path, "a.txt", "hello world\n")
    patch = {
        "op": "update",
        "filePath": "a.txt",
        "oldString": "no such line here",
        "newString": "x",
    }
    first = _apply_single(patch, str(tmp_path))
    assert "[REPEAT REJECTION" not in first
    second = _apply_single(patch, str(tmp_path))
    assert "[REPEAT REJECTION #2 via edit_file]" in second


def test_edit_intervening_success_does_not_reset_counter(tmp_path):
    """同一坏 oldString 重复=重复拒绝——中间夹成功编辑不重置计数。"""
    _write(tmp_path, "a.txt", "alpha\nbeta\n")
    bad = {
        "op": "update",
        "filePath": "a.txt",
        "oldString": "nope",
        "newString": "x",
    }
    _apply_single(bad, str(tmp_path))
    good = {
        "op": "update",
        "filePath": "a.txt",
        "oldString": "alpha",
        "newString": "ALPHA",
    }
    assert "Updated" in _apply_single(good, str(tmp_path))
    again = _apply_single(bad, str(tmp_path))
    assert "oldString not found" in again
    assert "[REPEAT REJECTION #2 via edit_file]" in again


# ── 版本戳：外部改动 → stale view 早拒 ────────────────────────


def test_stale_view_rejected_after_external_change(tmp_path):
    f = _write(tmp_path, "b.txt", "v1\n")
    record_file_version(f)  # 模拟 read_file 成功访问
    f.write_text("v2 changed by someone else\n", encoding="utf-8")
    patch = {
        "op": "update",
        "filePath": "b.txt",
        "oldString": "v1",
        "newString": "x",
    }
    msg = _apply_single(patch, str(tmp_path))
    assert "stale view" in msg
    assert "RETRY[action=reread_file_then_reapply]" in msg
    # 早拒发生 → 文件未被改动
    assert f.read_text(encoding="utf-8").startswith("v2")


def test_stale_view_carries_typed_code_not_fake_evidence(tmp_path):
    """#10（2026-09-11）：stale 回执**只给动作 + typed code，不给版本证据**。

    原实现打印 `size {known}B → {cur}B` —— 但版本元组是 `(mtime_ns, size)`，
    只印 size ⇒ 同长度内容改动时打印 `size 1077B → 1077B`（**两个相等的数字**）。
    那不是证据，是**伪造证据**：声称"变了"却给出看不出变化的量。

    ⇒ 现断言两件事：
    1. **typed code `FS_NOT_OBSERVED` 在**（可机检、可路由，DSH 风格）；
    2. **不再出现 `size ... B → ... B` 这种伪差异**（含相等数字的形态）。
    """
    f = _write(tmp_path, "same_len.txt", "aaaa\n")
    record_file_version(f)
    # 同长度内容改动 —— 正是原 bug 打印"两个相等 size"的场景
    f.write_text("bbbb\n", encoding="utf-8")
    msg = _apply_single(
        {"op": "update", "filePath": "same_len.txt",
         "oldString": "bbbb", "newString": "x"},
        str(tmp_path),
    )
    assert "FS_NOT_OBSERVED" in msg, f"缺少 typed code：{msg!r}"
    assert "RETRY[action=reread_file_then_reapply]" in msg
    assert "size" not in msg, (
        f"不得再打印版本证据（同长度改动会给出两个相等数字）：{msg!r}"
    )


def test_edit_after_read_allowed_and_updates_version(tmp_path):
    f = _write(tmp_path, "c.txt", "one two\n")
    record_file_version(f)
    patch = {
        "op": "update",
        "filePath": "c.txt",
        "oldString": "one",
        "newString": "ONE",
    }
    assert "Updated" in _apply_single(patch, str(tmp_path))
    # 刚写完立刻再编辑：不误伤（写后已刷新版本）
    patch2 = {
        "op": "update",
        "filePath": "c.txt",
        "oldString": "ONE two",
        "newString": "ONE 2",
    }
    assert "Updated" in _apply_single(patch2, str(tmp_path))
    assert f.read_text(encoding="utf-8") == "ONE 2\n"


def test_never_read_file_not_stale_blocked(tmp_path):
    """无访问记录（别的工具/会话创建）→ 不拦，走正常匹配。"""
    _write(tmp_path, "d.txt", "content here\n")
    patch = {
        "op": "update",
        "filePath": "d.txt",
        "oldString": "content",
        "newString": "CONTENT",
    }
    assert "Updated" in _apply_single(patch, str(tmp_path))
    assert "stale view" not in _apply_single(
        {**patch, "oldString": "CONTENT", "newString": "content"},
        str(tmp_path),
    )


# ── 降级终验拒（services 层集成）见 test_verdict_gate.py 追加段 ──────
