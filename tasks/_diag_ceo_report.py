"""Inspect TEST12 CEO report + org/tasks."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx

CEO = "943801d6-c288-44a9-8498-36ed0b15124f"
API = "http://127.0.0.1:4000"
PDB = Path(r"D:\PC_AI\Project\TEST12") / ".hiveweave" / "data.db"


def main() -> None:
    c = sqlite3.connect(str(PDB))
    c.row_factory = sqlite3.Row

    print("=== AGENTS ===")
    for a in c.execute(
        "SELECT short_id, name, role, status, permission_type, "
        "workspace_path, worktree_error FROM agents "
        "WHERE COALESCE(status,'') != 'archived' ORDER BY short_id"
    ):
        wp = a["workspace_path"]
        ok = bool(wp and Path(wp).exists())
        print(
            a["short_id"],
            a["name"],
            a["role"],
            a["permission_type"],
            "wt_ok=",
            ok,
            "err=",
            a["worktree_error"],
        )

    print("\n=== TASKS ===")
    for t in c.execute(
        "SELECT id, status, title, assignee_id, creator_id, parent_task_id, "
        "tags, progress, is_archived FROM tasks ORDER BY created_at"
    ):
        d = dict(t)
        d["id"] = (d["id"] or "")[:8]
        for k in ("assignee_id", "creator_id", "parent_task_id"):
            if d.get(k):
                d[k] = str(d[k])[:8]
        print(d)

    print("\n=== CEO VISIBLE MESSAGES (newest first) ===")
    for m in c.execute(
        "SELECT role, is_background, content, created_at FROM chat_messages "
        "WHERE agent_id=? ORDER BY created_at DESC LIMIT 40",
        (CEO,),
    ):
        content = m["content"] or ""
        # Prefer human-visible: non-bg assistant, or message_user-ish long text
        if m["role"] == "assistant" and m["is_background"] and content.strip() in (
            "(turn committed)",
            "好的，开始处理。",
        ):
            continue
        if m["role"] == "user" and m["is_background"]:
            # skip inbox digests unless short preview needed
            if content.startswith("## Messages") or content.startswith("## Goals"):
                continue
        print("---", m["created_at"], m["role"], "bg=", m["is_background"], "len=", len(content))
        print(content[:2500])
        print()

    print("\n=== RUNTIME ===")
    r = httpx.get(f"{API}/api/debug/agents/{CEO}/runtime", timeout=15)
    print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:3000])

    print("\n=== ATTESTATIONS ===")
    tables = [x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    att_tables = [t for t in tables if "attest" in t.lower() or "waiv" in t.lower()]
    print("tables", att_tables)
    for tn in att_tables:
        try:
            rows = c.execute(f"SELECT * FROM {tn} ORDER BY rowid DESC LIMIT 10").fetchall()
            print(tn, "count_sample", len(rows))
            for row in rows:
                print(dict(row))
        except Exception as e:
            print(tn, e)

    print("\n=== INBOX last 15 ===")
    for i in c.execute(
        "SELECT from_agent_id, to_agent_id, read, substr(message,1,200) m, created_at "
        "FROM inbox ORDER BY created_at DESC LIMIT 15"
    ):
        print(
            i["created_at"],
            (i["from_agent_id"] or "")[:8],
            "->",
            (i["to_agent_id"] or "")[:8],
            "r=",
            i["read"],
            "|",
            (i["m"] or "").replace("\n", " ")[:120],
        )

    # All agent runtimes briefly
    print("\n=== ALL AGENT RUNTIMES ===")
    for a in c.execute(
        "SELECT id, short_id, name FROM agents WHERE COALESCE(status,'') != 'archived'"
    ):
        rr = httpx.get(f"{API}/api/debug/agents/{a['id']}/runtime", timeout=10)
        if rr.status_code != 200:
            print(a["short_id"], a["name"], rr.status_code)
            continue
        j = rr.json()
        print(
            a["short_id"],
            a["name"],
            "exec=",
            j.get("execution"),
            "disp=",
            j.get("disposition"),
            "waits=",
            len(j.get("waits") or []),
            "obl=",
            len(j.get("obligations") or []),
        )

    c.close()


if __name__ == "__main__":
    main()
