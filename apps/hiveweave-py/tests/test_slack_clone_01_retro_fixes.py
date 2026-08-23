"""slack-clone_01 RETRO 修复回归测试.

覆盖 observations/slack-clone_01/RETRO.md 定案的 5 项修复：

- P0-1: bash 命令模式护栏（command_guard.evaluate_command）——
  taskkill //IM / rm -rf / pkill 等按名/宽杀灭与递归强删必须被拦，
  ask 无在线审批降级 deny + 疏通提示；精确 PID 豁免放行。
- P0-2: 进程级硬保护（init_process_protection）——受保护 PID 命中
  kill 族命令一律 deny，规则开关关闭时也生效；main lifespan 已接线。
- P1-3: STALL BREAK 未停泊且名下有 running 任务时，_arm_interrupted_resume
  延迟补偿唤醒一次（[TASK INTERRUPTED]），停泊/非 IDLE 不唤醒。
- P2-4: task_event_relay 收敛 —— 同批 claimed/running 中间态被更晚事件
  取代时跳过；submit/approved/rework 已有直推时 relay 不再重复 FYI。
- P2-5: upstream_nudge_relevant —— BLOCKED STALE 只在 upstream 仍有
  交付前状态（或失联/归档）时触发；全部已交付进审查管线时不催。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.services.command_guard import (
    GuardRule,
    _reset_protected_for_tests,
    add_rules,
    evaluate_command,
    init_process_protection,
    protected_pids,
    register_protected_pid,
    split_compound,
    wildcard_match,
)
import hiveweave.services.command_guard as command_guard


@pytest.fixture(autouse=True)
def _clean_command_guard_state(monkeypatch):
    """每个测试前后清受保护 PID / 追加规则 / 开关 env，防全局污染。"""
    monkeypatch.delenv("HIVEWEAVE_BASH_COMMAND_GUARD", raising=False)
    monkeypatch.delenv("HIVEWEAVE_PROTECTED_PIDS", raising=False)
    _reset_protected_for_tests()
    saved = list(command_guard._extra_rules)
    command_guard._extra_rules.clear()
    yield
    _reset_protected_for_tests()
    command_guard._extra_rules[:] = saved


# ════════════════════════════════════════════════════════════════════
# P0-1: 通配符与命令切分
# ════════════════════════════════════════════════════════════════════


class TestWildcardMatch:
    """opencode Wildcard.match 语义移植."""

    def test_star_matches_anything(self):
        assert wildcard_match("git status", "*") is True

    def test_trailing_space_star_matches_bare_prefix(self):
        """`" *"` 结尾同时匹配裸前缀与带参形式."""
        assert wildcard_match("git checkout", "git checkout *") is True
        assert wildcard_match("git checkout main", "git checkout *") is True

    def test_no_match_returns_false(self):
        assert wildcard_match("git status", "git checkout *") is False

    def test_regex_meta_escaped(self):
        """正则元字符按字面处理（`python.exe` 的 `.` 不是通配）."""
        assert wildcard_match("pythonXexe", "python.exe") is False
        assert wildcard_match("python.exe", "python.exe") is True

    def test_backslash_normalized(self):
        assert wildcard_match(r"del foo\\bar /s", "del */s *") is True

    @pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 大小写不敏感")
    def test_case_insensitive_on_windows(self):
        assert wildcard_match("TASKKILL /F /IM X", "taskkill *") is True


class TestSplitCompound:
    """复合命令切分（引号内不切，单个 & 保留）."""

    def test_and_or_semicolon_pipe(self):
        assert split_compound("a && b || c ; d | e") == ["a", "b", "c", "d", "e"]

    def test_newline_splits(self):
        assert split_compound("a\nb") == ["a", "b"]

    def test_single_ampersand_preserved(self):
        """`2>&1` 与后台 `&` 不是命令分隔符."""
        assert split_compound("foo 2>&1") == ["foo 2>&1"]

    def test_quoted_separator_not_split(self):
        assert split_compound('echo "a && b" && ls') == ['echo "a && b"', "ls"]


# ════════════════════════════════════════════════════════════════════
# P0-1: 规则表求值（findLast / ask 降级 / deny / allow）
# ════════════════════════════════════════════════════════════════════


class TestEvaluateCommandRules:
    """规则表三态 + findLast 覆盖."""

    def test_taskkill_image_name_denied(self):
        """事故根因：taskkill //F //IM python.exe 灭掉平台宿主 → 硬 deny."""
        v = evaluate_command("taskkill //F //IM python.exe")
        assert v.blocked is True
        assert v.action == "deny"
        assert v.rule == "taskkill *"
        assert "PID" in v.reason  # 疏通提示引导精确 PID

    def test_taskkill_bare_pid_allowed(self):
        """精确 PID 豁免（无受保护 PID 时放行）."""
        v = evaluate_command("taskkill //PID 12345 //F", protected=set())
        assert v.blocked is False

    def test_taskkill_mixed_im_and_pid_denied(self):
        """S1 回归：事故命令追加 //PID 的混合开关变体必须 deny.

        通配符 `taskkill */pid *` 豁免只校验「存在 /pid」不校验「不存在 //IM」，
        `taskkill //F //IM python.exe //PID 1234` 会穿透。谓词化后 IM 出现即不豁免。
        """
        v = evaluate_command("taskkill //F //IM python.exe //PID 1234")
        assert v.blocked is True
        assert v.action == "deny"
        assert v.rule == "taskkill *"

    def test_taskkill_pid_then_im_denied(self):
        """S1：//PID 在前 //IM 在后同样 deny（开关顺序无关）."""
        v = evaluate_command("taskkill //PID 1234 //IM python.exe //F")
        assert v.blocked is True
        assert v.rule == "taskkill *"

    def test_taskkill_pid_with_filter_denied(self):
        """S1：/FI 过滤器（可按镜像名匹配）不豁免."""
        v = evaluate_command('taskkill //PID 1234 //FI "IMAGENAME eq python.exe"')
        assert v.blocked is True

    def test_taskkill_pid_variable_not_exempted(self):
        """S1/M1：//PID 后是变量（非字面数字）→ 间接引用拦截."""
        v = evaluate_command("taskkill //PID $PIDVAR //F")
        assert v.blocked is True
        assert v.rule == "__indirect_pid__"

    def test_taskkill_exact_pid_with_t_allowed(self):
        """安全组合 //PID <数字> //T //F 仍豁免（不受 S1 收紧影响）."""
        v = evaluate_command("taskkill //PID 1234 //T //F", protected=set())
        assert v.blocked is False

    def test_pkill_denied(self):
        v = evaluate_command("pkill python")
        assert v.blocked is True
        assert v.rule == "pkill *"

    def test_killall_denied(self):
        v = evaluate_command("killall node")
        assert v.blocked is True

    def test_stop_process_denied(self):
        v = evaluate_command("Stop-Process -Name python -Force")
        assert v.blocked is True

    def test_wmic_process_delete_denied(self):
        v = evaluate_command(
            "wmic process where processid=1234 delete", protected=set()
        )
        assert v.blocked is True

    def test_rm_rf_ask_degrades_to_deny(self):
        """rm -rf 是 ask → 无在线审批降级 deny + 疏通提示."""
        v = evaluate_command("rm -rf build")
        assert v.blocked is True
        assert v.action == "deny"
        assert v.rule == "rm"
        assert "ask" in v.reason and "deny" in v.reason
        assert "delete_directory" in v.reason  # 疏通替代方案

    def test_rm_recursive_without_force_allowed(self):
        """rm -r（无 -f）不命中递归+强制谓词 → 放行."""
        v = evaluate_command("rm -r build")
        assert v.blocked is False

    def test_rm_force_without_recursive_allowed(self):
        """rm -f <file>（无递归）放行 —— 谓词必须 recursive∧force."""
        v = evaluate_command("rm -f tmp.log")
        assert v.blocked is False

    def test_rm_rf_long_flags_blocked(self):
        v = evaluate_command("rm --recursive --force build")
        assert v.blocked is True
        assert v.rule == "rm"

    def test_rm_rf_flag_not_confused_by_filename(self):
        """目标名含 f 不算 force（通配符表达不了，谓词只看 flag token）."""
        v = evaluate_command("rm -r frontend")
        assert v.blocked is False

    def test_kill_bare_allowed(self):
        v = evaluate_command("kill 12345", protected=set())
        assert v.blocked is False

    def test_kill_sigkill_ask_degrades(self):
        v = evaluate_command("kill -9 12345", protected=set())
        assert v.blocked is True
        assert "SIGTERM" in v.reason  # 疏通：先温和终止

    def test_kill_s_sigkill_ask_degrades(self):
        """L4 回归：标准写法 kill -s SIGKILL 必须 ask 降级（原通配符漏此形式）."""
        v = evaluate_command("kill -s SIGKILL 12345", protected=set())
        assert v.blocked is True
        assert "SIGTERM" in v.reason

    def test_kill_s_sigkill_lowercase_ask(self):
        v = evaluate_command("kill -s sigkill 12345", protected=set())
        assert v.blocked is True

    def test_kill_sigkill_word_ask(self):
        v = evaluate_command("kill -SIGKILL 12345", protected=set())
        assert v.blocked is True

    def test_kill_long_signal_equals_ask(self):
        """审计 F1 回归：GNU 长选项 kill --signal=9 必须 ask 降级."""
        assert evaluate_command("kill --signal=9 12345", protected=set()).blocked is True
        assert evaluate_command("kill --signal=SIGKILL 12345", protected=set()).blocked is True

    def test_kill_long_signal_two_token_ask(self):
        """审计 F1 回归：kill --signal SIGKILL 两 token 形式 also ask."""
        v = evaluate_command("kill --signal SIGKILL 12345", protected=set())
        assert v.blocked is True
        assert "SIGTERM" in v.reason

    def test_kill_long_signal_term_allowed(self):
        """审计 F1：温和长选项 --signal=TERM / --signal TERM 仍放行."""
        assert evaluate_command("kill --signal=TERM 12345", protected=set()).blocked is False
        assert evaluate_command("kill --signal TERM 12345", protected=set()).blocked is False

    def test_kill_term_signal_still_allowed(self):
        """温和信号不误入 ask：kill -s TERM / kill -15 放行."""
        assert evaluate_command("kill -s TERM 12345", protected=set()).blocked is False
        assert evaluate_command("kill -15 12345", protected=set()).blocked is False

    def test_kill_indirect_variable_denied(self):
        """M1 回归：kill $VAR 间接引用无法审计展开值 → 降级 deny."""
        v = evaluate_command("kill $MYPID", protected=set())
        assert v.blocked is True
        assert v.rule == "__indirect_pid__"

    def test_taskkill_indirect_variable_via_assignment_denied(self):
        """M1：P=4000; taskkill //PID $P //F 变量穿透受保护 PID → deny."""
        v = evaluate_command("P=4000; taskkill //PID $P //F", protected={4000})
        assert v.blocked is True

    def test_compound_any_deny_blocks_all(self):
        """复合命令任一子命令 deny 即整体 deny."""
        v = evaluate_command("echo ok && pkill python")
        assert v.blocked is True
        assert v.rule == "pkill *"

    def test_compound_all_safe_allowed(self):
        v = evaluate_command("git status && echo done")
        assert v.blocked is False

    def test_full_path_basename_normalized(self):
        """全路径/扩展名形式也能命中规则（首 token basename 化）."""
        v = evaluate_command(r"C:\\Windows\\System32\\taskkill.exe /F /IM x.exe")
        assert v.blocked is True
        assert v.rule == "taskkill *"

    def test_normal_commands_allowed(self):
        for cmd in ("git status", "ls -la", "echo hello", "pytest tests/ -q"):
            assert evaluate_command(cmd).blocked is False, cmd

    def test_findlast_extra_rules_override_defaults(self):
        """findLast：后追加的规则覆盖内置默认."""
        add_rules([GuardRule("pkill *", "allow")])
        v = evaluate_command("pkill python")
        assert v.blocked is False

    def test_guard_env_off_disables_rule_layer(self, monkeypatch):
        """HIVEWEAVE_BASH_COMMAND_GUARD=off 只关规则表层."""
        monkeypatch.setenv("HIVEWEAVE_BASH_COMMAND_GUARD", "off")
        v = evaluate_command("rm -rf build")
        assert v.blocked is False


