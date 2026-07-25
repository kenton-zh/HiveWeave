"""Verify reviewer's claims for report revision."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PDB = Path(r"D:\PC_AI\Project\TEST_YLGY\.hiveweave\data.db")
c = sqlite3.connect(str(PDB))
c.row_factory = sqlite3.Row

agents = {r["id"]: dict(r) for r in c.execute("select * from agents")}


def name(aid):
    a = agents.get(aid) or {}
    return f"{a.get('short_id','?')} {a.get('name','?')}"


print("=== WAIVE / ATTESTATION ===")
# work_logs attestation
att = list(
    c.execute(
        "select agent_id, type, summary, created_at from work_logs "
        "where type like '%attest%' or summary like '%attest%' or summary like '%waiv%' "
        "order by created_at"
    )
)
print("attest-ish work_logs", len(att))

# run_steps waive
waives = list(
    c.execute(
        "select * from run_steps where tool_name='waive_attestation' order by started_at"
    )
)
print("waive steps", len(waives))
for w in waives:
    aid = None
    # join via run
    run = c.execute(
        "select agent_id from agent_runs where id=?", (w["run_id"],)
    ).fetchone()
    excerpt = (w["result_excerpt"] or "")[:120]
    # try get args from elsewhere - may only have hash
    print(
        datetime.fromtimestamp(w["started_at"] / 1000).strftime("%H:%M:%S")
        if w["started_at"]
        else "?",
        name(run["agent_id"]) if run else "?",
        "err=",
        (w["error"] or "")[:40],
        "excerpt=",
        excerpt.replace("\n", " "),
    )

# inbox waive messages
print("\n=== INBOX WAIVE ===")
for r in c.execute(
    "select created_at, from_agent_id, substr(message,1,200) m from inbox "
    "where message like '%Attestation gate waived%' or message like '%waive%' "
    "order by created_at"
):
    print(
        datetime.fromtimestamp(r["created_at"] / 1000).strftime("%H:%M:%S"),
        name(r["from_agent_id"]),
        r["m"].replace("\n", " ")[:180],
    )

print("\n=== ATTESTATION COUNTS (work_logs types) ===")
print(Counter(r[0] for r in c.execute("select type from work_logs")))

# verification_cases
tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print("\ntables with verif/attest/evidence:", [t for t in tables if any(x in t for x in ("verif", "attest", "evidence", "waiver"))])

if "verification_cases" in tables:
    cols = [x[1] for x in c.execute("pragma table_info(verification_cases)")]
    print("verification_cases cols", cols)
    rows = list(c.execute("select * from verification_cases"))
    print("n=", len(rows))
    statuses = Counter(r["status"] if "status" in cols else "?" for r in rows)
    print("status", statuses)
    empty_notes = sum(1 for r in rows if not (r["review_notes"] if "review_notes" in cols else ""))
    empty_hash = sum(
        1
        for r in rows
        if "merge_commit_hash" in cols and not r["merge_commit_hash"]
    )
    print("empty review_notes", empty_notes, "empty merge_commit_hash", empty_hash)
    for r in rows[:5]:
        print(dict(r))

print("\n=== WAIT_CYCLE inbox ===")
for r in c.execute(
    "select created_at, from_agent_id, to_agent_id, substr(message,1,160) m from inbox "
    "where message like '%WAIT_CYCLE%' order by created_at"
):
    print(
        datetime.fromtimestamp(r["created_at"] / 1000).strftime("%H:%M:%S"),
        name(r["from_agent_id"]),
        "->",
        name(r["to_agent_id"]),
        r["m"].replace("\n", " "),
    )

print("\n=== evidence dir ===")
ev = Path(r"D:\PC_AI\Project\TEST_YLGY") / "evidence"
if not ev.exists():
    # search
    for p in Path(r"D:\PC_AI\Project\TEST_YLGY").rglob("evidence"):
        if p.is_dir():
            print("found", p)
            for f in p.rglob("*"):
                if f.is_file():
                    print(" ", f.relative_to(p))
else:
    for f in ev.rglob("*"):
        if f.is_file():
            print(f.relative_to(ev))

print("\n=== exhausted sampling: find turn exit exhausted in chat? ===")
# look at agent_runs interrupted
if "agent_runs" in tables:
    cols = [x[1] for x in c.execute("pragma table_info(agent_runs)")]
    print("agent_runs cols", cols)
    for r in c.execute(
        "select status, count(*) c from agent_runs group by status"
    ):
        print(dict(r))
