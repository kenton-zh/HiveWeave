"""LLM model service — model registry CRUD.

契约 18: ModelService
- Meta DB 中的 llm_models 表 CRUD
- list_all 对 api_key 脱敏（前 8 字符 + '...'）；get 返回完整 api_key
- seed_default_model / ensure_channel_models 启动种子（多渠道混用以摊配额）
- 补全 E9/E10: create/update 支持 supports_thinking/default_reasoning_effort/temperature

llm_models 表 schema 已完整，无需迁移。
"""

from __future__ import annotations

import itertools
import os
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)

# Default values (契约 18)
_DEFAULT_CONTEXT_WINDOW = 128_000
_DEFAULT_MAX_OUTPUT = 8_192

# Round-robin counter for active model pool (process-local).
# 限制说明：计数器只在单进程内单调递增。后端本就按单进程设计
# （InProcess pubsub / per-agent asyncio 锁 / 内存态 turn_session 均假设
# 单 worker），多 worker 或进程重启后分摊重新计数——重启后首轮总从
# pool[0] 开始，多 worker 下各进程独立轮转、全局配额分摊不均匀。
# 如需跨进程均匀分摊，应改为 DB 持久化游标或按 agent_id 哈希取模；
# 当前单进程部署下进程内轮询已足够，故保持简单实现。
_pool_counter = itertools.count()


class InvalidModelConfig(ValueError):
    """模型配置违反物理不变量（如 max_output_tokens >= context_window）。

    治本设计：非法配置必须在 Service 层被拒绝，而非 clamp 后悄悄落库。
    上游（检测层/Pydantic/API）正常情况下不会产出非法值，此异常作为
    最后防线——一旦触发说明上游有 bug，应让调用方明确感知并修复，
    而不是用 clamp 掩盖后让带病配置流入运行时。
    """


def _validate_invariant(context_window: int, max_output_tokens: int) -> None:
    """强制物理不变量：max_output_tokens 必须严格小于 context_window。

    语义：输出预算不可能吃掉整个窗口，必须给输入留空间。
    违反则抛 InvalidModelConfig，绝不 clamp。留 20% 给输入作为下限
    （推理模型 thinking + 实际输出可能很大，但再大也不能 > 80% 窗口）。
    """
    if max_output_tokens >= context_window:
        raise InvalidModelConfig(
            f"max_output_tokens ({max_output_tokens:,}) >= context_window "
            f"({context_window:,}): 输出预算吃掉整个窗口，输入零空间，"
            f"物理上不可能。请配置合理的 max_output_tokens。"
        )
    # 留至少 20% 窗口给输入 + 安全 buffer
    min_input_reserve = max(context_window * 0.2, 8_192)
    if max_output_tokens > context_window - min_input_reserve:
        raise InvalidModelConfig(
            f"max_output_tokens ({max_output_tokens:,}) 过大："
            f"context_window={context_window:,} 需至少留 "
            f"{int(min_input_reserve):,} 给输入，"
            f"max_output_tokens 上限为 {int(context_window - min_input_reserve):,}。"
        )


