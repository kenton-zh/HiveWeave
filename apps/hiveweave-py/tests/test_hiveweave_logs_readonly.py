"""s3-clone_06 P1-7：`.hiveweave/logs` 只读放行（agent 的诊断出口）。

霁岚/汐然/栖迟三人共 8 次被「禁触 .hiveweave 系统目录」挡在门外——他们只是
想 `cat .hiveweave/logs/dev-server-*.log` 看自己的服务为什么没起来。看不到
日志 → 只能盲重试 start_dev_server（与 P2 browse/dev-server flake 耦合）。

契约：
- 只读（cat / Get-Content / type 等，无写标记）→ 放行
- 任何写（重定向 > >>、rm/echo/tee/cp/mv…）→ 仍拦
- 其它系统文件（data.db / env.sh）→ 仍拦
"""

from __future__ import annotations

from hiveweave.tools.bash import _check_hiveweave_command


def test_read_dev_server_log_is_allowed():
    assert _check_hiveweave_command("cat .hiveweave/logs/dev-server-8036.log") is False
    assert _check_hiveweave_command("type .hiveweave\\logs\\dev-server-8036.log") is False
    assert (
        _check_hiveweave_command(
            "Get-Content .hiveweave\\logs\\dev-server-18765.log -Tail 100"
        )
        is False
    )


def test_writes_into_logs_stay_blocked():
    assert (
        _check_hiveweave_command("cat .hiveweave/logs/dev-server.log > out.txt")
        is True
    )
    assert _check_hiveweave_command("echo hi >> .hiveweave/logs/x.log") is True
    assert _check_hiveweave_command("rm .hiveweave/logs/old.log") is True
    assert _check_hiveweave_command("cp a.txt .hiveweave/logs/a.txt") is True


def test_other_system_files_stay_blocked():
    assert _check_hiveweave_command("cat .hiveweave/data.db") is True
    assert _check_hiveweave_command("cat .hiveweave/env.sh") is True


def test_powershell_cmdlets_do_not_bypass_the_guard():
    """2026-09-01 实测漏洞（既存，非本轮引入）：原表只有 POSIX/DOS 动词，
    而本平台强制走 pwsh —— `Remove-Item .hiveweave/data.db` /
    `Out-File .hiveweave/data.db` 用平台指定方言就整条绕过护栏。
    只读 cmdlet（Get-Content / Get-ChildItem）不得入表，否则只读放行自毁。"""
    assert _check_hiveweave_command("Remove-Item .hiveweave/data.db") is True
    assert _check_hiveweave_command("Out-File .hiveweave/data.db") is True
    assert _check_hiveweave_command("Copy-Item x .hiveweave/data.db") is True
    assert _check_hiveweave_command("Set-Content .hiveweave/data.db -Value 1") is True
    # 管道组合：读日志 + 管道删 = 写，必须拦（全名与**别名**都要拦——
    # 别名只在 FILE_OPS 表内、写标记表漏了，就会被只读门放行进真删，审计 [3]）
    for verb in ("Remove-Item", "ri", "mi", "cpi"):
        assert (
            _check_hiveweave_command(
                f"Get-ChildItem .hiveweave\\logs -Recurse | {verb}"
            )
            is True
        ), f"alias {verb} must stay blocked"
    # 只读 cmdlet 仍放行（未被误纳入写操作表）
    assert (
        _check_hiveweave_command(
            "Get-Content .hiveweave\\logs\\dev.log -Tail 100"
        )
        is False
    )


def test_fd_duplication_is_not_a_write():
    """`2>&1` / `1>&2` 是 fd 复制不是写入 —— 误判会把常见的 stderr 合并
    读日志命令一起拦掉（本轮新增只读放行时踩到）。"""
    assert (
        _check_hiveweave_command("cat .hiveweave/logs/a.log 2>&1 | head -20")
        is False
    )
    assert _check_hiveweave_command("cat .hiveweave/logs/a.log 1>&2") is False
    # 真正的写重定向照拦
    assert _check_hiveweave_command("cat .hiveweave/logs/a.log > b.txt") is True


def test_harmless_listing_untouched():
    assert _check_hiveweave_command("ls .hiveweave") is False
    assert _check_hiveweave_command("cd .hiveweave") is False
