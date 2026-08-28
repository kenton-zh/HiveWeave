"""内置探测项注册（幂等 —— main.py lifespan 在测试中会多次运行）。"""

from __future__ import annotations

_registered = False


def register_builtin_probes() -> None:
    """注册全部内置探测项；重复调用是 no-op（同 hooks.register_builtin_handlers）。"""
    global _registered
    if _registered:
        return
    from hiveweave.services.host_env import registry
    from hiveweave.services.host_env.probes import (  # noqa: F401
        platform as _platform_probes,
        sandbox as _sandbox_probes,
        shells as _shell_probes,
        toolchain as _toolchain_probes,
        workspace as _workspace_probes,
    )
    from hiveweave.services.host_env.types import ProbeTiming

    registry.register(
        "platform.os",
        _platform_probes.probe_platform_os,
        timing=ProbeTiming.STARTUP,
        description="操作系统族（Windows/Linux/Darwin）",
    )
    registry.register(
        "platform.arch",
        _platform_probes.probe_platform_arch,
        timing=ProbeTiming.STARTUP,
        description="CPU 架构",
    )
    registry.register(
        "shell.pwsh",
        _shell_probes.probe_shell_pwsh,
        timing=ProbeTiming.STARTUP,
        description="PowerShell 7+（≥7.6 full，翻译层建议实测版本）；T3.2 消费",
    )
    registry.register(
        "shell.git_bash",
        _shell_probes.probe_shell_git_bash,
        timing=ProbeTiming.STARTUP,
        description="Git for Windows 的 MSYS bash",
    )
    registry.register(
        "sandbox.acl",
        _sandbox_probes.probe_sandbox_acl,
        timing=ProbeTiming.STARTUP,
        description="Windows ACL 沙箱（icacls 回读验证）",
    )
    registry.register(
        "toolchain.git",
        _toolchain_probes.probe_toolchain_git,
        timing=ProbeTiming.LAZY,
        description="git 版本",
    )
    registry.register(
        "toolchain.node",
        _toolchain_probes.probe_toolchain_node,
        timing=ProbeTiming.LAZY,
        description="node 版本",
    )
    registry.register(
        "toolchain.npm",
        _toolchain_probes.probe_toolchain_npm,
        timing=ProbeTiming.LAZY,
        description="npm 版本",
    )
    registry.register(
        "workspace.cache_writable",
        _workspace_probes.probe_workspace_cache_writable,
        timing=ProbeTiming.LAZY,
        description="项目缓存目录可写性（path 参数）；T3.3 前置",
    )
    _registered = True
