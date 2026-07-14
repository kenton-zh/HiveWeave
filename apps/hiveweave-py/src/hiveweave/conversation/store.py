"""Per-agent conversation history with token-budget trimming and smart compaction.

契约 03: 对话历史与压缩
- Token-budget 裁剪（非消息条数）
- DB 懒加载 + 内存缓存
- Turn-level 裁剪（不拆 assistant(tool_calls)/tool(result) 对）
- Smart compaction via LLM — 摘要存入独立 compacted_prefix_cache（RECONCILE A1 修正）
- System 消息不入库（Streamer 每次重建）
- 孤立 tool 消息（无匹配 tool_call_id）被清理
"""

import asyncio
import json
import time
import uuid

import structlog

from hiveweave.conversation.compaction import (
    SUMMARY_MARKER,
    Compaction,
    resolve_compactor_callback,
)
from hiveweave.conversation.token_utils import (
    PRUNE_MINIMUM_TOKENS,
    PRUNE_PROTECT_TOKENS,
    TAIL_TURNS,
    estimate_tokens_for_messages,
)
from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db

logger = structlog.get_logger()

DEFAULT_CONTEXT_WINDOW = 128_000


class ConversationStore:
    """Per-agent 对话历史管理（内存缓存 + DB 持久化）。"""

    def __init__(self) -> None:
        # 历史缓存：(project_id, agent_id) -> list[dict]
        self._cache: dict[tuple[str, str], list[dict]] = {}
        # 压缩摘要缓存：(project_id, agent_id) -> summary_text
        self._prefix_cache: dict[tuple[str, str], str] = {}
        # per-agent 压缩锁 — 防止同一 agent 并发压缩导致竞态丢消息
        self._compaction_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._compaction = Compaction()
        # per-agent 写队列 — 串行化所有 DB 写操作，防止 persist/compaction/clear 竞态
        self._write_queues: dict[tuple[str, str], asyncio.Queue] = {}
        self._write_workers: dict[tuple[str, str], asyncio.Task] = {}
        # 标记已被 clear 的 agent，使排队中的 persist 任务被丢弃
        self._cleared_agents: set[tuple[str, str]] = set()

    def _get_compaction_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        """获取或创建 per-agent 压缩锁。"""
        lock = self._compaction_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._compaction_locks[key] = lock
        return lock

    def _get_write_queue(self, key: tuple[str, str]) -> asyncio.Queue:
        """获取或创建 per-agent 写队列。"""
        if key not in self._write_queues:
            self._write_queues[key] = asyncio.Queue()
            self._write_workers[key] = asyncio.create_task(
                self._write_worker(key)
            )
        return self._write_queues[key]

    async def _write_worker(self, key: tuple[str, str]) -> None:
        """per-agent 写队列 worker — 顺序执行所有 DB 写操作。"""
        queue = self._write_queues[key]
        while True:
            try:
                task = await queue.get()
                if task is None:
                    # 哨兵值，停止 worker
                    break
                func, args = task
                # 如果 agent 已被 clear 且这是 persist 操作，跳过
                if key in self._cleared_agents and func.__name__ == "_persist_turn":
                    logger.debug("persist_skipped_after_clear", key=key)
                    queue.task_done()
                    continue
                try:
                    await func(*args)
                except Exception as e:
                    logger.warning("write_queue_task_error", key=key, error=str(e))
                queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("write_worker_error", key=key, error=str(e))

    async def _enqueue_write(self, key: tuple[str, str], func, *args) -> None:
        """向 per-agent 写队列投递一个写任务。"""
        queue = self._get_write_queue(key)
        await queue.put((func, args))

    # ── 公共 API ─────────────────────────────────────────────

    async def get_history(
        self, agent_id: str, project_id: str, token_budget: int | None = None
    ) -> list[dict]:
        """获取对话历史，裁剪到 token budget 内，不含 system 消息。"""
        key = (project_id, agent_id)
        if key not in self._cache:
            loaded = await self._load_from_db(agent_id)
            self._cache[key] = self._clean_messages(loaded)
            # 加载持久化的压缩摘要（CRITICAL #1 持久化修复）
            if key not in self._prefix_cache:
                await self._load_compacted_prefix(agent_id, project_id)
        cleaned = self._clean_messages(self._cache[key])
        return self._trim_to_budget(cleaned, token_budget)

    async def append_turn(
        self, agent_id: str, project_id: str, messages: list[dict]
    ) -> None:
        """追加一轮消息，过滤 system，异步持久化到 DB。"""
        if not messages:
            return
        key = (project_id, agent_id)
        # 新消息写入，清除 clear 标记
        self._cleared_agents.discard(key)
        if key not in self._cache:
            loaded = await self._load_from_db(agent_id)
            existing = self._clean_messages(loaded)
            # 加载持久化的压缩摘要（CRITICAL #1 持久化修复）
            if key not in self._prefix_cache:
                await self._load_compacted_prefix(agent_id, project_id)
        else:
            existing = self._clean_messages(self._cache[key])

        # 过滤 system 消息（Streamer 负责重建）
        filtered_new = [m for m in messages if m.get("role") != "system"]
        if not filtered_new:
            return

        combined = existing + filtered_new
        self._cache[key] = combined

        # 通过 per-agent 写队列串行化持久化，防止与 compaction 竞态
        await self._enqueue_write(key, self._persist_turn, agent_id, filtered_new)

        # 触发 compaction 检查
        await self._maybe_trigger_compaction(agent_id, project_id, key, combined)

    async def clear(self, agent_id: str, project_id: str) -> None:
        """清空指定 agent 的缓存和 DB 记录。"""
        key = (project_id, agent_id)
        self._cache.pop(key, None)
        self._prefix_cache.pop(key, None)
        self._compaction_locks.pop(key, None)
        self._cleared_agents.add(key)  # 标记已清除，排队中的 persist 将被丢弃
        try:
            await project_db.execute(
                agent_id,
                "DELETE FROM conversation_turns WHERE agent_id = ?",
                [agent_id],
            )
        except Exception as e:
            logger.warning("clear_failed", agent_id=agent_id, error=str(e))
        # 清除标记（给一个短暂的窗口让排队任务被丢弃，然后移除标记）
        # 实际清除由下次 append_turn 重新创建队列时重置

    def clear_all(self) -> None:
        """清空所有内存缓存（不删 DB）— 启动时调用。"""
        self._cache.clear()
        self._prefix_cache.clear()
        self._compaction_locks.clear()
        self._cleared_agents.clear()
        # 停止所有 worker
        for key, queue in self._write_queues.items():
            queue.put_nowait(None)  # 哨兵值停止 worker
        self._write_queues.clear()
        self._write_workers.clear()

    def stop_project_workers(self, project_id: str) -> None:
        """停止项目下所有 agent 的 write worker — 删除项目时调用。

        防止后台 worker 在 evict_project_db 后重新打开 DB 连接。
        """
        keys_to_stop = [k for k in self._write_queues if k[0] == project_id]
        for key in keys_to_stop:
            queue = self._write_queues.pop(key, None)
            worker = self._write_workers.pop(key, None)
            if queue is not None:
                queue.put_nowait(None)  # 哨兵值停止 worker
            # worker 会在下一个循环迭代退出，不需要 cancel
            # 清理缓存
            self._cache.pop(key, None)
            self._prefix_cache.pop(key, None)
            self._compaction_locks.pop(key, None)
            self._cleared_agents.add(key)

    def get_compacted_prefix(self, project_id: str, agent_id: str) -> str | None:
        """获取压缩摘要（DeepSeek 前缀缓存 System 3 布局）。"""
        return self._prefix_cache.get((project_id, agent_id))

    async def maybe_compact_on_model_switch(
        self,
        agent_id: str,
        project_id: str,
        old_context_window: int = DEFAULT_CONTEXT_WINDOW,
        new_context_window: int = DEFAULT_CONTEXT_WINDOW,
        current_tokens: int = 0,
    ) -> bool:
        """模型切换时检查是否需要紧急压缩。返回 True 表示已压缩。"""
        if new_context_window >= old_context_window:
            return False
        budget = self._compaction.check_overflow(current_tokens, new_context_window)
        if budget is None:
            return False
        key = (project_id, agent_id)
        messages = self._cache.get(key) or await self._load_from_db(agent_id)
        if not messages:
            return False
        # 对齐读取预算（#4）
        read_budget = max(new_context_window // 2, 16_000)
        target_budget = min(budget, read_budget)
        logger.info("model_switch_compact", agent_id=agent_id, budget=target_budget)
        await self._do_compaction(agent_id, project_id, key, messages, target_budget)
        return True

    # ── Compaction ──────────────────────────────────────────

    async def _maybe_trigger_compaction(
        self, agent_id, project_id, key, messages
    ) -> None:
        total = estimate_tokens_for_messages(messages)
        ctx = await self._get_agent_context_window(agent_id)
        budget = self._compaction.check_overflow(total, ctx)
        if budget is not None:
            # 压缩目标对齐读取预算 — _build_messages 用 ctx//2 读取，
            # 压缩后 cache 应在此值以下，否则压缩对读取路径无帮助（#4）。
            read_budget = max(ctx // 2, 16_000)
            target_budget = min(budget, read_budget)
            logger.info(
                "compaction_triggered",
                agent_id=agent_id,
                total=total,
                target=target_budget,
            )
            await self._enqueue_write(
                key, self._do_compaction, agent_id, project_id, key, messages, target_budget
            )

    async def _do_compaction(self, agent_id, project_id, key, messages, budget) -> None:
        lock = self._get_compaction_lock(key)
        if lock.locked():
            # 已有压缩在进行 — 跳过，下次 append_turn 会重新检查
            logger.info("compaction_skipped_concurrent", agent_id=agent_id)
            return
        async with lock:
            try:
                callback = await resolve_compactor_callback(agent_id)
                compacted = await self._compaction.compact(messages, budget, callback)

                # 提取摘要到 prefix_cache，history 不含 summary（RECONCILE A1）
                summary_text = None
                history = []
                for m in compacted:
                    content = m.get("content") or ""
                    if m.get("role") == "system" and SUMMARY_MARKER in content:
                        summary_text = content
                    else:
                        history.append(m)

                # C3 fix: merge 而非覆盖 — _do_compaction 是 fire-and-forget task，
                # 耗时数秒，期间 append_turn 可能在 self._cache[key] 追加了新消息。
                # 直接覆盖会丢失这些新消息。这里读取当前缓存尾部（压缩期间新增的）
                # 追加到压缩结果后面再写回缓存。
                original_len = len(messages)
                current_cache = self._cache.get(key, [])
                if len(current_cache) > original_len:
                    # 压缩期间新增的消息 = 当前缓存中超出原始 messages 数量的尾部
                    new_messages = current_cache[original_len:]
                    history = history + new_messages

                self._cache[key] = history
                if summary_text is not None:
                    self._prefix_cache[key] = summary_text

                # 持久化压缩结果到 DB — 删除旧 turn 行，写入压缩后的 turn
                # 防止重启后从 DB 加载到已压缩的旧消息（CRITICAL #1）
                await self._persist_compaction(agent_id, history, summary_text)

                logger.info(
                    "compaction_applied",
                    agent_id=agent_id,
                    kept=len(history),
                    has_summary=summary_text is not None,
                )
            except Exception as e:
                logger.warning("compaction_error", agent_id=agent_id, error=str(e))

    async def _persist_compaction(
        self, agent_id: str, history: list[dict], summary: str | None
    ) -> None:
        """持久化压缩结果：删除旧 turn 行，写入压缩后的 turn + summary。

        防止重启后 _load_from_db 加载到已压缩的旧消息（CRITICAL #1）。
        summary 存入 agents 表的 compacted_prefix 列（如果存在），
        或作为特殊 turn 行写入 conversation_turns 表。
        """
        try:
            # 删除所有旧 turn
            await project_db.execute(
                agent_id,
                "DELETE FROM conversation_turns WHERE agent_id = ?",
                [agent_id],
            )
            # 写入压缩后的 history 为单个 turn
            if history:
                await self._persist_turn(agent_id, history)
            # summary 持久化到 agent 配置（通过 per-project DB）
            if summary is not None:
                try:
                    await project_db.execute(
                        agent_id,
                        "UPDATE agents SET compacted_prefix = ? WHERE id = ?",
                        [summary, agent_id],
                    )
                except Exception:
                    # compacted_prefix 列可能不存在（旧 schema）— 忽略
                    pass
        except Exception as e:
            logger.warning("persist_compaction_failed", agent_id=agent_id, error=str(e))

    async def _get_agent_context_window(self, agent_id: str) -> int:
        try:
            # agents 表在 per-project DB，llm_models 在 meta DB — 无法 JOIN
            agent_row = await project_db.query_one(
                agent_id,
                "SELECT model_id FROM agents WHERE id = ? LIMIT 1",
                [agent_id],
            )
            if agent_row and agent_row["model_id"]:
                model_row = await meta_db.query_one(
                    "SELECT context_window FROM llm_models WHERE id = ? LIMIT 1",
                    [agent_row["model_id"]],
                )
                if model_row and model_row["context_window"] and model_row["context_window"] > 0:
                    return model_row["context_window"]
        except Exception as e:
            logger.warning("get_context_window_failed", error=str(e))
        return DEFAULT_CONTEXT_WINDOW

    # ── 消息清理 ─────────────────────────────────────────────

    @staticmethod
    def _clean_messages(messages: list[dict]) -> list[dict]:
        """移除 system 消息 + 孤立 tool 消息（无匹配 tool_call_id）。"""
        no_system = [m for m in messages if m.get("role") != "system"]
        # 收集所有 tool_call_id
        tool_call_ids: set[str] = set()
        for m in no_system:
            tcs = m.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    tc_id = tc.get("id")
                    if tc_id:
                        tool_call_ids.add(tc_id)
        # 保留有匹配 tool_call_id 的 tool 消息
        return [
            m
            for m in no_system
            if not m.get("tool_call_id") or m["tool_call_id"] in tool_call_ids
        ]

    # ── 持久化裁剪（OpenCode prune 模式）──────────────────

    #: 永远不被裁剪的工具名（如 skill）
    _PRUNE_PROTECTED_TOOLS: set[str] = set()

    async def prune_persisted(self, agent_id: str, project_id: str) -> None:
        """持久化裁剪旧工具输出 — OpenCode prune() 模式。

        每轮结束后调用。逆序遍历 cache 中的消息：
        1. 跳过最近 2 轮（保护当前上下文）
        2. 停在压缩摘要边界
        3. 累积 tool 输出 token；保护窗口(40K)外的旧 tool 输出标记为裁剪候选
        4. 候选总量 > 20K 时，永久替换 cache + DB 中的内容为占位符

        与 _prune_tool_outputs（读时临时裁剪）的区别：
        - 本方法写入 cache 和 DB，后续请求永远看不到旧工具输出
        - 避免历史无限膨胀导致每次 get_history 都加载完整数据
        """
        key = (project_id, agent_id)
        messages = self._cache.get(key)
        if not messages or len(messages) < 6:
            return

        to_prune_indices: list[int] = []
        protected = 0
        turns = 0

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            role = msg.get("role", "")

            # 计算轮次（user 消息 = 一轮开始）
            if role == "user":
                turns += 1
            # 跳过最近 2 轮
            if turns < 2:
                continue

            # 停在压缩摘要边界
            if role == "system" and SUMMARY_MARKER in (msg.get("content") or ""):
                break

            # 只处理 tool 结果消息
            if "tool_call_id" not in msg:
                continue

            # 已被裁剪过 → 停止（更早的也已裁剪）
            if msg.get("content") == "[Old tool result content cleared]":
                break

            tokens = estimate_tokens_for_messages([msg])
            new_protected = protected + tokens
            if new_protected <= PRUNE_PROTECT_TOKENS:
                protected = new_protected
            else:
                to_prune_indices.append(i)

        if not to_prune_indices:
            return

        prune_tokens = sum(
            estimate_tokens_for_messages([messages[i]]) for i in to_prune_indices
        )
        if prune_tokens < PRUNE_MINIMUM_TOKENS:
            return  # 收益不足，不执行

        # 永久替换 cache 中的内容（in-place 修改）
        for i in to_prune_indices:
            messages[i] = {**messages[i], "content": "[Old tool result content cleared]"}

        # 持久化到 DB — 通过写队列串行化，读取执行时的最新 cache
        await self._enqueue_write(key, self._persist_pruned, agent_id, key)

        logger.info(
            "prune_persisted",
            agent_id=agent_id,
            pruned_count=len(to_prune_indices),
            pruned_tokens=prune_tokens,
            protected_tokens=protected,
        )

    async def _persist_pruned(self, agent_id: str, key: tuple[str, str]) -> None:
        """持久化裁剪后的历史到 DB — 删除所有旧 turn，写入当前 cache 为单个 turn。

        在写队列中执行，读取执行时的最新 cache（可能包含 prune 后新追加的消息）。
        """
        messages = self._cache.get(key)
        if not messages:
            return
        try:
            await project_db.execute(
                agent_id,
                "DELETE FROM conversation_turns WHERE agent_id = ?",
                [agent_id],
            )
            if messages:
                await self._persist_turn(agent_id, list(messages))
        except Exception as e:
            logger.warning("persist_pruned_failed", agent_id=agent_id, error=str(e))

    # ── Token 预算裁剪 ───────────────────────────────────────

    @staticmethod
    def _trim_to_budget(messages: list[dict], budget: int | None) -> list[dict]:
        """裁剪到 token budget 内。

        持久化 prune 已在 append_turn 后执行，cache 中的旧工具输出已是占位符。
        此方法仅做预算检查：如果仍超限，用 turn-level 裁剪丢弃最旧的轮次。
        """
        if budget is None or budget <= 0:
            return messages
        total = estimate_tokens_for_messages(messages)
        if total <= budget:
            return messages
        return ConversationStore._trim_turns(messages, budget, total)

    @staticmethod
    def _trim_turns(messages: list[dict], budget: int, total: int) -> list[dict]:
        """Turn-level 裁剪：保留最近 TAIL_TURNS 轮完整。"""
        turns = ConversationStore._split_into_turns(messages)
        if len(turns) <= TAIL_TURNS:
            return ConversationStore._trim_messages(messages, budget, total)
        recent = [m for t in turns[-TAIL_TURNS:] for m in t]
        recent_tokens = estimate_tokens_for_messages(recent)
        if recent_tokens > budget:
            return ConversationStore._trim_messages(recent, budget, recent_tokens)
        # 尝试塞入更旧的 turn
        remaining = budget - recent_tokens
        fitting: list[list[dict]] = []
        for turn in reversed(turns[:-TAIL_TURNS]):
            tt = estimate_tokens_for_messages(turn)
            if tt <= remaining:
                fitting.insert(0, turn)
                remaining -= tt
            else:
                break
        return [m for t in fitting for m in t] + recent

    @staticmethod
    def _split_into_turns(messages: list[dict]) -> list[list[dict]]:
        """按 user 消息分割 turn。每个 turn = [user, assistant, tool...]"""
        turns: list[list[dict]] = []
        current: list[dict] = []
        for msg in messages:
            if msg.get("role") == "user" and current:
                turns.append(current)
                current = [msg]
            else:
                current.append(msg)
        if current:
            turns.append(current)
        return turns

    @staticmethod
    def _trim_messages(messages: list[dict], budget: int, total: int) -> list[dict]:
        """回退：消息级裁剪，不拆 assistant(tool_calls)/tool(result) 对。"""
        result = list(messages)
        cur = total
        while len(result) > 1 and cur > budget:
            drop = 1
            if "tool_calls" in result[0] and "tool_call_id" in result[1]:
                drop = 2
            elif "tool_call_id" in result[0] and "tool_calls" in result[1]:
                drop = 2
            cur -= estimate_tokens_for_messages(result[:drop])
            result = result[drop:]
        return result

    # ── DB 持久化 ────────────────────────────────────────────

    async def _load_from_db(self, agent_id: str) -> list[dict]:
        try:
            rows = await project_db.query(
                agent_id,
                "SELECT raw_messages FROM conversation_turns "
                "WHERE agent_id = ? ORDER BY turn_index ASC",
                [agent_id],
            )
            messages: list[dict] = []
            for row in rows:
                raw = row["raw_messages"] or "[]"
                try:
                    msgs = json.loads(raw)
                    if isinstance(msgs, list):
                        messages.extend(msgs)
                except (json.JSONDecodeError, TypeError):
                    pass
            return messages
        except Exception as e:
            logger.warning("load_from_db_failed", agent_id=agent_id, error=str(e))
            return []

    async def _load_compacted_prefix(
        self, agent_id: str, project_id: str
    ) -> None:
        """从 per-project DB 加载持久化的压缩摘要到 _prefix_cache。

        CRITICAL #1 修复：重启后 _prefix_cache 为空，需从 agents.compacted_prefix
        列恢复，否则 agent 重新面对完整未压缩历史。
        """
        key = (project_id, agent_id)
        try:
            row = await project_db.query_one(
                agent_id,
                "SELECT compacted_prefix FROM agents WHERE id = ? LIMIT 1",
                [agent_id],
            )
            if row and row["compacted_prefix"]:
                self._prefix_cache[key] = row["compacted_prefix"]
        except Exception as e:
            # compacted_prefix 列可能不存在（旧 schema 未迁移）— 静默跳过
            logger.debug("load_compacted_prefix_skipped", agent_id=agent_id, error=str(e))

    async def _persist_turn(self, agent_id: str, messages: list[dict]) -> None:
        try:
            turn_id = str(uuid.uuid4())
            now = int(time.time() * 1000)
            raw = json.dumps(messages, ensure_ascii=False)
            tokens = estimate_tokens_for_messages(messages)
            # C2 fix: 单语句原子计算 turn_index，消除 SELECT MAX + INSERT 的 TOCTOU 竞态
            # COALESCE(MAX(turn_index), -1) + 1 在同一条 INSERT...SELECT 中完成，
            # SQLite 的语句级原子性保证并发不会产生重复 turn_index
            await project_db.execute(
                agent_id,
                "INSERT INTO conversation_turns "
                "(id, agent_id, turn_index, raw_messages, approx_tokens, created_at) "
                "SELECT ?, ?, COALESCE(MAX(turn_index), -1) + 1, ?, ?, ? "
                "FROM conversation_turns WHERE agent_id = ?",
                [turn_id, agent_id, raw, tokens, now, agent_id],
            )
        except Exception as e:
            logger.warning("persist_turn_failed", agent_id=agent_id, error=str(e))


# 模块级单例
conversation_store = ConversationStore()
