#!/usr/bin/env python3
"""E13 (复盘 P1) 测试洁净室 —— 验收运行前的系统化残留清理。

复盘 S1/S4：验收前残留状态（僵尸 streaming、孤儿 running run、残留 worktree
分支）串扰后续用例，导致「一个挂载缺陷拖垮 58 用例」类的假红色。本脚本在
验收运行前清场，只清平台自愈范畴的残留，**不删任何用户/agent 业务数据**：

1. 清僵尸 streaming 消息（is_streaming=1 → 归位，同主程序启动逻辑）；
2. 清算残留 running agent_runs（→ interrupted，同 E16 startup sweep）；
3. 打印各项目 running agent / 残留概览，供报告聚类。

用法（apps/hiveweave-py 目录下）:
    uv run python ../../scripts/acceptance-cleanroom.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


async def _main() -> int:
    # 保证能 import hiveweave（脚本放 repo scripts/，包在 apps/hiveweave-py）
    here = Path(__file__).resolve()
    pkg = here.parents[1] / "apps" / "hiveweave-py"
    sys.path.insert(0, str(pkg / "src"))
    sys.path.insert(0, str(pkg))

    try:
        from hiveweave.db import meta as meta_db
        from hiveweave.services.chat_message import ChatMessageService
        from hiveweave.services.run_ledger import sweep_stale_agent_runs
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: cannot import hiveweave (cwd = {os.getcwd()}): {e}", file=sys.stderr)
        print("Run from <repo>/apps/hiveweave-py with: uv run python ../../scripts/acceptance-cleanroom.py", file=sys.stderr)
        return 2

    projects = await meta_db.query("SELECT id, workspace_path FROM projects WHERE 1=1")
    if not projects:
        print("cleanroom: no projects in meta DB — nothing to clean.")
        return 0

    zombie = 0
    swept = 0
    per_project: list[str] = []
    for p in projects:
        ws = p["workspace_path"]
        pid = p["id"]
        n_z = n_r = 0
        try:
            from hiveweave.db.project import ensure_project_db

            conn = await ensure_project_db(ws)
            try:
                svc = ChatMessageService(pid)
                n_z = await svc.clear_stuck_streaming()
            except Exception:
                n_z = 0
            n_r = await sweep_stale_agent_runs(ws)
            try:
                cur = await conn.execute(
                    "SELECT COUNT(*) AS c FROM agents WHERE status = 'running'"
                )
                row = await cur.fetchone()
                await cur.close()
                running_agents = int(row["c"] or 0) if row else 0
            except Exception:
                running_agents = -1
        except Exception as e:  # noqa: BLE001
            per_project.append(f"  - {pid[:8]} ({ws}): ERROR {e}")
            continue
        zombie += n_z
        swept += n_r
        per_project.append(
            f"  - {pid[:8]} ({ws}): zombie_streaming={n_z} stale_runs={n_r}"
            + (f" running_agents={running_agents}" if running_agents >= 0 else "")
        )

    print(f"cleanroom: projects={len(projects)} zombie_streaming_cleared={zombie} "
          f"stale_runs_swept={swept}")
    print("\n".join(per_project))

    print(
        "\n聚类放行提示：验收结论请按根因聚类呈现（单一根因不得一票否决全部"
        "已绿模块），放行附「已知问题清单」而非二元全有全无。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))