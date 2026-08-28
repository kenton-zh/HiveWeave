"""sandbox.acl 探测项 — Windows ACL 沙箱能力的真机验证。

等级语义（full/partial 都要求 icacls 真跑成功，差别在回读验证）：

- **full**：``icacls <自建临时目录>`` 退出 0 **且输出回读含该目录路径**
  （§4.11 grant 后读回验证同款纪律）；
- **partial**：icacls 跑得动但回读验证未通过（输出形态与预期不符 ——
  沙箱层可尝试管控，验证不完整）；
- **unavailable**：非 Windows（不适用）/ 配置关闭 / icacls 不在 PATH /
  执行异常。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..runner import run_command
from ..types import CapabilityLevel, CapabilityUnavailableError, ProbeResult

from hiveweave.config import settings


def probe_sandbox_acl(*, timeout_s: float = 5.0, **_) -> ProbeResult:
    """``sandbox.acl`` — 自建临时目录跑 icacls 并回读。"""
    if not sys.platform.startswith("win"):
        raise CapabilityUnavailableError(
            "sandbox.acl is a windows-only capability",
            probe="sandbox.acl",
            reason="windows-only",
        )
    if not settings.acl_sandbox:
        raise CapabilityUnavailableError(
            "ACL sandbox disabled by config (HIVEWEAVE_ACL_SANDBOX=off)",
            probe="sandbox.acl",
            reason="config-off",
        )
    probe_dir = tempfile.mkdtemp(prefix="hiveweave-probe-acl-")
    try:
        # icacls 输出走系统 ANSI 代码页（中文 Windows 是 GBK）：TEMP 含非
        # ASCII 时 utf-8 解码变乱码 → 回读恒 miss → 误报 partial（审计
        # P1-1 实证）。mbcs = 当前 ANSI 代码页，路径无损。
        listing = run_command(
            ["icacls", probe_dir], timeout_s=timeout_s, encoding="mbcs"
        )
    finally:
        try:
            Path(probe_dir).rmdir()
        except OSError:
            pass
    if listing.returncode != 0:
        raise CapabilityUnavailableError(
            f"icacls listing failed (exit {listing.returncode}): "
            f"{(listing.stderr or listing.stdout or '').strip()[:200]}",
            probe="sandbox.acl",
            reason="icacls-failed",
        )
    stdout = listing.stdout or ""
    # 回读验证：icacls 成功输出首行是「路径 + 依赖 ACE 列表」。
    if probe_dir.lower() in stdout.lower():
        return ProbeResult(
            name="sandbox.acl",
            level=CapabilityLevel.FULL,
            detail="icacls listing round-trip verified",
            data={"icacls": stdout.splitlines()[0][:160] if stdout else ""},
        )
    return ProbeResult(
        name="sandbox.acl",
        level=CapabilityLevel.PARTIAL,
        detail="icacls ran but read-back did not match probed dir "
               "(unexpected output shape)",
        data={"icacls_exit": listing.returncode},
    )
