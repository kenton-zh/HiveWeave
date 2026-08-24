"""LLM model service — model registry CRUD.

契约 18: ModelService
- Meta DB 中的 llm_models 表 CRUD
- list_all 对 api_key 脱敏（前 8 字符 + '...'）；get 返回完整 api_key
- seed_default_model / ensure_channel_models 启动种子（多渠道混用以摊配额）
- 补全 E9/E10: create/update 支持 supports_thinking/default_reasoning_effort/temperature
- thinking_format: 思考方言（空=跟协议推断）
"""

from __future__ import annotations

import itertools
import os
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db
from hiveweave.llm.thinking import normalize_thinking_format
from hiveweave.llm.wire_endpoint import apply_wire_endpoint

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


class NoModelConfiguredError(RuntimeError):
    """Agent/tier 无可解析模型（未配置或解析失败）——「缺失」而非「非法」。

    一次性子调用（review/audit 回调）依赖它把「无模型」与网络/HTTP 故障
    分成不同的软失败 reason。调用方按类型捕获，禁止子串嗅探异常文案。
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


def _apply_wire_on_update(attrs: dict, existing: dict) -> None:
    """Strip leftover endpoint suffixes when URL or protocol is patched."""
    if "base_url" not in attrs and "provider_type" not in attrs:
        return
    url = (
        attrs["base_url"]
        if "base_url" in attrs and attrs["base_url"] is not None
        else (existing.get("base_url") or "")
    )
    proto = (
        attrs["provider_type"]
        if "provider_type" in attrs and attrs["provider_type"] is not None
        else (existing.get("provider_type") or "")
    )
    prefix, proto = apply_wire_endpoint(url, proto)
    attrs["base_url"] = prefix
    attrs["provider_type"] = proto


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
        base_url, provider_type = apply_wire_endpoint(
            attrs.get("base_url") or "",
            attrs.get("provider_type"),
        )
        api_key = attrs.get("api_key", "")
        context_window = attrs.get("context_window", _DEFAULT_CONTEXT_WINDOW)
        max_output = attrs.get("max_output_tokens", _DEFAULT_MAX_OUTPUT)
        supports_thinking = 1 if attrs.get("supports_thinking", False) else 0
        thinking_format = normalize_thinking_format(attrs.get("thinking_format"))
        is_active = 0 if attrs.get("is_active") is False else 1
        default_reasoning_effort = attrs.get("default_reasoning_effort")
        temperature = attrs.get("temperature")
        tier = attrs.get("tier") or None  # "management" | "executor" | None（"" 视为未分类）

        # 物理不变量：max_output_tokens 必须严格小于 context_window。
        # 治本：非法配置在落库前拒绝，绝不 clamp 后悄悄写入。
        _validate_invariant(context_window, max_output)

        await meta_db.execute(
            "INSERT INTO llm_models (id, name, model_id, base_url, api_key, "
            "provider_type, "
            "context_window, max_output_tokens, supports_thinking, "
            "thinking_format, default_reasoning_effort, temperature, "
            "is_active, tier, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [model_pk, name, model_id, base_url, api_key,
             provider_type,
             context_window, max_output, supports_thinking,
             thinking_format, default_reasoning_effort, temperature,
             is_active, tier, now_ms, now_ms])
        log.info("model_created", model_pk=model_pk, name=name, model_id=model_id)
        return {"id": model_pk, "name": name, "model_id": model_id}

    async def get(self, model_pk: str) -> dict | None:
        """Get a model by ID or model_id. Returns full api_key (not masked).

        契约 18: get_model — api_key 完整返回（Streamer 需完整 key 调 LLM）。
        支持按 id（UUID）或 model_id（如 step-3.7-flash）查询。
        agents.model_id 优先存 UUID；存量数据可能为名称，此时同名多渠道
        返回最近更新的活跃行（确定性兜底，而非物理顺序第一行）。
        """
        row = await meta_db.query_one(
            "SELECT id, name, model_id, base_url, api_key, provider_type, "
            "context_window, "
            "max_output_tokens, supports_thinking, thinking_format, "
            "default_reasoning_effort, "
            "temperature, is_active, fallback, tier, created_at, updated_at "
            "FROM llm_models WHERE id = ? OR model_id = ? "
            "ORDER BY is_active DESC, updated_at DESC, id DESC LIMIT 1",
            [model_pk, model_pk])
        if row is None:
            return None
        return self._row_to_model(row, mask_key=False)

    async def find_by_name(self, name: str) -> dict | None:
        row = await meta_db.query_one(
            "SELECT id, name, model_id, base_url, api_key, provider_type, "
            "context_window, max_output_tokens, supports_thinking, "
            "thinking_format, default_reasoning_effort, temperature, "
            "is_active, fallback, tier, "
            "created_at, updated_at FROM llm_models WHERE name = ? "
            "ORDER BY is_active DESC, updated_at DESC, id DESC LIMIT 1",
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
        existing = await self.get(model_pk)
        if existing is not None:
            _apply_wire_on_update(attrs, existing)

        fields: list[str] = []
        params: list = []
        for key in ("name", "model_id", "base_url", "api_key",
                    "provider_type",
                    "context_window", "max_output_tokens",
                    "default_reasoning_effort", "temperature",
                    "fallback"):
            if key in attrs and attrs[key] is not None:
                fields.append(f"{key} = ?")
                params.append(attrs[key])
        # tier 单独处理："" 表示清空（置 NULL），非空字符串正常写入。
        # 不能放进上面的通用循环——那里 `is not None` 会把 "" 当有效值存成空串。
        if "tier" in attrs:
            fields.append("tier = ?")
            params.append(attrs["tier"] or None)
        if "supports_thinking" in attrs and attrs["supports_thinking"] is not None:
            fields.append("supports_thinking = ?")
            params.append(1 if attrs["supports_thinking"] else 0)
        if "thinking_format" in attrs and attrs["thinking_format"] is not None:
            fields.append("thinking_format = ?")
            params.append(normalize_thinking_format(attrs["thinking_format"]))
        if "is_active" in attrs:
            fields.append("is_active = ?")
            params.append(1 if attrs["is_active"] else 0)
        if not fields:
            return None

        # 物理不变量校验（PATCH 语义：merge 现有值后校验）。
        # 治本：若本次 update 会把 max_output/context_window 改成非法组合，
        # 在落库前拒绝。auto-correct 走的也是这条路径，脏数据检测值若
        # 违反不变量会被这里拦住，不会流入 DB。
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
        """Delete a model by ID.

        2026-08-24：删除即永久——同时写入 tombstone（global_settings），
        阻止启动时 ensure_channel_models 按渠道名自动重建。用户从模型清单
        删除的渠道模型不再因 .env 残留配置而复活。
        - get 按 id 或 model_id 命中，删除统一用解析出的 ``id``（M2 审计）。
        - 先写 tombstone 再删行，删行失败回滚 tombstone（A1 审计：避免
          "行已删但无 tombstone"导致重启后静默复活）。
        """
        model = await self.get(model_pk)
        if model is None:
            log.info("model_delete_missing", model_pk=model_pk)
            return
        # 只对渠道模型落 tombstone，键锁定为渠道常量名（防改名后与 ensure 查询脱钩）
        name = model.get("name") or ""
        channel_name = name if name in self._CHANNEL_NAMES else None
        if channel_name:
            await self._set_tombstone(channel_name)
        try:
            await meta_db.execute(
                "DELETE FROM llm_models WHERE id = ?", [model["id"]]
            )
        except Exception:
            if channel_name:
                await self._clear_tombstone(channel_name)
            raise
        log.info("model_deleted", model_pk=model_pk, name=name)

    # ── Tombstone（删除即永久，2026-08-24）─────────────────────
    # ensure_channel_models 会在启动时按 .env 的 key 自动 upsert 渠道模型。
    # 若用户已从模型清单删除该渠道，删除动作写 tombstone，启动 ensure 跳过
    # 重建；用户手动重新创建（create）则清除同名 tombstone 恢复正常渠道更新。
    #
    # tombstone 键必须用**渠道常量名**（而非 DB 中的可编辑展示名）：若用户先把
    # 渠道模型改名再删除，按 DB 名写 tombstone 会与 ensure 用常量名查询脱钩，
    # 导致删除的渠道在重启后被静默重建（B1 审计）。只对渠道模型落 tombstone，
    # 改名后的模型不再命中渠道名集合 → 属普通删除，不阻塞下次 .env 重建。

    _TOMBSTONE_PREFIX = "model_tombstone:"
    _ARK_PLAN_NAME = "DeepSeek V4 Flash (ARK Plan)"
    _ARK_CODING_NAME = "DeepSeek V4 Flash (ARK Coding)"
    _STEP_NAME = "Step 3.7 Flash"
    _CHANNEL_NAMES = frozenset({_ARK_PLAN_NAME, _ARK_CODING_NAME, _STEP_NAME})

    async def _set_tombstone(self, name: str) -> None:
        """记录某渠道模型已被用户删除（阻止 ensure 自动重建）。"""
        from hiveweave.services.settings import SettingsService
        await SettingsService().set(
            f"{self._TOMBSTONE_PREFIX}{name}", int(time.time() * 1000)
        )

    async def _clear_tombstone(self, name: str) -> None:
        """清除某渠道模型的删除标记（用户手动重新创建时撤销删除）。"""
        from hiveweave.services.settings import SettingsService
        await SettingsService().delete(f"{self._TOMBSTONE_PREFIX}{name}")

    async def _is_tombstoned(self, name: str) -> bool:
        """该渠道模型是否曾被用户删除（tombstone 存在即永久跳过重建）。"""
        from hiveweave.services.settings import SettingsService
        return bool(
            await SettingsService().get(f"{self._TOMBSTONE_PREFIX}{name}")
        )

    async def list_all(self) -> list[dict]:
        """List all models (api_key masked). ORDER BY created_at ASC.

        契约 18: list_models — api_key 脱敏（前 8 字符 + '...'，nil 保持 nil）。
        异常返回 []（fail-empty）。
        """
        try:
            rows = await meta_db.query(
                "SELECT id, name, model_id, base_url, api_key, provider_type, "
                "context_window, "
                "max_output_tokens, supports_thinking, thinking_format, "
                "default_reasoning_effort, "
                "temperature, is_active, tier, created_at, updated_at "
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
                "thinking_format, default_reasoning_effort, temperature, "
                "is_active, fallback, tier, "
                "created_at, updated_at "
                "FROM llm_models WHERE is_active = 1 ORDER BY created_at ASC"
            )
            return [self._row_to_model(r, mask_key=False) for r in rows]
        except Exception as e:
            log.warning("list_active_full_failed", error=str(e))
            return []

    async def pick_from_pool(self, preferred: str | None = None) -> dict | None:
        """Round-robin among active models to spread provider rate limits.

        DEPRECATED for tier-aware routing — use resolve_model() instead.
        Kept for backward compat if model_pool_enabled and no tier configured.
        """
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

    # ── Tier-aware model resolution ─────────────────────────

    async def get_tier_config(self, tier: str) -> dict[str, str | None]:
        """Read primary/backup model IDs for a tier from global_settings.

        Keys: model_tier_{tier}_primary / model_tier_{tier}_backup
        Falls back to legacy default_coordinator_model / default_executor_model
        for primary if the new keys are unset.
        """
        from hiveweave.services.settings import SettingsService
        svc = SettingsService()

        primary = await svc.get(f"model_tier_{tier}_primary")
        backup = await svc.get(f"model_tier_{tier}_backup")

        # Legacy fallback for primary
        if not primary:
            if tier == "management":
                primary = await svc.get("default_coordinator_model")
            elif tier == "executor":
                primary = await svc.get("default_executor_model")

        return {"primary": primary, "backup": backup}

    async def resolve_model(
        self,
        tier: str,
        preferred: str | None = None,
        skip_model_ids: set[str] | None = None,
        *,
        strict: bool = False,
    ) -> dict | None:
        """Resolve model config by tier: primary → backup (strict, no cross-tier).

        Resolution order:
        0. Agent 显式指定的模型（preferred，如 UI「使用模型」切换）— 最高优先
        1. Tier primary from global_settings
        2. Tier backup from global_settings
        3. Any active model with matching tier column
        4. Fallback: pick_from_pool (legacy, only if no tier data at all)

        skip_model_ids: DB 主键(UUID)集合，用于 failover 时排除失败的主用模型。
            仅按主键匹配——主备模型可共用同一 model_id（靠编号/记录区分），
            若按 model_id 匹配会误伤备用模型。
        strict: True 时跳过第 4 步及 emergency pool 兜底——tier 完全
            解析不出就返回 None，绝不把其他档位的模型冒充本档。适合
            「辅助能力回退」场景（如 vision 回退 management），避免语义失真。
        """
        skip = skip_model_ids or set()

        def _skipped(model: dict) -> bool:
            """Check if model matches skip set by DB primary key (id) only."""
            return model.get("id") in skip

        # 0. 显式指定的模型优先（UI「使用模型」切换立即生效）
        if preferred:
            model = await self.get(preferred)
            if model and model.get("is_active") and not _skipped(model):
                log.debug(
                    "model_resolve_explicit",
                    tier=tier,
                    model=model.get("name"),
                    preferred=preferred,
                )
                return model

        tier_cfg = await self.get_tier_config(tier)

        # Try primary
        primary_id = tier_cfg["primary"]
        if primary_id and primary_id not in skip:
            model = await self.get(primary_id)
            if model and model.get("is_active") and not _skipped(model):
                return model

        # Try backup
        backup_id = tier_cfg["backup"]
        if backup_id and backup_id not in skip:
            model = await self.get(backup_id)
            if model and model.get("is_active") and not _skipped(model):
                log.info(
                    "model_resolve_backup",
                    tier=tier,
                    backup=backup_id,
                    skipped_primary=primary_id,
                )
                return model

        # Try any active model with matching tier column
        active = await self.list_active_full()
        for m in active:
            if m.get("tier") == tier and not _skipped(m):
                log.info("model_resolve_tier_column", tier=tier, model=m.get("name"))
                return m

        # Last resort: if no tier data exists at all, fall back to pool
        # (backward compat for deployments that haven't configured tiers)
        if not primary_id and not backup_id and not strict:
            has_any_tier = any(m.get("tier") for m in active)
            if not has_any_tier:
                log.debug("model_resolve_no_tier_configured", tier=tier)
                return await self.pick_from_pool(preferred)

        # Configured models all unresolvable — try pool as emergency fallback
        if active and not strict:
            for m in active:
                if not _skipped(m):
                    log.warning(
                        "model_resolve_emergency_pool",
                        tier=tier,
                        model=m.get("name"),
                    )
                    return m

        return None

    async def resolve_vision_model(
        self,
        skip_model_ids: set[str] | None = None,
    ) -> dict | None:
        """Resolve the model for the optional look_at_image helper.

        Dedicated Settings slots first (``vision_model_primary`` /
        ``vision_model_backup``). If those are empty or stale, fall through
        to the management chat model the operator already configured —
        look_at_image is auxiliary, not a second required panel.
        Management fallback is strict (no pool): if the management tier
        itself cannot be resolved, return None instead of passing off an
        arbitrary executor-tier model as "management".
        """
        from hiveweave.services.settings import SettingsService

        skip = skip_model_ids or set()
        svc = SettingsService()
        primary = await svc.get("vision_model_primary")
        backup = await svc.get("vision_model_backup")

        for slot_id in (primary, backup):
            if not slot_id or slot_id in skip:
                continue
            model = await self.get(slot_id)
            if model and model.get("is_active") and model.get("id") not in skip:
                if slot_id == backup and primary:
                    log.info(
                        "vision_model_resolve_backup",
                        backup=slot_id,
                        skipped_primary=primary,
                    )
                return model

        mgmt = await self.resolve_model(
            "management", skip_model_ids=skip, strict=True
        )
        if mgmt:
            log.info(
                "vision_model_resolve_management_fallback",
                model=mgmt.get("name"),
                model_id=mgmt.get("id"),
            )
            return mgmt
        return None

    async def resolve_image_gen_model(
        self,
        skip_model_ids: set[str] | None = None,
    ) -> dict | None:
        """Resolve the Seedream / image-generation model for generate_image.

        Prefer dedicated Settings keys (生图模型设置面板):
        ``image_gen_model_id`` / ``image_gen_base_url`` / ``image_gen_api_key``.

        Fallback: ``image_gen_model_primary`` → ``llm_models`` row (legacy).
        No fallthrough to chat tiers or vision slots.
        """
        from hiveweave.services.settings import SettingsService

        skip = skip_model_ids or set()
        svc = SettingsService()

        model_id = (await svc.get("image_gen_model_id") or "").strip()
        base_url = (await svc.get("image_gen_base_url") or "").strip()
        api_key = (await svc.get("image_gen_api_key") or "").strip()
        if model_id and base_url and api_key:
            return {
                "id": "image_gen_direct",
                "name": "生图模型",
                "model_id": model_id,
                "base_url": base_url,
                "api_key": api_key,
                "is_active": True,
            }

        primary = await svc.get("image_gen_model_primary")
        if not primary or primary in skip:
            return None
        model = await self.get(primary)
        if model and model.get("is_active") and model.get("id") not in skip:
            return model
        return None

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
        if plan_key and not await self._is_tombstoned(self._ARK_PLAN_NAME):
            row = await self.upsert_by_name(
                {
                    "name": self._ARK_PLAN_NAME,
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
        coding_tombstoned = await self._is_tombstoned(self._ARK_CODING_NAME)
        if not coding_key and not coding_tombstoned:
            existing_coding = await self.find_by_name(self._ARK_CODING_NAME)
            if existing_coding and existing_coding.get("api_key"):
                coding_key = existing_coding["api_key"]
                coding_url = existing_coding.get("base_url") or coding_url
                coding_model = existing_coding.get("model_id") or coding_model

        if (
            coding_key
            and coding_key != plan_key
            and not coding_tombstoned
        ):
            row = await self.upsert_by_name(
                {
                    "name": self._ARK_CODING_NAME,
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
        if ark_key and not await self._is_tombstoned(self._ARK_PLAN_NAME):
            attrs = {
                "name": self._ARK_PLAN_NAME,
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
        if api_key and not await self._is_tombstoned(self._STEP_NAME):
            attrs = {
                "name": self._STEP_NAME,
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
        d["thinking_format"] = normalize_thinking_format(d.get("thinking_format"))
        d["is_active"] = bool(d.get("is_active"))
        key = d.get("api_key")
        if mask_key:
            if key:
                d["api_key"] = key[:8] + "..."
            else:
                d["api_key"] = None
        return d
