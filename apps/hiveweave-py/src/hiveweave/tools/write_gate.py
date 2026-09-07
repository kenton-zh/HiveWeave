"""进程内写路径闸 —— DSH ``isConcurrencySafe`` 谓词的落地形态（46/11 #6）。

背景：子代理与父 agent **共享 worktree**（``tools/subagent.py`` 提示词明示
「concurrent writes to the same files collide」），并发写同一文件会互相
践踏；提示词劝阻已被实战证明不够（Sync refused ×7，五修未收敛）。

设计（照搬 DSH 谓词思路，进程内最小实现）：
- **谓词**：每调用声明自己能否安全并发。读类与其余工具默认 ``True``；
  写类工具（write_file / edit_file / apply_patch）仅当目标路径**当前没有
  在飞的写**时才 ``True``——串行的是同一文件的写，不是整个任务。
- **advisory 不阻断**：冲突时返回结构化冲突文案（含出路：稍后重试/换文件/
  走任务协调），不抛异常、不进硬门计数——对齐 DSH「advice, never a block」。
- **registry**：``{normcase(normpath(abs_path))}`` 集合，acquire/release
  配对（调用方 ``try/finally``）。进程内有效即可——单后端进程是全部写
  流量的必经点；跨进程冲突由 git 层/文件锁兜底，不在本闸职责。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()

#: 声明并发安全性的写类工具集合（其余工具默认 safe）。
WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "apply_patch"})


def _key(file_path: str, workspace: str) -> str:
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(workspace) / p
    # normcase：Windows 大小写/短路径差异归一，同一文件只会有一个键
    return os.path.normcase(os.path.normpath(str(p)))


def is_held(file_path: str, workspace: str) -> bool:
    """该路径当前是否有在飞的写。"""
    with _LOCK:
        return _key(file_path, workspace) in _INFLIGHT


def is_concurrency_safe(tool_name: str, file_path: str | None, workspace: str) -> bool:
    """DSH ``isConcurrencySafe(args)`` 对应物：该调用能否安全并发执行。

    非写类工具恒 ``True``；写类工具看目标路径是否在飞。供调度器/闸共用。
    """
    if tool_name not in WRITE_TOOLS or file_path is None:
        return True
    return not is_held(file_path, workspace)


def try_acquire(file_path: str, workspace: str) -> bool:
    """抢到该路径的写权返回 True；已被占（他人 in-flight）返回 False。"""
    k = _key(file_path, workspace)
    with _LOCK:
        if k in _INFLIGHT:
            return False
        _INFLIGHT.add(k)
        return True


def release(file_path: str, workspace: str) -> None:
    """释放写权。调用方必须 ``try/finally`` 配对，防异常泄漏持有。"""
    with _LOCK:
        _INFLIGHT.discard(_key(file_path, workspace))


def conflict_message(file_path: str) -> str:
    """advisory 冲突回执（不阻断语义：给行为出口，模型可换路重试）。"""
    return (
        f"write conflict: another concurrent writer (subagent sharing this "
        f"worktree, or the parent agent) is writing `{file_path}` right now; "
        f"this call was NOT executed. Options: (1) retry in a moment, "
        f"(2) write to a different file and merge later, or (3) coordinate "
        f"ownership of this file via task/chat before writing. "
        f"[write_gate/concurrency]"
    )


def inflight_paths() -> list[str]:
    """当前在飞写路径快照（观测用）。"""
    with _LOCK:
        return sorted(_INFLIGHT)


def reset_for_tests() -> None:
    with _LOCK:
        _INFLIGHT.clear()
