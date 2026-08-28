"""workspace.* 探测项（LAZY，带 path 参数，按参数缓存）。

``workspace.cache_writable`` 是 T3.3（每 session 私有缓存目录）的前置：
- **full**：``<path>/.hiveweave-cache`` 可创建/已存在且探针文件可写可删；
- **partial**：缓存目录已存在但探针文件写入失败（只读/锁）；
- **unavailable**：path 不存在 / 缓存目录无法创建。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..types import CapabilityLevel, CapabilityUnavailableError, ProbeResult

# 与 acl_sandbox 共用同一常量，防两处漂移（T3.3 改私有缓存时也是这一处）。
from hiveweave.services.acl_sandbox.policy import CACHE_REL


def probe_workspace_cache_writable(
    *, path: str, timeout_s: float = 5.0, **_
) -> ProbeResult:
    """``workspace.cache_writable`` — 真写一个探针文件再删掉（不是 os.access 猜）。"""
    del timeout_s  # 纯文件系统操作，亚秒级；签名对齐 ProbeFn 契约
    if not path or not Path(path).is_dir():
        raise CapabilityUnavailableError(
            f"workspace path does not exist or is not a directory: {path!r}",
            probe="workspace.cache_writable",
            reason="path-missing",
        )
    cache_dir = Path(path) / CACHE_REL
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise CapabilityUnavailableError(
            f"cannot create cache dir {cache_dir}: {e}",
            probe="workspace.cache_writable",
            reason="cache-dir-unwritable",
        ) from e
    probe_file = cache_dir / f".host-env-probe-{os.getpid()}-{int(time.time() * 1000)}"
    data = {"cache_dir": str(cache_dir)}
    try:
        probe_file.write_text("probe", encoding="utf-8")
    except OSError as e:
        return ProbeResult(
            name="workspace.cache_writable",
            level=CapabilityLevel.PARTIAL,
            detail=f"cache dir exists but probe file write failed: {e}",
            data=data,
        )
    try:
        probe_file.unlink()
    except OSError as e:
        # 能写不能删 —— Windows 文件锁的典型前兆（T3.3 要治的 EPERM unlink）。
        return ProbeResult(
            name="workspace.cache_writable",
            level=CapabilityLevel.PARTIAL,
            detail=f"probe file written but unlink failed (lock?): {e}",
            data={**data, "leftover_probe": probe_file.name},
        )
    return ProbeResult(
        name="workspace.cache_writable",
        level=CapabilityLevel.FULL,
        detail="cache dir accepts write+unlink",
        data=data,
    )
