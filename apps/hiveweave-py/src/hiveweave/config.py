"""Application configuration — environment variables and constants."""

from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 4000  # 契约 constants.md: 前端兼容性，端口 4000

    # Meta DB
    # 契约 11: Meta DB 默认路径 apps/hiveweave-py/data/hiveweave.db
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

    # External skills directory (best-effort; 不存在则返回空)
    # 默认为空 — 需通过环境变量 HIVEWEAVE_EXTERNAL_SKILLS_DIR 指定
    external_skills_dir: str = ""

    # gstack browse CLI binary (optional). Empty = auto-detect common install paths.
    # Example Windows: C:\Users\...\ .claude\skills\gstack\browse\dist\browse.exe
    browse_bin: str = ""

    model_config = {
        "env_prefix": "HIVEWEAVE_",
        "env_file": ".env",
        "extra": "ignore",
    }

    def get_meta_db_path(self) -> str:
        """Return resolved Meta DB path."""
        if self.meta_db_path:
            return self.meta_db_path
        # Default: apps/hiveweave-py/data/hiveweave.db
        # config.py 位于 apps/hiveweave-py/src/hiveweave/config.py
        # parents[2] = apps/hiveweave-py/
        app_root = Path(__file__).resolve().parents[2]
        return str(app_root / "data" / "hiveweave.db")


settings = Settings()


def resolve_browse_bin() -> Path | None:
    """Locate the gstack browse CLI binary.

    Order: HIVEWEAVE_BROWSE_BIN → common Claude skills installs → D:\\PC_AI\\Project\\gstack.
    """
    if settings.browse_bin:
        p = Path(settings.browse_bin).expanduser()
        if p.is_file():
            return p

    home = Path.home()
    candidates = [
        home / ".claude" / "skills" / "gstack" / "browse" / "dist" / "browse.exe",
        home / ".claude" / "skills" / "gstack" / "browse" / "dist" / "browse",
        Path(r"D:\PC_AI\Project\gstack\browse\dist\browse.exe"),
        Path(r"D:\PC_AI\Project\gstack\browse\dist\browse"),
        home / ".claude" / "skills" / "browse" / "dist" / "browse.exe",
        home / ".claude" / "skills" / "browse" / "dist" / "browse",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None
