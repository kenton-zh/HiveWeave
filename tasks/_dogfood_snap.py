"""One-shot dogfood snapshot for TEST_YLGY."""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

WS = Path(r"D:\PC_AI\Project\TEST_YLGY")
PDB = WS / ".hiveweave" / "data.db"
PID = "6cb59d6b-6be4-42e4-a9ba-e79e34b29451"


def main() -> None:
    print("DB exists:", PDB.exists())
    conn = sqlite3.connect(str(PDB))
    conn.row_factory = sqlite3.Row

    print("\n=== AGENTS ===")
    agents = conn.execute(
        "SELECT short_id, name, role, status, permission_type, "
        "workspace_path, worktree_error FROM agents ORDER BY short_id"
    ).fetchall()
    active = [a for a in agents if a["status"] != "archived"]
    archived = [a for a in agents if a["status"] == "archived"]
    for a in agents:
        wp = a["workspace_path"] or ""
        ok = bool(wp and Path(wp).exists())
        print(
            f"{a['short_id']} {a['name']!s:8} {a['status']:10} "
            f"{a['role'][:28]!s:28} wt_ok={ok} err={a['worktree_error']}"
        )
    print(f"active={len(active)} archived={len(archived)}")

    print("\n=== TASK STATUS COUNTS ===")
    for r in conn.execute("SELECT status, count(*) c FROM tasks GROUP BY status"):
        print(dict(r))

    print("\n=== TASKS (latest 25) ===")
    cols = {c[1] for c in conn.execute("PRAGMA table_info(tasks)")}
    has_cj = "contract_json" in cols
    has_ss = "slice_status" in cols
    extra = ""
    if has_cj:
        extra += ", substr(coalesce(contract_json,''),1,50) cj"
    if has_ss:
        extra += ", slice_status"
    sql = (
        "SELECT substr(id,1,8) id, status, progress, "
        "substr(coalesce(title,''),1,48) title, "
        "substr(coalesce(assignee_id,''),1,8) asg"
        f"{extra} FROM tasks ORDER BY created_at DESC LIMIT 25"
    )
    for t in conn.execute(sql):
        print(dict(t))

    print("\n=== CONTRACT / SLICE USAGE ===")
    if has_cj:
        n = conn.execute(
            "SELECT count(*) FROM tasks WHERE contract_json IS NOT NULL "
            "AND trim(contract_json) != ''"
        ).fetchone()[0]
        print("tasks_with_contract_json:", n)
    else:
        print("NO contract_json column")

    print("\n=== DISMISS LOG / GUARDRAILS ===")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for name in ("org_dismiss_log", "personnel_records", "staffing_demands"):
        if name in tables:
            print(f"-- {name} --")
            for r in conn.execute(f"SELECT * FROM {name} ORDER BY rowid DESC LIMIT 15"):
                print(dict(r))
        else:
            print(f"{name}: missing")

    print("\n=== STREAMING / WAITS ===")
    print("streaming=", conn.execute("SELECT count(*) FROM chat_messages WHERE is_streaming=1").fetchone()[0])
    if "agent_waits" in tables:
        for w in conn.execute("SELECT * FROM agent_waits ORDER BY rowid DESC LIMIT 10"):
            print(dict(w))

    print("\n=== RECENT INBOX ===")
    for i in conn.execute(
        "SELECT datetime(created_at/1000,'unixepoch','localtime') ts, "
        "substr(from_agent_id,1,8) frm, substr(to_agent_id,1,8) too, read, "
        "substr(replace(coalesce(message,''), char(10), ' '),1,110) m "
        "FROM inbox ORDER BY created_at DESC LIMIT 18"
    ):
        print(dict(i))

    print("\n=== USER QUESTIONS PENDING ===")
    if "questions" in tables or "pending_questions" in tables:
        qtable = "questions" if "questions" in tables else "pending_questions"
        for r in conn.execute(f"SELECT * FROM {qtable} ORDER BY rowid DESC LIMIT 10"):
            print(dict(r))
    else:
        qish = [t for t in tables if "quest" in t.lower()]
        print("question-ish tables:", qish)

    print("\n=== TOOL USAGE (run_steps) ===")
    if "run_steps" in tables:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(run_steps)")]
        print("cols:", cols)
        name_col = "tool_name" if "tool_name" in cols else ("name" if "name" in cols else None)
        if name_col:
            for r in conn.execute(
                f"SELECT {name_col} n, count(*) c FROM run_steps "
                f"WHERE {name_col} IS NOT NULL GROUP BY {name_col} ORDER BY c DESC LIMIT 30"
            ):
                print(dict(r))
        # dismiss / hire counts
        for tool in ("dismiss_agent", "hire_agent", "get_platform_state", "create_task", "commit_turn"):
            if name_col:
                c = conn.execute(
                    f"SELECT count(*) FROM run_steps WHERE {name_col}=?", (tool,)
                ).fetchone()[0]
                print(f"count_{tool}={c}")

    print("\n=== DEBUG METRICS API ===")
    try:
        with urllib.request.urlopen(f"http://localhost:4000/api/debug/metrics", timeout=5) as resp:
            print(resp.read().decode()[:2000])
    except Exception as e:
        print("metrics err", e)

    print("\n=== AGENT RUNTIME (active) ===")
    for a in active:
        aid = None
        # need full id
        row = conn.execute("SELECT id, short_id, name FROM agents WHERE short_id=?", (a["short_id"],)).fetchone()
        aid = row["id"]
        try:
            with urllib.request.urlopen(
                f"http://localhost:4000/api/debug/agents/{aid}/runtime", timeout=5
            ) as resp:
                data = json.loads(resp.read().decode())
                print(
                    a["short_id"],
                    a["name"],
                    "exec=", data.get("execution") or data.get("executionState"),
                    "disp=", data.get("disposition"),
                    "obl=", len(data.get("obligations") or data.get("actionableObligations") or []),
                )
                # compact
                keys = list(data.keys())
                print("  keys:", keys)
                if data.get("waits"):
                    print("  waits:", data["waits"])
        except Exception as e:
            print(a["short_id"], "runtime err", e)

    conn.close()


if __name__ == "__main__":
    main()
