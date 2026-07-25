"""Dig waive reasons and attestation kinds."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
c = sqlite3.connect(r"D:\PC_AI\Project\TEST_YLGY\.hiveweave\data.db")
c.row_factory = sqlite3.Row
agents = {r["id"]: dict(r) for r in c.execute("select * from agents")}


def nm(aid):
    a = agents.get(aid) or {}
    return f"{a.get('short_id')} {a.get('name')}"


tables = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
print("tables", [t for t in tables if "attest" in t or "verif" in t or "waiver" in t])

if "attestations" in tables:
    cols = [x[1] for x in c.execute("pragma table_info(attestations)")]
    print("attestations cols", cols)
    kinds = Counter(
        r[0] for r in c.execute("select kind from attestations")
    )
    print("kinds", kinds)
    print("total", sum(kinds.values()))
    # waivers
    for r in c.execute(
        "select * from attestations where kind='waiver' order by created_at"
    ):
        d = dict(r)
        payload = d.get("payload_json") or d.get("payload") or d.get("meta") or ""
        reason = d.get("reason") or ""
        print(
            "\nWAIVER",
            datetime.fromtimestamp(d["created_at"] / 1000).strftime("%H:%M:%S"),
            "agent=",
            nm(d.get("agent_id")),
            "task=",
            (d.get("task_id") or "")[:8],
            "reason=",
            reason[:200],
        )
        if payload:
            print("  payload", str(payload)[:300])

# inbox reasons
print("\n=== INBOX WAIVE FULL ===")
for r in c.execute(
    "select created_at, from_agent_id, message from inbox "
    "where message like '%Attestation gate waived%' order by created_at"
):
    print(
        datetime.fromtimestamp(r["created_at"] / 1000).strftime("%H:%M:%S"),
        nm(r["from_agent_id"]),
    )
    print(" ", r["message"][:400].replace("\n", " | "))

# evidence dir
root = Path(r"D:\PC_AI\Project\TEST_YLGY")
print("\n=== screenshots/evidence files ===")
for pat in ("**/evidence/**", "**/*screenshot*", "**/*.png", "**/*phaser*"):
    hits = list(root.glob(pat))[:30]
    if hits:
        print(pat, len(list(root.glob(pat))))
        for h in list(root.glob(pat))[:15]:
            if h.is_file():
                print(" ", h.relative_to(root))

# WAIT_CYCLE unique events
print("\n=== WAIT_CYCLE unique ===")
seen = set()
for r in c.execute(
    "select created_at, message from inbox where message like '%WAIT_CYCLE%' order by created_at"
):
    key = r["message"][:120]
    ts = datetime.fromtimestamp(r["created_at"] / 1000).strftime("%H:%M:%S")
    if key not in seen:
        seen.add(key)
        print(ts, r["message"][:200].replace("\n", " "))
print("inbox rows", c.execute("select count(*) from inbox where message like '%WAIT_CYCLE%'").fetchone()[0], "unique msgs", len(seen))

# exhausted - look at chat for EXIT GATE exhausted?
print("\n=== search exhausted in chat ===")
n = c.execute(
    "select count(*) from chat_messages where content like '%exhausted%' or content like '%EXIT GATE%' or content like '%gate exhausted%'"
).fetchone()[0]
print("chat hits", n)
for r in c.execute(
    "select agent_id, substr(content,1,150) c from chat_messages "
    "where content like '%exhausted%' or content like '%MAX_REPAIR%' "
    "order by created_at desc limit 8"
):
    print(nm(r["agent_id"]), r["c"].replace("\n", " "))
