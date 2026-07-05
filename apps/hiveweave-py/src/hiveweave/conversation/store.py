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
        self._compaction = Compaction()

    # ── 公共 API ─────────────────────────────────────────────

    async def get_history(
        self, agent_id: str, project_id: str, token_budget: int | None = None
    ) -> list[dict]:
        """获取对话历史，裁剪到 token budget 内，不含 system 消息。"""
        key = (project_id, agent_id)
        if key not in self._cache:
            loaded = await self._load_from_db(agent_id)
            self._cache[key] = self._clean_messages(loaded)
        cleaned = self._clean_messages(self._cache[key])
        return self._trim_to_budget(cleaned, token_budget)

    async def append_turn(
        self, agent_id: str, project_id: str, messages: list[dict]
    ) -> None:
        """追加一轮消息，过滤 system，异步持久化到 DB。"""
        if not messages:
            return
        key = (project_id, agent_id)
        if key not in self._cache:
            loaded = await self._load_from_db(agent_id)
            existing = self._clean_messages(loaded)
        else:
            existing = self._clean_messages(self._cache[key])

        # 过滤 system 消息（Streamer 负责重建）
        filtered_new = [m for m in messages if m.get("role") != "system"]
        if not filtered_new:
            return

        combined = existing + filtered_new
        self._cache[key] = combined

        # 异步持久化（fire-and-forget，失败仅日志）
        asyncio.create_task(self._persist_turn(agent_id, filtered_new))

        # 触发 compaction 检查
        await self._maybe_trigger_compaction(agent_id, project_id, key, combined)

    async def clear(self, agent_id: str, project_id: str) -> None:
        """清空指定 agent 的缓存和 DB 记录。"""
        key = (project_id, agent_id)
        self._cache.pop(key, None)
        self._prefix_cache.pop(key, None)
        try:
            await project_db.execute(
                agent_id,
                "DELETE FROM conversation_turns WHERE agent_id = ?",
                [agent_id],
            )
        except Exception as e:
            logger.warning("clear_failed", agent_id=agent_id, error=str(e))

    def clear_all(self) -> None:
        """清空所有内存缓存（不删 DB）— 启动时调用。"""
        self._cache.clear()
        self._prefix_cache.clear()

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
        logger.info("model_switch_compact", agent_id=agent_id, budget=budget)
        await self._do_compaction(agent_id, project_id, key, messages, budget)
        return True

    # ── Compaction ──────────────────────────────────────────

    async def _maybe_trigger_compaction(
        self, agent_id, project_id, key, messages
    ) -> None:
        total = estimate_tokens_for_messages(messages)
        ctx = await self._get_agent_context_window(agent_id)
        budget = self._compaction.check_overflow(total, ctx)
        if budget is not None:
            logger.info("compaction_triggered", agent_id=agent_id, total=total)
            asyncio.create_task(
                self._do_compaction(agent_id, project_id, key, messages, budget)
            )

    async def _do_compaction(self, agent_id, project_id, key, messages, budget) -> None:
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
            logger.info(
                "compaction_applied",
                agent_id=agent_id,
                kept=len(history),
                has_summary=summary_text is not None,
            )
        except Exception as e:
            logger.warning("compaction_error", agent_id=agent_id, error=str(e))

    async def _get_agent_context_window(self, agent_id: str) -> int:
        try:
            row = await meta_db.query_one(
                "SELECT a.model_id, m.context_window FROM agents a "
                "LEFT JOIN llm_models m ON a.model_id = m.id "
                "WHERE a.id = ? LIMIT 1",
                [agent_id],
            )
            if row and row["context_window"] and row["context_window"] > 0:
                return row["context_window"]
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

    # ── Token 预算裁剪 ───────────────────────────────────────

    @staticmethod
    def _trim_to_budget(messages: list[dict], budget: int | None) -> list[dict]:
        if budget is None or budget <= 0:
            return messages
        pruned = ConversationStore._prune_tool_outputs(messages)
        total = estimate_tokens_for_messages(pruned)
        if total <= budget:
            return pruned
        return ConversationStore._trim_turns(pruned, budget, total)

    @staticmethod
    def _prune_tool_outputs(messages: list[dict]) -> list[dict]:
        """清除保护窗口外的旧 tool 输出（OpenCode prune() 模式）。"""
        to_prune: list[dict] = []
        protected = 0
        for msg in reversed(messages):
            if "tool_call_id" in msg:
                tokens = estimate_tokens_for_messages([msg])
                new_protected = protected + tokens
                if new_protected <= PRUNE_PROTECT_TOKENS:
                    protected = new_protected
                else:
                    to_prune.append(msg)
        prune_tokens = sum(estimate_tokens_for_messages([m]) for m in to_prune)
        if prune_tokens < PRUNE_MINIMUM_TOKENS:
            return messages
        prune_ids = {m["tool_call_id"] for m in to_prune}
        return [
            {**m, "content": "[Old tool result content cleared]"}
            if "tool_call_id" in m and m["tool_call_id"] in prune_ids
            else m
            for m in messages
        ]

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
