"""s3-clone_07 报告 P0-1：`.hiveweave/sandbox-temp` 进护栏白名单（两层同步）。

沙箱把 TEMP 设为 `<workspace>/.hiveweave/sandbox-temp/<agent>`（policy.py:27），
引导话术教 agent 往里写（bash.py:86-96）；但 bash 与 file 两个护栏的白名单都只有
shared|reports|drafts|worktrees|handoffs——两条正当策略相乘成死锁：7 Agent 撞 11 次，
出现 icacls 提权未遂、git restore 绕行。

契约：
- bash 层（_check_hiveweave_command）与 file 层（_check_hiveweave_dir）都放行
  sandbox-temp 内的读写删（agent 私有 scratch）
- 前缀目录（sandbox-temp2/）、其它系统文件（data.db/env.sh/tool_outputs）仍拦
- 教训（审计 [A2]）：护栏动词表与白名单必须与平台强制方言/引导话术对齐
"""

from __future__ import annotations

from hiveweave.tools.bash import _check_hiveweave_command
from hiveweave.tools.file import _check_hiveweave_dir

WS = "D:/proj/demo"


def test_file_layer_allows_sandbox_temp():
    assert _check_hiveweave_dir(f"{WS}/.hiveweave/sandbox-temp/A318/scratch.txt", WS) is False
    assert _check_hiveweave_dir(f"{WS}/.hiveweave/sandbox-temp/A318/tmp/deep/x.bin", WS) is False


def test_file_layer_still_protects_system_files():
    assert _check_hiveweave_dir(f"{WS}/.hiveweave/data.db", WS) is True
    assert _check_hiveweave_dir(f"{WS}/.hiveweave/tool_outputs/x", WS) is True
    assert _check_hiveweave_dir(f"{WS}/.hiveweave/env.sh", WS) is True


def test_bash_layer_allows_sandbox_temp_ops():
    BS = chr(92)
    assert _check_hiveweave_command(f"echo x > .hiveweave{BS}sandbox-temp{BS}A318{BS}f.txt") is False
    assert _check_hiveweave_command(f"cat .hiveweave{BS}sandbox-temp{BS}A318{BS}f.txt") is False


def test_bash_layer_prefix_and_system_files_stay_blocked():
    BS = chr(92)
    # 前缀目录（logs-backup 同款逻辑）：sandbox-temp2 / sandbox-temp.old 必须仍拦
    assert _check_hiveweave_command(f"cat .hiveweave{BS}sandbox-temp2{BS}x") is True
    assert _check_hiveweave_command(f"cat .hiveweave{BS}sandbox-temp.old{BS}x") is True
    assert _check_hiveweave_command("cat .hiveweave/data.db") is True
