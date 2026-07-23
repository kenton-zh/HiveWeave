"""Query TEST11 live state for regression driving."""
from __future__ import annotations

import sqlite3
from pathlib import Path

m = sqlite3.connect(r"D:\PC_AI\Project\HiveWeave\apps\hiveweave-py\data\hiveweave.db")
ws = m.execute('SELECT workspace_path FROM projects WHERE name="TEST11"').fetchone()[0]
print("ws=", ws)
m.close()

pdb = str(Path(ws) / ".hiveweave" / "data.db")
c = sqlite3.connect(pdb)
c.row_factory = sqlite3.Row
print("=== agents ===")
for a in c.execute(
    "SELECT id, short_id, name, role, status, permission_type "
    "FROM agents WHERE status!='archived' ORDER BY short_id"
):
    print(
        a["short_id"],
        a["name"],
        a["role"],
        a["status"],
        a["permission_type"],
        a["id"][:8],
    )
print("=== open tasks ===")
for t in c.execute(
    "SELECT id, status, title, assignee_id FROM tasks "
    "WHERE COALESCE(is_archived,0)=0 AND status NOT IN ('closed','cancelled') "
    "ORDER BY updated_at DESC LIMIT 20"
):
    print(t["id"][:8], t["status"], (t["title"] or "")[:50])
c.close()
