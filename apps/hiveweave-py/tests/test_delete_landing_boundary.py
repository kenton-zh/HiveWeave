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
        targets, indirect, _skipped = _extract_delete_targets(
            ["remove-item", "-Recurse", "-Force", "D:/proj/tmp/x"]
        )
        assert targets == ["D:/proj/tmp/x"]
        assert indirect is False

    def test_path_switch_value_is_target(self):
        targets, _, _skipped = _extract_delete_targets(
            ["remove-item", "-Path", "D:/proj/tmp", "-Recurse"]
        )
        assert targets == ["D:/proj/tmp"]

    def test_unix_recursive_force(self):
        targets, _, _skipped = _extract_delete_targets(["rm", "-rf", "frontend"])
        assert targets == ["frontend"]

    def test_indirect_reference_flagged(self):
        for cmd in (["rm", "-rf", "$TARGET"], ["rm", "-rf", "%TMP%"],
                    ["rm", "-rf", "`$x"], ["rm", "-rf", "$(pwd)/x"]):
            _t, indirect, _skipped = _extract_delete_targets(cmd)
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


# ── P0-1：相对路径删除必须有正确的 cwd 解析基准 ──────────────────────
#
# 审计实测：`resolve_delete_landing_for_agent` 从不传 cwd ⇒ 相对目标按
# **后端进程 CWD** 解析 ⇒ 一律判越界 ⇒ agent「自己建的自己删」全部被误杀
# （且是 deny 不是 ask，连审批机会都没有 —— 比不做落点判定更糟）。
#
# 下面**走真实调用形态** `resolve_delete_landing_for_agent`（审计要求：
# 不能只直调 `resolve_delete_landing` 绕过 cwd 装配），只 mock 授权树事实。


class TestAgentDeleteLandingCwd:
    """P0-1 守卫：经真实调用形态验证 cwd 装配。"""

    @staticmethod
    def _patch_tree(monkeypatch, *, worktree: str):
        """把 agent 的授权树事实钉死为 ``worktree``（其余沿用真实实现）。"""
        from unittest.mock import AsyncMock

        import hiveweave.db.meta as meta_mod
        import hiveweave.services.acl_sandbox.integration as integ
        import hiveweave.services.worktree_review as wr

        monkeypatch.setattr(
            meta_mod, "get_agent_by_id",
            AsyncMock(return_value={"project_id": "p1"}),
        )
        monkeypatch.setattr(
            wr, "agent_worktree_path", AsyncMock(return_value=worktree),
        )
        monkeypatch.setattr(
            integ, "fetch_additional_writable_dirs",
            AsyncMock(return_value=()),
        )

    async def test_relative_delete_without_cwd_is_allowed(
        self, monkeypatch, tmp_path
    ):
        """审计复现点：相对路径删除**不传 cwd** 时不得被误杀。

        boundary 即该 agent 的授权树根、也是它的执行根 ⇒ 缺省回退 boundary
        后 `sub/x` 落在树内 → None（allow）。旧实现（无 cwd 回退、按进程 CWD
        解析）会判越界 —— 本用例即该回归的守卫。
        """
        from hiveweave.services.command_guard import (
            resolve_delete_landing_for_agent,
        )

        wt = tmp_path / ".hiveweave" / "worktrees" / "A044"
        wt.mkdir(parents=True)
        self._patch_tree(monkeypatch, worktree=str(wt))

        v = await resolve_delete_landing_for_agent("rm -rf sub", agent_id="a1")
        assert v is None, f"相对路径删除被误杀：{v}"

    async def test_relative_delete_with_explicit_cwd_is_allowed(
        self, monkeypatch, tmp_path
    ):
        """显式传入执行目录（= 授权树根）→ 同样放行。"""
        from hiveweave.services.command_guard import (
            resolve_delete_landing_for_agent,
        )

        wt = tmp_path / ".hiveweave" / "worktrees" / "A044"
        wt.mkdir(parents=True)
        self._patch_tree(monkeypatch, worktree=str(wt))

        v = await resolve_delete_landing_for_agent(
            "rm -rf sub", agent_id="a1", cwd=str(wt)
        )
        assert v is None

    async def test_relative_escape_is_denied(self, monkeypatch, tmp_path):
        """相对路径逃逸（`../outside`）必须 deny —— 放宽不能变成拆隔离。"""
        from hiveweave.services.command_guard import (
            resolve_delete_landing_for_agent,
        )

        wt = tmp_path / ".hiveweave" / "worktrees" / "A044"
        wt.mkdir(parents=True)
        self._patch_tree(monkeypatch, worktree=str(wt))

        v = await resolve_delete_landing_for_agent(
            "rm -rf ../outside", agent_id="a1"
        )
        assert v is not None
        assert v.blocked is True and v.action == "deny"

    async def test_absolute_outside_is_denied(self, monkeypatch, tmp_path):
        """绝对路径越界仍 deny。"""
        from hiveweave.services.command_guard import (
            resolve_delete_landing_for_agent,
        )

        wt = tmp_path / ".hiveweave" / "worktrees" / "A044"
        wt.mkdir(parents=True)
        self._patch_tree(monkeypatch, worktree=str(wt))

        outside = tmp_path / "other-place"
        outside.mkdir(parents=True)
        v = await resolve_delete_landing_for_agent(
            f"rm -rf {outside.as_posix()}", agent_id="a1"
        )
        assert v is not None and v.action == "deny"


