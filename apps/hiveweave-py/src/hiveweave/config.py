"""Application configuration — environment variables and constants."""

from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    # Security: 默认仅监听 loopback，避免暴露可执行 bash 的 Agent 平台到局域网。
    # 需要外部访问时显式设置 HIVEWEAVE_HOST=0.0.0.0 并配合 HIVEWEAVE_API_KEY。
    host: str = "127.0.0.1"
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

    # Wait Contract default TTLs (ms) — P0 Hard Gates Phase 2
    wait_ttl_agent_ms: int = 15 * 60 * 1000
    wait_ttl_user_ms: int = 60 * 60 * 1000
    wait_ttl_task_ms: int = 30 * 60 * 1000
    wait_ttl_external_ms: int = 30 * 60 * 1000
    wait_ttl_timer_ms: int = 15 * 60 * 1000

    # Attestation max age (ms) — Phase 3
    attestation_max_age_ms: int = 24 * 60 * 60 * 1000

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

    Order: HIVEWEAVE_BROWSE_BIN → common Claude skills installs.
    """
    if settings.browse_bin:
        p = Path(settings.browse_bin).expanduser()
        if p.is_file():
            return p

    home = Path.home()
    candidates = [
        home / ".claude" / "skills" / "gstack" / "browse" / "dist" / "browse.exe",
        home / ".claude" / "skills" / "gstack" / "browse" / "dist" / "browse",
        home / ".claude" / "skills" / "browse" / "dist" / "browse.exe",
        home / ".claude" / "skills" / "browse" / "dist" / "browse",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def warn_if_insecure(host: str, api_key: str) -> None:
    """启动时检测不安全配置并打醒目警告。

    - 无 API key 且监听非 loopback 接口：高危（任何人可调 bash）→ WARNING
    - 无 API key 但仅 loopback：dev 友好但生产需 key → 提示性 WARNING
    """
    import logging

    log = logging.getLogger("hiveweave.security")
    # 0.0.0.0 在 Windows 上等价于 127.0.0.1（仅监听 loopback），
    # 但在 Linux/macOS 上会监听所有接口 — 视为非 loopback 以保守告警。
    is_loopback = host in ("127.0.0.1", "localhost", "::1")
    if not api_key and not is_loopback:
        log.warning(
            "!! SECURITY WARNING !! "
            "HIVEWEAVE_API_KEY is empty and host=%s is not loopback. "
            "Anyone on the network can operate this Agent platform (which can execute bash). "
            "Set HIVEWEAVE_API_KEY or bind to 127.0.0.1.",
            host,
        )
    elif not api_key:
        log.warning(
            "HIVEWEAVE_API_KEY is empty (open access). "
            "Safe only on loopback host=%s. Set HIVEWEAVE_API_KEY for production.",
            host,
        )
