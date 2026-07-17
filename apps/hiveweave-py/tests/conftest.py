"""Pytest 全局夹具。

每个测试结束后关闭该测试期间打开的 aiosqlite 连接（meta DB 单例 +
per-project 连接缓存）。aiosqlite 的连接 worker 线程是**非守护线程**，
不关闭时线程会一直阻塞在队列读取上，导致 pytest 全量单进程跑完汇总后
无法退出（exit hang）。生产进程里这些连接本就该常驻，无需改动 db 层。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _close_db_connections_after_test():
    yield
    try:
        from hiveweave.db.project import close_all

        await close_all()
    except Exception:
        pass
    try:
        from hiveweave.db.meta import close_meta_db

        await close_meta_db()
    except Exception:
        pass
