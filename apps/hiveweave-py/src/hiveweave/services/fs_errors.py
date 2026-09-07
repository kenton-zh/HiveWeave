"""FS 错误码分类学 —— DSH fsio 11 码的最小落地集（46/11 #4，2026-09-08）。

目标（DSH 教条）：编排层要能区分「**证据未产生**（观察缺席）」与
「**证据不可读**（有产生记录但读失败）」——两者处置完全不同（漏步补做
vs 权限/锁排障）。以前 read_file 只回一句自然语言错误，上层无法机判。

- ``classify_oserror``：OSError → 稳定错误码（跨 Windows/POSIX errno 归一）
- ``observed_absent``：**观察到缺席**事件——与报错正交；read_file 在
  ENOENT 时先发事实（``fs.absent``，进 L3 总线可触发按事实唤醒），
  再返回带 ``error_code="NOT_FOUND"`` 的失败结果。
"""

from __future__ import annotations

import errno

# 稳定错误码（勿改值：回执/取证/事实均引用）
NOT_FOUND = "NOT_FOUND"  # ENOENT / ENOTDIR —— 路径不存在
PERMISSION_DENIED = "PERMISSION_DENIED"  # EACCES / EPERM
NOT_A_DIRECTORY = "NOT_A_DIRECTORY"  # 父段是文件（ENOThreadId 未用，独立文案）
IS_A_DIRECTORY = "IS_A_DIRECTORY"
UNKNOWN = "FS_UNKNOWN"

# read_file 失败结果里承载错误码的键（工具结果 dict 顶层）
ERROR_CODE_KEY = "error_code"


def classify_oserror(exc: OSError) -> str:
    """OSError → 稳定错误码。Windows 的 WinError 已被 Python 映射成子类。"""
    if isinstance(exc, FileNotFoundError):
        return NOT_FOUND
    if isinstance(exc, IsADirectoryError):
        return IS_A_DIRECTORY
    if isinstance(exc, NotADirectoryError):
        return NOT_A_DIRECTORY
    if isinstance(exc, PermissionError):
        return PERMISSION_DENIED
    code = getattr(exc, "errno", None)
    if code in (errno.ENOENT, errno.ENOTDIR):
        return NOT_FOUND
    if code in (errno.EACCES, errno.EPERM):
        return PERMISSION_DENIED
    return UNKNOWN


def observed_absent(path: str, *, agent_id: str | None = None,
                    project_id: str | None = None,
                    source: str = "platform") -> None:
    """发布「观察到缺席」事实（不抛错，正交于失败返回）。

    编排语义：有产生记录 + 读失败 = 不可读；无产生记录 + absent = 漏步。
    本事件只回答后者。best-effort——总线/落库故障不影响原失败返回。
    """
    try:
        from hiveweave.services import fact_bus

        fact_bus.publish(
            "fs.absent",
            path,
            {"value": "absent", "agent_id": agent_id or ""},
            source=source,
            project_id=project_id,
        )
    except Exception:
        pass
