"""One-shot migration: strip platform-reserved tags from existing tasks.

TEST19 教训: agent 曾给普通任务打 tags=["verify", ...] 导致 14+ 处 VERIFY
特殊逻辑误伤。修复后 create_task 会在入口剥离保留 tag, 但运行中的库
已有存量污染。本脚本扫全部 per-project DB, 把 title 不以 ``VERIFY:``
开头的任务 tags 里的保留 tag（verify/mandatory/post-merge）剥掉。

用法（apps/hiveweave-py 下）:
    uv run python scripts/strip_reserved_tags.py [--dry-run]

--dry-run 只报告不修改。已归档/已关闭任务同样处理（保持库数据一致）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiosqlite

from hiveweave.config import settings as app_settings
from hiveweave.services.tasks.verify import is_verify_title

RESERVED = frozenset({"verify", "mandatory", "post-merge"})


def _strip(tags_raw: object) -> tuple[object, list[str]]:
    """Return (new_tags, stripped). tags_raw is None | str(JSON) | list."""
    if tags_raw is None:
        return None, []
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]
    else:
        try:
            tags = json.loads(tags_raw)
        except (ValueError, TypeError):
            return tags_raw, []
        if not isinstance(tags, list):
            return tags_raw, []
    stripped = [t for t in tags if str(t).strip().lower() in RESERVED]
    if not stripped:
        return tags_raw, []
    clean = [t for t in tags if str(t).strip().lower() not in RESERVED]
    # 与 crud.create_task 保持一致：空 tags 存 NULL（不写 "[]"）
    new_tags = json.dumps(clean, ensure_ascii=False) if clean else None
    return new_tags, stripped


async def _migrate_project(ws_path: str, dry_run: bool) -> dict:
    db_path = Path(ws_path).resolve() / ".hiveweave" / "data.db"
    if not db_path.exists():
        return {"db": str(db_path), "tasks": 0, "skipped": 1}
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA busy_timeout=5000")
        cur = await conn.execute(
            "SELECT id, title, tags FROM tasks "
            "WHERE tags IS NOT NULL AND tags != ''"
        )
        rows = await cur.fetchall()
        await cur.close()

        affected = 0
        for row in rows or []:
            title = row["title"] or ""
            # H1 收口：与运行时判定一致（覆盖 【】/[]/全角冒号形态）。
            if is_verify_title(title):
                continue  # 系统 VERIFY 任务保留 tag 是合法的
            new_tags, stripped = _strip(row["tags"])
            if not stripped:
                continue
            affected += 1
            if not dry_run:
                await conn.execute(
                    "UPDATE tasks SET tags = ? WHERE id = ?",
                    [new_tags, row["id"]],
                )
        if not dry_run and affected:
            await conn.commit()
        return {"db": str(db_path), "tasks": len(rows or []), "affected": affected}
    finally:
        await conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    meta_path = app_settings.get_meta_db_path()
    meta = await aiosqlite.connect(meta_path)
    meta.row_factory = aiosqlite.Row
    try:
        cur = await meta.execute(
            "SELECT workspace_path FROM projects WHERE workspace_path IS NOT NULL"
        )
        projects = await cur.fetchall()
        await cur.close()
    finally:
        await meta.close()

    total = 0
    for p in projects or []:
        res = await _migrate_project(p["workspace_path"], args.dry_run)
        total += res.get("affected", 0)
        tag = "[DRY]" if args.dry_run else "    "
        if res.get("skipped"):
            print(f"{tag} {res['db']}: skipped (no DB)")
        else:
            print(
                f"{tag} {res['db']}: tasks={res['tasks']} "
                f"affected={res.get('affected', 0)}"
            )
    print(f"{'would strip' if args.dry_run else 'stripped'} {total} rows")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
