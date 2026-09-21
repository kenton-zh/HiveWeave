"""P1-8a：`python3` / `pip3` 的归一化必须覆盖 **pwsh 分支**（四个分支的唯一例外）。

病灶：`_normalize_command`（`python3`→`python`、`pip3`→`pip`）被 **Git Bash / cmd /
unix 三个分支调用，唯独 pwsh 分支不调**（自述「命令已是 PowerShell 方言」）⇒
`python3` 原样落到 pwsh。附带缺陷：旧 `\\bpython3\\b` **过宽** ——
`python3.11` → `python.11`、`C:/tools/python3/bin/x` → `C:/tools/python/bin/x`。

判据全部是**状态判据**（实际构造的 argv / 真实退出码 / 描述串），本仓禁用文案断言。

⚠ 环境实测（采基线，非推定）：本机装了 PowerShell 7（`shutil.which("pwsh")` 有值），
且 `python3` 在**本机**能解析（WorkBuddy 托管运行时提供 `python3.exe`）⇒ **本地
e2e 的 before/after 不可作判别器**（改造前 rc 已是 0）。真正有判别力的是 **AC1/AC2
的 argv 断言**（它们钉的是「pwsh 分支有没有做归一化」，与环境无关）。
"""

from __future__ import annotations

import shutil

import pytest

from hiveweave.services.acl_sandbox.integration import build_confined_argv
from hiveweave.tools import bash as B

PW_DIALECT = "pw" + "sh"
PWSH = shutil.which(PW_DIALECT)
BASH_DESCRIPTION = B.PWSH_TOOL_DESCRIPTION


# ── AC-unit：边界（命令位置 vs 路径/版本号/包名）────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('python3 -c "print(1)"', 'python -c "print(1)"'),
        ("pip3 install x", "pip install x"),
        ("sudo python3 x.py", "sudo python x.py"),
        ("uv run python3 -m pytest", "uv run python -m pytest"),
        ("python3", "python"),
        ("python3.exe -c 1", "python.exe -c 1"),
        ("pip3.exe install x", "pip.exe install x"),
        # 反例：以下都**不得**被改写（旧 \b 边界会改写坏）
        ("python3.11 -m http.server 8787", "python3.11 -m http.server 8787"),
        ("C:/tools/python3/bin/x", "C:/tools/python3/bin/x"),
        ("python3/bin/x", "python3/bin/x"),
        ("python3-x", "python3-x"),
        ("apt install python3-dev", "apt install python3-dev"),
    ],
)
def test_normalize_only_at_command_position(raw, expected):
    assert B._normalize_command(raw, skip_cmd_mapping=True) == expected


# ── AC1：pwsh 分支实际构造的 argv 末元素 ──────────────────────


@pytest.mark.asyncio
async def test_native_pwsh_argv_normalizes_python3(monkeypatch, tmp_path):
    captured: dict = {}

    async def _fake_exec(*args, **kwargs):
        captured["argv"] = list(args)
        # 用 OSError 贴近真实 spawn 失败路径（`_run_native` 只接 OSError 系，
        # 会把它转成 runner_failed 事实位并返回，而不是上抛）
        raise OSError("stop-after-capture")

    monkeypatch.setattr(
        "hiveweave.util.win_subprocess.hidden_exec", _fake_exec
    )
    res = await B._run_native(
        'python3 -c "print(1)"', str(tmp_path), 10, dialect=PW_DIALECT
    )
    argv = captured["argv"]
    assert argv[0].lower().endswith("pwsh.exe"), argv[0]
    assert "python3" not in argv[-1], argv[-1]
    assert "python -c" in argv[-1], argv[-1]
    # 仍走 pwsh（没被换成别的壳）
    assert res["error"].startswith("Failed to spawn shell")


@pytest.mark.asyncio
async def test_native_pwsh_argv_keeps_version_suffix(monkeypatch, tmp_path):
    captured: dict = {}

    async def _fake_exec(*args, **kwargs):
        captured["argv"] = list(args)
        # 用 OSError 贴近真实 spawn 失败路径（`_run_native` 只接 OSError 系，
        # 会把它转成 runner_failed 事实位并返回，而不是上抛）
        raise OSError("stop-after-capture")

    monkeypatch.setattr(
        "hiveweave.util.win_subprocess.hidden_exec", _fake_exec
    )
    await B._run_native(
        "python3.11 -m http.server 8787", str(tmp_path), 10, dialect=PW_DIALECT
    )
    assert "python3.11" in captured["argv"][-1]


# ── AC2：受限路径 argv ───────────────────────────────────────


def test_confined_pwsh_argv_normalizes_and_keeps_pwsh_syntax():
    argv = build_confined_argv('python3 -c "print(1)"', dialect=PW_DIALECT)
    assert "python3" not in argv[-1] and "python -c" in argv[-1]

    # 合法 pwsh 语法不得被改写，unix 动词也不得被 unix→cmd 映射
    got = build_confined_argv("Get-ChildItem | Select-Object -First 3", dialect=PW_DIALECT)
    assert "Get-ChildItem" in got[-1] and "Select-Object" in got[-1]
    assert "ls -la" in build_confined_argv("ls -la", dialect=PW_DIALECT)[-1]


@pytest.mark.skipif(not PWSH, reason="PowerShell 7 not installed")
def test_confined_default_branch_also_normalizes():
    """`dialect="bash"` + pwsh 可用这一支过去也是裸串直传（审计 A3）。

    `run_command` 在 pwsh 宿主仍暴露且走本支 ⇒ 不接上则默认受限模式下
    `python3` 仍原样落 pwsh。注意：仍**不得**做 unix 动词映射（verbatim 契约）。
    """
    argv = build_confined_argv("python3 -c 1 && ls -la", dialect="bash")
    assert "python3" not in argv[-1], argv[-1]
    assert "python -c 1" in argv[-1]
    assert "ls -la" in argv[-1], "unix 动词不得被映射（verbatim 契约）"


# ── AC3：端到端真跑（只认 exit_code；stderr 文案仅辅助）────────


@pytest.mark.asyncio
@pytest.mark.skipif(not PWSH, reason="PowerShell 7 not installed")
async def test_e2e_python3_through_real_path(tmp_path):
    """端到端经**真实入口**执行 ⇒ exit_code=0。

    ⚠ **这不是判别器，是回归网**（审计 A2 订正后重写）：旧版自己调
    `_normalize_command` 再喂 shell —— 撤掉真实入口里的接线它照样绿（自证式）。
    改为走真实入口后至少覆盖接线；但本机 `python3` 本就能解析 ⇒ **改造前也是 0**，
    无 before/after 判别力（真判别力在 AC1/AC2 的 argv 断言）。
    """
    res = await B._run_native(
        'python3 -c "print(1)"', str(tmp_path), 60, dialect=PW_DIALECT
    )
    assert res.get("exit_code") == 0, res
    assert "1" in (res.get("stdout") or ""), res


# ── AC4：两处提示面都要带这句（pwsh_main 是整体覆盖，不继承）────


def test_both_prompt_surfaces_mention_python3():
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS

    assert "python3" in BASH_DESCRIPTION
    assert "Dialect contract" in BASH_DESCRIPTION
    assert "python3" in TOOL_PARAM_SCHEMAS["pwsh"]["properties"]["command"]["description"]
    assert "python3" in TOOL_PARAM_SCHEMAS["pwsh_main"]["description"]
