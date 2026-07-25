"""One-shot TEST12 CEO diagnosis."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx

CEO = "943801d6-c288-44a9-8498-36ed0b15124f"
API = "http://127.0.0.1:4000"
PDB = Path(r"D:\PC_AI\Project\TEST12") / ".hiveweave" / "data.db"


def main() -> None:
    r = httpx.get(f"{API}/api/debug/agents/{CEO}/runtime", timeout=15)
    print("=== RUNTIME ===")
    print(r.status_code)
    print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:4000])

    c = sqlite3.connect(str(PDB))
    c.row_factory = sqlite3.Row

    print("\n=== AGENTS ===")
    for a in c.execute(
        "SELECT short_id, name, role, status FROM agents "
        "WHERE COALESCE(status,'') != 'archived' ORDER BY short_id"
    ):
        print(dict(a))

    print("\n=== RECENT CHAT ===")
    for m in c.execute(
        "SELECT role, is_background, is_streaming, length(content) n, "
        "substr(content,1,240) c, created_at FROM chat_messages "
        "WHERE agent_id=? ORDER BY created_at DESC LIMIT 10",
        (CEO,),
    ):
        print(
            m["created_at"],
            m["role"],
            f"bg={m['is_background']}",
            f"n={m['n']}",
            "|",
            (m["c"] or "").replace("\n", " ")[:180],
        )

    print("\n=== INBOX recent ===")
    for i in c.execute(
        "SELECT from_agent_id, to_agent_id, read, substr(message,1,160) m, created_at "
        "FROM inbox ORDER BY created_at DESC LIMIT 12"
    ):
        print(
            i["created_at"],
            (i["from_agent_id"] or "")[:8],
            "->",
            (i["to_agent_id"] or "")[:8],
            f"read={i['read']}",
            "|",
            (i["m"] or "")[:100],
        )

    print("\n=== TASKS ===")
    try:
        for t in c.execute(
            "SELECT substr(id,1,8) id, status, title, tags FROM tasks "
            "ORDER BY created_at DESC LIMIT 15"
        ):
            print(dict(t))
    except Exception as e:
        print("tasks err", e)

    c.close()


if __name__ == "__main__":
    main()
