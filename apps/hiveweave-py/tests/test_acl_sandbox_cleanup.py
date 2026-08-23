"""ACL 沙箱清理工具单测（spec §7.4）—— 非 Windows 也可跑的错误路径。"""

from __future__ import annotations

import pytest

from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox import cleanup


def test_capability_sid_prefix():
    """本沙箱能力 SID 专属区间（spec §4.3）。"""
    assert cleanup._CAPABILITY_SID_PREFIX == "S-1-4-"


def test_clean_tree_missing_root_raises():
    """目标不存在 → fail-closed 报错（不静默）。"""
    with pytest.raises(SandboxUnavailableError):
        cleanup.clean_tree("Z:/definitely/not/exists/__x__", dry_run=True)


def test_clean_tree_rejects_relative(tmp_path):
    """相对路径无意义 —— 本工具只处理绝对目标（缺失路径仍报错）。"""
    with pytest.raises(SandboxUnavailableError):
        cleanup.clean_tree("relative/x", dry_run=True)
