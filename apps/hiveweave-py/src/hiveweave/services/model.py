"""LLM model service — model registry CRUD.

契约 18: ModelService
- Meta DB 中的 llm_models 表 CRUD
- list_all 对 api_key 脱敏（前 8 字符 + '...'）；get 返回完整 api_key
- seed_default_model 启动种子（OPENCODE_API_KEY → DeepSeek V4 Flash Free）
- 补全 E9/E10: create/update 支持 supports_thinking/default_reasoning_effort/temperature

llm_models 表 schema 已完整，无需迁移。
"""

import os
import time
import uuid

import structlog

from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)

# Default values (契约 18)
_DEFAULT_CONTEXT_WINDOW = 128_000
_DEFAULT_MAX_OUTPUT = 8_192


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
            "temperature, is_active, created_at, updated_at "
            "FROM llm_models WHERE id = ? OR model_id = ? LIMIT 1",
            [model_pk, model_pk])
        if row is None:
            return None
        return self._row_to_model(row, mask_key=False)

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

    async def seed_default_model(self) -> dict | str:
        """Seed default model if table is empty.

        契约 18: seed_default_model (E14: Elixir 未实现，Python 必须实现)
        - 表已有记录 → 返回 'already_seeded'
        - STEP_API_KEY 缺失 → 返回 {'error': 'no_api_key'}
        - 否则种子阶跃星辰 step-3.7-flash 并返回模型 dict
        """
        count_row = await meta_db.query_one(
            "SELECT COUNT(*) AS cnt FROM llm_models")
        if count_row and count_row["cnt"] > 0:
            return "already_seeded"

        api_key = os.environ.get("STEP_API_KEY", "")
        if not api_key:
            log.warning("seed_default_model_no_api_key")
            return {"error": "no_api_key"}

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
