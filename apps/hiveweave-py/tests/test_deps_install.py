"""批次 39-处置3 单元测试：#2 契约 deps 代装（validate + install）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hiveweave.services.venv_setup import (
    install_project_deps_async,
    validate_deps,
)


def test_validate_deps_ok_specs():
    ok, bad = validate_deps(["fastapi>=0.110", "uvicorn>=0.29", "pydantic"])
    assert ok == ["fastapi>=0.110", "uvicorn>=0.29", "pydantic"]
    assert bad == []


def test_validate_deps_rejects_flags_and_urls():
    ok, bad = validate_deps(
        ["-e", "somepkg", "--index-url", "https://evil.example/x.whl", "good-pkg"]
    )
    # flag 全拒；裸包名合法（它自己无害，flag 才是危险载体）
    assert "somepkg" in ok and "good-pkg" in ok
    assert set(bad) == {"-e", "--index-url", "https://evil.example/x.whl"}


def test_validate_deps_extras_and_version_specs():
    ok, bad = validate_deps(["uvicorn[standard]>=0.29,<1.0", "requests~=2.31"])
    assert ok == ["uvicorn[standard]>=0.29,<1.0", "requests~=2.31"]
    assert bad == []


@pytest.mark.asyncio
async def test_install_rejects_unsafe_before_running(tmp_path):
    """不安全 dep 在执行前即拒——不产生子进程。"""
    res = await install_project_deps_async(
        str(tmp_path), ["--index-url", "https://evil"], timeout_s=10
    )
    assert res["ok"] is False
    assert "unsafe" in res["reason"]


@pytest.mark.asyncio
async def test_install_missing_venv_fails_loud(tmp_path):
    res = await install_project_deps_async(
        str(tmp_path), ["fastapi"], timeout_s=10
    )
    assert res["ok"] is False
    assert "不存在" in res["reason"]


@pytest.mark.asyncio
async def test_install_runs_probe_deps_in_real_venv(tmp_path):
    """真实端到端：建 venv → uv pip install 纯标准库无害包 → 通过。"""
    # 用宿主 venv 的 python 建一个真 venv（同机 uv 已由 venv_setup 使用）
    venv_dir = tmp_path / ".venv"
    import subprocess
    import sys

    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    # 探针包选极小的 six（纯 python，无编译）
    res = await install_project_deps_async(
        str(tmp_path), ["six"], timeout_s=120
    )
    assert res["ok"], f"{res}\n{res.get('output', '')[-400:]}"
    # 验证 six 真的装进了 venv
    site = venv_dir / "Lib" / "site-packages"
    if not site.exists():  # POSIX
        site = venv_dir / "lib"
    assert any("six" in p.name.lower() for p in site.rglob("six*")) or any(
        "six" in str(p).lower() for p in site.iterdir()
    )


# ── #3 cache 分母 scope 语义（DeepSeek input-inclusive）──────────────────


def test_cache_hit_percent_input_inclusive():
    from hiveweave.llm.util import cache_hit_percent

    # DeepSeek: prompt=1000 = hit 800 + miss 200 → 命中率 = 800/1000 = 80%
    assert cache_hit_percent(1000, 800, 200, input_inclusive=True) == 80
    # disjoint 语义（Anthropic）：分母 = input+read+write
    assert cache_hit_percent(1000, 800, 200) == round(800 / 2000 * 100)


def test_deepseek_rows_get_inclusive_basis():
    from hiveweave.services.token_meter import _with_cache_scope

    row = {
        "providers": "deepseek",
        "input_tokens": 1000,
        "cache_read_tokens": 800,
        "cache_creation_tokens": 200,
    }
    out = _with_cache_scope(row)
    # 800/1000 = 80%（inclusive 分母 = input 本身，不再双计）
    assert out["cache_hit_percent"] == 80
    assert "includes cache" in out["cache_hit_basis"]


def test_mixed_providers_conservative_basis():
    from hiveweave.services.token_meter import _with_cache_scope

    row = {
        "providers": "deepseek,anthropic",
        "input_tokens": 1000,
        "cache_read_tokens": 800,
        "cache_creation_tokens": 200,
    }
    out = _with_cache_scope(row)
    # 混合场景保守口径（disjoint 公式，命中率只低不高）
    assert out["cache_hit_basis"] != "cache_read/input (provider prompt includes cache hit+miss)"