# ════════════════════════════════════════════════════════════════════
# P0-2: 进程级硬保护（底线层，规则无法覆盖）
# ════════════════════════════════════════════════════════════════════


class TestProtectedPidHardLayer:
    """kill 族命中受保护 PID 一律 deny."""

    def test_taskkill_protected_pid_denied(self):
        """精确 PID 豁免挡不住受保护 PID —— PID 硬层先于规则."""
        v = evaluate_command("taskkill //PID 4000 //F", protected={4000})
        assert v.blocked is True
        assert v.rule == "__protected_pid__"
        assert "平台宿主" in v.reason

    def test_taskkill_single_slash_pid_denied(self):
        v = evaluate_command("taskkill /PID 4000 /F", protected={4000})
        assert v.blocked is True

    def test_kill_protected_pid_denied(self):
        v = evaluate_command("kill 4000", protected={4000})
        assert v.blocked is True
        assert v.rule == "__protected_pid__"

    def test_stop_process_id_denied(self):
        v = evaluate_command("Stop-Process -Id 4000", protected={4000})
        assert v.blocked is True
        assert v.rule == "__protected_pid__"

    def test_wmic_processid_denied(self):
        v = evaluate_command(
            "wmic process where processid=4000 call terminate", protected={4000}
        )
        assert v.blocked is True
        assert v.rule == "__protected_pid__"

    def test_protected_layer_survives_guard_off(self, monkeypatch):
        """规则开关关闭时 PID 硬保护仍生效（底线永不关闭）."""
        monkeypatch.setenv("HIVEWEAVE_BASH_COMMAND_GUARD", "off")
        v = evaluate_command("taskkill //PID 4000 //F", protected={4000})
        assert v.blocked is True
        assert v.rule == "__protected_pid__"

    def test_unprotected_pid_not_blocked_by_hard_layer(self):
        v = evaluate_command("taskkill //PID 9999 //F", protected={4000})
        assert v.blocked is False

    def test_register_protected_pid_global(self):
        register_protected_pid(31415)
        assert 31415 in protected_pids()
        v = evaluate_command("kill 31415")  # protected=None → 全局集合
        assert v.blocked is True

    def test_init_registers_self_and_env_pids(self, monkeypatch):
        """init_process_protection：自身 PID + env 注入必须入集."""
        monkeypatch.setenv("HIVEWEAVE_PROTECTED_PIDS", "424242, 424243")
        pids = init_process_protection()
        assert os.getpid() in pids
        assert 424242 in pids and 424243 in pids
        # 全局集合同步生效：kill 自身被硬层拦
        v = evaluate_command(f"kill {os.getpid()}")
        assert v.blocked is True
        assert v.rule == "__protected_pid__"

    def test_main_lifespan_wires_protection(self):
        """P0-2 接线冒烟：main.py lifespan 必须调用 init_process_protection."""
        from pathlib import Path

        import hiveweave.main as main_mod

        src = Path(main_mod.__file__).read_text(encoding="utf-8")
        assert "init_process_protection" in src


