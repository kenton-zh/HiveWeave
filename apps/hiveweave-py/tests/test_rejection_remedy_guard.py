"""批E#3 任务3 守卫：拒绝必带处方（结构化 remedy）。

约定（``tools/result.py`` ``ToolResult.err`` docstring + 各文件头注释）：
拒绝类回执必须带**可执行**的下一步 —— 结构化键 ``remedy``（或等价的可抄
样例段）。本守卫用 AST 扫描已收编文件的**全部**拒绝字典：

1. ``tools/tasks/submit.py`` —— 每个 ``issues.append({...})`` 字典：
   带 ``code`` 键 ⇒ 必须带 ``remedy`` 键。**新增无处方拒绝分支 ⇒ 转红**。
2. ``services/git_worktree/merge_support.py`` —— 每个含 ``reason`` 键的
   字典字面量 ⇒ 必须带 ``remedy`` 键（该文件的 reason 字典全是 merge 拒绝）。
3. 具名拒绝（审计点名的三条）逐一断言处方内容：
   - submit_task filesChanged 磁盘存在性拒绝 → 处方指向 ``git diff
     --name-status main...HEAD`` 真名单；
   - git_worktree_merge branchName 缺参 → ``LEGACY_PARAM_EXAMPLES`` 带形状样例；
   - OUT_OF_BOUNDARY_HINT → 处方指向 read_file 的跨树只读通道。

范围声明：守卫只扫上面两个已收编文件 —— 未收编文件（service_merge.py 等）
的旧拒绝分支仍在分期补处方，收编一个加一个扫描目标。

扫描限界（P2·独立审计 2026-09-26 记录在案）：本守卫**只匹配字典字面量**
直接出现在 ``.append(...)`` 实参位的形态。以下形态**不在扫描内**，靠评审
而非测试兜底：① 先赋值给局部变量再 ``issues.append(var)``；②
``dict(...)`` 调用形态构造；③ 键为 f-string / 非字符串常量的字典。
扩展判据要换 AST visitor（追踪局部变量数据流），成本高于当前收益 ——
显式声明不做，避免"守卫看起来全覆盖"的错觉。
"""
from __future__ import annotations

import ast
from pathlib import Path

import hiveweave.tools  # noqa: F401 — 注册表填充
from hiveweave.tools.base import get_tool_def
from hiveweave.tools.executor import LEGACY_PARAM_EXAMPLES
from hiveweave.tools.result import ToolResult
from hiveweave.util import path_guard

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "hiveweave"


def _dict_keys(d: ast.Dict) -> list[str]:
    return [
        k.value for k in d.keys if isinstance(k, ast.Constant)
        and isinstance(k.value, str)
    ]


def _iter_issue_dicts(tree: ast.AST) -> list[ast.Dict]:
    """``<something>.append({...})`` 里的字典字面量。"""
    out: list[ast.Dict] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    out.append(arg)
    return out


def _iter_literal_dicts(tree: ast.AST) -> list[ast.Dict]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Dict)]


def test_submit_task_issue_dicts_all_carry_remedy():
    """守卫①：submit.py 每个带 code 的 issue 字典必须带 remedy。"""
    src = (_SRC_ROOT / "tools" / "tasks" / "submit.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    missing = []
    for d in _iter_issue_dicts(tree):
        keys = _dict_keys(d)
        if "code" in keys and "remedy" not in keys:
            missing.append(keys)
    assert not missing, (
        f"submit.py 发现 {len(missing)} 个无处方拒绝分支（code={missing}）—— "
        "批E#3 契约：拒绝必带结构化 remedy（一句可执行的下一步）。"
        "新增门禁分支时必须同时给处方。"
    )


def test_merge_support_reject_dicts_all_carry_remedy():
    """守卫②：merge_support.py 每个 reason 字典必须带 remedy。"""
    src = (
        (_SRC_ROOT / "services" / "git_worktree" / "merge_support.py")
        .read_text(encoding="utf-8")
    )
    tree = ast.parse(src)
    missing = []
    for d in _iter_literal_dicts(tree):
        keys = _dict_keys(d)
        if "reason" in keys and "remedy" not in keys:
            missing.append(keys)
    assert not missing, (
        f"merge_support.py 发现 {len(missing)} 个无处方拒绝字典"
        f"（keys={missing}）—— merge 拒绝必须带 remedy。"
    )


def test_files_changed_missing_remedy_points_at_disk_truth():
    """具名拒绝①：filesChanged 磁盘存在性拒绝 → 处方 = git diff 真名单。"""
    src = (_SRC_ROOT / "tools" / "tasks" / "submit.py").read_text(encoding="utf-8")
    assert "git diff --name-status main...HEAD" in src, (
        "files_changed_missing 的处方必须指向磁盘真名单命令 "
        "（以磁盘为准，不要凭记忆列路径）"
    )


def test_git_worktree_merge_missing_param_has_shape_example():
    """具名拒绝②：branchName 缺参 → 回执带正确参数形状样例。"""
    example = LEGACY_PARAM_EXAMPLES.get("git_worktree_merge")
    assert example, "git_worktree_merge 必须在 LEGACY_PARAM_EXAMPLES 里给形状样例"
    assert "branchName" in example
    # 验证路径真把样例拼进缺参回执
    exec_src = (_SRC_ROOT / "tools" / "executor.py").read_text(encoding="utf-8")
    assert "LEGACY_PARAM_EXAMPLES.get(tool_name)" in exec_src


def test_out_of_boundary_hint_carries_readonly_channel_remedy():
    """具名拒绝③：跨树拒绝处方指向 read_file 的跨树只读通道。"""
    hint = path_guard.OUT_OF_BOUNDARY_HINT
    assert "read_file" in hint and "tree=" in hint, (
        "OUT_OF_BOUNDARY_HINT 必须给出跨树只读取物的官方通道"
        "（read_file(filePath=…, tree=…)），不能只剩『找人代跑』"
    )


def test_apply_patch_carries_param_example_in_registry():
    """apply_patch 注册时必须声明正确形状样例（错误回执附带）。"""
    td = get_tool_def("apply_patch")
    assert td is not None
    assert td.param_example, "apply_patch 的 param_example 缺失"
    assert '"patches"' in td.param_example and "filePath" in td.param_example


def test_toolresult_remedy_key_flows_through_to_dict():
    """结构化契约本体：err(remedy=...) 经 to_dict 原样透出。"""
    r = ToolResult.err("nope", fact="bad_args", remedy="do X then retry")
    d = r.to_dict()
    assert d["remedy"] == "do X then retry"
    assert d["success"] is False
