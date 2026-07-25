"""Collect dogfood metrics for TEST_YLGY 2026-07-24 report."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PDB = Path(r"D:\PC_AI\Project\TEST_YLGY\.hiveweave\data.db")
OUT = Path(r"D:\PC_AI\Project\HiveWeave\tasks\_dogfood_metrics.json")


def ts_ms(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms)


def main():
    c = sqlite3.connect(str(PDB))
    c.row_factory = sqlite3.Row

    agents = [dict(r) for r in c.execute("SELECT * FROM agents ORDER BY short_id")]
    agent_by_id = {a["id"]: a for a in agents}

    tasks = [dict(r) for r in c.execute("SELECT * FROM tasks ORDER BY created_at")]
    status_counts = Counter(t["status"] for t in tasks)
    with_contract = sum(
        1
        for t in tasks
        if (t.get("contract_json") or "").strip()
    )

    # timeline
    first_task = min((t["created_at"] for t in tasks), default=None)
    last_task = max((t.get("updated_at") or t["created_at"] for t in tasks), default=None)

    # run_steps tool counts
    tool_counts = Counter()
    tool_errors = Counter()
    for r in c.execute(
        "SELECT tool_name, status, error FROM run_steps WHERE tool_name IS NOT NULL"
    ):
        tool_counts[r["tool_name"]] += 1
        if r["error"] or (r["status"] and r["status"] not in ("ok", "success", "done", None)):
            if r["error"]:
                tool_errors[r["tool_name"]] += 1

    # dismiss / hire
    dismiss_n = tool_counts.get("dismiss_agent", 0)
    hire_n = tool_counts.get("hire_agent", 0)

    # inbox patterns
    inbox = [dict(r) for r in c.execute("SELECT * FROM inbox ORDER BY created_at")]
    inbox_prefixes = Counter()
    for m in inbox:
        msg = (m.get("message") or "").strip()
        if msg.startswith("["):
            end = msg.find("]")
            if end > 0:
                inbox_prefixes[msg[1:end]] += 1

    # waits
    waits = [dict(r) for r in c.execute("SELECT * FROM agent_waits")]
    uncleared = [w for w in waits if not w.get("cleared_at")]
    wait_phases = Counter(w.get("phase") for w in waits)

    # chat volume
    chat_n = c.execute("SELECT count(*) FROM chat_messages").fetchone()[0]
    streaming_z = c.execute(
        "SELECT count(*) FROM chat_messages WHERE is_streaming=1"
    ).fetchone()[0]

    # work logs
    wl_types = Counter(
        r[0] for r in c.execute("SELECT type FROM work_logs")
    )

    # personnel / dismiss log
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    dismiss_log = []
    if "org_dismiss_log" in tables:
        dismiss_log = [dict(r) for r in c.execute("SELECT * FROM org_dismiss_log")]

    # task titles summary
    task_summary = []
    for t in tasks:
        asg = agent_by_id.get(t.get("assignee_id") or "", {})
        title = (t.get("title") or "").split("\n")[0][:80]
        task_summary.append(
            {
                "id": (t["id"] or "")[:8],
                "status": t["status"],
                "progress": t.get("progress"),
                "assignee": asg.get("short_id") or "?",
                "assignee_name": asg.get("name"),
                "title": title,
                "created": ts_ms(t.get("created_at")),
                "updated": ts_ms(t.get("updated_at")),
                "has_contract": bool((t.get("contract_json") or "").strip()),
            }
        )

    # agents summary
    agent_summary = []
    for a in agents:
        wp = a.get("workspace_path") or ""
        agent_summary.append(
            {
                "short_id": a.get("short_id"),
                "name": a.get("name"),
                "role": a.get("role"),
                "status": a.get("status"),
                "permission_type": a.get("permission_type"),
                "wt_ok": bool(wp and Path(wp).exists()),
                "worktree_error": (a.get("worktree_error") or "")[:120] or None,
                "created": ts_ms(a.get("created_at")),
            }
        )

    # run_steps duration / count
    run_n = 0
    if "agent_runs" in tables:
        runs = [dict(r) for r in c.execute("SELECT * FROM agent_runs")]
        run_n = len(runs)
        run_status = Counter(r.get("status") for r in runs)
    else:
        runs = []
        run_status = {}

    step_n = c.execute("SELECT count(*) FROM run_steps").fetchone()[0]

    # rework / reject evidence
    review_approve = 0
    review_rework = 0
    # can't easily parse args; use inbox
    review_approve = inbox_prefixes.get("TASK APPROVED", 0)
    # submitted etc.

    metrics = {
        "project": "TEST_YLGY",
        "workspace": str(PDB.parent.parent),
        "window": {
            "first_task": ts_ms(first_task),
            "last_task_update": ts_ms(last_task),
            "duration_min": round((last_task - first_task) / 60000, 1)
            if first_task and last_task
            else None,
        },
        "agents": agent_summary,
        "agent_counts": {
            "total": len(agents),
            "active": sum(1 for a in agents if a.get("status") != "archived"),
            "archived": sum(1 for a in agents if a.get("status") == "archived"),
        },
        "tasks": {
            "status_counts": dict(status_counts),
            "total": len(tasks),
            "with_contract_json": with_contract,
            "summary": task_summary,
        },
        "tools": {
            "top": tool_counts.most_common(40),
            "dismiss_agent": dismiss_n,
            "hire_agent": hire_n,
            "waive_attestation": tool_counts.get("waive_attestation", 0),
            "get_platform_state": tool_counts.get("get_platform_state", 0),
            "commit_turn": tool_counts.get("commit_turn", 0),
            "review_task": tool_counts.get("review_task", 0),
            "git_worktree_merge": tool_counts.get("git_worktree_merge", 0),
            "errors_by_tool": tool_errors.most_common(20),
        },
        "inbox": {
            "total": len(inbox),
            "prefixes": inbox_prefixes.most_common(30),
        },
        "waits": {
            "total": len(waits),
            "uncleared": len(uncleared),
            "phases": dict(wait_phases),
        },
        "chat_messages": chat_n,
        "streaming_zombies": streaming_z,
        "work_log_types": dict(wl_types),
        "dismiss_log": dismiss_log,
        "runs": {"count": run_n, "status": dict(run_status)},
        "run_steps": step_n,
        "review_approved_inbox": review_approve,
    }

    OUT.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: metrics[k] for k in ("window", "agent_counts", "tasks", "tools", "inbox", "waits", "runs", "run_steps", "chat_messages")}, ensure_ascii=False, indent=2)[:8000])
    print("\nWROTE", OUT)


if __name__ == "__main__":
    main()
