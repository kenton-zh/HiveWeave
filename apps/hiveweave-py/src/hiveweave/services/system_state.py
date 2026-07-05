"""System state — global pause flag and startup recovery.

契约 15: 系统状态与启动恢复
- 进程内共享状态：system_paused 布尔标志（agent chat 入口检查）
- 每小时孤儿审批清理 sweep（Approval 尚未实现，pass 占位）
- 启动时清理 zombie streaming（is_streaming=true 的行）
- 花名迁移（可选，后续实现）
- 优雅停机：取消后台任务
"""

import asyncio

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

logger = structlog.get_logger()

# 每小时清理间隔（秒）— 契约 15: 3_600_000 ms
HOURLY_CLEANUP_INTERVAL = 3600

# 清除 zombie streaming 的 SQL
_ZOMBIE_STREAMING_SQL = "UPDATE chat_messages SET is_streaming = 0 WHERE is_streaming = 1"


class SystemState:
    """全局系统状态 + 后台清理任务。

    对齐 Elixir Services.SystemState（GenServer + ETS）：
    - paused?/pause/resume 直接读写内存布尔值（进程内共享）
    - init 时启动每小时定时器 → cleanup_orphaned_approvals
    """

    def __init__(self) -> None:
        self._paused: bool = False
        self._cleanup_task: asyncio.Task | None = None
        self._running: bool = False

    # ── 暂停 / 恢复 ──────────────────────────────────────────

    def paused(self) -> bool:
        """系统是否全局暂停。true 时 agent chat 返回 :paused 错误。"""
        return self._paused

    def pause(self) -> None:
        """暂停系统 — agent chat 将被拒绝。"""
        self._paused = True
        logger.info("system_paused")

    def resume(self) -> None:
        """恢复系统。"""
        self._paused = False
        logger.info("system_resumed")

    # ── 生命周期 ─────────────────────────────────────────────

    async def startup(self) -> None:
        """启动恢复 — 清理 zombie streaming + 启动每小时 sweep。

        契约 15 boot_existing_projects 流程：
        1. 清除 is_streaming=true 的 zombie 行（跨所有项目 per-project DB）
        2. 清理孤儿审批请求（Approval 尚未实现，pass 占位）
        3. 启动每小时定时清理 sweep
        4. 花名迁移（可选，后续实现）
        """
        self._running = True

        # 1. 清除 zombie streaming
        await self._clear_stuck_streaming()

        # 2. 清理孤儿审批（初始执行一次）
        await self._cleanup_orphaned_approvals()

        # 3. 花名迁移（可选 — 后续实现）
        # await self._migrate_flower_names()

        # 4. 启动每小时 sweep
        self._cleanup_task = asyncio.create_task(self._hourly_sweep())

        logger.info("system_state_started")

    async def shutdown(self) -> None:
        """优雅停机 — 取消后台任务。

        契约 15 prep_stop：停机时持久化 game time（GameTime 尚未实现）。
        当前仅取消后台清理任务。
        """
        self._running = False
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        # TODO: 契约 15 — 持久化所有项目的 game time（GameTime.Server 未实现）
        logger.info("system_state_stopped")

    # ── 后台清理 sweep ───────────────────────────────────────

    async def _hourly_sweep(self) -> None:
        """每小时审批清理循环 — 异常不崩溃。"""
        while self._running:
            try:
                await asyncio.sleep(HOURLY_CLEANUP_INTERVAL)
                await self._cleanup_orphaned_approvals()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("hourly_cleanup_failed", error=str(e))

    async def _cleanup_orphaned_approvals(self) -> None:
        """清理孤儿审批请求。

        契约 15: Approval.cleanup_orphaned_requests() — Approval 尚未实现，pass 占位。
        实现后替换为：from hiveweave.services.approval import approval_service
                     await approval_service.cleanup_orphaned_requests()
        """
        try:
            # TODO: Approval service not yet implemented
            pass
        except Exception as e:
            logger.warning("cleanup_orphaned_approvals_failed", error=str(e))

    async def _clear_stuck_streaming(self) -> None:
        """清除所有项目 per-project DB 中 is_streaming=true 的 zombie 行。

        契约 15: 启动时清除上次崩溃残留的流式消息标记。
        遍历 Meta DB 中的所有 projects，对每个项目 ensure_project_db 后执行 UPDATE。
        """
        try:
            projects = await meta_db.query(
                "SELECT id, workspace_path FROM projects"
            )
            cleared = 0
            for p in projects:
                ws = p["workspace_path"]
                if not ws:
                    continue
                try:
                    conn = await project_db.ensure_project_db(ws)
                    cursor = await conn.execute(_ZOMBIE_STREAMING_SQL)
                    affected = cursor.rowcount
                    await conn.commit()
                    await cursor.close()
                    if affected and affected > 0:
                        cleared += affected
                except Exception as e:
                    logger.warning(
                        "clear_streaming_project_failed",
                        project_id=p["id"],
                        error=str(e),
                    )
            logger.info(
                "clear_stuck_streaming_done",
                project_count=len(projects),
                cleared_rows=cleared,
            )
        except Exception as e:
            logger.warning("clear_stuck_streaming_failed", error=str(e))


# 模块级单例
system_state = SystemState()
