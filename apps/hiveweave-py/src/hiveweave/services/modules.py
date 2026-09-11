"""Module service — 模块树 CRUD + 子树读取（批次 7 · 蓝图 §modules）。

## 为什么有这张表（判据来源：我们自己的设计文档，非 DSH）

蓝图的组织模型是 **CEO → 中层 → 叶子** 的编制，而**中层按「功能面」切分模块**
（`prompts/coordinator.py:148` Module Ownership Rule：每个工程师端到端拥有
一个功能模块）。此前 `modules` 表**只有 DDL、零写入方** —— 读取路由
`GET /api/org/modules` 永远返回空数组，是「一个永远说谎的 API」
（批次 5 实测；fixplan §6 #13 因此一度判它死表）。

批次 5 按裁决保留形状（`docs/AI工程组织_MVP蓝图.md:283-287`），**写侧交批次 7**：

    modules (id, project_id, name, path, description,
             parent_module_id REFERENCES modules(id),   -- 模块树
             status,                                    -- active|completed|archived
             current_agent_id REFERENCES agents(id),     -- 当前负责人
             created_at, updated_at)

## 与记忆下沉的关系（蓝图 `:299`）

`memories.module_id` 是**归档回指**：agent 解散时其私有记忆转 `scope='archive'`
并按 `module_id` 归属模块（`services/memory.py:584 archive_agent_memories`），
继任者按 `get_archived_memories(project_id, module_id)` 取回前任经验。
**这条链路此前是断的**：`archive` 层兜底把 NULL `module_id` 填成 `agent_id`
（M3 审计 2026-08-05），于是 `module_id` 里存的是**前任的 agent id**，
而不是真实模块 id —— 继任者「按模块取前任记忆」实际退化成「按前任 id 取」。
本模块提供真实模块 id 后，`bind_agent` / 归档可在写入时挂上真模块。

## 纪律

- 所有写走 `execute_by_project`（per-workspace 写锁 + rollback/re-raise）。
- 环检测在**写操作内部**做（fixplan §0 统一判据：约束要落在做那件事的操作里，
  不能靠调用方自觉）。
"""

from __future__ import annotations

import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db
from hiveweave.db.project import (
    ProjectDbError,
    execute_by_project,
)

log = structlog.get_logger(__name__)

# 合法状态（蓝图 :284 的 enum：'active' | 'completed' | 'archived'）。
VALID_STATUSES: frozenset[str] = frozenset({"active", "completed", "archived"})

# 存量库补批次 7 需要的三列。标记键 = (workspace, 连接世代)
# （机制见 db.project.schema_marker_key_for_project）—— 与 tasks/inbox 同族：
# project_id 只是槽位，库才是载体；同路径库整代重建后旧标记必须失效，
# 否则补列被静默跳过、下游炸 ``no such column``。
_MISSING_COLUMNS = [
    ("parent_module_id", "TEXT"),
    ("status", "TEXT DEFAULT 'active'"),
    ("current_agent_id", "TEXT"),
]
_migrated: set[tuple[str, int]] = set()

# 查询统一列序（读侧与写侧共用，避免两处列序漂移）。
_COLUMNS = (
    "id", "project_id", "name", "path", "description",
    "parent_module_id", "status", "current_agent_id",
    "created_at", "updated_at",
)


async def ensure_schema(project_id: str) -> None:
    """给存量库补 modules 的三列（幂等；正典 DDL 已含，新库不跑）。

    与 inbox/dispatch 同一纪律：补列失败**不标记完成**，下次调用重试
    （吞错后照标 = 本进程生命周期内永久短路，是 inbox 09-08 事故的形状）。
    """
    key = await project_db.schema_marker_key_for_project(project_id)
    if key in _migrated:
        return
    pending = False
    for col_name, col_def in _MISSING_COLUMNS:
        try:
            await execute_by_project(
                project_id,
                f"ALTER TABLE modules ADD COLUMN {col_name} {col_def}",
            )
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue  # 列已存在 —— 正常幂等路径
            pending = True
            log.warning(
                "modules_schema_alter_failed",
                project_id=project_id, column=col_name, error=str(e)[:200],
            )
    if not pending:
        _migrated.add(key)


def _row_to_module(row) -> dict:
    d = dict(row)
    # 只读派生字段：是否有子模块由 get_subtree 填，避免读侧各写一份判定。
    return d


