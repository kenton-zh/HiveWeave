"""Application configuration — environment variables and constants."""

from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 4000  # 契约 constants.md: 前端兼容性，端口 4000

    # Meta DB
    # 契约 11: Meta DB 默认路径 packages/db/data/hiveweave.db
    # Elixir 用 HIVEWEAVE_META_DB_PATH，TS 用 HIVEWEAVE_DB_PATH
    meta_db_path: str = ""

    # API Key auth (契约 19: ApiKeyAuth — 环境变量未设则开放)
    api_key: str = ""

    # CORS origins — 白名单（生产安全）。
    # R1 fix: 不使用 ["*"]，仅允许前端 Vite dev (5173) + preview (4173) 端口。
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ]

    # OpenCode API key (契约 18: seedDefaultModel 读此变量)
    opencode_api_key: str = ""

    model_config = {
        "env_prefix": "HIVEWEAVE_",
        "env_file": ".env",
        "extra": "ignore",
    }

    def get_meta_db_path(self) -> str:
        """Return resolved Meta DB path."""
        if self.meta_db_path:
            return self.meta_db_path
        # Default: <repo_root>/packages/db/data/hiveweave.db
        # From apps/hiveweave-py/ that's ../../packages/db/data/hiveweave.db
        repo_root = Path(__file__).resolve().parents[4]
        return str(repo_root / "packages" / "db" / "data" / "hiveweave.db")


settings = Settings()