# ════════════════════════════════════════════════════════════════════
# P0-1: shell 包装解包
# ════════════════════════════════════════════════════════════════════


class TestShellWrapperUnwrap:
    """powershell/cmd/bash -c 内层递归评估（最深 3 层）."""

    def test_powershell_c_inner_denied(self):
        v = evaluate_command('powershell -c "rm -rf x"')
        assert v.blocked is True
        assert "powershell" in v.reason  # 标注经由包装

    def test_cmd_c_inner_denied(self):
        v = evaluate_command("cmd /c pkill python")
        assert v.blocked is True
        assert v.rule == "pkill *"

    def test_bash_c_inner_denied(self):
        v = evaluate_command("bash -c 'pkill node'")
        assert v.blocked is True

    def test_wrapper_inner_pid_protected(self):
        v = evaluate_command("cmd /c taskkill /pid 4000 /f", protected={4000})
        assert v.blocked is True
        assert v.rule == "__protected_pid__"

    def test_encoded_command_unauditable_denied(self):
        """-EncodedCommand base64 无法审计 → deny."""
        v = evaluate_command("powershell -enc UwB0AG8AcAA=")
        assert v.blocked is True
        assert v.rule == "wrapper:encoded"

    def test_powershell_multi_token_args_joined(self):
        """M2 回归：PS -Command 后所有剩余 token 拼接执行，非引用多 token 也要审到."""
        v = evaluate_command("powershell -c rm -rf x")
        assert v.blocked is True

    def test_powershell_scriptblock_inner_audited(self):
        """M2：{ ... } scriptblock 剥壳后审计内层（Stop-Process 按名杀灭）."""
        v = evaluate_command("powershell -Command { Stop-Process -Name python }")
        assert v.blocked is True

    def test_powershell_short_enc_e_denied(self):
        """M3 回归：-e 短缩写编码命令同样不可审计 → deny（原只认 -enc）."""
        v = evaluate_command("powershell -e UwB0AG8AcAA=")
        assert v.blocked is True
        assert v.rule == "wrapper:encoded"

    def test_powershell_short_enc_en_denied(self):
        """M3：-en 前缀缩写（EncodedCommand 真实最短唯一前缀）→ deny."""
        v = evaluate_command("powershell -en UwB0AG8AcAA=")
        assert v.blocked is True
        assert v.rule == "wrapper:encoded"

    def test_wrapper_nested_over_depth_denied(self):
        """L7：嵌套包装超 3 层 → 无法审计 → 降级 deny（不再静默放行）."""
        deep = "powershell -c " * 4 + "taskkill /IM python.exe"
        v = evaluate_command(deep)
        assert v.blocked is True

    def test_wrapper_safe_inner_allowed(self):
        v = evaluate_command("cmd /c echo hello")
        assert v.blocked is False


