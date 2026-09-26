"""批E#3 工具契约单元测试（returncode 三态 / apply_patch 形状 / grep include /
跨树只读通道）。

上游依据：
- pi ``packages/ai/src/utils/validation.ts``（HEAD 2b0a123de，行号现测
  formatValidationPath ~:282-293）—— 校验错误逐条 instancePath + 给形状，
  不做 expected-vs-provided 值 diff。
- DSH ``docs/defensive-patterns.md``（HEAD 477b4f420）——「Report orthogonal
  outcomes independently」：退出码是独立事实，绝不伪造（补 0 = 假成功）。
- DSH ``packages/fs/tool-fs-search/src/grep.ts:67-78`` validateInclude ——
  非法 include 显式拒绝并给 ``{a,b}`` 替代写法。
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from hiveweave.services.acl_sandbox.spawn import LongRunningJob
from hiveweave.tools import pipeline
import hiveweave.tools  # noqa: F401 — 注册表填充
from hiveweave.tools.bash import _ConfinedDevProc
from hiveweave.tools.grep import execute_grep, validate_include_shape
from hiveweave.tools.patch import ApplyPatchParams, apply_patch


# ── 任务1：_ConfinedDevProc.returncode 三态 ────────────────────────────


class _JobWithCode:
    """沙箱 job 替身：可编排「运行中 / 已退出带码 / 已退出码未知」。"""

    pid = 4242

    def __init__(self, *, exited: bool, code: int | None, has_getter: bool = True):
        self._exited = exited
        self._code = code
        if has_getter:
            self.exit_code = lambda: (code if exited else None)

    def is_exited(self) -> bool:
        return self._exited

    def terminate(self) -> None:
        pass


def test_returncode_none_while_running():
    """运行中 ⇒ None（不伪造退出码）。"""
    proc = _ConfinedDevProc(_JobWithCode(exited=False, code=None))
    assert proc.returncode is None
    assert proc.poll() is None


def test_returncode_real_value_after_exit():
    """已退出 ⇒ 真实退出码（非 0 的失败码必须原样透出）。"""
    proc = _ConfinedDevProc(_JobWithCode(exited=True, code=3))
    assert proc.returncode == 3
    assert proc.poll() == 3


def test_returncode_zero_after_clean_exit_is_real_zero():
    """干净退出 ⇒ 0 是**真实** 0（与旧 shim「一律返回 0」语义不同源）。"""
    proc = _ConfinedDevProc(_JobWithCode(exited=True, code=0))
    assert proc.returncode == 0


def test_returncode_never_fabricates_zero_when_job_has_no_exit_code():
    """job 不提供 exit_code（旧替身/异常）⇒ None，**绝不补 0**（假成功）。"""
    proc = _ConfinedDevProc(_JobWithCode(exited=True, code=None, has_getter=False))
    assert proc.returncode is None
    assert proc.poll() is None


def test_returncode_none_when_getter_raises():
    """退出码获取抛异常 ⇒ 如实回 None（未知 ≠ 成功）。"""
    job = _JobWithCode(exited=True, code=None)
    job.exit_code = lambda: (_ for _ in ()).throw(RuntimeError("handle gone"))
    proc = _ConfinedDevProc(job)
    assert proc.returncode is None


def test_long_running_job_exposes_exit_code():
    """LongRunningJob 必须有真实退出码通道（shim 委托目标，@property 面）。"""
    assert hasattr(LongRunningJob, "exit_code")
    # 未知/句柄失效态：不抛、回 None（Windows/非 Windows 均成立）
    job = LongRunningJob.__new__(LongRunningJob)
    job._spawned = types.SimpleNamespace(h_proc=None, pid=1)
    assert job.exit_code is None


@pytest.mark.asyncio
async def test_dev_server_early_exit_unknown_code_is_outcome_unknown(tmp_path):
    """早退分支：退出码已知 ⇒ 按事实区分成败；不可知 ⇒ outcome_unknown 出口。

    （批E#3 抽出的可测接缝 ``_early_exit_receipt_or_none``；全链路接线在
    test_dev_server_sandbox_wiring.py，AST 调用点守卫在
    test_devserver_exit_semantics.py。）
    """
    from hiveweave.tools.dev_server_tools import _early_exit_receipt_or_none

    log_path = tmp_path / "dev-server-3100.log"
    log_file = open(log_path, "wb")  # noqa: SIM115 — 测试句柄，用完即关

    running = _ConfinedDevProc(_JobWithCode(exited=False, code=None))
    assert _early_exit_receipt_or_none(
        running, "cmd", log_path, {}, log_file
    ) is None, "仍在跑 ⇒ None（继续健康探测）"

    exited_unknown = _ConfinedDevProc(_JobWithCode(exited=True, code=None))
    r_unknown = _early_exit_receipt_or_none(
        exited_unknown, "cmd", log_path, {"enforcement": "confined"}, log_file
    )
    assert r_unknown is not None and r_unknown.success is False
    assert r_unknown.fact == "outcome_unknown", (
        "退出码不可知 ⇒ outcome_unknown（不许把未知谎报成 0=成功或 N=失败）"
    )
    assert "exit code could not be determined" in (r_unknown.error or "")
    assert r_unknown.to_dict().get("enforcement") == "confined"
    log_file.close()

    # 已知码路径不回归：0 ⇒ ok 事实回执；非 0 ⇒ command_failed。
    log_file2 = open(log_path, "wb")  # noqa: SIM115
    ok = _early_exit_receipt_or_none(
        _ConfinedDevProc(_JobWithCode(exited=True, code=0)),
        "cmd", log_path, {}, log_file2,
    )
    assert ok is not None and ok.success is True and ok.to_dict()["exit_code"] == 0
    bad = _early_exit_receipt_or_none(
        _ConfinedDevProc(_JobWithCode(exited=True, code=1)),
        "cmd", log_path, {}, log_file2,
    )
    assert bad is not None and bad.success is False and bad.fact == "command_failed"
    log_file2.close()


# ── 任务2：apply_patch 参数形状（同形 + extra=forbid + 样例） ────────────


def test_patch_item_accepts_same_shape_as_top_level():
    """数组项与顶层直传**同形**：camelCase / snake_case / 常见变体全收。"""
    m = ApplyPatchParams(**{"patches": [
        {"op": "update", "file": "a.py", "old_str": "x", "new_str": "y"},
    ]})
    assert m.patches[0].file_path == "a.py"
    assert m.patches[0].old_string == "x"

    m2 = ApplyPatchParams(**{"patches": [
        {"op": "add", "filePath": "b.py", "content": "hi"},
    ]})
    assert m2.patches[0].file_path == "b.py"

    m3 = ApplyPatchParams(**{"patches": [
        {"filePath": "c.py", "oldString": "x", "newString": "y"},
    ]})
    assert m3.patches[0].file_path == "c.py"
    assert m3.patches[0].op == "update"  # op 推断照常


def test_patch_item_missing_filepath_reports_instance_path():
    """缺 filePath ⇒ 逐条 instancePath（点号形态进 error、方括号进 violations）。"""
    params, error, violations = (
        hiveweave.tools.base.get_tool_def("apply_patch")
        .validate_detailed({"patches": [{"op": "add"}]})
    )
    assert params is None and error
    assert "patches.0.filePath" in error  # pi 式逐条 instancePath
    assert any(v["path"] == "patches[0].filePath" for v in violations)


def test_extra_fields_are_forbidden():
    """extra=forbid：未知键显式报错（顶层与数组项两处），不再静默吞。"""
    with pytest.raises(Exception) as ei:
        ApplyPatchParams(**{"patches": [{"op": "add", "filePath": "c.py"}], "nonsense": 1})
    assert "nonsense" in str(ei.value)
    with pytest.raises(Exception) as ei2:
        ApplyPatchParams(**{"patches": [{"op": "add", "filePath": "c.py", "bogus": 2}]})
    assert "patches.0.bogus" in str(ei2.value)


@pytest.mark.asyncio
async def test_pipeline_parameter_error_receipt_carries_example():
    """参数校验失败回执直接给**可抄的正确形状 JSON 样例**（pi 模式）。"""
    result = await pipeline.execute_registered_tool(
        tool_name="apply_patch",
        raw_args={"patches": [{"op": "add"}]},
        agent_id="a1",
        workspace_path=".",
        permission=None,
        approval=None,
        ctx=None,
    )
    assert result["success"] is False
    err = result["error"]
    assert "Parameter error in 'apply_patch'" in err
    assert "patches.0.filePath" in err
    assert "Correct shape example (copy this)" in err
    assert '"patches"' in err and '"filePath"' in err


@pytest.mark.asyncio
async def test_model_dump_feeds_inner_apply(tmp_path: Path):
    """pydantic 模型 dump 出的 dict 能被内层 apply_patch 正常消费（别名链）。"""
    f = tmp_path / "a.txt"
    f.write_text("hello x world\n", encoding="utf-8")
    m = ApplyPatchParams(**{"patches": [
        {"op": "update", "file": "a.txt", "old_str": "x", "new_str": "y"},
    ]})
    res = await apply_patch(
        patches=[p.model_dump(exclude_none=True) for p in m.patches],
        workspace_path=str(tmp_path),
    )
    assert res["success"] is True, res
    assert "hello y world" in f.read_text(encoding="utf-8")


# ── 任务6：grep include 形状校验 + 契约描述 ────────────────────────────


def test_include_shape_valid_globs_pass():
    assert validate_include_shape("*.py") is None
    # 花括号内逗号 = alternation，不是列表（DSH 原话）
    assert validate_include_shape("*.{ts,tsx}") is None


def test_include_shape_rejects_negation_and_comma_lists_with_remedy():
    err_neg = validate_include_shape("!.py")
    assert err_neg and "negated" in err_neg
    err_comma = validate_include_shape("*.py,*.js")
    assert err_comma and "{a,b}" in err_comma, (
        "拒绝必须带替代写法（DSH validateInclude 原话：use {a,b} alternation instead）"
    )
    err_blank = validate_include_shape("   ")
    assert err_blank and "non-empty" in err_blank


@pytest.mark.asyncio
async def test_execute_grep_rejects_bad_include_before_search(tmp_path: Path):
    res = await execute_grep(
        pattern="x", path="", include="*.py,*.js",
        workspace_path=str(tmp_path),
    )
    assert res["success"] is False
    assert "{a,b}" in res["error"]


def test_grep_tool_description_states_contract():
    """工具描述写明 DSH 契约：纯正则 / 不经 shell / 不要加引号 / \\( 字面括号。"""
    from hiveweave.tools.base import get_tool_def

    desc = get_tool_def("grep").description
    assert "no shell" in desc.lower() or "NO shell" in desc
    assert "\\(" in desc


# ── 任务5：跨树只读取物通道 ────────────────────────────────────────────


def _make_project_with_tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    tree = root / ".hiveweave" / "worktrees" / "A461-b"
    tree.mkdir(parents=True)
    (tree / "src").mkdir()
    (tree / "src" / "x.py").write_text("value = 42\n", encoding="utf-8")
    (root / "main.txt").write_text("main\n", encoding="utf-8")
    return root, tree


@pytest.mark.asyncio
async def test_cross_tree_read_channel_reads_and_names_source_tree(tmp_path: Path):
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    res = await read_file(
        file_path="src/x.py", offset=0, limit=100,
        workspace_path=str(root), project_root=str(root),
        tree="A461-b",
    )
    assert res["success"] is True, res
    assert "value = 42" in res["output"]
    assert "worktree A461-b" in res["output"], "回执必须注明来源树"


@pytest.mark.asyncio
async def test_cross_tree_read_rejects_unknown_tree_with_remedy(tmp_path: Path):
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    res = await read_file(
        file_path="src/x.py", offset=0, limit=100,
        workspace_path=str(root), project_root=str(root),
        tree="NOPE-9",
    )
    assert res["success"] is False
    assert "限本项目" in res["error"]


@pytest.mark.asyncio
async def test_cross_tree_read_rejects_escape_and_hiveweave(tmp_path: Path):
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    esc = await read_file(
        file_path="../../main.txt", offset=0, limit=10,
        workspace_path=str(root), project_root=str(root),
        tree="A461-b",
    )
    assert esc["success"] is False and "escapes worktree" in esc["error"]
    hw = await read_file(
        file_path=".hiveweave/data.db", offset=0, limit=10,
        workspace_path=str(root), project_root=str(root),
        tree="A461-b",
    )
    assert hw["success"] is False and "跨树只读通道只覆盖项目文件" in hw["error"]


# ── P0（独立审计 2026-09-26）：tree= 侧逃逸闸（负例 ≥5 + 正例不回归）────


@pytest.mark.asyncio
async def test_p0_tree_rejects_absolute_tree_path(tmp_path: Path):
    """负例①：tree= 项目外绝对目录（审计实测形态）⇒ 拒绝，读不到。"""
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    res = await read_file(
        file_path="secret.txt", offset=0, limit=10,
        workspace_path=str(root), project_root=str(root),
        tree=str(outside),
    )
    assert res["success"] is False
    assert "单个 worktree 目录名" in res["error"]
    assert "secret" not in res.get("output", "")


@pytest.mark.asyncio
async def test_p0_tree_rejects_dotdot_tree(tmp_path: Path):
    """负例②：tree= 含 ``..``（向上越级）⇒ 拒绝。"""
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    for bad in ("..", "A461-b/../..", "A461-b/../outside"):
        res = await read_file(
            file_path="src/x.py", offset=0, limit=10,
            workspace_path=str(root), project_root=str(root),
            tree=bad,
        )
        assert res["success"] is False, f"tree={bad!r} 必须被拒"
        assert "单个 worktree 目录名" in res["error"], f"tree={bad!r}: {res['error']}"


@pytest.mark.asyncio
async def test_p0_tree_rejects_backslash_separator(tmp_path: Path):
    """负例③：tree= 含反斜杠分隔符（Windows 形态）⇒ 拒绝。"""
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    res = await read_file(
        file_path="src/x.py", offset=0, limit=10,
        workspace_path=str(root), project_root=str(root),
        tree="A461-b\\..\\..\\outside",
    )
    assert res["success"] is False
    assert "单个 worktree 目录名" in res["error"]


@pytest.mark.asyncio
async def test_p0_tree_rejects_drive_letter(tmp_path: Path):
    """负例④：tree= 盘符形态（C: / C:\\…）⇒ 拒绝。"""
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    for bad in ("C:", "c:", "C:\\Windows"):
        res = await read_file(
            file_path="src/x.py", offset=0, limit=10,
            workspace_path=str(root), project_root=str(root),
            tree=bad,
        )
        assert res["success"] is False, f"tree={bad!r} 必须被拒"
        assert "单个 worktree 目录名" in res["error"]


@pytest.mark.asyncio
async def test_p0_tree_rejects_resolve_escape_simulated_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """负例⑤：resolve 后偏移出 worktrees 根（模拟树内符号链接）⇒ 拒绝。

    审计建议的 mock 方式：monkeypatch ``Path.resolve``，让 ``…/worktrees/
    A461-b`` 的 resolve 结果落到项目外目录 —— 旧实现拿逃逸后的根自比恒过，
    新实现锚定 worktrees 根必须拒绝。
    """
    import pathlib

    from hiveweave.tools import file as file_mod
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    real_resolve = pathlib.Path.resolve

    def fake_resolve(self: pathlib.Path, strict: bool = False) -> pathlib.Path:
        resolved = real_resolve(self, strict=strict)
        if (
            resolved.name == "A461-b"
            and ".hiveweave" in resolved.parts
            and "worktrees" in resolved.parts
        ):
            return real_resolve(outside)  # 模拟符号链接把树根指向项目外
        return resolved

    monkeypatch.setattr(pathlib.Path, "resolve", fake_resolve)
    res = await read_file(
        file_path="src/x.py", offset=0, limit=10,
        workspace_path=str(root), project_root=str(root),
        tree="A461-b",
    )
    monkeypatch.undo()
    assert res["success"] is False
    assert "worktrees/" in res["error"], res["error"]
    assert "secret" not in res.get("output", "")


@pytest.mark.asyncio
async def test_p0_tree_positive_still_reads(tmp_path: Path):
    """正例不回归：合法单名 tree= 读本树文件照常（且标注来源树）。"""
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    res = await read_file(
        file_path="src/x.py", offset=0, limit=10,
        workspace_path=str(root), project_root=str(root),
        tree="A461-b",
    )
    assert res["success"] is True, res
    assert "value = 42" in res["output"]
    assert "worktree A461-b" in res["output"]


@pytest.mark.asyncio
async def test_p2_tree_and_shared_path_are_mutually_exclusive(tmp_path: Path):
    """P2：tree= 撞平台共享路径（reports/** 等）⇒ 显式报错，不静默忽略。"""
    from hiveweave.tools.file import read_file

    root, _tree = _make_project_with_tree(tmp_path)
    res = await read_file(
        file_path=".hiveweave/reports/t1/report.md", offset=0, limit=10,
        workspace_path=str(root), project_root=str(root),
        tree="A461-b",
    )
    assert res["success"] is False
    assert "互斥" in res["error"] and "tree=" in res["error"]


def test_write_side_has_no_cross_tree_param():
    """窄通道只读：写侧没有任何跨树参数（write_file 无 tree）。"""
    from hiveweave.tools.file import WriteFileParams

    assert "tree" not in WriteFileParams.model_fields
