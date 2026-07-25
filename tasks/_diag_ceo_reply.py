"""Read latest TEST12 CEO visible replies."""
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

    print("=== CEO recent messages (newest first) ===")
    for m in c.execute(
        "SELECT role, is_background, content, created_at FROM chat_messages "
        "WHERE agent_id=? ORDER BY created_at DESC LIMIT 12",
        (CEO,),
    ):
        content = m["content"] or ""
        if m["role"] == "assistant" and content.strip() in (
            "(turn committed)",
            "好的，开始处理。",
        ):
            continue
        if m["role"] == "user" and m["is_background"] and (
            content.startswith("## Messages") or content.startswith("## Goals")
        ):
            continue
        print(
            "---",
            m["created_at"],
            m["role"],
            "bg=",
            m["is_background"],
            "len=",
            len(content),
        )
        print(content[:3500])
        print()

    print("=== RUNTIME ===")
    try:
        r = httpx.get(f"{API}/api/debug/agents/{CEO}/runtime", timeout=10)
        print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:1500])
    except Exception as e:
        print("runtime err", e)

    print("=== A004 worktree_error ===")
    for a in c.execute(
        "SELECT short_id, name, worktree_error FROM agents "
        "WHERE COALESCE(status,'')!='archived' ORDER BY short_id"
    ):
        print(dict(a))

    c.close()


if __name__ == "__main__":
    main()
