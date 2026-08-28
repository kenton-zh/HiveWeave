"""宿主环境探测框架（platform-issue-remediation Phase 0）。

启动时把「这个宿主能做什么」探成一份不可变结果，供 T3.2（Windows 下
allowed_tools 移除 bash）/ T3.3（私有缓存目录）等消费。把散落 8 文件
20+ 处、4 种写法的 ``if windows`` 收敛到这里（迁移纪律：逐步替换，每替
一处跑一次测试，不一次性全改）。

公开 API：

- :func:`register_builtin_probes` — 注册内置探测项（幂等）；
- :func:`run_startup_probes` — lifespan 启动时跑全部 STARTUP 探测项；
- :func:`get_capability` / :func:`aget_capability` — 按名取结果
  （STARTUP 必须已跑过；LAZY 首访真跑 ``??=``）；
- :func:`capability_snapshot` — 诊断视图。
"""
from __future__ import annotations

from .probes import register_builtin_probes
from .registry import ProbeEntry, all_entries, register, unregister
from .runner import (
    DEFAULT_PROBE_TIMEOUT_S,
    aget_capability,
    capability_snapshot,
    evict_cache,
    get_capability,
    reset_runner,
    run_command,
    run_startup_probes,
)
from .tools_filter import (
    filter_tools_for_host,
    host_hidden_tools,
    host_tool_deny_reason,
)
from .types import (
    CapabilityLevel,
    CapabilityUnavailableError,
    ProbeFn,
    ProbeResult,
    ProbeTiming,
)

__all__ = [
    "DEFAULT_PROBE_TIMEOUT_S",
    "CapabilityLevel",
    "CapabilityUnavailableError",
    "ProbeEntry",
    "ProbeFn",
    "ProbeResult",
    "ProbeTiming",
    "aget_capability",
    "all_entries",
    "capability_snapshot",
    "evict_cache",
    "filter_tools_for_host",
    "get_capability",
    "host_hidden_tools",
    "host_tool_deny_reason",
    "register",
    "register_builtin_probes",
    "reset_runner",
    "run_command",
    "run_startup_probes",
    "unregister",
]
