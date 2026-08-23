"""ACL 沙箱异常（spec docs/spec/windows-acl-sandbox.md §5.6 异常纪律）。"""

from __future__ import annotations


class SandboxUnavailableError(RuntimeError):
    """沙箱初始化/执行失败 —— fail-closed 的唯一出口。

    service.spawn_confined 只在「非 Windows / 配置关」两种情形返回 None，
    其余一切异常（含意外 bug）都必须以本异常向上抛，绝不降级 native。
    """

    def __init__(self, message: str, *, api_name: str = "", win32_code: int | None = None):
        super().__init__(message)
        self.api_name = api_name
        self.win32_code = win32_code

    def to_tool_dict(self) -> dict:
        """对齐 DSH Win32Error：工具层把错误对象转换为对 agent 的提示。"""
        detail = self.win32_code if self.win32_code is not None else "n/a"
        return {
            "success": False,
            "blocked": True,
            "error": (
                f"沙箱不可用，已拒绝执行（fail-closed）：{self}"
                f"{f' [API={self.api_name}, Win32Err={detail}]' if self.api_name else ''}"
            ),
        }