# ── P0-2：`/` 开头的绝对 POSIX 路径不得被当开关吞掉 ──────────────────
#
# 审计实测：`_extract_delete_targets(["rm","-rf","-q","/outside",
# "D:/proj/build"])` 返回 `(["D:/proj/build"], False)` —— `/outside`
# **静默丢弃** ⇒ 混入一个界内目标即整体 allow，真越界目标被放行。


class TestSlashLeadingAbsolutePaths:
    def test_posix_absolute_survives_with_sibling_target(self):
        targets, indirect, _skipped = _extract_delete_targets(
            ["rm", "-rf", "-q", "/etc/passwd", "D:/proj/build"]
        )
        assert indirect is False
        assert "/etc/passwd" in targets, f"绝对路径被当开关吞掉：{targets}"

    def test_posix_absolute_alone_survives(self):
        targets, _, _skipped = _extract_delete_targets(["rm", "-rf", "/outside"])
        assert targets == ["/outside"]

    def test_root_slash_survives(self):
        targets, _, _skipped = _extract_delete_targets(["rm", "-rf", "/"])
        assert "/" in targets

    def test_cmd_single_letter_switches_still_skipped(self):
        """cmd 的 `/s /q /f` 仍是开关 —— 修复不能把开关当路径。"""
        targets, _, _skipped = _extract_delete_targets(
            ["del", "/s", "/q", "D:/proj/build"]
        )
        assert targets == ["D:/proj/build"]

    def test_mixed_cmd_switch_and_absolute_denies(self, ):
        """`del /s /q /outside D:/proj/build`：开关跳过，两个目标都要参与判定。

        这是审计给的原始反例：修复前 `D:/proj/build` 在界内 ⇒ 整体 allow，
        `/outside` 的真越界被静默放行。
        """
        from hiveweave.services.command_guard import resolve_delete_landing

        targets, _, _skipped = _extract_delete_targets(
            ["del", "/s", "/q", "/outside", "D:/proj/build"]
        )
        assert "/outside" in targets
        assert "D:/proj/build" in targets
        v = resolve_delete_landing(
            "del /s /q /outside D:/proj/build",
            boundary_root="D:/proj", temp_dir="D:/proj/.hiveweave/sandbox-temp/a1",
        )
        assert v is not None and v.action == "deny", "混合场景必须 deny"


# ── P0-2（2026-09-12）：空目标两态必须可区分 ────────────────────────
#
# 两者都 fail-closed（都 deny），但 typed code 与文案不同：把「我解析不出来」
# 说成「你没有目标」，执行者会按错误方向重写命令。
# 判据借 DSH `ApprovalOutcome` 的「unavailable ≠ rejected，各有专属文案」。


class TestEmptyTargetsTwoStates:
    def test_unknown_switch_marks_skipped_suspicious(self):
        """未知 `-` 开关 → 解析器如实标记"我跳过了可疑 token"。"""
        targets, indirect, skipped = _extract_delete_targets(
            ["rm", "-rf", "--some-unknown-flag"]
        )
        assert targets == []
        assert indirect is False
        assert skipped is True, "未知开关必须标记为 skipped_suspicious"

    def test_known_flags_do_not_mark_skipped(self):
        """已知开关（-r/-f/-Recurse/-Force）不算可疑 —— 否则误报成灾。"""
        _t, _i, skipped = _extract_delete_targets(
            ["rm", "-rf", "D:/proj/x"]
        )
        assert skipped is False

    def test_no_args_at_all_is_genuinely_no_target(self):
        """裸 `rm` 无参数 → 真·无目标，不是解析失败。"""
        targets, indirect, skipped = _extract_delete_targets(["rm"])
        assert targets == []
        assert indirect is False
        assert skipped is False

    def test_typed_codes_differ_between_the_two_empty_states(self):
        """核心断言：两种空态的 typed code **必须不同**。"""
        from hiveweave.services.command_guard import resolve_delete_landing

        v_no_target = resolve_delete_landing(
            "rm -rf", boundary_root="D:/proj"
        )
        v_unparsed = resolve_delete_landing(
            "rm -rf --unknown-flag", boundary_root="D:/proj"
        )
        assert v_no_target is not None and v_unparsed is not None
        # 两者都是 fail-closed
        assert v_no_target.action == "deny"
        assert v_unparsed.action == "deny"
        # 但 code 不同 —— 这正是 P0-2 的修复点
        assert v_no_target.rule != v_unparsed.rule, (
            "两种空目标共用了同一 typed code —— 诊断信息会是错的（P0-2）"
        )
        assert v_no_target.rule == "__delete_no_target__"
        assert v_unparsed.rule == "__delete_target_unparsed__"

    def test_unparsed_message_tells_you_it_could_not_parse(self):
        from hiveweave.services.command_guard import resolve_delete_landing

        v = resolve_delete_landing("rm -rf --unknown-flag", boundary_root="D:/proj")
        # 文案必须说明是"解析不出来"，而不是"你没有目标"
        assert "解析" in v.reason or "无法归类" in v.reason
