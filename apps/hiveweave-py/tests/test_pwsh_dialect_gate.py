"""P0-2 (TEST_DSH_33)/P1-3（B 结构解）: bash→pwsh 方言 gate + pwsh 工具。

TEST_DSH_33 实测 126 次方言失败（占失败步 41.9%）：受限沙箱 shell = pwsh。
P1-3（B 结构解，对齐 deepseek-harness"不翻译、原生双工具"）词典翻译层
退役后，gate 契约更新为：
- pwsh 原生/兼容命令（Get-Content / python / git / 裸 echo）→ 放行
- unix 惯用语（head/tail/ls 带 flag/grep/wc/mkdir -p/cat|grep）→ **前置拒绝**，
  出带等价写法的可操作错误（曾"翻译后放行"的形态现在同样被拦 —— 兑现
  bash 工具 description 承诺的 rejected up front with the pwsh equivalent）
- 非受限 pwsh 环境（Git Bash native / cmd 兜底）→ gate 闭嘴
- pwsh 工具（dialect="pwsh"）→ 命令本就是 PowerShell，不走 gate
"""

from __future__ import annotations

import pytest

from hiveweave.tools.bash import (
    _pwsh_dialect_gate,
    _segment_head_token,
    _split_command_segments,
    detect_untranslated_unix,
)


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch):
    """强制方言 gate 生效（无视平台/沙箱探测）。"""
    monkeypatch.setattr(
        "hiveweave.tools.bash._pwsh_is_effective_shell", lambda: True
    )


# ── P1-3（B 结构解）：pwsh 原生/兼容命令放行（回归保护）─────────


@pytest.mark.parametrize(
    "command",
    [
        "Get-Content x -TotalCount 80",  # PowerShell 方言
        "python script.py",              # 外部程序，pwsh 直调
        "echo hello world",              # 裸 echo 无 unix flag → pwsh 兼容
        "git status",
        "cd src; Get-ChildItem",
    ],
)
def test_gate_passes_native_or_pwsh_idiom(gate_on, command):
    assert _pwsh_dialect_gate(command) is None, command


# ── P1-3（B 结构解）：unix 惯用语不再转译 —— 前置拒绝 + 给等价 ──


@pytest.mark.parametrize(
    "command",
    [
        "head -n 800 docs/design.md",   # TEST_DSH_33 曾原样透传 → 现在前置拒
        "head -80 docs/design.md",
        "tail -n 20 out.log",
        "tail -20 out.log",
        "ls -lh src",                   # pwsh 有 ls 别名但 flag 语义不同
        "ls -la src",
        'grep -rn "judge" src/',
        "wc -l x.ts",
        "cat app.log | grep warning",   # 管道尾 grep
        # R3 P0-2：模型在 pwsh 工具里照写 unix 管道（2518560b 实证）——
        # gate 对 pwsh 方言不再短路，照样拒。
        "Get-Content README.md | head -n 50",
        "Get-Content out.log -Tail 5 | grep error",
        # 注：`mkdir -p` 不在本组 —— pwsh 有 mkdir 别名（兼容命令），
        # `-p` 由 pwsh 自身参数校验报可见错误，模型可自纠，非静默错译。
    ],
)
def test_gate_rejects_unix_idiom_verbatim(gate_on, command):
    err = _pwsh_dialect_gate(command)
    assert err is not None, command
    # 文案必须指路 pwsh 工具（第二出路），且禁止「换个 flag 重试」
    assert "pwsh" in err
    assert "Do not retry" in err


# ── 翻译不了的形态：拦截并给等价写法 ────────────────────────


@pytest.mark.parametrize(
    ("command", "needle"),
    [
        ('sed -n "1,40p" R.md', "Get-Content"),
        ("awk '{print $1}' data.tsv", "ForEach-Object"),
        ("grep -v warning app.log", "Select-String"),
        # 类 2：pwsh 有同名别名但 unix flag 语义对不上 → 仅带 flag 时拦
        ("ps aux", "Get-Process"),
        ('find src -name "*.tsx" | head -5', "Get-ChildItem"),
        ("cat p.json | head -n 30", "Get-Content"),
    ],
)
def test_gate_blocks_unix_only_with_hint(gate_on, command, needle):
    err = _pwsh_dialect_gate(command)
    assert err is not None, command
    assert needle in err
    # 文案必须指路 pwsh 工具（第二出路），且禁止「换个 flag 重试」
    assert "pwsh" in err
    assert "Do not retry" in err


def test_gate_blocks_ls_with_unmapped_flag(gate_on):
    """`ls -x` 这类翻译表没有的 flag 组合 → 类 2 拦截。"""
    err = _pwsh_dialect_gate("ls -x src")
    assert err is not None
    assert "ls" in err


def test_gate_quiet_when_not_pwsh_shell(monkeypatch: pytest.MonkeyPatch):
    """非受限 pwsh 环境（Git Bash native）→ gate 必须闭嘴（防误伤真 bash）。"""
    monkeypatch.setattr(
        "hiveweave.tools.bash._pwsh_is_effective_shell", lambda: False
    )
    assert _pwsh_dialect_gate('sed -n "1,40p" R.md') is None


# ── 引号感知的段切分（防误拦）───────────────────────────────