class ModuleService:
    """模块树 CRUD + 递归子树读取。"""

    # ── 内部：连通性 ──────────────────────────────────────────

    @staticmethod
    async def _conn(project_id: str):
        workspace = await meta_db.get_project_workspace(project_id)
        if not workspace:
            raise ProjectDbError(f"Workspace not found for project {project_id}")
        return await project_db.ensure_project_db(workspace)

    # ── 写侧 ──────────────────────────────────────────────────

    async def create_module(
        self,
        project_id: str,
        name: str,
        *,
        parent_module_id: str | None = None,
        path: str | None = None,
        description: str | None = None,
        current_agent_id: str | None = None,
        status: str = "active",
    ) -> dict:
        """建一个模块节点（支持树形：``parent_module_id`` 指定父节点）。

        校验在**本操作内部**完成（fixplan §0：约束落在做那件事的操作里）：
        - name 非空；
        - status 合法；
        - parent_module_id 若给出，必须**存在于本项目**（防跨项目挂载 /
          挂到不存在的父上 → 孤儿节点）。

        返回新建模块 dict。
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("module name is required")
        if status not in VALID_STATUSES:
            raise ValueError(
                f"invalid module status {status!r}; "
                f"expected one of {sorted(VALID_STATUSES)}"
            )
        await ensure_schema(project_id)

        if parent_module_id:
            parent = await self.get_module(project_id, parent_module_id)
            if parent is None:
                raise ValueError(
                    f"parent module not found in project {project_id}: "
                    f"{parent_module_id}"
                )

        module_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        await execute_by_project(
            project_id,
            "INSERT INTO modules (id, project_id, name, path, description, "
            "parent_module_id, status, current_agent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [module_id, project_id, name, path, description,
             parent_module_id, status, current_agent_id, now_ms, now_ms],
        )
        log.info(
            "modules.create",
            project_id=project_id, module_id=module_id,
            parent_module_id=parent_module_id, name=name,
        )
        return {
            "id": module_id, "project_id": project_id, "name": name,
            "path": path, "description": description,
            "parent_module_id": parent_module_id, "status": status,
            "current_agent_id": current_agent_id,
            "created_at": now_ms, "updated_at": now_ms,
        }

    async def update_module(
        self, project_id: str, module_id: str, **fields
    ) -> dict | None:
        """更新模块字段（name / path / description / parent_module_id /
        status / current_agent_id 中给定的项）。

        ``parent_module_id`` 变更带**环检测**：新父不能是本模块自己或它的后代
        （否则树成环，``get_subtree`` 会无限递归）。检测在本操作内部做。
        """
        await ensure_schema(project_id)
        current = await self.get_module(project_id, module_id)
        if current is None:
            return None

        allowed = {
            "name", "path", "description",
            "parent_module_id", "status", "current_agent_id",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return current

        if "status" in updates and updates["status"] not in VALID_STATUSES:
            raise ValueError(
                f"invalid module status {updates['status']!r}; "
                f"expected one of {sorted(VALID_STATUSES)}"
            )
        if "name" in updates:
            updates["name"] = (updates["name"] or "").strip()
            if not updates["name"]:
                raise ValueError("module name cannot be blank")

        if "parent_module_id" in updates:
            new_parent = updates["parent_module_id"]
            if new_parent:
                if new_parent == module_id:
                    raise ValueError("a module cannot be its own parent")
                parent = await self.get_module(project_id, new_parent)
                if parent is None:
                    raise ValueError(
                        f"parent module not found in project {project_id}: "
                        f"{new_parent}"
                    )
                # 环检测：新父不能落在本模块的子树里
                subtree_ids = {
                    m["id"] for m in await self.get_subtree(project_id, module_id)
                }
                if new_parent in subtree_ids:
                    raise ValueError(
                        f"cannot set parent to {new_parent}: it is a "
                        f"descendant of {module_id} (would create a cycle)"
                    )

        now_ms = int(time.time() * 1000)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [now_ms, module_id, project_id]
        await execute_by_project(
            project_id,
            f"UPDATE modules SET {set_clause}, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            params,
        )
        log.info(
            "modules.update",
            project_id=project_id, module_id=module_id,
            fields=sorted(updates),
        )
        return await self.get_module(project_id, module_id)

    async def bind_agent(
        self, project_id: str, module_id: str, agent_id: str | None
    ) -> dict | None:
        """把模块的当前负责人绑到 agent（``None`` = 解绑）。

        这是「中层按模块派活」与「记忆按模块归属」共同的接线点：
        ``archive_agent_memories`` 归档时按本模块的 ``current_agent_id``
        可知该模块的前任是谁（见本模块 docstring 的蓝图 :299 说明）。
        """
        return await self.update_module(
            project_id, module_id, current_agent_id=agent_id
        )

    async def delete_module(
        self, project_id: str, module_id: str, *, cascade: bool = False
    ) -> bool:
        """删模块。有子模块时**默认拒绝**（fail-closed），``cascade=True``
        才连子树一起删。

        默认拒绝而不是静默级联：静默删子树会连带丢掉挂在子模块上的
        ``memories.module_id`` 归属（蓝图 :299），而记忆是**不可逆**的
        冻结经验。让调用方显式选择级联。
        """
        await ensure_schema(project_id)
        current = await self.get_module(project_id, module_id)
        if current is None:
            return False

        subtree = await self.get_subtree(project_id, module_id)
        descendants = [m["id"] for m in subtree if m["id"] != module_id]
        if descendants and not cascade:
            raise ValueError(
                f"module {module_id} has {len(descendants)} descendant(s); "
                "pass cascade=True to delete the whole subtree"
            )

        targets = [module_id, *descendants]
        placeholders = ", ".join("?" * len(targets))
        await execute_by_project(
            project_id,
            f"DELETE FROM modules WHERE project_id = ? AND id IN ({placeholders})",
            [project_id, *targets],
        )
        log.info(
            "modules.delete", project_id=project_id, module_id=module_id,
            deleted=len(targets), cascade=cascade,
        )
        return True

    # ── 读侧 ──────────────────────────────────────────────────

    async def list_modules(
        self, project_id: str, *, status: str | None = None
    ) -> list[dict]:
        """列出项目全部模块（可选按 status 过滤）。"""
        await ensure_schema(project_id)
        conn = await self._conn(project_id)
        sql = (
            f"SELECT {', '.join(_COLUMNS)} FROM modules WHERE project_id = ?"
        )
        params: list = [project_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY name"
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_module(r) for r in rows]

    async def get_module(self, project_id: str, module_id: str) -> dict | None:
        """取单个模块。"""
        await ensure_schema(project_id)
        conn = await self._conn(project_id)
        cursor = await conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM modules "
            "WHERE project_id = ? AND id = ?",
            [project_id, module_id],
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_module(row) if row else None

    async def get_subtree(
        self, project_id: str, root_module_id: str
    ) -> list[dict]:
        """递归取子树（含 root 自身）。BFS，自带 visited 防环兜底。

        返回顺序：root 在前，其后按层级展开 —— 调用方（中层派活 /
        记忆归属检查）据此可按「父先于子」处理。
        """
        await ensure_schema(project_id)
        conn = await self._conn(project_id)
        cursor = await conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM modules WHERE project_id = ?",
            [project_id],
        )
        rows = await cursor.fetchall()
        await cursor.close()
        by_id = {r["id"]: _row_to_module(r) for r in rows}

        if root_module_id not in by_id:
            return []

        children: dict[str | None, list[str]] = {}
        for mid, m in by_id.items():
            children.setdefault(m.get("parent_module_id"), []).append(mid)

        out: list[dict] = []
        seen: set[str] = set()
        queue = [root_module_id]
        while queue:
            mid = queue.pop(0)
            if mid in seen:
                continue  # 数据层若已成环（历史脏数据），兜底不无限递归
            seen.add(mid)
            node = by_id.get(mid)
            if node is None:
                continue
            out.append(node)
            queue.extend(children.get(mid, []))
        return out

    async def get_archived_memories_for_module(
        self, project_id: str, module_id: str, *, include_descendants: bool = True
    ) -> list[dict]:
        """按模块取归档记忆（蓝图 :299 的回指消费端）。

        ``include_descendants=True``（默认）时把整棵子树的 ``module_id``
        都纳入 —— 模块树是「中层按功能面切分」的产物，子模块的经验属于
        同一个功能面，继任者查「本模块的前任经验」自然应含子模块。

        返回的每条记忆额外带 ``_via_module_id``，让调用方知道它是直接命中
        本模块还是从某个子模块捞上来的（多树/多层语境下归因的必要信息，
        与 fixplan §10.5 的 ``cwd_display`` 同理：缺它就归因错位）。
        """
        from hiveweave.services.memory import MemoryService

        if include_descendants:
            module_ids = [m["id"] for m in await self.get_subtree(
                project_id, module_id
            )]
        else:
            module_ids = [module_id]
        if not module_ids:
            return []

        mem = MemoryService()
        out: list[dict] = []
        for mid in module_ids:
            entries = await mem.get_archived_memories(project_id, mid)
            for e in entries:
                out.append({**e, "_via_module_id": mid})
        # 新记忆优先（与 read_memory 的 DESC 口径一致）
        out.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
        return out
