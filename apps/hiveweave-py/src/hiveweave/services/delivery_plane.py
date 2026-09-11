"""交付物平面（delivery plane）—— 权威事实 + 视觉门降档。

**问题（fixplan §6 #12）**：``policy_from_submit_gate`` 是纯查表，只认
submitGate 枚举，**不认识"这个项目的交付物是什么平面"**。后果：一个
native-desktop / cli / library 项目如果把 gate 填成 ``module_visual``，平台会
给它派 ``ui_browser_e2e``（浏览器端到端）—— 而该项目根本没有 web 界面，
gate 变成"跑不通就赖执行者"的不可满足验收。

**修法**：引入独立的「交付物平面」权威事实，视觉门遇非 web 平面时**降档**
到对应的非视觉门，且**降档必须显式留痕**（tag + 回执），不许静默改写。

判据来源 = 我们自己的既有实现（fixplan §10 纪律：外部参照 DSH 无此场景，
不作判据）：
- 项目级元数据权威源 = per-project DB ``project_meta``（``api/projects.py:486``
  ``_fetch_project_meta``；正典 DDL ``db/schema.py`` 的 ``project_meta`` 建表）
- 任务 tag 平面 = ``tasks`` 表的 tags 列（次选，任务未带项目级事实时兜底）

**平面取值**（本模块是唯一定义处，测试直接引用本清单，不手抄）：
``web`` / ``native-desktop`` / ``game-engine`` / ``cli`` / ``library``
"""
from __future__ import annotations

from typing import Any

# ── 平面枚举 ────────────────────────────────────────────────
# 本清单是唯一权威定义（正典列定义在本模块抬头文档；测试引用 DELIVERY_PLANES）
DELIVERY_PLANES: tuple[str, ...] = (
    "web",
    "native-desktop",
    "game-engine",
    "cli",
    "library",
)

# 视觉门 → 降档目标（非 web 平面时）。键是 policy_id，值是 (降档 policy_id)。
# 只列**视觉门**：generic_tests / docs_only / code_audit 与平面无关，不动。
VISUAL_POLICY_DOWNGRADE: dict[str, str] = {
    "ui_browser_e2e": "generic_tests",
    "code_audit_visual": "code_audit_unit",
}

# 项目级事实落点（project_meta 列名）。正典 DDL + 懒迁移见 db/schema.py。
DELIVERY_PLANE_COLUMN = "delivery_plane"

# 任务级 tag 前缀（次选平面事实）：`plane:web` / `plane:cli` …
PLANE_TAG_PREFIX = "plane:"


def normalize_delivery_plane(raw: Any) -> str | None:
    """归一化平面取值；非法/空 → None（**不猜测**：None 表示"未知"，不是 web）。

    宽容写法（大小写、下划线）归一到连字符形态，避免 "native_desktop" 这类
    自然变体被当成未知而静默放过降档。
    """
    if raw is None:
        return None
    s = str(raw).strip().lower().replace("_", "-").replace(" ", "-")
    if not s:
        return None
    return s if s in DELIVERY_PLANES else None


def plane_from_tags(tags: list[str] | tuple[str, ...] | None) -> str | None:
    """从任务 tag 里取次选平面事实（``plane:<x>``）；无 → None。"""
    for t in tags or []:
        t_l = str(t).strip().lower()
        if t_l.startswith(PLANE_TAG_PREFIX):
            plane = normalize_delivery_plane(t_l[len(PLANE_TAG_PREFIX):])
            if plane:
                return plane
    return None


def resolve_delivery_plane(
    *,
    project_plane: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
) -> str | None:
    """解析交付物平面：**项目级字段优先，任务 tag 次选**。

    两者都缺 → None（未知）。未知时**不降档**（保持既有行为，避免拿一个
    猜出来的平面去改写用户的 gate —— 那正是"静默"的一种）。
    """
    return normalize_delivery_plane(project_plane) or plane_from_tags(tags)


def downgrade_policy_for_plane(
    policy_id: str, plane: str | None
) -> tuple[str, str | None]:
    """视觉门遇非 web 平面 → 降档。返回 ``(生效 policy_id, 留痕原因 | None)``。

    - ``plane`` 为 ``web`` / None → 原样返回（未知不降档）。
    - 非 web 且 policy_id 是视觉门 → 返回降档目标 + 显式原因串（调用方必须
      把它写进任务 tag 与回执，**不许静默**）。
    - 非视觉门 → 原样返回。
    """
    if plane is None or plane == "web":
        return policy_id, None
    target = VISUAL_POLICY_DOWNGRADE.get(policy_id)
    if not target:
        return policy_id, None
    reason = (
        f"delivery plane '{plane}' is not web — visual gate '{policy_id}' "
        f"downgraded to '{target}' (no browser surface to exercise)."
    )
    return target, reason


def downgrade_tag(policy_id: str, target: str, plane: str) -> str:
    """降档留痕 tag（写入任务 tags，使降档在任务账本上可见、可审计）。"""
    return f"gate_downgraded:{policy_id}->{target}@plane={plane}"


async def fetch_project_plane(project_id: str | None) -> str | None:
    """读项目级交付物平面事实（``project_meta.delivery_plane``）。

    读取失败/无项目/列为空 → None（**未知不降档**，不猜）。best-effort：
    平面是"锦上添花"的降档依据，读不到就退回既有行为，不因它引入新故障面。
    """
    if not project_id:
        return None
    try:
        from hiveweave.db import project as project_db

        conn = await project_db.get_project_db_by_project_id(str(project_id))
        cursor = await conn.execute(
            f"SELECT {DELIVERY_PLANE_COLUMN} FROM project_meta WHERE project_id = ?",
            [str(project_id)],
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        return normalize_delivery_plane(row[0])
    except Exception:  # noqa: BLE001 — 平面读取 best-effort，缺失即未知
        return None


async def resolve_and_downgrade(
    policy_id: str,
    *,
    project_id: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, str | None, str | None]:
    """选路的**唯一收口**：解析平面 → 视觉门降档。

    返回 ``(生效 policy_id, 留痕 tag | None, 降档原因 | None)``。
    调用方（create / dispatch / api 三处）必须把 tag 写进任务、把原因写进
    回执 —— 降档**不许静默**。
    """
    plane = resolve_delivery_plane(
        project_plane=await fetch_project_plane(project_id), tags=tags
    )
    effective, reason = downgrade_policy_for_plane(policy_id, plane)
    tag = (
        downgrade_tag(policy_id, effective, plane)
        if reason and plane
        else None
    )
    return effective, tag, reason
