"""ACL 写受限令牌沙箱（spec docs/spec/windows-acl-sandbox.md）。

公共 API：
- **spawn_agent_command —— agent 命令 spawn 的唯一入口**（#1 治本：判定+路由+盖戳）
- spawn_confined —— 受限执行编排入口（§5.6；调用方一般经 `spawn_agent_command`）
- resolve_spawn_decision / SpawnDecision —— 执行面判定（**唯一判定点**，
  理由闭合枚举 + 强度上报字段）
- SandboxUnavailableError —— fail-closed 异常
- WriteGrant / RestrictedTokenFactory / ConfinedRunner —— 底层原语（测试用）
- policy 模块 —— 边界源解析 + SID 组装（§5.5）

P0 状态：env 默认 off（未接线 bash 入口，§5.7 属 P1）；off 时行为与现状
逐字节一致。
"""

from __future__ import annotations

from hiveweave.services.acl_sandbox import sentinel, telemetry
from hiveweave.services.acl_sandbox.entry import (
    RoutedSpawn,
    SpawnContext,
    spawn_agent_command,
)
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.grant import (
    CACHE_MASK,
    GRANT_MASK,
    WriteGrant,
)
from hiveweave.services.acl_sandbox.policy import (
    SpawnDecision,
    make_decision,
    resolve_spawn_decision,
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
    "RoutedSpawn",
    "SandboxUnavailableError",
    "SpawnContext",
    "SpawnDecision",
    "WriteGrant",
    "ensure_standing_grants",
    "make_decision",
    "resolve_spawn_decision",
    "revoke_agent_temp",
    "sentinel",
    "shutdown_runner",
    "spawn_agent_command",
    "spawn_confined",
    "telemetry",
]
