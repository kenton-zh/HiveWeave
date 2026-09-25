"""平台级工具入参形状归一（TEST_DSH_70 P1-6）。

71 轮实测 **103 条** ``{"item": …}`` 群体形态（commit_turn 67 + submit_task 35
+ create_task 1，6 agent），上游各自为战：acceptance 门内批1 已解包
（``services/tasks/acceptance.py::_unwrap_single_key_ids``），门外的工具入参
仍被静默丢弃（``turn_tools.py`` 的 isinstance 检查 + 裸 except 吞）。

本模块把「单键群体形态解包」提取成 services / tools 两层都可引的叶子：
**不 import 任何 hiveweave 兄弟模块**（连 structlog 都不引）⇒ 无循环依赖。

口径（与批1 acceptance P0-1b 一致，勿漂移）：

- dict **恰好一个未知键**且其值是 ``str | list | tuple`` ⇒ 解包采用内层；
- 多键 dict **禁止** ``values()`` 展平（歧义）⇒ 原样交回，由调用方/pydantic
  按形状错拒绝（fail-closed，不是静默吞）；
- ``int`` / 嵌套 ``dict`` 等一律不解包 —— 保持修前 fail-closed 语义。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "unwrap_single_key_group",
    "coerce_list_param",
    "shape_summary",
]


def shape_summary(value: Any) -> str:
    """入参形状的一句话摘要（用于拒绝回执/日志，不打印全量内容）。"""
    if isinstance(value, dict):
        return "dict(keys=" + ",".join(sorted(str(k) for k in value))[:120] + ")"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def unwrap_single_key_group(
    value: Any, *, known_keys: tuple[str, ...] = ()
) -> Any | None:
    """``{"item": […]}`` 单键群体形态解包。

    dict **恰好一个未知键**（``known_keys`` 里的键不计入）且内层值是
    ``str | list | tuple`` ⇒ 返回内层值；其余一律 ``None``（多键歧义禁展平、
    非 str/list/tuple 内层保持 fail-closed —— 与批1 acceptance 同口径）。
    """
    if not isinstance(value, dict):
        return None
    known = set(known_keys)
    unknown = [k for k in value if k not in known]
    if len(unknown) != 1:
        return None
    inner = value[unknown[0]]
    if isinstance(inner, (str, list, tuple)):
        return inner
    return None


def _coerce_str_list(raw: str) -> list[str]:
    """字符串入参 → list：JSON 数组解析；失败则整串当单元素（tools.helpers
    ``coerce_to_list`` 的既有口径，本地复刻以保持本模块零内部依赖）。"""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [raw]
    if isinstance(parsed, list):
        return parsed
    return [raw]


def coerce_list_param(
    value: Any, *, known_keys: tuple[str, ...] = ()
) -> Any:
    """list 型工具参数的平台级归一口径（pydantic ``mode="before"`` 校验器用）。

    - ``None`` → ``None``（未传）；
    - str → JSON 数组解析或单元素列表（既有 ``coerce_to_list`` 口径）；
    - ``{"item": […]}`` 群体单键 dict → 解包成 list（P1-6 主修）；
    - list / tuple → list；
    - **多键 dict 不展平**：原样交回 —— pydantic 会按「should be a valid
      list」报形状错，调用方拿到的是显式拒绝而非静默丢数据。

    ``known_keys``：解包时不计入的合法键（如 ``attestation_ids`` 声明 dict）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _coerce_str_list(value)
    if isinstance(value, dict):
        inner = unwrap_single_key_group(value, known_keys=known_keys)
        if inner is None:
            return value
        if isinstance(inner, str):
            return _coerce_str_list(inner)
        return list(inner)
    if isinstance(value, (list, tuple)):
        return list(value)
    return value
