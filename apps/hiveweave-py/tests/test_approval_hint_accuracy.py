"""#6 守卫（2026-09-13 TEST_DSH_55 报告 §3 第 6 条）：审批文案不得断言未知事实。

现场两条：
1. 超时文案断言「**项目的无人值守运行方式是既定方式**，不是审核人失职」——
   而本项目 `unattended_mode` **并未登记**（`global_settings` 的 5 个 key 里
   没有它，见 issue-6 §1.3）⇒ 断言与事实相反。
2. 无人值守分支（`is_unattended_mode` 为真）**从未发起审批请求**，
   却复用了描述「超时 120s、未批准也未拒绝」的 `APPROVAL_TIMEOUT_HINT`
   ⇒ 文案与实际不符 = 给模型错误事实。

两条都由「文案只陈述本分支确知的事」修掉。本文件钉住它们。
"""
from __future__ import annotations

import ast
from pathlib import Path

from hiveweave.services.approval import (
    APPROVAL_TIMEOUT_HINT,
    UNATTENDED_DENY_HINT,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "hiveweave"


def test_timeout_hint_does_not_assert_unattended_mode():
    """超时文案不得断言「运行方式是既定的」——平台此时无从判断。"""
    assert "既定方式" not in APPROVAL_TIMEOUT_HINT
    # 但必须保留**诚实**表述（承认不知道）与既有可区分性锚点：
    # `tests/test_guard_ask_approval.py` 锚定后两个。
    assert "无法观测" in APPROVAL_TIMEOUT_HINT
    assert "approval_channel_unavailable" in APPROVAL_TIMEOUT_HINT
    assert "审批请求超时" in APPROVAL_TIMEOUT_HINT


def test_unattended_hint_does_not_claim_timeout():
    """无人值守文案不得**声称发生过超时**——该分支从未发起过审批请求。

    注意断言的是「声称」形态，不是「超时」二字：文案里「本条**未经** 120s
    超时」是**否定**表述，必须允许（它正是要澄清的事实）。
    """
    assert "审批请求超时" not in UNATTENDED_DENY_HINT
    assert "未批准也未拒绝" not in UNATTENDED_DENY_HINT
    assert "超时 %d" not in UNATTENDED_DENY_HINT
    # 必须如实说明是「不发起、不等待」
    assert "不发起" in UNATTENDED_DENY_HINT
    assert "不等待" in UNATTENDED_DENY_HINT


def _unattended_branch_names(path: Path) -> list[str]:
    """AST：`if await is_unattended_mode(...)` 分支体内引用的常量名。

    用 AST 而非文本子串——文本会被注释/docstring 糊过去（本仓库纪律）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Await)
            and isinstance(test.value, ast.Call)
            and getattr(test.value.func, "id", None) == "is_unattended_mode"
        ):
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Name) and sub.id.isupper():
                    found.append(sub.id)
    return found


def test_unattended_branches_use_dedicated_hint():
    """两处无人值守分支必须用专用文案，**不得**复用超时文案。

    阳性对照：把任一处改回 `APPROVAL_TIMEOUT_HINT` → 本用例转红。
    """
    for rel in ("tools/executor.py", "tools/pipeline.py"):
        names = _unattended_branch_names(_SRC / rel)
        assert "UNATTENDED_DENY_HINT" in names, f"{rel} 未使用专用文案"
        assert "APPROVAL_TIMEOUT_HINT" not in names, (
            f"{rel} 的无人值守分支仍复用超时文案 —— 它从未发起过审批请求，"
            "却会声称「超时 120s」（报告 §3 第 6 条）"
        )


def test_real_timeout_branches_still_use_timeout_hint():
    """反向守卫：**真超时**分支必须继续用超时文案（别把改动做成一刀切）。

    `command_guard.py` 的 `except PermissionTimeout` 与 executor/pipeline 的
    同指纹分支都确实超时过 —— 它们保留 `APPROVAL_TIMEOUT_HINT` 是对的。
    """
    src = (_SRC / "services/command_guard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    kept = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and getattr(node.type, "id", "") == "PermissionTimeout":
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(sub, ast.Name) and sub.id == "APPROVAL_TIMEOUT_HINT":
                    kept = True
    assert kept, "真超时分支（PermissionTimeout）必须继续用 APPROVAL_TIMEOUT_HINT"
    for rel in ("tools/executor.py", "tools/pipeline.py"):
        body = (_SRC / rel).read_text(encoding="utf-8")
        assert "approval_timeout_marked" in body, (
            f"{rel} 的同指纹超时分支应保留（它确实超时过）"
        )
