"""批次 39-处置4 单元测试：#1 折叠谓词 + 签名 preexisting（回归保护）。"""

from __future__ import annotations

from hiveweave.services.inbox import is_foldable_wait_message
from hiveweave.services.venv_setup import validate_deps


def test_fold_predicate_allowlist():
    assert is_foldable_wait_message("normal") is True
    assert is_foldable_wait_message("chat") is True
    assert is_foldable_wait_message("Chat ") is True  # 大小写/空白容忍
    assert is_foldable_wait_message("notify") is False
    assert is_foldable_wait_message("ask") is False
    assert is_foldable_wait_message("system") is False
    assert is_foldable_wait_message(None) is False
    assert is_foldable_wait_message("") is False


def test_validate_deps_safety():
    ok, bad = validate_deps(["fastapi>=0.110", "uvicorn[standard]>=0.29"])
    assert ok == ["fastapi>=0.110", "uvicorn[standard]>=0.29"]
    assert bad == []
    # flag / URL / shell 元字符全拒
    ok2, bad2 = validate_deps(
        ["-e", "--index-url", "pkg; rm -rf /", "https://x/y.tgz", "good"]
    )
    assert ok2 == ["good"]
    assert len(bad2) == 4
