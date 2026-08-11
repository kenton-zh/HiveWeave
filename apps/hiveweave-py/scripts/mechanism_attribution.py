"""机制效果归因遥测查询（只读分析脚本，F5 audit）。

数据已存在（audit 验证）：
- agent_activations.trigger_type 记录唤醒机制（task / trigger / turn_exit_gate /
  open_task_reminder / interrupted_resume / chat）
- task_events.actor_id + event_type 记录任务事件与执行 agent

归因口径：同一 agent 在唤醒后 N 分钟窗口内是否推进了任务状态
（task_events.to_status 非空 = 状态变更事件），得到各触发机制的实效。

机制消息计数：
- open_task_reminder 唤醒（trigger_detail 以 [TASK ADVANCE] 开头，
  见 agents/agent.py open_task_reminder 来源）
- [TASK STALL]（game_time.py task_stall_nudge 消息前缀）
- [WAIT_TIMEOUT]（wait_contract 超时消息前缀）

只读：SQLite mode=ro URI，绝不修改数据。

用法（apps/hiveweave-py 下）:
    uv run python scripts/mechanism_attribution.py [DB 路径]

DB 路径缺省：env HIVEWEAVE_PROJECT_DB；再缺省 <cwd>/.hiveweave/data.db
（契约 11: per-project DB 位于 <workspace>/.hiveweave/data.db）。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

WINDOW_MIN_5 = 5 * 60 * 1000
WINDOW_MIN_30 = 30 * 60 * 1000


def _readonly_uri(db_path: Path) -> str:
    """SQLite mode=ro URI（同 hiveweave.db.project._sqlite_readonly_uri 口径）。"""
    posix = db_path.resolve().as_posix()
    encoded = quote(posix, safe="/:")
    if encoded.startswith("/"):
        return f"file://{encoded}?mode=ro"   # POSIX
    return f"file:///{encoded}?mode=ro"      # Windows


def _resolve_db_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env = os.environ.get("HIVEWEAVE_PROJECT_DB")
    if env:
        return Path(env)
    return Path.cwd() / ".hiveweave" / "data.db"


def count_window(
    lst: list[tuple[int, bool]], t: int, win: int
) -> tuple[int, int]:
    """(窗口内事件数, 窗口内状态变更事件数)。lst 按 created_at 升序。"""
    end = t + win
    ev = sc = 0
    for ts, is_status in lst:
        if ts < t:
            continue
        if ts > end:
            break
        ev += 1
        if is_status:
            sc += 1
    return ev, sc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="机制效果归因遥测（只读 SQLite 分析）"
    )
    parser.add_argument(
        "db", nargs="?",
        help="per-project DB 路径（缺省: $HIVEWEAVE_PROJECT_DB 或 <cwd>/.hiveweave/data.db）",
    )
    args = parser.parse_args()
    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        print(f"错误: DB 不存在: {db_path}")
        return 1

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    con = sqlite3.connect(_readonly_uri(db_path), uri=True)
    con.row_factory = sqlite3.Row
    try:
        activations = con.execute(
            "SELECT agent_id, trigger_type, created_at FROM agent_activations "
            "WHERE trigger_type IS NOT NULL AND trigger_type != ''"
        ).fetchall()
        events = con.execute(
            "SELECT actor_id, to_status, created_at FROM task_events "
            "WHERE actor_id IS NOT NULL"
        ).fetchall()

        by_agent: dict[str, list[tuple[int, bool]]] = defaultdict(list)
        for e in events:
            by_agent[e["actor_id"]].append(
                (e["created_at"], e["to_status"] is not None)
            )
        for lst in by_agent.values():
            lst.sort()

        # 按触发器类型聚合: n 唤醒数 / sc5,sc30 有状态推进的唤醒数 /
        # ev5,ev30 窗口内归属事件总数
        stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"n": 0, "sc5": 0, "sc30": 0, "ev5": 0, "ev30": 0}
        )
        for a in activations:
            s = stats[a["trigger_type"]]
            s["n"] += 1
            agent_events = by_agent.get(a["agent_id"], [])
            ev5, sc5 = count_window(agent_events, a["created_at"], WINDOW_MIN_5)
            ev30, sc30 = count_window(agent_events, a["created_at"], WINDOW_MIN_30)
            s["ev5"] += ev5
            s["ev30"] += ev30
            if sc5:
                s["sc5"] += 1
            if sc30:
                s["sc30"] += 1

        stall_msgs = con.execute(
            "SELECT COUNT(*) FROM inbox WHERE message LIKE '[TASK STALL]%'"
        ).fetchone()[0]
        wait_msgs = con.execute(
            "SELECT COUNT(*) FROM inbox WHERE message LIKE '[WAIT_TIMEOUT]%'"
        ).fetchone()[0]
        adv_wake = con.execute(
            "SELECT COUNT(*) FROM agent_activations WHERE trigger_type = 'open_task_reminder'"
        ).fetchone()[0]
    finally:
        con.close()

    total_wake = sum(s["n"] for s in stats.values())
    print(f"==== 机制效果归因遥测（只读） ====")
    print(f"DB: {db_path.resolve()}")
    print(f"唤醒总数: {total_wake}  任务事件总数(有 actor): {len(events)}")
    print()

    header = ("触发器类型", "唤醒数", "5min推进唤醒", "30min推进唤醒",
              "5min归属事件", "30min归属事件", "平均事件/唤醒")
    widths = [12, 8, 10, 10, 10, 10, 12]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths))
    print(line)
    print("-" * len(line))
    for trig, s in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
        n = s["n"]
        avg = s["ev30"] / n if n else 0.0
        row = (
            trig, str(n),
            f"{s['sc5']} ({s['sc5'] * 100 // n}%)",
            f"{s['sc30']} ({s['sc30'] * 100 // n}%)",
            str(s["ev5"]), str(s["ev30"]), f"{avg:.1f}",
        )
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))

    print()
    print("── 机制消息计数 ──")
    print(f"open_task_reminder 唤醒（[TASK ADVANCE]）: {adv_wake}")
    print(f"[TASK STALL] inbox 消息: {stall_msgs}")
    print(f"[WAIT_TIMEOUT] inbox 消息: {wait_msgs}")
    print()
    print("口径: 唤醒后 N 分钟内同一 agent（task_events.actor_id = 唤醒 agent）")
    print("产生 to_status 非空的任务状态变更事件 = 该唤醒推进了任务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
