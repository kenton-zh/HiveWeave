"""#15 递归删除审批：判据整体移位到 **acl_sandbox 授权树落点**（fixplan §6 #15）。

判据来源（**不是 DSH**）：DSH 只有「一棵树 + 全共享」，无 per-agent worktree
维度 —— 对「谁在哪个树里」它连问题都没遇到过（fixplan §10）。本仓的授权事实
是 ``services/acl_sandbox/policy.py:54`` 的 ``boundary_root`` + ``:57`` 的
``temp_dir`` + ``extra_dirs``（§10.1 出处表）。

行为断言（非文本子串）：改回「一律 ask / 一律 deny」即打红。
"""

from __future__ import annotations

from hiveweave.services.command_guard import (
    _extract_delete_targets,
    resolve_delete_landing,
)

ROOT = "D:/proj"
TEMP = "D:/proj/.hiveweave/sandbox-temp/agent1"


class TestRemoveItemPathExtraction:
    """实现坑：``_extract_file_paths_from_command`` 的 file_cmds 不含
    ``remove-item`` ⇒ 事故那条 PowerShell 删除命令对敏感/.hiveweave 护栏
    静默 no-op。补齐后必须能提取。"""

    def test_remove_item_target_is_extracted(self):
        from hiveweave.tools.bash import _extract_file_paths_from_command

        got = _extract_file_paths_from_command(
            "Remove-Item -Recurse -Force D:/proj/.env"
        )
        assert "D:/proj/.env" in got

    def test_remove_item_hiveweave_path_is_extracted(self):
        from hiveweave.tools.bash import _extract_file_paths_from_command

        got = _extract_file_paths_from_command(
            "Remove-Item -Recurse -Force D:/proj/.hiveweave/data.db"
        )
        assert any(".hiveweave" in p for p in got), got

    def test_unix_forms_still_extracted(self):
        from hiveweave.tools.bash import _extract_file_paths_from_command

        assert ".env" in _extract_file_paths_from_command("rm -rf .env")
        assert ".env" in _extract_file_paths_from_command("del .env")


class TestDeleteTargetExtraction:
    def test_powershell_flags_skipped(self):
        targets, indirect = _extract_delete_targets(
            ["remove-item", "-Recurse", "-Force", "D:/proj/tmp/x"]
        )
        assert targets == ["D:/proj/tmp/x"]
        assert indirect is False

    def test_path_switch_value_is_target(self):
        targets, _ = _extract_delete_targets(
            ["remove-item", "-Path", "D:/proj/tmp", "-Recurse"]
        )
        assert targets == ["D:/proj/tmp"]

    def test_unix_recursive_force(self):
        targets, _ = _extract_delete_targets(["rm", "-rf", "frontend"])
        assert targets == ["frontend"]

    def test_indirect_reference_flagged(self):
        for cmd in (["rm", "-rf", "$TARGET"], ["rm", "-rf", "%TMP%"],
                    ["rm", "-rf", "`$x"], ["rm", "-rf", "$(pwd)/x"]):
            _t, indirect = _extract_delete_targets(cmd)
            assert indirect is True, cmd


class TestBoundaryLanding:
    """验收（fixplan §6 #15）：R6=0、自建路径删除**零 permission_requests**；
    负样本：越界 Remove-Item -Recurse → deny 且零审批行。"""

    def test_self_created_temp_delete_is_allowed(self):
        """agent 自建私有 temp 下的删除 → 落点在授权树内 → allow（None=无意见）。"""
        v = resolve_delete_landing(
            f"Remove-Item -Recurse -Force {TEMP}/scratch",
            boundary_root=ROOT, temp_dir=TEMP,
        )
        assert v is None  # 无意见 = 交给 allow，不进 ask

    def test_delete_inside_worktree_is_allowed(self):
        v = resolve_delete_landing(
            "rm -rf D:/proj/feature/src/tmp",
            boundary_root=ROOT, temp_dir=TEMP,
        )
        assert v is None

    def test_extra_dir_is_inside(self):
        v = resolve_delete_landing(
            "Remove-Item -Recurse -Force D:/shared-cache/x",
            boundary_root=ROOT, temp_dir=TEMP, extra_dirs=("D:/shared-cache",),
        )
        assert v is None

    def test_out_of_boundary_is_denied_not_asked(self):
        """越界 → **deny**（fail-closed），不是 ask（不挂起等审批）。"""
        v = resolve_delete_landing(
            "Remove-Item -Recurse -Force D:/other-agent-worktree",
            boundary_root=ROOT, temp_dir=TEMP,
        )
        assert v is not None
        assert v.blocked is True
        assert v.action == "deny"
        assert v.action != "ask"

    def test_out_of_boundary_hint_points_to_delete_directory(self):
        v = resolve_delete_landing(
            "Remove-Item -Recurse -Force D:/elsewhere",
            boundary_root=ROOT, temp_dir=TEMP,
        )
        assert v is not None and "delete_directory" in v.reason

    def test_indirect_reference_is_denied(self):
        v = resolve_delete_landing(
            "Remove-Item -Recurse -Force $env:TARGET",
            boundary_root=ROOT, temp_dir=TEMP,
        )
        assert v is not None and v.action == "deny"

    def test_parent_sibling_is_out_of_boundary(self):
        """前缀相似但不是子路径（D:/proj2 vs D:/proj）必须判越界。"""
        v = resolve_delete_landing(
            "rm -rf D:/proj2/data", boundary_root=ROOT, temp_dir=TEMP,
        )
        assert v is not None and v.action == "deny"

    def test_no_boundary_means_no_opinion(self):
        """无边界事实 → 不发明规则（退回规则表既有行为）。"""
        assert resolve_delete_landing("rm -rf /tmp/x") is None

    def test_non_delete_command_untouched(self):
        assert resolve_delete_landing(
            "python -m pytest tests/", boundary_root=ROOT, temp_dir=TEMP,
        ) is None

    def test_compound_command_checks_every_subcommand(self):
        """复合命令里任一子命令越界即 deny（不能只看第一条）。"""
        v = resolve_delete_landing(
            f"Remove-Item -Recurse -Force {TEMP}/a && rm -rf D:/outside",
            boundary_root=ROOT, temp_dir=TEMP,
        )
        assert v is not None and v.action == "deny"
