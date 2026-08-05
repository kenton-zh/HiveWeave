"""Regression tests: both Meta DB and per-project DB must use DELETE journal mode.

TEST18 incident (2026-08-05): commit 535fd20 changed per-project DB from
DELETE to WAL ("Bug F"), overriding contract 11's intent to avoid Windows
WAL orphaning / generation fork. Under multi-connection concurrency the old
WAL became an orphan, writes bypassed it and landed directly on the main DB,
truncating it to 555 pages while B-tree pages 556-1595 only existed in the
orphaned 4MB WAL → "invalid page number" corruption.

These tests guard against anyone re-introducing WAL to "fix a symptom".
A DB in WAL mode would also create -wal / -shm sibling files; DELETE mode
must not. See CLAUDE.md § db 契约 11.
"""

from __future__ import annotations

import os

import pytest

from hiveweave.db import meta as meta_db
from hiveweave.db import project as project_db


@pytest.mark.asyncio
async def test_meta_db_uses_delete_journal_mode(tmp_path, monkeypatch):
    """Meta DB must open in DELETE journal mode (not WAL)."""
    meta_path = str(tmp_path / "meta" / "hiveweave.db")
    # Point the Meta DB at a temp path so we never touch the real one.
    monkeypatch.setattr(meta_db.app_settings, "meta_db_path", meta_path)

    # Ensure a fresh singleton (previous tests may have opened the real DB).
    await meta_db.close_meta_db()
    await meta_db.init_meta_db()

    conn = await meta_db.get_meta_db()
    row = await conn.execute("PRAGMA journal_mode")
    val = (await row.fetchone())[0]
    await row.close()
    assert val == "delete", f"Meta DB journal_mode should be 'delete', got {val!r}"

    # DELETE mode must NOT spawn -wal/-shm sibling files.
    assert not os.path.exists(meta_path + "-wal")
    assert not os.path.exists(meta_path + "-shm")


@pytest.mark.asyncio
async def test_project_db_uses_delete_journal_mode(tmp_path):
    """Per-project DB must open in DELETE journal mode (not WAL)."""
    ws = str(tmp_path / "ws")
    conn = await project_db.ensure_project_db(ws)

    row = await conn.execute("PRAGMA journal_mode")
    val = (await row.fetchone())[0]
    await row.close()
    assert val == "delete", f"Project DB journal_mode should be 'delete', got {val!r}"

    db_path = os.path.join(ws, ".hiveweave", "data.db")
    assert os.path.exists(db_path)
    assert not os.path.exists(db_path + "-wal")
    assert not os.path.exists(db_path + "-shm")