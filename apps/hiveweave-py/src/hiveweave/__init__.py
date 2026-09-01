"""HiveWeave backend — Python port from Elixir/Phoenix."""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"


def _bootstrap_dotenv() -> None:
    """把 ``apps/hiveweave-py/.env`` 灌进 ``os.environ``（不覆盖已有变量）。

    根因（2026-08-30 实测）：仓库里 34 处 ``os.environ.get("HIVEWEAVE_…")``
    分散在 main / services.model / game_time / streamer.constants /
    command_guard 等模块，而 ``config.py`` 走 pydantic-settings 自带的
    ``env_file=".env"`` —— 两条通道互不相通，os.environ 侧永远只看到默认值。

    后果：.env 里调好的 ``HIVEWEAVE_STREAM_HARD_TIMEOUT_S=1710``、
    ``LLM_MAX_CONCURRENT=12`` 等参数从未生效，平台一直跑默认 570s，
    13 次 stream_hard_timeout 烧掉 123.5 分钟墙钟（≈总墙钟 40%）。

    本函数在包导入时最先执行，让两条通道看到同一份配置；``override=False``
    保证 shell 显式导出的变量优先。缺失 dotenv 或 .env 时静默跳过——
    配置加载失败绝不应阻止启动。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    try:
        # parents: [0]=hiveweave [1]=src [2]=hiveweave-py
        env_file = Path(__file__).resolve().parents[2] / ".env"
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    except Exception:  # noqa: BLE001
        pass


_bootstrap_dotenv()
