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

    # OpenCode API key (legacy seed path)
    opencode_api_key: str = ""

    # Volcengine Ark — Agent Plan (OpenAI-compatible /api/plan/v3)
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/plan/v3"
    ark_model_id: str = "deepseek-v4-flash"

    # Optional second Ark channel (Coding Plan) for quota mixing
    ark_coding_api_key: str = ""
    ark_coding_base_url: str = "https://ark.cn-beijing.volces.com/api/coding/v3"
    ark_coding_model_id: str = "deepseek-v4-flash"

    # 专用压缩（compactor）模型 — llm_models 表 id（HIVEWEAVE_COMPACTOR_MODEL_ID）。
    # 空 = 用 agent 自己的模型。建议配一个便宜的 non-reasoning 模型：
    # reasoning 模型会把 max_tokens 预算花在思考链上导致摘要 content 空
    # （TEST18 巡检 P0），且解耦 agent 主模型故障与压缩故障。
    compactor_model_id: str = ""

    # Round-robin across active models to spread rate limits (default on)
    model_pool_enabled: bool = True

    # External skills directory (best-effort; 不存在则返回空)
    # 默认为空 — 需通过环境变量 HIVEWEAVE_EXTERNAL_SKILLS_DIR 指定
    external_skills_dir: str = ""

    # SkillHub 国内技能商店 (https://skillhub.cn)
    # 当国外商店 (skills.sh) 不可达时自动路由到 SkillHub 搜索。
    # HIVEWEAVE_SKILLHUB_ENABLED=false 可关闭此降级路径。
    skillhub_enabled: bool = True
    # SkillHub 搜索 API（默认 lightmake.site）
    skillhub_search_url: str = "https://lightmake.site/api/v1/search"

    # agent-browser CLI binary (optional). Empty = auto-detect:
    # packaged Electron resources → node_modules/agent-browser (dev).
    # Example Windows: C:\...\apps\web\node_modules\agent-browser\bin\agent-browser-win32-x64.exe
    browse_bin: str = ""

    # Wait Contract default TTLs (ms) — P0 Hard Gates Phase 2
    wait_ttl_agent_ms: int = 15 * 60 * 1000
    wait_ttl_user_ms: int = 60 * 60 * 1000
    wait_ttl_task_ms: int = 30 * 60 * 1000
    wait_ttl_external_ms: int = 30 * 60 * 1000
    wait_ttl_timer_ms: int = 15 * 60 * 1000

    # Attestation max age (ms) — Phase 3
    attestation_max_age_ms: int = 24 * 60 * 60 * 1000

    # SQLite busy_timeout (ms). Journal stays DELETE; this only waits on locks.
    sqlite_busy_timeout_ms: int = 5000

    # ACL 写受限令牌沙箱（spec docs/spec/windows-acl-sandbox.md）。
    # P3 默认 on：受限 shell = pwsh 承载，命令 verbatim（P1-3 B 结构解，
    # 词典翻译 _normalize_for_pwsh 已退役，unix-only 前置拒绝给 pwsh 等价）；
    # 确需关闭用 env HIVEWEAVE_ACL_SANDBOX=off。
    acl_sandbox: bool = True
    # 专用排空线程池大小（§5.4）。
    acl_max_concurrent: int = 32
    # JS 工具链降级开关：confined=受限 / native=白名单命令走 native（显式 fail-open）。
    acl_js_toolchain: str = "confined"
    # 哨兵探针周期（秒，P1 §13 判据；沙箱 on 时后端周期注入 S-1-4 探针）。
    acl_sentinel_interval_s: int = 300

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


def sqlite_busy_timeout_sql() -> str:
    """PRAGMA for DELETE-mode SQLite connections (HIVEWEAVE_SQLITE_BUSY_TIMEOUT_MS)."""
    ms = int(getattr(settings, "sqlite_busy_timeout_ms", 5000) or 5000)
    if ms < 0:
        ms = 0
    return f"PRAGMA busy_timeout={ms}"


def agent_browser_bin_name() -> str:
    """Platform-specific agent-browser native binary filename (bin/*)."""
    import platform as _platform

    system = _platform.system().lower()
    machine = _platform.machine().lower()
    if system == "windows":
        # Windows ARM64 runs the x64 binary via emulation (upstream choice).
        return "agent-browser-win32-x64.exe"
    if system == "darwin":
        arch = "arm64" if machine in ("aarch64", "arm64") else "x64"
        return f"agent-browser-darwin-{arch}"
    arch = "arm64" if machine in ("aarch64", "arm64") else "x64"
    return f"agent-browser-linux-{arch}"


def _repo_root() -> Path:
    # config.py 位于 apps/hiveweave-py/src/hiveweave/config.py → parents[4] = 仓库根
    return Path(__file__).resolve().parents[4]


def resolve_browse_bin() -> Path | None:
    """Locate the agent-browser CLI binary.

    Order: HIVEWEAVE_BROWSE_BIN (explicit) → packaged Electron resources
    (desktop; the Electron main process injects HIVEWEAVE_BROWSE_BIN for
    backends it spawns, this bounded ancestor-walk covers standalone
    backends inside the resources tree) → node_modules/agent-browser
    (source/dev install, pnpm hoists to the repo root or per-workspace).
    """
    if settings.browse_bin:
        p = Path(settings.browse_bin).expanduser()
        if p.is_file():
            return p

    import platform as _platform

    system = _platform.system().lower()
    machine = _platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "x64"
    bin_name = agent_browser_bin_name()
    candidates: list[Path] = []
    # Packaged state: resources/<something>/agent-browser/<bin>.
    # Python 后端常位于 resources 目录内（Electron 解包布局），向上找。
    for anc in Path(__file__).resolve().parents:
        candidates.append(anc / "resources" / "agent-browser" / bin_name)
        if len(candidates) >= 6:
            break
    # Dev state: node_modules/agent-browser/bin/<bin>.
    repo_root = _repo_root()
    for nm in (
        repo_root / "node_modules",
        repo_root / "apps" / "web" / "node_modules",
        Path(__file__).resolve().parents[2] / "node_modules",
    ):
        candidates.append(nm / "agent-browser" / "bin" / bin_name)

    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue

    # Fallback: any same-platform binary inside a found agent-browser package
    # (covers musl-linux and arm64/emulation mismatches). Skip the Node
    # wrapper (agent-browser.js) — we spawn the native binary directly.
    # Filtered by platform prefix so a partial install never yields a binary
    # for another OS/arch (sorted alphabetically would pick darwin/arm first).
    fallback_prefixes: list[str] = []
    if system == "windows":
        fallback_prefixes = ["agent-browser-win32-"]
    elif system == "darwin":
        fallback_prefixes = [f"agent-browser-darwin-{arch}"]
    else:
        # musl precedes glibc: sorted() puts musl names first.
        fallback_prefixes = [
            f"agent-browser-linux-musl-{arch}",
            f"agent-browser-linux-{arch}",
        ]
    for nm in (
        repo_root / "node_modules",
        repo_root / "apps" / "web" / "node_modules",
        Path(__file__).resolve().parents[2] / "node_modules",
    ):
        try:
            bin_dir = nm / "agent-browser" / "bin"
            if not bin_dir.is_dir():
                continue
            for prefix in fallback_prefixes:
                for cand in sorted(bin_dir.glob(f"{prefix}*")):
                    if cand.suffix == ".js" or not cand.is_file():
                        continue
                    return cand
        except OSError:
            continue
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
