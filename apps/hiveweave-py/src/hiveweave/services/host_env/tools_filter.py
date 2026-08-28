"""宿主 → 工具暴露过滤（T3.2：Windows pwsh 宿主不暴露 bash/bash_main）。

依据（计划 §4 T3.2）：DSH 架构里 bash / pwsh 是两个一等工具，部署侧按
平台选用；HiveWeave 已有独立 pwsh 工具（无翻译层），差别只在暴露哪个。
Windows 且 pwsh 可用 → 从暴露清单移除 bash / bash_main（新建的 pwsh_main
顶替 MAIN 里程碑测试位）。翻译层代码保留不删（配置可逆、代码删除不可逆）。

实现路径选「工具注册层平台过滤」（计划三选一里的第 3 条）：不动存量
agent 行的 allowed_tools JSON，回滚 = 摘掉过滤。过滤点：
- ``permission.get_tools_for_agent``（模型可见清单）
- ``policy.tool_hard_deny``（纵深防御：模型按陈旧提示直调 bash 时给出
  可操作错误，指向 pwsh/pwsh_main）
"""
from __future__ import annotations

import sys

from .types import CapabilityLevel

#: Windows pwsh 宿主上被 pwsh / pwsh_main 顶替的工具
_SHELL_REPLACED_ON_WINDOWS = frozenset({"bash", "bash_main"})


def _windows_with_pwsh() -> bool:
    """Windows 且 pwsh 可用（以启动探测结果为准）。

    探测未跑 / 查询失败 → 返回 False（bash 系保持暴露，与历史行为一致）：
    生产路径 lifespan 必跑 ``run_startup_probes``，消费时点（Agent 工具表
    构建）总在启动之后；测试与直调环境无探测，保守回退保证零行为漂移
    （首版回退 True 曾误伤 4 个既存 policy/seal 测试 —— 审计后改为保守）。
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        from .runner import get_capability

        result = get_capability("shell.pwsh")
        return result.level in (CapabilityLevel.FULL, CapabilityLevel.PARTIAL)
    except Exception:
        return False


def host_hidden_tools() -> frozenset[str]:
    """本宿主不暴露的工具名集合。"""
    if _windows_with_pwsh():
        return _SHELL_REPLACED_ON_WINDOWS
    return frozenset()


def filter_tools_for_host(names: list[str] | set[str]) -> list[str]:
    """按宿主过滤工具暴露清单（顺序保持稳定）。"""
    hidden = host_hidden_tools()
    if not hidden:
        return list(names)
    return [n for n in names if n not in hidden]


def host_tool_deny_reason(tool_name: str) -> str | None:
    """宿主不可用工具的 deny 文案（tool_hard_deny 纵深防御用）。"""
    if tool_name in host_hidden_tools():
        return (
            f"Tool '{tool_name}' is not exposed on this host: Windows with "
            f"pwsh available runs PowerShell natively — use 'pwsh' (your "
            f"worktree) or 'pwsh_main' (project root / MAIN VERIFY) instead. "
            f"No unix→pwsh translation layer is provided for these."
        )
    return None
