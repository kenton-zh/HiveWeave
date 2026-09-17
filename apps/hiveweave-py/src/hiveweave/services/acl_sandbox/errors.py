"""ACL 沙箱异常（spec docs/spec/windows-acl-sandbox.md §5.6 异常纪律）。"""

from __future__ import annotations


class SandboxUnavailableError(RuntimeError):
    """沙箱初始化/执行失败 —— fail-closed 的唯一出口。

    `service.spawn_confined` 只在「非 Windows / 配置关 / 项目级逃生门」三种情形
    返回 None（判定由 `policy.resolve_spawn_decision` 给出具名理由），
    其余一切异常（含意外 bug）都必须以本异常向上抛，绝不降级 native。
    """

    def __init__(self, message: str, *, api_name: str = "", win32_code: int | None = None):
        super().__init__(message)
        self.api_name = api_name
        self.win32_code = win32_code

    def _detail(self) -> str:
        detail = self.win32_code if self.win32_code is not None else "n/a"
        return (
            f"沙箱不可用，已拒绝执行（fail-closed）：{self}"
            f"{f' [API={self.api_name}, Win32Err={detail}]' if self.api_name else ''}"
        )

    def to_tool_dict(self, **extra) -> dict:
        """对齐 DSH Win32Error：工具层把错误对象转换为对 agent 的提示。

        L6（2026-09-11）：沙箱自身不可用 = 命令从未执行 = runner 故障（平台侧），
        不是模型参数错，故显式携带 ``fact="runner_failed"``，并经
        ``finalize_fact_dict`` 展开派生键（否则下游读 ``runner_failed`` KeyError）。

        ⚠ 返回**裸 dict** —— 只给"函数契约本来就是 dict"的调用方（`bash.py` 的
        `_run_registered_dev_server` 一族）。返回 `ToolResult` 的工具请用
        :meth:`to_tool_result`：混用会让调用方（与测试）拿到 dict 却按
        `ToolResult` 用（实测 `AttributeError: 'dict' object has no attribute
        'success'`）。

        ``**extra``（F5，2026-09-17）：给调用方带上**执行面事实**的口子 ——
        典型是 ``executed=False``（命令从未启动）。本方法**不自己推断**它
        （有没有进程是 `entry.spawn_agent_command` 才知道的事），只负责透传。
        """
        from hiveweave.tools.result import finalize_fact_dict

        return finalize_fact_dict({
            "success": False,
            "blocked": True,
            "fact": "runner_failed",
            "error": self._detail(),
            **extra,
        })

    def to_tool_result(self, **extra):
        """同一错误，**类型化**出口（`ToolResult`）—— 给返回 ToolResult 的工具用。

        ``**extra`` 同 :meth:`to_tool_dict`（F5：透传 ``executed`` 等执行面事实）。
        """
        from hiveweave.tools.result import ToolResult

        return ToolResult(
            success=False,
            output="",
            error=self._detail(),
            blocked=True,
            fact="runner_failed",
            extra=extra,
        )