# ════════════════════════════════════════════════════════════════════
# P0-1: 挂点集成（bash.py / pipeline.py）
# ════════════════════════════════════════════════════════════════════


class TestBashToolIntegration:
    """execute_run_command / _check_shell_security 必须过命令护栏."""

    @pytest.fixture(autouse=True)
    def _sandbox_off(self, monkeypatch):
        """P3 默认 on：本类测护栏（非沙箱）—— 显式关沙箱。"""
        from hiveweave.config import settings

        monkeypatch.setattr(settings, "acl_sandbox", False)

    def setup_method(self):
        self.workspace = tempfile.mkdtemp(prefix="hiveweave_guard_test_")

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.workspace, ignore_errors=True)

    async def test_rm_rf_blocked_in_run_command(self):
        from hiveweave.tools.bash import execute_run_command

        result = await execute_run_command(
            command="rm -rf some_dir",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert "Command blocked" in result["error"]
        assert result["output"] == ""  # 未实际执行

    async def test_pkill_blocked_in_run_command(self):
        """pkill 按名杀灭必须被拦（process_registry 镜像护栏或命令护栏均可）."""
        from hiveweave.tools.bash import execute_run_command

        result = await execute_run_command(
            command="pkill python",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert result["success"] is False
        assert result["output"] == ""  # 未实际执行
        err = result["error"]
        assert "Refusing" in err or "Command blocked" in err

    async def test_normal_command_not_blocked_by_guard(self):
        from hiveweave.tools.bash import execute_run_command

        result = await execute_run_command(
            command="echo hello",
            cwd="",
            timeout_ms=5000,
            workspace_path=self.workspace,
        )
        assert "Command blocked" not in (result.get("error") or "")

    def test_pipeline_shell_security_precheck(self):
        """rm -rf 只被命令护栏拦（不经自毁/镜像护栏）→ 走 Command blocked 文案."""
        from hiveweave.tools.pipeline import _check_shell_security

        err = _check_shell_security(SimpleNamespace(command="rm -rf build"))
        assert err is not None and "Command blocked" in err

    def test_pipeline_shell_security_blocks_image_kill(self):
        from hiveweave.tools.pipeline import _check_shell_security

        err = _check_shell_security(SimpleNamespace(command="pkill node"))
        assert err is not None and ("Refusing" in err or "Command blocked" in err)

    def test_pipeline_shell_security_allows_safe(self):
        from hiveweave.tools.pipeline import _check_shell_security

        assert _check_shell_security(SimpleNamespace(command="git status")) is None


# ════════════════════════════════════════════════════════════════════
# P1-3: STALL BREAK 补偿唤醒（_arm_interrupted_resume）
# ════════════════════════════════════════════════════════════════════


def _make_resume_agent():
    """最小 Agent 替身：仅 _arm_interrupted_resume 依赖的属性."""
    from hiveweave.agents.agent import Agent, AgentState

    with patch.object(Agent, "__init__", lambda self, *a, **k: None):
        ag = Agent.__new__(Agent)
    ag.id = "agent-resume-test"
    ag.status = AgentState.IDLE
    ag.disposition = "runnable"
    ag.chat = AsyncMock()
    ag._interrupted_resume_timer = None
    ag._INTERRUPTED_RESUME_DELAY_S = 0.02  # 测试提速：45s → 20ms
    return ag


class TestInterruptedResume:
    """STALL BREAK 未停泊 → 延迟补偿唤醒 running 任务."""

    async def test_arm_empty_refs_no_timer(self):
        ag = _make_resume_agent()
        ag._arm_interrupted_resume([])
        assert ag._interrupted_resume_timer is None

    async def test_fire_wakes_with_interrupted_hint(self):
        ag = _make_resume_agent()
        ag._arm_interrupted_resume(["task-aaa", "task-bbb"])
        assert ag._interrupted_resume_timer is not None
        await asyncio.sleep(0.15)
        ag.chat.assert_awaited_once()
        hint, = ag.chat.await_args.args
        opts = ag.chat.await_args.kwargs["opts"]
        assert "[TASK INTERRUPTED]" in hint
        assert "task-aaa" in hint and "task-bbb" in hint
        assert opts["source"] == "interrupted_resume"
        assert opts["trigger"] is True
        assert opts["is_background"] is True

    async def test_fire_skipped_when_not_idle(self):
        """补偿期内 agent 已被其他路径唤醒（PROCESSING）→ 不重复唤醒."""
        from hiveweave.agents.agent import AgentState

        ag = _make_resume_agent()
        ag._arm_interrupted_resume(["task-aaa"])
        ag.status = AgentState.PROCESSING
        await asyncio.sleep(0.15)
        ag.chat.assert_not_called()

    async def test_fire_skipped_when_blocked(self):
        """补偿期内被停泊/升级（disposition=blocked）→ 不与 park 语义打架."""
        ag = _make_resume_agent()
        ag._arm_interrupted_resume(["task-aaa"])
        ag.disposition = "blocked"
        await asyncio.sleep(0.15)
        ag.chat.assert_not_called()

    async def test_cancel_prevents_fire(self):
        """新 chat/cancel 入口取消补偿定时器."""
        ag = _make_resume_agent()
        ag._arm_interrupted_resume(["task-aaa"])
        ag._cancel_interrupted_resume()
        assert ag._interrupted_resume_timer is None
        await asyncio.sleep(0.15)
        ag.chat.assert_not_called()

    async def test_rearm_replaces_previous_timer(self):
        """重复 arm 只保留最后一次（单飞）."""
        ag = _make_resume_agent()
        ag._arm_interrupted_resume(["task-old"])
        ag._arm_interrupted_resume(["task-new"])
        await asyncio.sleep(0.15)
        ag.chat.assert_awaited_once()
        hint, = ag.chat.await_args.args
        assert "task-new" in hint and "task-old" not in hint


# ════════════════════════════════════════════════════════════════════
# P2-4: task_event_relay 收敛（同批合并 + 直推去重）
# ════════════════════════════════════════════════════════════════════


def _relay_with_events(events: list[dict]):
    """构造注入了假事件的 TaskEventRelay 测试环境."""
    from hiveweave.services.task_event_relay import TaskEventRelay

    svc = MagicMock()
    svc.get_undelivered = AsyncMock(return_value=list(events))
    svc.mark_delivered = AsyncMock()
    relay = TaskEventRelay()
    return relay, svc


class TestRelayBatchMerge:
    """同批 claimed/running 中间态被更晚事件取代时跳过."""

    async def test_superseded_intermediate_fyi_skipped(self):
        events = [
            {"id": "e1", "task_id": "tA", "event_type": "task.claimed"},
            {"id": "e2", "task_id": "tA", "event_type": "task.running"},
            {"id": "e3", "task_id": "tA", "event_type": "task.submitted"},
            {"id": "e4", "task_id": "tB", "event_type": "task.claimed"},
        ]
        relay, svc = _relay_with_events(events)
        processed_ids: list[str] = []

        async def fake_process_one(_pid, ev):
            processed_ids.append(ev["id"])

        relay._process_one = fake_process_one  # type: ignore[method-assign]
        with patch(
            "hiveweave.services.task.TaskEventService", return_value=svc
        ):
            n = await relay.process_pending("p1")

        assert n == 2
        assert processed_ids == ["e3", "e4"]  # 中间态被取代，tB 裸 claimed 保留
        # 全部 4 条（含跳过的）都标 delivered —— 不反复重投
        svc.mark_delivered.assert_awaited_once()
        marked = svc.mark_delivered.await_args.args[1]
        assert sorted(marked) == ["e1", "e2", "e3", "e4"]

    async def test_single_claimed_not_superseded(self):
        events = [{"id": "e1", "task_id": "tA", "event_type": "task.claimed"}]
        relay, svc = _relay_with_events(events)

        async def fake_process_one(_pid, ev):
            return None

        relay._process_one = fake_process_one  # type: ignore[method-assign]
        with patch(
            "hiveweave.services.task.TaskEventService", return_value=svc
        ):
            n = await relay.process_pending("p1")
        assert n == 1

    async def test_empty_events_short_circuit(self):
        relay, svc = _relay_with_events([])
        with patch(
            "hiveweave.services.task.TaskEventService", return_value=svc
        ):
            n = await relay.process_pending("p1")
        assert n == 0
        svc.mark_delivered.assert_not_called()


class TestRelayDirectDedupe:
    """工具层已 wake=True 直推的事件，relay 不再重复 FYI."""

    def _patch_query(self, task_row: dict, inbox_has_direct: bool):
        async def fake_query(_pid, sql, _params):
            if "FROM tasks" in sql:
                return [task_row]
            if "FROM inbox" in sql:
                return [{"x": 1}] if inbox_has_direct else []
            return []

        return patch("hiveweave.services.task._query", fake_query)

    def _event(self, event_type: str) -> dict:
        return {
            "id": "ev-1",
            "task_id": "task-1",
            "event_type": event_type,
            "actor_id": "assignee-1",
            "payload": "{}",
            "created_at": 1_700_000_000_000,
        }

    async def test_direct_already_sent_skips_relay(self):
        from hiveweave.services.task_event_relay import TaskEventRelay

        task_row = {
            "assignee_id": "assignee-1",
            "creator_id": "creator-1",
            "title": "T",
        }
        sends: list[dict] = []
        inbox = MagicMock()
        inbox.send_message = AsyncMock(side_effect=lambda **kw: sends.append(kw))

        relay = TaskEventRelay()
        with (
            self._patch_query(task_row, inbox_has_direct=True),
            patch(
                "hiveweave.services.task_event_relay.InboxService",
                return_value=inbox,
            ),
        ):
            await relay._process_one("p1", self._event("task.submitted"))
        assert sends == []  # 直推已送达 → relay 静默

    async def test_no_direct_falls_back_to_relay(self):
        """REST 逃生门等无直推路径 → relay 兜底照发."""
        from hiveweave.services.task_event_relay import TaskEventRelay

        task_row = {
            "assignee_id": "assignee-1",
            "creator_id": "creator-1",
            "title": "T",
        }
        sends: list[dict] = []
        inbox = MagicMock()
        inbox.send_message = AsyncMock(side_effect=lambda **kw: sends.append(kw))

        relay = TaskEventRelay()
        with (
            self._patch_query(task_row, inbox_has_direct=False),
            patch(
                "hiveweave.services.task_event_relay.InboxService",
                return_value=inbox,
            ),
        ):
            await relay._process_one("p1", self._event("task.submitted"))
        assert len(sends) == 1
        assert sends[0]["to_agent_id"] == "creator-1"
        assert sends[0]["wake"] is False
        assert "ev-1" in sends[0]["idempotency_key"]

    async def test_dedupe_window_binds_current_event_not_history(self):
        """审计 F9 回归：去重窗口绑定本轮事件，不向前扫 60s 命中上一轮直推.

        场景：submit → rework → 60s 内再 submit。若窗口是 `created_at >=
        ev_ts - 60s`，第二轮会命中上一轮残留 [TASK SUBMITTED] 直推 → 静默
        吞掉本轮"待审查"通知。修复后窗口 = [ev_ts - 5s, ev_ts + 60s]。
        """
        from hiveweave.services.task_event_relay import (
            TaskEventRelay,
            _DIRECT_DEDUPE_BACKWARD_MS,
            _DIRECT_DEDUPE_SKEW_MS,
        )

        task_row = {
            "assignee_id": "assignee-1",
            "creator_id": "creator-1",
            "title": "T",
        }
        captured: list[dict] = []
        inbox = MagicMock()
        inbox.send_message = AsyncMock()

        async def fake_query(_pid, sql, params):
            if "FROM tasks" in sql:
                return [task_row]
            if "FROM inbox" in sql:
                captured.append({"sql": sql, "params": params})
                return []
            return []

        relay = TaskEventRelay()
        ev = self._event("task.submitted")
        with (
            patch("hiveweave.services.task._query", fake_query),
            patch(
                "hiveweave.services.task_event_relay.InboxService",
                return_value=inbox,
            ),
        ):
            await relay._process_one("p1", ev)

        assert captured, "去重查询未执行"
        lo, hi = captured[0]["params"][-2:]
        ev_ts = 1_700_000_000_000
        # 下界绑定本轮事件（只容忍同轮毫秒级抖动），上界容忍直推落库稍晚
        assert lo == max(0, ev_ts - _DIRECT_DEDUPE_BACKWARD_MS)
        assert hi == ev_ts + _DIRECT_DEDUPE_SKEW_MS
        # 上一轮直推（60s 前）被排除在窗口外——不会误压
        assert lo > ev_ts - 60_000

    async def test_dedupe_query_failure_conservative_send(self):
        """去重查询失败时保守放行（兜底语义：宁重勿丢）."""
        from hiveweave.services.task_event_relay import TaskEventRelay

        task_row = {
            "assignee_id": "assignee-1",
            "creator_id": "creator-1",
            "title": "T",
        }

        async def fake_query(_pid, sql, _params):
            if "FROM tasks" in sql:
                return [task_row]
            if "FROM inbox" in sql:
                raise RuntimeError("db down")
            return []

        sends: list[dict] = []
        inbox = MagicMock()
        inbox.send_message = AsyncMock(side_effect=lambda **kw: sends.append(kw))

        relay = TaskEventRelay()
        with (
            patch("hiveweave.services.task._query", fake_query),
            patch(
                "hiveweave.services.task_event_relay.InboxService",
                return_value=inbox,
            ),
        ):
            await relay._process_one("p1", self._event("task.submitted"))
        assert len(sends) == 1


# ════════════════════════════════════════════════════════════════════
# P2-5: BLOCKED STALE upstream 状态感知
# ════════════════════════════════════════════════════════════════════


class TestUpstreamNudgeRelevant:
    """upstream 全已交付 → 不催；仍有交付前/失联/归档 → 催."""

    def test_no_refs_defaults_relevant(self):
        from hiveweave.services.game_time import upstream_nudge_relevant

        assert upstream_nudge_relevant({}, {}) is True

    def test_upstream_running_is_relevant(self):
        from hiveweave.services.game_time import upstream_nudge_relevant

        task = {"depends_on": ["u1"]}
        assert upstream_nudge_relevant(task, {"u1": {"status": "running"}}) is True

    def test_all_upstream_delivered_not_relevant(self):
        """毛糙点根因：upstream 已 submit 进审查管线 → blocked 方无可行动."""
        from hiveweave.services.game_time import upstream_nudge_relevant

        task = {"depends_on": ["u1", "u2"]}
        by_id = {"u1": {"status": "closed"}, "u2": {"status": "submitted"}}
        assert upstream_nudge_relevant(task, by_id) is False

    @pytest.mark.parametrize(
        "status", ["submitted", "reviewing", "approved", "verifying", "closed"]
    )
    def test_each_delivered_status_not_relevant(self, status: str):
        from hiveweave.services.game_time import upstream_nudge_relevant

        task = {"depends_on": ["u1"]}
        assert upstream_nudge_relevant(task, {"u1": {"status": status}}) is False

    @pytest.mark.parametrize(
        "status", ["created", "claimed", "running", "rework", "blocked", "archived"]
    )
    def test_each_pre_delivery_status_relevant(self, status: str):
        from hiveweave.services.game_time import upstream_nudge_relevant

        task = {"depends_on": ["u1"]}
        assert upstream_nudge_relevant(task, {"u1": {"status": status}}) is True

    def test_missing_upstream_row_is_relevant(self):
        """upstream 行缺失（需重新定向/升级）→ 催."""
        from hiveweave.services.game_time import upstream_nudge_relevant

        task = {"depends_on": ["ghost"]}
        assert upstream_nudge_relevant(task, {}) is True

    def test_depends_on_json_string_parsed(self):
        from hiveweave.services.game_time import upstream_nudge_relevant

        task = {"depends_on": '["u1"]'}
        assert upstream_nudge_relevant(task, {"u1": {"status": "closed"}}) is False
        assert upstream_nudge_relevant(task, {"u1": {"status": "running"}}) is True

    def test_blocked_reason_dependency_ref(self):
        """blocked_reason `dependency: <id 前缀>` 也算 upstream 引用."""
        from hiveweave.services.game_time import upstream_nudge_relevant

        uid = "abc12345-def0-1234-5678-abcdefghijkl"
        task = {"blocked_reason": f"Dependency: {uid[:8]} waiting"}
        assert (
            upstream_nudge_relevant(task, {uid: {"status": "submitted"}}) is False
        )
        assert upstream_nudge_relevant(task, {uid: {"status": "running"}}) is True
