"""宿主环境探测 — 类型定义（platform-issue-remediation Phase 0）。

把散落 8 文件 20+ 处的 ``if windows`` 判断收敛成启动时一次能力探测。
参照 deepseek-harness 两套机制里的「注册式」（runtime-diagnostics/invariants
``register(packageName, installer)``：重复注册抛错、返回 disposer），
不抄 sandbox-local 的硬编码 —— TS 有编译期穷尽检查兜底，Python 没有。

三值语义（抄 DSH sandbox 的精髓，与语言无关）：

- ``ProbeResult.level`` 只有 ``full | partial`` 两值；
- **不可知不塞进等级字段而是抛错** —— 探测函数返回合法 ProbeResult 或抛
  :class:`CapabilityUnavailableError`，没有第三种（DSH 的
  ``SandboxUnavailableError``，抛点 sandbox-local:494）；
- 探测异常 = 不可用，不是「大概能用」（fail-closed）。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class CapabilityLevel(str, Enum):
    """能力等级。只有两值 —— 「不可知」走异常，不在这里。"""

    FULL = "full"
    PARTIAL = "partial"


class ProbeTiming(str, Enum):
    """探测时机。

    - ``STARTUP``：影响工具暴露与策略（platform.* / shell.pwsh / sandbox.acl），
      必须在 Agent 起来前定完（T3.2 用探测结果决定 allowed_tools）→ 启动时跑。
    - ``LAZY``：只影响诊断展示（toolchain.* / workspace.*）→ 首次访问时懒跑
      并缓存（DSH ``??= chainVerdict()``，sandbox-local:492）。
    """

    STARTUP = "startup"
    LAZY = "lazy"


@dataclass(frozen=True)
class ProbeResult:
    """单次探测结果（不可变对象）。

    ``data`` 是探测附带的只读事实（版本号、路径、解析出的数字），供消费方
    使用；等级判定本身的依据写进 ``detail``，不要只在 data 里。
    """

    name: str
    level: CapabilityLevel
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen dataclass 包一层只读映射：外部改原字典、对 data 原地写入
        # 都被挡掉（审计 P2-4：浅拷贝挡不住 r.data['a']=999）。
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


class CapabilityUnavailableError(Exception):
    """能力不可用 —— fail-closed 的表达形式。

    与「partial（降级可用）」的边界：partial = 能力在，但只能管控一部分；
    unavailable = 能力不存在 / 探测失败 / 与本平台不适用。``reason`` 是给
    日志与消费方看的机器可读短语（如 ``"windows-only"`` / ``"timeout"`` /
    ``"pwsh-not-on-path"``），``probe`` 是探测项名。
    """

    def __init__(self, message: str, *, probe: str = "", reason: str = "unknown") -> None:
        super().__init__(message)
        self.probe = probe
        self.reason = reason


#: 探测函数签名：``fn(*, timeout_s: float, **params) -> ProbeResult``。
#: 必须真跑一次命令（DSH：spawnSync + ``-- true``，status===0 才算过），
#: 不看文件存在与否；失败/超时抛 :class:`CapabilityUnavailableError`。
ProbeFn = Callable[..., ProbeResult]
