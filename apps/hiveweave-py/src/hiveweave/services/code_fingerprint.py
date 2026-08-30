"""Platform source-code fingerprint (F14 — 回归闭环的平台侧).

背景（r4 审计）：修复代码写对了但没提交、进程没重载，旧代码继续跑——
「改了不知道改没改、改了不知道生效没生效」是四轮报告反复出现的根因之一。
本模块把「已重载」做成可查询事实位：

- 启动时记录一次源码树指纹（关键 ``.py`` 文件 mtime 集合的 hash）,
  进程存活期间保持不变。
- ``code_drift()`` 重新计算当前指纹并与启动指纹比对 —— 不一致即
  「源码变更后未重载」，由 ``get_platform_state`` 显著告警。

设计约束：
- 开销小：只 hash mtime（不含内容），启动一次 + 查询时一次，全量扫描
  每次约 300 个 py 文件，Windows SSD 上可忽略。
- 只覆盖平台后端源码树（`apps/hiveweave-py/src/hiveweave/`），不含
  data.db / .pyc / venv —— 防止业务数据写入制造假阳性。
- best-effort：任何失败返回 ``drift=False, reason=...``（观测告警
  的存在不得阻塞平台）。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# 平台后端源码根 — 本文件在 src/hiveweave/services/code_fingerprint.py
_SRC_ROOT = Path(__file__).resolve().parents[2]

# 忽略的后缀/目录：编译产物、数据、测试隔离目录。
_IGNORE_SUFFIXES = (".pyc", ".pyo")
_IGNORE_DIRS = {"__pycache__", ".venv", "venv", "node_modules"}

# 文件级启动快照：rel → (mtime, size)，drift 时直接列出变更文件。
_startup_snapshot: dict[str, tuple[int, int]] = {}
_startup_fingerprint: str | None = None
_startup_at_ms: int = 0
_notified: set[str] = set()


def _snapshot_now() -> dict[str, tuple[int, int]]:
    snap: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(_SRC_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fn in filenames:
            if fn.endswith(_IGNORE_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
                snap[os.path.relpath(full, _SRC_ROOT)] = (int(st.st_mtime), int(st.st_size))
            except OSError:
                continue
    return snap


def _hash_snapshot(snap: dict[str, tuple[int, int]]) -> str:
    dig = hashlib.sha256()
    for rel in sorted(snap):
        mtime, size = snap[rel]
        dig.update(rel.encode("utf-8", errors="replace"))
        dig.update(b"\0")
        dig.update(str(mtime).encode("utf-8", errors="replace"))
        dig.update(b"\0")
        dig.update(str(size).encode("utf-8", errors="replace"))
        dig.update(b"\0")
    return dig.hexdigest()[:16]


def record_startup_fingerprint() -> str | None:
    """Record the startup fingerprint. Call from lifespan startup."""
    global _startup_fingerprint, _startup_at_ms, _startup_snapshot
    try:
        _startup_snapshot = _snapshot_now()
        _startup_fingerprint = _hash_snapshot(_startup_snapshot)
        _startup_at_ms = int(time.time() * 1000)
        log.info(
            "code_fingerprint.recorded",
            fingerprint=_startup_fingerprint,
            src_root=str(_SRC_ROOT),
            file_count=len(_startup_snapshot),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("code_fingerprint.record_failed", error=str(e))
        _startup_fingerprint = None
    return _startup_fingerprint


def startup_fingerprint() -> str | None:
    """Recorded startup fingerprint; None when startup never recorded."""
    return _startup_fingerprint


def startup_at_ms() -> int:
    """Wall-clock ms of the recorded startup fingerprint."""
    return _startup_at_ms


def code_drift(detail: bool = True) -> dict[str, Any]:
    """Return ``{drift, reason, current, startup, changed_files}``.

    ``drift=True`` 表示源码树自启动以来发生过变更（很可能未重载）。
    best-effort：指纹不可算时返回 drift=False（fail-open，不误报）。
    """
    cur = _snapshot_now()
    base = _startup_snapshot
    base_fp = _startup_fingerprint
    cur_fp = _hash_snapshot(cur)
    if not base or base_fp is None:
        return {
            "drift": False,
            "reason": "fingerprint unavailable (startup not recorded)",
            "current": cur_fp,
            "startup": base_fp,
            "changed_files": [],
        }
    if cur_fp == base_fp:
        return {
            "drift": False,
            "reason": "",
            "current": cur_fp,
            "startup": base_fp,
            "changed_files": [],
        }
    changed: list[str] = []
    if detail:
        for rel in sorted(cur):
            base_entry = base.get(rel)
            if base_entry is None:
                changed.append(f"{rel} (new)")
            elif cur[rel] != base_entry:
                changed.append(f"{rel} (mtime/size changed)")
        for rel in sorted(base):
            if rel not in cur:
                changed.append(f"{rel} (removed)")
    changed = changed[:50]
    note = (
        "Platform source code changed on disk since process start — "
        "the running process may NOT include these changes. "
        "Restart the backend to load them. (F14 code fingerprint)"
    )
    if "drift" not in _notified:
        _notified.add("drift")
        log.warning("code_fingerprint.drift_detected", changed=changed[:30])
    return {
        "drift": True,
        "reason": note,
        "current": cur_fp,
        "startup": base_fp,
        "changed_files": changed,
    }