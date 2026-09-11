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


def test_same_length_same_tick_rewrite_is_still_detected(tmp_path, monkeypatch):
    """根因回归（2026-09-12）：`(mtime_ns, size)` 同刻度同长度改写必须仍被检出。

    **这是原来那个 flake 的根因**，而它不是 flake：实测「连续无间改写 100 次」
    两元组漏检 **56-75%**（多次复测 56/66/72/74/75；`sha256` 对照 **0/200**）。
    本机 mtime 刻度约 1ms，两次连续写几乎必然跨不过刻度 + 同长度 ⇒ 元组
    逐字节相同。**引用时给区间，别拿单点 56% 当常数**（那是最低的样本）。

    ⚠ 本用例**必须冻结 stat**，不能靠"碰运气撞同刻度"：
    首版用循环跑 30 次连续改写，实测只有 **2/50** 轮真的落在同一刻度
    （`record_file_version` 自己在两次写之间做了文件 I/O，把那个毫秒吃掉了），
    结果把内容摘要比较整段删掉它**照样全绿** —— 是一张假的"已验证"证书。
    最紧的循环形态也只到 13/100，仍然赌概率。

    ⇒ 改为 **monkeypatch `_stat_segments` 返回冻结的 (mtime_ns, size)**：
    前两段必然相同（这正是要测的分支），而内容**真的变了**。
    此时唯一能救回"检出"的只有第三段内容摘要 —— 删掉它就必然红。
    """
    from hiveweave.tools import file as file_mod

    f = _write(tmp_path, "tick.txt", "aaaa\n")
    frozen = file_mod._stat_segments(f)

    def _frozen_stat(_path):
        return frozen  # mtime 与 size 都冻住：前两段永远"看起来没变"

    # record 与 check 走同一把尺 → 两侧都冻，模拟"同一刻度内的改写"
    monkeypatch.setattr(file_mod, "_stat_segments", _frozen_stat)

    record_file_version(f)
    f.write_text("bbbb\n", encoding="utf-8")  # 同长度，内容真变

    assert file_mod.check_file_version(f) == "FS_NOT_OBSERVED", (
        "前两段相同但内容已变 —— 必须靠第三段内容摘要检出；"
        "此处返回 None 说明版本戳退回了不比较内容的形态（陈旧检测形同虚设）"
    )


def test_same_tick_branch_really_reads_content(tmp_path, monkeypatch):
    """护栏的护栏：证明上面那条用例**真的走到了第三段**，不是碰巧前两段就不同。

    直接验"前两段被冻住"这一前提成立 —— 否则上面那条会在错误的位置变绿/变红。
    """
    from hiveweave.tools import file as file_mod

    f = _write(tmp_path, "frozen.txt", "aaaa\n")
    frozen = file_mod._stat_segments(f)

    monkeypatch.setattr(file_mod, "_stat_segments", lambda _p: frozen)
    record_file_version(f)
    before = file_mod._content_digest(f)
    f.write_text("bbbb\n", encoding="utf-8")
    after = file_mod._content_digest(f)

    assert file_mod._stat_segments(f) == frozen, "stat 未被冻住，用例前提失效"
    assert before != after, "内容摘要对同长度改写不敏感 —— 第三段也是假的"


def test_unchanged_file_is_not_flagged(tmp_path):
    """反向保护：真的没改就不能报 stale（否则每次都逼重读）。"""
    from hiveweave.tools.file import check_file_version

    f = _write(tmp_path, "same.txt", "content\n")
    record_file_version(f)
    assert check_file_version(f) is None
    # 连读多次仍然稳定
    for _ in range(5):
        assert check_file_version(f) is None


def test_record_and_check_use_same_token_shape(tmp_path):
    """两端必须用同一把尺 —— 登记侧不算摘要则比对失去判据。"""
    from hiveweave.tools import file as file_mod

    f = _write(tmp_path, "shape.txt", "xyz\n")
    record_file_version(f)
    key = str(Path(f).resolve())
    entry = file_mod._version_cache.get(key)
    assert entry is not None
    assert len(entry) == 3, "版本戳必须是 (mtime_ns, size, digest) 三段"
    assert entry[2], "登记侧的内容摘要不能为空 —— 否则比对无意义"


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
