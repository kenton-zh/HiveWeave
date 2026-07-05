"""Global settings service — key-value store.

契约 18: SettingsService
- Meta DB 中的 global_settings 表，简单 key-value 存储（如 operatorName）
- set 用 ON CONFLICT 原子 upsert（修复 E11: 替代 DELETE+INSERT）
- get 异常返回 None（fail-null）; list_all 异常返回 {}（fail-empty）
- delete 异常静默（fail-silent，删除是幂等的）

global_settings 表 schema 已完整，无需迁移。
"""

import time

import structlog

from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)


class SettingsService:
    """Global key-value settings — CRUD on Meta DB.

    所有操作路由到 Meta DB（全局单例）。
    """

    async def get(self, key: str) -> str | None:
        """Get a setting value by key.

        契约 18: get — 不存在/异常返回 None（fail-null）。
        """
        try:
            row = await meta_db.query_one(
                "SELECT value FROM global_settings WHERE key = ? LIMIT 1", [key])
            return row["value"] if row else None
        except Exception as e:
            log.warning("settings_get_failed", key=key, error=str(e))
            return None

    async def set(self, key: str, value) -> str:
        """Upsert a setting (atomic via ON CONFLICT).

        契约 18: set — value 用 str(value) 转字符串存储。
        修复 E11: 用 INSERT ... ON CONFLICT(key) DO UPDATE 原子 upsert。
        Returns the stored string value.
        """
        now_ms = int(time.time() * 1000)
        str_value = str(value)
        db = await meta_db.get_meta_db()
        await db.execute(
            "INSERT INTO global_settings (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            [key, str_value, now_ms])
        await db.commit()
        log.info("setting_set", key=key)
        return str_value

    async def delete(self, key: str) -> None:
        """Delete a setting by key.

        契约 18: delete — 总是返回 None（异常也静默，fail-silent）。
        """
        try:
            await meta_db.execute(
                "DELETE FROM global_settings WHERE key = ?", [key])
        except Exception as e:
            log.warning("settings_delete_failed", key=key, error=str(e))

    async def list_all(self) -> dict[str, str]:
        """Get all settings as a dict.

        契约 18: all — SELECT key, value ORDER BY key, 转 {key: value}。
        异常返回 {}（fail-empty）。
        """
        try:
            rows = await meta_db.query(
                "SELECT key, value FROM global_settings ORDER BY key")
            return {r["key"]: r["value"] for r in rows}
        except Exception as e:
            log.warning("settings_all_failed", error=str(e))
            return {}