class ModelService:
    """LLM model registry — CRUD on Meta DB.

    所有操作路由到 Meta DB（全局单例）。
    """

    async def create(self, attrs: dict) -> dict:
        """Create a model. Returns {id, name, model_id}.

        契约 18: create_model
        - id 缺省 → UUID
        - context_window 缺省 → 128_000
        - max_output_tokens 缺省 → 8_192
        - is_active: attrs['is_active'] is not False → 1
        - 补全 E10: 支持 supports_thinking 参数
        """
        model_pk = attrs.get("id") or str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        name = attrs.get("name", "")
        model_id = attrs.get("model_id", "")
        base_url = attrs.get("base_url", "")
        api_key = attrs.get("api_key", "")
        provider_type = attrs.get("provider_type", "")
        context_window = attrs.get("context_window", _DEFAULT_CONTEXT_WINDOW)
        max_output = attrs.get("max_output_tokens", _DEFAULT_MAX_OUTPUT)
        supports_thinking = 1 if attrs.get("supports_thinking", False) else 0
        is_active = 0 if attrs.get("is_active") is False else 1
        default_reasoning_effort = attrs.get("default_reasoning_effort")
        temperature = attrs.get("temperature")

        # 物理不变量：max_output_tokens 必须严格小于 context_window。
        # 治本：非法配置在落库前拒绝，绝不 clamp 后悄悄写入。
        _validate_invariant(context_window, max_output)

        await meta_db.execute(
            "INSERT INTO llm_models (id, name, model_id, base_url, api_key, "
            "provider_type, "
            "context_window, max_output_tokens, supports_thinking, "
            "default_reasoning_effort, temperature, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [model_pk, name, model_id, base_url, api_key,
             provider_type,
             context_window, max_output, supports_thinking,
             default_reasoning_effort, temperature, is_active, now_ms, now_ms])
        log.info("model_created", model_pk=model_pk, name=name, model_id=model_id)
        return {"id": model_pk, "name": name, "model_id": model_id}

    async def get(self, model_pk: str) -> dict | None:
        """Get a model by ID or model_id. Returns full api_key (not masked).

        契约 18: get_model — api_key 完整返回（Streamer 需完整 key 调 LLM）。
        支持按 id（UUID）或 model_id（如 step-3.7-flash）查询，因为 agent.config
        中存储的是 model_id 字段而非数据库主键。
        """
        row = await meta_db.query_one(
            "SELECT id, name, model_id, base_url, api_key, provider_type, "
            "context_window, "
            "max_output_tokens, supports_thinking, default_reasoning_effort, "
            "temperature, is_active, fallback, created_at, updated_at "
            "FROM llm_models WHERE id = ? OR model_id = ? LIMIT 1",
            [model_pk, model_pk])
        if row is None:
            return None
        return self._row_to_model(row, mask_key=False)

    async def find_by_name(self, name: str) -> dict | None:
        row = await meta_db.query_one(
            "SELECT id, name, model_id, base_url, api_key, provider_type, "
            "context_window, max_output_tokens, supports_thinking, "
            "default_reasoning_effort, temperature, is_active, fallback, "
            "created_at, updated_at FROM llm_models WHERE name = ? LIMIT 1",
            [name],
        )
        return self._row_to_model(row, mask_key=False) if row else None

    async def upsert_by_name(self, attrs: dict) -> dict:
        """Create or update a channel model keyed by display name."""
        name = attrs.get("name") or ""
        existing = await self.find_by_name(name) if name else None
        if existing:
            await self.update(existing["id"], attrs)
            refreshed = await self.get(existing["id"])
            return refreshed or existing
        return await self.create(attrs)

    async def update(self, model_pk: str, attrs: dict) -> str | None:
        """Update a model. Only non-None fields updated.

        契约 18: update_model
        - is_active 用 'is_active' in attrs 判断（支持显式 False）
        - 无字段时返回 None（表示 "No fields to update"）
        - 补全 E9: 支持 default_reasoning_effort / temperature
        Returns the model ID on success, None if no fields to update.
        """
        fields: list[str] = []
        params: list = []
        for key in ("name", "model_id", "base_url", "api_key",
                    "provider_type",
                    "context_window", "max_output_tokens",
                    "default_reasoning_effort", "temperature"):
            if key in attrs and attrs[key] is not None:
                fields.append(f"{key} = ?")
                params.append(attrs[key])
        if "supports_thinking" in attrs and attrs["supports_thinking"] is not None:
            fields.append("supports_thinking = ?")
            params.append(1 if attrs["supports_thinking"] else 0)
        if "is_active" in attrs:
            fields.append("is_active = ?")
            params.append(1 if attrs["is_active"] else 0)
        if not fields:
            return None

        # 物理不变量校验（PATCH 语义：merge 现有值后校验）。
        # 治本：若本次 update 会把 max_output/context_window 改成非法组合，
        # 在落库前拒绝。auto-correct 走的也是这条路径，脏数据检测值若
        # 违反不变量会被这里拦住，不会流入 DB。
        existing = await self.get(model_pk)
        if existing is not None:
            merged_ctx = attrs.get("context_window", existing.get("context_window")) or _DEFAULT_CONTEXT_WINDOW
            merged_max = attrs.get("max_output_tokens", existing.get("max_output_tokens")) or _DEFAULT_MAX_OUTPUT
            _validate_invariant(merged_ctx, merged_max)

        now_ms = int(time.time() * 1000)
        fields.append("updated_at = ?")
        params.append(now_ms)
        params.append(model_pk)
        await meta_db.execute(
            f"UPDATE llm_models SET {', '.join(fields)} WHERE id = ?",
            params)
        log.info("model_updated", model_pk=model_pk)
        return model_pk

    async def delete(self, model_pk: str) -> None:
        """Delete a model by ID."""
        await meta_db.execute("DELETE FROM llm_models WHERE id = ?", [model_pk])
        log.info("model_deleted", model_pk=model_pk)

    async def list_all(self) -> list[dict]:
        """List all models (api_key masked). ORDER BY created_at ASC.

        契约 18: list_models — api_key 脱敏（前 8 字符 + '...'，nil 保持 nil）。
        异常返回 []（fail-empty）。
        """
        try:
            rows = await meta_db.query(
                "SELECT id, name, model_id, base_url, api_key, provider_type, "
                "context_window, "
                "max_output_tokens, supports_thinking, default_reasoning_effort, "
                "temperature, is_active, created_at, updated_at "
                "FROM llm_models ORDER BY created_at ASC")
            return [self._row_to_model(r, mask_key=True) for r in rows]
        except Exception as e:
            log.warning("list_models_failed", error=str(e))
            return []

    async def list_active(self) -> list[dict]:
        """List active models (is_active=1). ORDER BY created_at ASC.

        契约 18: get_active_models — 返回 [{id, name, model_id}]。
        """
        try:
            rows = await meta_db.query(
                "SELECT id, name, model_id FROM llm_models "
                "WHERE is_active = 1 ORDER BY created_at ASC")
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("list_active_models_failed", error=str(e))
            return []

    async def list_active_full(self) -> list[dict]:
        """Active models with full api_key (for pool / streamer)."""
        try:
            rows = await meta_db.query(
                "SELECT id, name, model_id, base_url, api_key, provider_type, "
                "context_window, max_output_tokens, supports_thinking, "
                "default_reasoning_effort, temperature, is_active, fallback, "
                "created_at, updated_at "
                "FROM llm_models WHERE is_active = 1 ORDER BY created_at ASC"
            )
            return [self._row_to_model(r, mask_key=False) for r in rows]
        except Exception as e:
            log.warning("list_active_full_failed", error=str(e))
            return []

    async def pick_from_pool(self, preferred: str | None = None) -> dict | None:
        """Round-robin among active models to spread provider rate limits."""
        active = await self.list_active_full()
        if not active:
            return await self.get(preferred) if preferred else None
        if len(active) == 1:
            return active[0]
        idx = next(_pool_counter) % len(active)
        chosen = active[idx]
        log.debug(
            "model_pool_pick",
            chosen=chosen.get("name"),
            base_url=(chosen.get("base_url") or "")[:48],
            pool_size=len(active),
            preferred=preferred,
        )
        return chosen

    async def ensure_channel_models(self) -> dict:
        """Upsert Ark Plan (+ optional Coding) channels for multi-quota pooling."""
        from hiveweave.config import settings

        ensured: list[str] = []

        plan_key = (
            (settings.ark_api_key or "").strip()
            or os.environ.get("HIVEWEAVE_ARK_API_KEY", "").strip()
            or os.environ.get("ARK_API_KEY", "").strip()
        )
        plan_url = (
            (settings.ark_base_url or "").strip()
            or os.environ.get(
                "HIVEWEAVE_ARK_BASE_URL",
                "https://ark.cn-beijing.volces.com/api/plan/v3",
            )
        )
        plan_model = (
            (settings.ark_model_id or "").strip()
            or os.environ.get("HIVEWEAVE_ARK_MODEL_ID", "deepseek-v4-flash")
        )
        if plan_key:
            row = await self.upsert_by_name(
                {
                    "name": "DeepSeek V4 Flash (ARK Plan)",
                    "model_id": plan_model,
                    "base_url": plan_url.rstrip("/"),
                    "api_key": plan_key,
                    "provider_type": "openai-compatible",
                    "context_window": 1_024_000,
                    "max_output_tokens": 384_000,
                    "supports_thinking": True,
                    "is_active": True,
                }
            )
            ensured.append(str(row.get("id") or "plan"))
            log.info(
                "channel_model_ensured",
                channel="ark_plan",
                model_id=plan_model,
                base_url=plan_url[:56],
            )

        coding_key = (
            (settings.ark_coding_api_key or "").strip()
            or os.environ.get("HIVEWEAVE_ARK_CODING_API_KEY", "").strip()
        )
        coding_url = (
            (settings.ark_coding_base_url or "").strip()
            or os.environ.get(
                "HIVEWEAVE_ARK_CODING_BASE_URL",
                "https://ark.cn-beijing.volces.com/api/coding/v3",
            )
        )
        coding_model = (
            (settings.ark_coding_model_id or "").strip()
            or os.environ.get("HIVEWEAVE_ARK_CODING_MODEL_ID", "deepseek-v4-flash")
        )
        if not coding_key:
            existing_coding = await self.find_by_name(
                "DeepSeek V4 Flash (ARK Coding)"
            )
            if existing_coding and existing_coding.get("api_key"):
                coding_key = existing_coding["api_key"]
                coding_url = existing_coding.get("base_url") or coding_url
                coding_model = existing_coding.get("model_id") or coding_model

        if coding_key and coding_key != plan_key:
            row = await self.upsert_by_name(
                {
                    "name": "DeepSeek V4 Flash (ARK Coding)",
                    "model_id": coding_model,
                    "base_url": coding_url.rstrip("/"),
                    "api_key": coding_key,
                    "provider_type": "openai-compatible",
                    "context_window": 1_024_000,
                    "max_output_tokens": 384_000,
                    "supports_thinking": True,
                    "is_active": True,
                }
            )
            ensured.append(str(row.get("id") or "coding"))
            log.info(
                "channel_model_ensured",
                channel="ark_coding",
                model_id=coding_model,
                base_url=coding_url[:56],
            )
        elif coding_key and coding_key == plan_key:
            log.info("channel_model_skip_coding_same_key")

        active = await self.list_active()
        return {
            "ensured": ensured,
            "active_count": len(active),
            "active": [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "model_id": a.get("model_id"),
                }
                for a in active
            ],
        }

    async def seed_default_model(self) -> dict | str:
        """Ensure Ark channels; seed first model only if table empty."""
        try:
            ensured = await self.ensure_channel_models()
            if ensured.get("ensured"):
                return {"ensured_channels": ensured}
        except Exception as e:
            log.warning("ensure_channel_models_failed", error=str(e))

        count_row = await meta_db.query_one(
            "SELECT COUNT(*) AS cnt FROM llm_models")
        if count_row and count_row["cnt"] > 0:
            return "already_seeded"

        ark_key = (
            os.environ.get("HIVEWEAVE_ARK_API_KEY", "")
            or os.environ.get("ARK_API_KEY", "")
        )
        if ark_key:
            attrs = {
                "name": "DeepSeek V4 Flash (ARK Plan)",
                "model_id": os.environ.get(
                    "HIVEWEAVE_ARK_MODEL_ID", "deepseek-v4-flash"
                ),
                "base_url": os.environ.get(
                    "HIVEWEAVE_ARK_BASE_URL",
                    "https://ark.cn-beijing.volces.com/api/plan/v3",
                ),
                "api_key": ark_key,
                "provider_type": "openai-compatible",
                "context_window": 1_024_000,
                "max_output_tokens": 384_000,
                "supports_thinking": True,
                "is_active": True,
            }
            result = await self.create(attrs)
            log.info("default_model_seeded_ark", model_id=result["id"])
            return result

        api_key = os.environ.get("STEP_API_KEY", "")
        if api_key:
            attrs = {
                "name": "Step 3.7 Flash",
                "model_id": "step-3.7-flash",
                "base_url": "https://api.stepfun.com/step_plan/v1",
                "api_key": api_key,
                "provider_type": "anthropic",
                "context_window": 200_000,
                "max_output_tokens": _DEFAULT_MAX_OUTPUT,
                "supports_thinking": False,
                "is_active": True,
            }
            result = await self.create(attrs)
            log.info("default_model_seeded", model_id=result["id"])
            return result

        log.warning("seed_default_model_no_api_key")
        return {"error": "no_api_key"}

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _row_to_model(row, mask_key: bool = False) -> dict:
        d = dict(row)
        d["supports_thinking"] = bool(d.get("supports_thinking"))
        d["is_active"] = bool(d.get("is_active"))
        key = d.get("api_key")
        if mask_key:
            if key:
                d["api_key"] = key[:8] + "..."
            else:
                d["api_key"] = None
        return d