def test_split_segments_quotes_do_not_split():
    segs = _split_command_segments('git commit -m "fix: parse; sed edge case"')
    assert len(segs) == 1
    assert "sed edge case" in segs[0]


def test_split_segments_operators_do_split():
    segs = _split_command_segments("cd /tmp && ls -la ; pwd")
    assert len(segs) == 3


def test_head_token_strips_env_prefix_and_path():
    assert _segment_head_token("FOO=bar wc -l x") == "wc"
    assert _segment_head_token("/usr/bin/sed -i s/a/b/ f") == "sed"
    assert _segment_head_token("C:\\Windows\\System32\\sort.exe x") == "sort"


def test_detect_after_normalize_pipe_with_translated_head(gate_on):
    """管道尾的 head 无文件参数 → 翻译规则（要求 head -N file）不适用，
    head 连同 find 一起被拦——拦下来给等价写法，不再静默透传。"""
    err = _pwsh_dialect_gate('find src -name "*.tsx" | head -5')
    assert err is not None
    assert "find" in err
    assert "head" in err


def test_detect_untranslated_unix_dedupes_same_head():
    """同一命令里同名命令段只提示一次（seen 去重）。"""
    err = detect_untranslated_unix("wc -l a.txt && wc -l b.txt")
    assert err is not None
    assert err.count("wc →") == 1


# ── pwsh 工具注册不变式（5 处接线缺一不可）─────────────────


def test_pwsh_tool_wiring_invariants():
    """新工具 5 处接线：注册表 / executor schema / 权限 / 硬门 / 超时表。

    对照 bash 逐项校验——pwsh 与 bash 同壳层同语义，任何一处漏接
    都会复现「注册可见但调用即死」（python_script 事故形态）。
    """
    from hiveweave.services.permission import (
        ALL_TOOLS,
        COORDINATOR_BUILDER_TOOLS,
        READONLY_TOOLS,
        READWRITE_TOOLS,
    )
    from hiveweave.services.policy import TOOL_CAPABILITY
    from hiveweave.tools.base import _TOOL_REGISTRY
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    for registry in (READONLY_TOOLS, READWRITE_TOOLS, ALL_TOOLS):
        assert "pwsh" in registry, registry
    assert "pwsh" in COORDINATOR_BUILDER_TOOLS
    # 硬门与 bash 同能力位（BASH_SHELL）——未知能力位会落 mode 兜底 ask
    assert TOOL_CAPABILITY.get("pwsh") == TOOL_CAPABILITY.get("bash")
    # executor schema 是 LLM 的唯一主源（@tool 只是回退）
    assert "pwsh" in TOOL_PARAM_SCHEMAS
    props = sorted((TOOL_PARAM_SCHEMAS["pwsh"] or {}).get("properties", {}))
    assert props == ["background", "command", "taskId", "testEvidence", "timeout"]
    # @tool 注册表（回退路径）
    assert "pwsh" in _TOOL_REGISTRY
    d = _TOOL_REGISTRY["pwsh"]
    assert getattr(d, "security_level", None) == "shell"
    assert getattr(d, "requires_workspace", None) is True


def test_pwsh_description_declares_dialect():
    """方言声明必须在 schema description 里（P1-6：引导缺口的治本位）。"""
    from hiveweave.tools.bash import PWSH_TOOL_DESCRIPTION

    # 提示词注入给模型的是「写 PowerShell」而非「写 bash」
    assert "PowerShell" in PWSH_TOOL_DESCRIPTION
    assert "bash" in PWSH_TOOL_DESCRIPTION.lower()


# ── 挂载点集成（gate 必须真的挂在 execute_bash 上）─────────


@pytest.mark.asyncio
async def test_execute_bash_mounts_dialect_gate(
    gate_on, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """删掉 execute_bash 里的 gate 调用不能逃过测试：unix-only 命令必须在
    执行前被拦成 blocked（而非透传给 pwsh 报「不是内部或外部命令」）。"""
    from hiveweave.tools.bash import execute_bash

    result = await execute_bash('sed -n "1,40p" R.md', "", str(tmp_path))
    assert result["success"] is False
    assert result["blocked"] is True
    assert "Select-Object" in result["error"]  # 等价写法在场
    assert "pwsh" in result["error"]


@pytest.mark.asyncio
async def test_execute_bash_pwsh_dialect_skips_gate(
    gate_on, monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """pwsh 工具（dialect="pwsh"）不做 unix 规范化——PowerShell 方言直接
    到执行层；同一命令若走 bash 方言则会被 gate 拦（对照组在上一用例）。"""

    async def fake_exec(*args, **kwargs):
        return {"output": "ok", "stdout": "ok", "stderr": "",
                "exit_code": 0, "timed_out": False, "error": None}

    monkeypatch.setattr("hiveweave.tools.bash._run_sandboxed", fake_exec)
    from hiveweave.tools.bash import execute_bash

    # "Get-ChildItem -Force" 是 PowerShell 方言；bash 方言的翻译表不认识它
    result = await execute_bash(
        "Get-ChildItem -Force", "", str(tmp_path), dialect="pwsh"
    )
    assert result["success"] is True
    assert result["exit_code"] == 0
