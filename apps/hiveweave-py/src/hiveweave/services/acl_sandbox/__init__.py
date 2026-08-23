"""ACL 写受限令牌沙箱（spec docs/spec/windows-acl-sandbox.md）。

公共 API：
- spawn_confined —— 受限执行编排入口（§5.6）
- SandboxUnavailableError —— fail-closed 异常
- WriteGrant / RestrictedTokenFactory / ConfinedRunner —— 底层原语（测试用）
- policy 模块 —— 边界源解析 + SID 组装（§5.5）

P0 状态：env 默认 off（未接线 bash 入口，§5.7 属 P1）；off 时行为与现状
逐字节一致。
"""

from __future__ import annotations

from hiveweave.services.acl_sandbox import sentinel, telemetry
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.grant import (
    CACHE_MASK,
    GRANT_MASK,
    WriteGrant,
)
from hiveweave.services.acl_sandbox.service import (
    REJECTION_DIALECT,
    ensure_standing_grants,
    revoke_agent_temp,
    shutdown_runner,
    spawn_confined,
)
from hiveweave.services.acl_sandbox.spawn import (
    ConfinedRunner,
    LongRunningJob,
)
from hiveweave.services.acl_sandbox.token import RestrictedTokenFactory

__all__ = [
    "CACHE_MASK",
    "GRANT_MASK",
    "ConfinedRunner",
    "LongRunningJob",
    "REJECTION_DIALECT",
    "RestrictedTokenFactory",
    "SandboxUnavailableError",
    "WriteGrant",
    "ensure_standing_grants",
    "revoke_agent_temp",
    "sentinel",
    "shutdown_runner",
    "spawn_confined",
    "telemetry",
]
